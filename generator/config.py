"""All tunable knobs for the synthetic company live here."""
from dataclasses import dataclass, field

# Scale and time window
SEED = 42
N_USERS = 5000
START_DATE = "2024-10-01"
N_MONTHS = 24
CURRENCY = "USD"
NEGOTIATED_DISCOUNT = 0.05  # EA discount off list price applied to every meter

COMPANY = "Contoso Retail India"
BILLING_ACCOUNT_ID = "8611537"
REGION_PRIMARY = "centralindia"
REGION_DR = "southindia"

SUBSCRIPTIONS = {
    "prod": ("a1f3c2d4-0001-4c1e-9b7a-1f2e3d4c5b6a", "contoso-prod"),
    "nonprod": ("a1f3c2d4-0002-4c1e-9b7a-1f2e3d4c5b6a", "contoso-nonprod"),
    "shared": ("a1f3c2d4-0003-4c1e-9b7a-1f2e3d4c5b6a", "contoso-shared-platform"),
}

# Teams: (name, headcount weight, cost center, main locations)
TEAMS = [
    ("Customer Support", 0.18, "CC-410", ["Pune", "Hyderabad"]),
    ("Engineering", 0.18, "CC-210", ["Bengaluru", "Pune", "Remote"]),
    ("Sales", 0.13, "CC-310", ["Mumbai", "Bengaluru", "Chennai"]),
    ("Operations", 0.10, "CC-510", ["Mumbai", "Chennai"]),
    ("Finance", 0.08, "CC-110", ["Mumbai"]),
    ("Marketing", 0.06, "CC-320", ["Mumbai", "Bengaluru"]),
    ("Data & Analytics", 0.06, "CC-220", ["Bengaluru", "Remote"]),
    ("IT", 0.05, "CC-230", ["Pune", "Mumbai"]),
    ("HR", 0.04, "CC-120", ["Mumbai"]),
    ("Procurement", 0.03, "CC-520", ["Mumbai"]),
    ("Legal", 0.02, "CC-130", ["Mumbai"]),
    ("Executive", 0.01, "CC-100", ["Mumbai"]),
]
ALL = "__all__"

# Approximate Indian public / festival holidays in the window
HOLIDAYS = [
    "2024-10-02", "2024-10-12", "2024-10-31", "2024-11-01", "2024-12-25",
    "2025-01-26", "2025-03-14", "2025-03-31", "2025-08-15", "2025-08-27",
    "2025-10-02", "2025-10-20", "2025-10-21", "2025-12-25", "2026-01-26",
    "2026-03-04", "2026-03-20", "2026-08-15", "2026-09-14",
]
DIWALI = ["2024-10-31", "2025-10-20", "2026-11-08"]


@dataclass
class App:
    app_id: str
    name: str
    kind: str  # avd | internal_web | customer | data | ml | devtest | platform
    owner: str
    users: list = field(default_factory=list)  # teams that use it, or [ALL]
    adoption: float = 0.0  # share of eligible users who use it
    daily_use: float = 0.0  # chance an adopted user opens it on a workday
    hours: float = 0.0  # mean session length
    profile: str = "office"  # office | shift | dev | adhoc
    cpu: float = 20.0  # mean CPU % a session drives on its host
    actions_per_hour: float = 0.0  # business actions per session hour
    weekend_use: float = 0.03
    # external demand for customer-facing apps
    base_tx: float = 0.0
    tx_growth: float = 0.0  # yearly growth
    festive_lift: float = 0.0  # peak multiplier added around Diwali
    revenue_per_tx: float = 0.0
    # AVD pool settings
    sku: str = ""
    sessions_per_host: int = 0
    peak_overlap: float = 0.85
    min_hosts: int = 2
    window_hours: float = 11.0  # hours the scaled-out hosts stay on per workday


