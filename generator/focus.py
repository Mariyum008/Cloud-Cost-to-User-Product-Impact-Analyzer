"""Turn metered usage into FOCUS 1.0 cost rows, matching Microsoft's export columns."""
import json
import uuid
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from . import config as C

REF = Path(__file__).resolve().parent.parent / "data" / "reference"
X_COLS = ["x_BilledCostInUsd", "x_EffectiveCostInUsd", "x_ListCostInUsd", "x_ContractedCostInUsd",
          "x_BillingExchangeRate", "x_EffectiveUnitPrice", "x_PricingBlockSize", "x_PricingCurrency",
          "x_ResourceGroupName", "x_ResourceType", "x_SkuMeterCategory", "x_SkuMeterSubcategory",
          "x_SkuMeterName", "x_SkuDescription", "x_SkuTerm", "x_ServicePeriodStart", "x_ServicePeriodEnd"]
ARROW = {"Decimal": pa.float64(), "DateTime": pa.timestamp("us"), "String": pa.string(), "JSON": pa.string(),
         "Number": pa.float64(), "Boolean": pa.bool_()}


def load_reference():
    # Microsoft open data: service names/categories, resource type display names, region names
    svc = pd.read_csv(REF / "Services.csv")
    svc["ns"] = svc["ResourceType"].str.split("/").str[0]
    svc = svc.assign(pref=(svc["ConsumedService"] == svc["ns"]).astype(int)).sort_values("pref", ascending=False)
    svc = svc.drop_duplicates("ResourceType").set_index("ResourceType")
    rt = pd.read_csv(REF / "ResourceTypes.csv").set_index("ResourceType")["SingularDisplayName"]
    reg = pd.read_csv(REF / "Regions.csv").drop_duplicates("RegionId").set_index("RegionId")["RegionName"]
    meta = json.load(open(REF / "FocusCost_1.0.json"))
    cols = [c for c in meta["Columns"] if not c["ColumnName"].startswith("x_") or c["ColumnName"] in X_COLS]
    # The metadata file lists ConsumedUnit as Decimal; the FOCUS spec defines it as a string
    types = {c["ColumnName"]: ARROW[c["DataType"]] for c in cols} | {"ConsumedUnit": pa.string()}
    schema = pa.schema(list(types.items()))
    return svc, rt, reg, schema


def _uid(*parts):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "/".join(map(str, parts))))


