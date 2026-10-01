"""Azure-style names and resource IDs, shared by demand and infra so sessions join to costs."""
import re

from . import config as C


def slug(text):
    # "AVD Finance Desktops" -> "avd-finance-desktops"
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def short(app):
    # Compact app tag used inside resource names, e.g. "a02"
    return app.app_id.lower()


def subscription_for(app, env):
    # Platform apps live in the shared subscription, everything else by environment
    if app.kind == "platform":
        return C.SUBSCRIPTIONS["shared"]
    return C.SUBSCRIPTIONS["prod" if env == "prod" else "nonprod"]


def resource_group(app, env):
    return f"rg-{slug(app.name)}-{env}"


def resource_id(app, env, arm_type, name, parent=None):
    # Lowercase ARM ID; nested types (e.g. sql/servers/databases) take a parent name
    sub, _ = subscription_for(app, env)
    ns, *types = arm_type.split("/")
    path = f"{types[0]}/{parent}/{types[1]}/{name}" if parent else f"{types[0]}/{name}"
    return f"/subscriptions/{sub}/resourcegroups/{resource_group(app, env)}/providers/{ns}/{path}".lower()


def avd_host_id(app, i):
    return resource_id(app, "prod", "microsoft.compute/virtualmachines", f"vm-{short(app)}-avd-{i:03d}")


def primary_resource_id(app):
    # The compute resource a non-AVD session runs on
    if app.kind == "data":
        return resource_id(app, "prod", "microsoft.synapse/workspaces/sqlpools", "dwprod", parent=f"syn-{short(app)}-prod")
    if app.kind == "ml":
        return resource_id(app, "prod", "microsoft.databricks/workspaces", f"dbw-{short(app)}-prod")
    return resource_id(app, "prod", "microsoft.web/serverfarms", f"asp-{short(app)}-prod")