APPS = [
    App("A01", "AVD Contact Center Desktops", "avd", "IT", ["Customer Support"], 1.0, 0.92, 7.5, "shift", 22, 0,
        weekend_use=0.55, sku="D4s_v5", sessions_per_host=8, peak_overlap=0.42, min_hosts=6, window_hours=22),
    App("A02", "AVD Finance Desktops", "avd", "IT", ["Finance", "Procurement"], 1.0, 0.9, 6.0, "office", 35, 0,
        sku="E8s_v5", sessions_per_host=6, min_hosts=2),
    App("A03", "AVD Developer Workstations", "avd", "IT", ["Engineering", "Data & Analytics"], 0.7, 0.82, 6.5, "dev", 45, 0,
        sku="D8s_v5", sessions_per_host=4, peak_overlap=0.8, min_hosts=4, window_hours=12),
    App("A04", "AVD Office Desktops", "avd", "IT",
        ["Sales", "Marketing", "HR", "Operations", "Legal", "Executive", "IT"], 0.6, 0.75, 5.0, "office", 18, 0,
        sku="D4s_v5", sessions_per_host=10, min_hosts=3),
    App("A05", "Finance ERP", "internal_web", "Finance", ["Finance", "Procurement", "Operations", "Executive"],
        0.9, 0.6, 3.0, "office", 15, 14),
    App("A06", "HR Portal", "internal_web", "HR", [ALL], 0.95, 0.12, 0.3, "adhoc", 5, 3),
    App("A07", "Sales CRM", "internal_web", "Sales", ["Sales", "Marketing", "Customer Support", "Executive"],
        0.8, 0.7, 2.5, "office", 12, 9),
    App("A08", "Support Ticketing", "internal_web", "Customer Support", ["Customer Support", "IT", "Operations"],
        0.9, 0.85, 4.0, "shift", 10, 7, weekend_use=0.5),
    App("A09", "Document Management", "internal_web", "IT", [ALL], 0.7, 0.4, 0.8, "adhoc", 8, 5),
    App("A10", "BI Reporting", "internal_web", "Data & Analytics",
        ["Finance", "Sales", "Marketing", "Data & Analytics", "Executive", "Operations"], 0.6, 0.4, 1.2, "office", 25, 6),
    App("A11", "Customer Portal", "customer", "Customer Support", base_tx=38000, tx_growth=0.22, festive_lift=0.9),
    App("A12", "E-commerce Platform", "customer", "Engineering", base_tx=26000, tx_growth=0.35, festive_lift=1.6,
        revenue_per_tx=21.0),
    App("A13", "Payments Service", "customer", "Finance", base_tx=0, tx_growth=0.35, festive_lift=1.6),
    App("A14", "Mobile App Backend", "customer", "Engineering", base_tx=310000, tx_growth=0.45, festive_lift=1.1),
    App("A15", "Data Warehouse", "data", "Data & Analytics", ["Data & Analytics", "Finance"], 0.45, 0.55, 2.0,
        "office", 30, 12),
    App("A16", "ETL Pipelines", "data", "Data & Analytics"),
    App("A17", "ML Platform", "ml", "Data & Analytics", ["Data & Analytics"], 0.5, 0.45, 3.0, "dev", 40, 2),
    App("A18", "Dev/Test Environments", "devtest", "Engineering"),
    App("A19", "Backup & DR", "platform", "IT"),
    App("A20", "Shared Platform", "platform", "IT"),
]

