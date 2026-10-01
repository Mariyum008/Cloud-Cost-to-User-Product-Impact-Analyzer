"""Who uses what, when: calendar, employees, sessions, and customer demand."""
import numpy as np
import pandas as pd

from . import config as C
from .naming import avd_host_id, primary_resource_id


class Calendar:
    def __init__(self):
        start = pd.Timestamp(C.START_DATE)
        end = start + pd.DateOffset(months=C.N_MONTHS)
        self.dates = pd.date_range(start, end - pd.Timedelta(days=1), freq="D")
        self.n = len(self.dates)
        self.dow = self.dates.dayofweek.values
        self.holiday = self.dates.isin(pd.to_datetime(C.HOLIDAYS))
        self.workday = (self.dow < 5) & ~self.holiday
        self.days_in_month = self.dates.days_in_month.values
        self.day_of_month = self.dates.day.values
        self.years = np.arange(self.n) / 365.0

    def idx(self, date):
        # Day index of a date string, clipped to the window
        return int(np.clip((pd.Timestamp(date) - self.dates[0]).days, 0, self.n))


def make_users(rng, cal):
    # Employees with team, location, and hire/leave dates
    w = np.array([t[1] for t in C.TEAMS])
    team_idx = rng.choice(len(C.TEAMS), C.N_USERS, p=w / w.sum())
    hire = np.where(rng.random(C.N_USERS) < 0.85, 0, rng.integers(30, max(31, cal.n - 120), C.N_USERS))
    leaves = rng.random(C.N_USERS) < 0.09
    leave = np.where(leaves, np.minimum(hire + rng.integers(90, cal.n, C.N_USERS), cal.n), cal.n)
    users = pd.DataFrame({
        "user_id": [f"U{i:05d}" for i in range(1, C.N_USERS + 1)],
        "team": [C.TEAMS[t][0] for t in team_idx],
        "cost_center": [C.TEAMS[t][2] for t in team_idx],
        "location": [rng.choice(C.TEAMS[t][3]) for t in team_idx],
        "employment_type": np.where(rng.random(C.N_USERS) < 0.1, "Contractor", "Full-time"),
        "hire_date": np.where(hire == 0, pd.NaT, cal.dates[np.minimum(hire, cal.n - 1)]),
        "leave_date": np.where(leave >= cal.n, pd.NaT, cal.dates[np.minimum(leave, cal.n - 1)]),
    })
    users["hire_date"] = pd.to_datetime(users["hire_date"])
    users["leave_date"] = pd.to_datetime(users["leave_date"])
    days = np.arange(cal.n)
    active = (days[None, :] >= hire[:, None]) & (days[None, :] < leave[:, None])
    intensity = rng.lognormal(0, 0.35, C.N_USERS)  # hidden ground truth, not exported
    return users, team_idx, active, intensity


def _start_hours(rng, profile, k):
    # Login time of day by usage profile
    if profile == "shift":
        return rng.choice([6.0, 14.0, 22.0], k, p=[0.45, 0.4, 0.15]) + rng.normal(0, 0.25, k)
    if profile == "dev":
        return rng.normal(10.4, 1.1, k)
    if profile == "adhoc":
        return rng.uniform(9, 18.5, k)
    return rng.normal(9.5, 0.7, k)


def avd_pool_size(app, sessions_per_day):
    # Hosts the scaling plan keeps on each day, and total pool size
    needed = np.ceil(sessions_per_day * app.peak_overlap / (app.sessions_per_host * 0.85))
    peak = np.maximum(needed, app.min_hosts).astype(int)
    return peak, int(np.ceil(peak.max() * 1.1))