class FocusWriter:
    def __init__(self, infra, cal, path):
        self.infra, self.cal = infra, cal
        self.svc, self.rt, self.reg, self.schema = load_reference()
        self.bp_start = cal.dates.to_period("M").start_time.values
        self.bp_end = (cal.dates.to_period("M") + 1).start_time.values
        self.writer = pq.ParquetWriter(path, self.schema)
        self.buffer, self.rows = [], 0

    # ---------- reservations ----------
    def reservations(self):
        """Split eligible usage into covered/on-demand and build unused + purchase rows."""
        out = []
        for name, key, apps, qty, start, term, disc in C.RESERVATIONS:
            s = self.cal.idx(start)
            e = self.cal.idx(pd.Timestamp(start) + pd.DateOffset(months=term))
            elig = [m for m in self.infra.meters if m["key"] == key
                    and self.infra.resources[m["r"]]["app_id"] in apps
                    and self.infra.resources[m["r"]]["environment"] == "prod"]
            if not elig:
                continue  # no matching usage inside the window
            cap = np.zeros(self.cal.n)
            cap[s:e] = qty * 24.0
            usage = np.sum([m["qty"] for m in elig], axis=0)
            covered = np.minimum(cap, usage)
            frac = np.divide(covered, usage, out=np.zeros_like(usage), where=usage > 0)
            rid = f"/providers/microsoft.capacity/reservationorders/{_uid(name, 'order')}/reservations/{_uid(name)}"
            info = dict(id=rid, name=name, key=key, qty=qty, term=term, disc=disc, cap=cap,
                        unused=cap - covered, start=pd.Timestamp(start), r0=elig[0]["r"])
            for m in elig:
                m["commit"] = (info, m["qty"] * frac)
            out.append(info)
        self.commits = out
        return pd.DataFrame([{
            "reservation_id": c["id"], "reservation_name": c["name"], "sku_key": c["key"], "quantity": c["qty"],
            "term_months": c["term"], "discount_vs_contracted": c["disc"], "start_date": c["start"],
            "end_date": c["start"] + pd.DateOffset(months=c["term"])} for c in out])

    # ---------- row builders ----------
    def _const(self, res, key):
        # Columns that are the same for every row of one resource meter
        cat, sub, mname, unit, block, price = C.PRICES[key]
        arm = res["arm_type"]
        svc = self.svc.loc[arm] if arm in self.svc.index else None
        region = res["region_id"]
        return {
            "BillingAccountId": C.BILLING_ACCOUNT_ID, "BillingAccountName": C.COMPANY,
            "BillingAccountType": "Billing Account", "BillingCurrency": C.CURRENCY, "ChargeClass": None,
            "ChargeDescription": f"{mname} - {self.reg.get(region, region)}", "ProviderName": "Microsoft",
            "PublisherName": "Microsoft", "InvoiceIssuerName": "Microsoft", "PricingUnit": unit,
            "ConsumedUnit": unit, "RegionId": region, "RegionName": self.reg.get(region, region),
            "ResourceId": res["resource_id"], "ResourceName": res["resource_name"],
            "ResourceType": self.rt.get(arm, arm),
            "ServiceCategory": svc["ServiceCategory"] if svc is not None else "Other",
            "ServiceName": svc["ServiceName"] if svc is not None else cat,
            "SkuId": _uid("sku", key)[:13].upper(), "SkuPriceId": _uid("price", key, region)[:18].upper(),
            "SubAccountId": f"/subscriptions/{res['subscription_id']}", "SubAccountName": res["subscription_name"],
            "SubAccountType": "Subscription", "Tags": res["tags"], "x_BillingExchangeRate": 1.0,
            "x_PricingBlockSize": float(block), "x_PricingCurrency": C.CURRENCY,
            "x_ResourceGroupName": res.get("resource_group"), "x_ResourceType": arm,
            "x_SkuMeterCategory": cat, "x_SkuMeterSubcategory": sub, "x_SkuMeterName": mname,
            "x_SkuDescription": f"{cat} {sub} {mname}", "_list": price, "_block": block,
        }

    def _emit(self, const, days, consumed, priced, kind, commit=None):
        # One block of daily rows; kind = ondemand | used | unused
        list_p = const.pop("_list")
        block = const.pop("_block")
        contracted_p = list_p * (1 - C.NEGOTIATED_DISCOUNT)
        eff_p = contracted_p * (1 - commit["disc"]) if commit else contracted_p
        pq_ = priced / block
        df = pd.DataFrame({
            "ChargePeriodStart": self.cal.dates.values[days],
            "ChargePeriodEnd": self.cal.dates.values[days] + np.timedelta64(1, "D"),
            "BillingPeriodStart": self.bp_start[days], "BillingPeriodEnd": self.bp_end[days],
            "ConsumedQuantity": consumed, "PricingQuantity": pq_,
            "ListCost": pq_ * list_p, "ContractedCost": pq_ * contracted_p, "EffectiveCost": pq_ * eff_p,
        })
        df["BilledCost"] = 0.0 if commit else df["EffectiveCost"]
        df["ListUnitPrice"], df["ContractedUnitPrice"], df["x_EffectiveUnitPrice"] = list_p, contracted_p, eff_p
        df["ChargeCategory"], df["ChargeFrequency"] = "Usage", "Usage-Based"
        df["PricingCategory"] = "Committed" if commit else "Standard"
        if commit:
            df["CommitmentDiscountId"], df["CommitmentDiscountName"] = commit["id"], commit["name"]
            df["CommitmentDiscountCategory"], df["CommitmentDiscountType"] = "Usage", "Reservation"
            df["CommitmentDiscountStatus"] = "Unused" if kind == "unused" else "Used"
            df["x_SkuTerm"] = float(commit["term"])
        for k, v in const.items():
            df[k] = v
        self._push(df)

    def _push(self, df):
        for col in ["BilledCost", "EffectiveCost", "ListCost", "ContractedCost"]:
            df[f"x_{col}InUsd"] = df[col]
        df["x_ServicePeriodStart"], df["x_ServicePeriodEnd"] = df["ChargePeriodStart"], df["ChargePeriodEnd"]
        self.buffer.append(df)
        if sum(len(b) for b in self.buffer) > 200_000:
            self.flush()

    def flush(self):
        if not self.buffer:
            return
        df = pd.concat(self.buffer, ignore_index=True).reindex(columns=self.schema.names)
        self.writer.write_table(pa.Table.from_pandas(df, schema=self.schema, preserve_index=False))
        self.rows += len(df)
        self.buffer = []

    def write(self):
        # Usage rows, split into on-demand and reservation-covered parts
        for m in self.infra.meters:
            res = self.infra.resources[m["r"]]
            q = m["qty"]
            covered = m["commit"][1] if "commit" in m else np.zeros_like(q)
            ondemand = q - covered
            d = np.nonzero(ondemand > 1e-9)[0]
            if len(d):
                self._emit(self._const(res, m["key"]), d, ondemand[d], ondemand[d], "ondemand")
            d = np.nonzero(covered > 1e-9)[0]
            if len(d):
                self._emit(self._const(res, m["key"]), d, covered[d], covered[d], "used", m["commit"][0])
        for c in self.commits:
            self._commit_rows(c)
        self.flush()
        self.writer.close()
        return self.rows

    def _commit_rows(self, c):
        # Unused reservation hours (daily) and the monthly reservation payment
        any_res = self.infra.resources[c["r0"]]
        base = dict(any_res, resource_id=c["id"], resource_name=c["name"], tags="{}", resource_group=None)
        d = np.nonzero(c["unused"] > 1e-9)[0]
        if len(d):
            const = self._const(base, c["key"])
            const["ResourceType"] = "Reservation"
            self._emit(const, d, np.full(len(d), np.nan), c["unused"][d], "unused", c)
        months = pd.Series(c["cap"]).groupby(self.bp_start).sum()
        eff_p = C.PRICES[c["key"]][5] * (1 - C.NEGOTIATED_DISCOUNT) * (1 - c["disc"])
        const = self._const(base, c["key"])
        const.pop("_list"), const.pop("_block")
        const["ResourceType"] = "Reservation"
        rows = []
        for start, hours in months.items():
            if hours <= 0:
                continue
            cost = hours * eff_p
            row = dict(const, ChargePeriodStart=start, ChargePeriodEnd=start + pd.DateOffset(months=1),
                       BillingPeriodStart=start, BillingPeriodEnd=start + pd.DateOffset(months=1),
                       ChargeCategory="Purchase", ChargeFrequency="Recurring", PricingCategory="Standard",
                       PricingQuantity=float(c["qty"]), PricingUnit="Units", ConsumedQuantity=None, ConsumedUnit=None,
                       BilledCost=cost, EffectiveCost=0.0, ListCost=cost, ContractedCost=cost,
                       ListUnitPrice=cost / c["qty"], ContractedUnitPrice=cost / c["qty"], x_EffectiveUnitPrice=0.0,
                       CommitmentDiscountId=c["id"], CommitmentDiscountName=c["name"],
                       CommitmentDiscountCategory="Usage", CommitmentDiscountType="Reservation",
                       x_SkuTerm=float(c["term"]), ChargeDescription=f"{c['name']} - monthly payment")
            rows.append(row)
        if rows:
            self._push(pd.DataFrame(rows))