# List prices (USD) - approximate Central India pay-as-you-go rates, rounded.
# key: (meter category, meter subcategory, meter name, pricing unit, block size, list unit price)
PRICES = {
    "vm:D4s_v5": ("Virtual Machines", "Dsv5 Series", "D4s v5", "Hours", 1, 0.192),
    "vm:D8s_v5": ("Virtual Machines", "Dsv5 Series", "D8s v5", "Hours", 1, 0.384),
    "vm:E8s_v5": ("Virtual Machines", "Esv5 Series", "E8s v5", "Hours", 1, 0.504),
    "vm:D2s_v5": ("Virtual Machines", "Dsv5 Series", "D2s v5", "Hours", 1, 0.096),
    "vm:NC24ads_A100_v4": ("Virtual Machines", "NCadsA100v4 Series", "NC24ads A100 v4", "Hours", 1, 3.673),
    "disk:P10": ("Storage", "Premium SSD Managed Disks", "P10 LRS Disk", "Units/Month", 1, 19.71),
    "disk:P30": ("Storage", "Premium SSD Managed Disks", "P30 LRS Disk", "Units/Month", 1, 135.17),
    "asp:P1v3": ("Azure App Service", "Premium v3 Plan", "P1 v3 App", "Hours", 1, 0.124),
    "asp:P2v3": ("Azure App Service", "Premium v3 Plan", "P2 v3 App", "Hours", 1, 0.248),
    "asp:P3v3": ("Azure App Service", "Premium v3 Plan", "P3 v3 App", "Hours", 1, 0.496),
    "sql:vcore": ("SQL Database", "General Purpose - Compute Gen5", "vCore", "Hours", 1, 0.252),
    "sql:storage": ("SQL Database", "General Purpose - Storage", "Data Stored", "GB/Month", 1, 0.115),
    "aks:uptime": ("Azure Kubernetes Service", "Standard", "Standard Uptime SLA", "Hours", 1, 0.10),
    "cosmos:ru": ("Azure Cosmos DB", "Azure Cosmos DB - Autoscale", "100 RU/s", "Units/Hour", 100, 0.012),
    "cosmos:storage": ("Azure Cosmos DB", "Azure Cosmos DB", "Data Stored", "GB/Month", 1, 0.25),
    "files:premium": ("Storage", "Premium Files", "Premium LRS Provisioned", "GB/Month", 1, 0.16),
    "redis:P1": ("Redis Cache", "Premium", "P1 Cache Instance", "Hours", 1, 0.554),
    "storage:hot": ("Storage", "Tiered Block Blob", "Hot LRS Data Stored", "GB/Month", 1, 0.0208),
    "storage:cool": ("Storage", "Tiered Block Blob", "Cool LRS Data Stored", "GB/Month", 1, 0.0115),
    "storage:writes": ("Storage", "Tiered Block Blob", "Hot LRS Write Operations", "Units", 10000, 0.065),
    "storage:reads": ("Storage", "Tiered Block Blob", "Hot Read Operations", "Units", 10000, 0.0052),
    "bw:internet": ("Bandwidth", "Rtn Preference: MGN", "Standard Data Transfer Out", "GB", 1, 0.12),
    "bw:interregion": ("Bandwidth", "Inter-Region", "Intra Continent Data Transfer Out", "GB", 1, 0.08),
    "appgw:fixed": ("Application Gateway", "WAF v2", "Standard Fixed Cost", "Hours", 1, 0.443),
    "appgw:cu": ("Application Gateway", "WAF v2", "Standard Capacity Units", "Units/Hour", 1, 0.0144),
    "lb:rules": ("Load Balancer", "Standard", "Standard Overage LB Rules and Outbound Rules", "Hours", 1, 0.025),
    "vpn:VpnGw2": ("VPN Gateway", "VpnGw2", "VpnGw2", "Hours", 1, 0.49),
    "pip:static": ("Virtual Network", "IP Addresses", "Standard IPv4 Static Public IP", "Hours", 1, 0.005),
    "law:ingest": ("Log Analytics", "Analytics Logs", "Analytics Logs Data Ingestion", "GB", 1, 2.99),
    "kv:ops": ("Key Vault", "Standard", "Operations", "Units", 10000, 0.03),
    "adf:runs": ("Azure Data Factory v2", "Cloud", "Cloud Orchestration Activity Run", "Units", 1000, 1.0),
    "adf:diu": ("Azure Data Factory v2", "Cloud", "Cloud Data Movement", "Hours", 1, 0.25),
    "syn:dw500c": ("Azure Synapse Analytics", "Dedicated SQL Pool", "DW500c", "Hours", 1, 7.55),
    "dbx:jobs": ("Azure Databricks", "Premium", "Premium Jobs Compute DBU", "Units/Hour", 1, 0.30),
    "dbx:allpurpose": ("Azure Databricks", "Premium", "Premium All-purpose Compute DBU", "Units/Hour", 1, 0.55),
    "backup:instance": ("Backup", "Azure VM", "Azure VM Protected Instances", "Units/Month", 1, 10.0),
    "backup:storage": ("Backup", "Azure VM", "GRS Data Stored", "GB/Month", 1, 0.0448),
    "asr:instance": ("Azure Site Recovery", "Azure Site Recovery", "VM Replicated to Azure", "Units/Month", 1, 25.0),
}

# Reservations: (name, price key covered, apps covered, quantity, start, term months, discount vs contracted)
RESERVATIONS = [
    ("RI-SQL-GP-Gen5-3yr", "sql:vcore", ["A05", "A07", "A12"], 40, "2024-10-01", 36, 0.52),
    ("RI-D8sv5-Ecommerce-1yr", "vm:D8s_v5", ["A12"], 10, "2025-04-01", 12, 0.37),  # expires, not renewed
    ("RI-D4sv5-ContactCenter-3yr", "vm:D4s_v5", ["A01"], 40, "2025-10-01", 36, 0.58),
]