def app_sessions(app, users, team_idx, active, intensity, cal, rng, next_id):
    """Sessions for one internal app plus its daily driver series."""
    team_names = np.array([t[0] for t in C.TEAMS])
    eligible = np.ones(C.N_USERS, bool) if C.ALL in app.users else np.isin(team_names[team_idx], app.users)
    adopters = np.where(eligible & (rng.random(C.N_USERS) < app.adoption))[0]
    pair_int = intensity[adopters] * rng.lognormal(0, 0.2, len(adopters))

    # Daily open probability, lower on weekends and holidays
    day_factor = np.where(cal.workday, 1.0, app.weekend_use)
    p = np.clip(app.daily_use * day_factor[None, :] * pair_int[:, None] ** 0.3, 0, 0.99)
    mask = active[adopters] & (rng.random((len(adopters), cal.n)) < p)
    u_loc, d = np.nonzero(mask)
    order = np.argsort(d, kind="stable")
    u_loc, d = u_loc[order], d[order]
    k = len(d)
    pi = pair_int[u_loc]

    # Session attributes
    density = app.sessions_per_host if app.kind == "avd" else (10 if app.kind in ("data", "ml") else 60)
    hours = np.clip(rng.gamma(4, app.hours / 4, k) * pi ** 0.4, 0.05, 9.5 if app.profile == "shift" else 13)
    cpu_share = np.clip(app.cpu / density * pi ** 0.5 * rng.lognormal(0, 0.25, k), 0.05, 60)
    mem_gb = np.clip((3.5 if app.kind == "avd" else 0.15) * pi ** 0.5 * rng.lognormal(0, 0.2, k), 0.02, 16)
    actions = rng.poisson(app.actions_per_hour * hours * pi)
    start = cal.dates.values[d] + (np.clip(_start_hours(rng, app.profile, k), 0, 23.9) * 3.6e12).astype("timedelta64[ns]")

    sessions_per_day = np.bincount(d, minlength=cal.n)
    pool = None
    if app.kind == "avd":
        # Spread each day's sessions across the hosts the scaling plan has running
        peak, n_hosts = avd_pool_size(app, sessions_per_day)
        first = np.searchsorted(d, d, side="left")
        host = (np.arange(k) - first) % peak[d]
        resource_ids = np.array([avd_host_id(app, i) for i in range(n_hosts)])[host]
        host_cpu_h = np.zeros((n_hosts, cal.n))
        np.add.at(host_cpu_h, (host, d), cpu_share / 100 * hours)
        pool = {"peak": peak, "n_hosts": n_hosts, "host_cpu_hours": host_cpu_h}
    else:
        resource_ids = np.full(k, primary_resource_id(app))

    df = pd.DataFrame({
        "session_id": np.arange(next_id, next_id + k, dtype=np.int64),
        "user_id": users["user_id"].values[adopters][u_loc],
        "app_id": app.app_id,
        "resource_id": resource_ids,
        "session_date": cal.dates.values[d],
        "session_start": start,
        "duration_hours": hours.round(3),
        "cpu_share_pct": cpu_share.round(3),
        "memory_gb": mem_gb.round(3),
        "business_actions": actions.astype(np.int32),
    })
    drivers = {
        "sessions": sessions_per_day.astype(float),
        "users": sessions_per_day.astype(float),  # one session per user per day
        "hours": np.bincount(d, weights=hours, minlength=cal.n),
        "cpu_hours": np.bincount(d, weights=cpu_share / 100 * hours, minlength=cal.n),
        "actions": np.bincount(d, weights=actions, minlength=cal.n).astype(float),
        "adopters": float(len(adopters)),
    }
    return df, drivers, pool


def external_demand(app, cal, rng):
    """Daily customer transactions with growth, weekly shape, payday and Diwali lift."""
    trend = (1 + app.tx_growth) ** cal.years
    weekly = np.where(cal.dow >= 5, 1.18, 1.0) if app.app_id == "A12" else np.where(cal.dow >= 5, 0.88, 1.03)
    payday = np.where(cal.day_of_month <= 5, 1.08, 1.0)
    day_num = (cal.dates - cal.dates[0]).days.values
    festive = np.zeros(cal.n)
    for dw in C.DIWALI:
        center = (pd.Timestamp(dw) - cal.dates[0]).days - 8  # sales peak about a week before Diwali
        festive += np.exp(-0.5 * ((day_num - center) / 7.0) ** 2)
    noise = rng.lognormal(0, 0.05, cal.n)
    return app.base_tx * trend * weekly * payday * (1 + app.festive_lift * festive) * noise, festive
