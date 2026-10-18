"""A synthetic but realistic company for Interlock to protect: an online retailer with the
usual service mesh, datastores, event topics and feature flags, in prod and staging, plus the
teams that own them and the humans who authorise agents.

Edges point from the thing that depends to the thing it depends on:
  (checkout)-[:DEPENDS_ON {kind:'calls'}]->(payments)
so "what does a change to payments affect?" is a walk backwards along DEPENDS_ON.
"""

# name: (kind, team, tier)   tier 1 = customer-facing money path, 3 = nice to have
SERVICES = {
    "web-frontend": ("service", "Growth", 1),
    "mobile-bff": ("service", "Growth", 2),
    "api-gateway": ("service", "Platform", 1),
    "auth": ("service", "Identity", 1),
    "users": ("service", "Identity", 2),
    "catalog": ("service", "Catalog", 2),
    "search": ("service", "Catalog", 2),
    "search-indexer": ("service", "Catalog", 3),
    "recommendations": ("service", "Growth", 3),
    "reviews": ("service", "Catalog", 3),
    "pricing": ("service", "Catalog", 2),
    "promotions": ("service", "Growth", 3),
    "cart": ("service", "Checkout", 1),
    "checkout": ("service", "Checkout", 1),
    "payments": ("service", "Payments", 1),
    "pg-adapter": ("service", "Payments", 1),
    "fraud-check": ("service", "Payments", 2),
    "orders": ("service", "Checkout", 1),
    "inventory": ("service", "Fulfilment", 2),
    "shipping": ("service", "Fulfilment", 2),
    "warehouse": ("service", "Fulfilment", 3),
    "notifications": ("service", "Growth", 3),
    "email": ("service", "Growth", 3),
    "sms": ("service", "Growth", 3),
    "analytics-ingest": ("service", "Platform", 3),
}
DATASTORES = {
    "users-db": ("datastore", "Identity", 1),
    "sessions-cache": ("datastore", "Identity", 1),
    "catalog-db": ("datastore", "Catalog", 2),
    "search-index": ("datastore", "Catalog", 2),
    "cart-cache": ("datastore", "Checkout", 1),
    "orders-db": ("datastore", "Checkout", 1),
    "payments-db": ("datastore", "Payments", 1),
    "inventory-db": ("datastore", "Fulfilment", 2),
    "data-warehouse": ("datastore", "Platform", 3),
}
TOPICS = {
    "order-events": ("topic", "Platform", 2),
    "payment-events": ("topic", "Platform", 2),
    "inventory-events": ("topic", "Platform", 2),
}
FLAGS = {
    "new-checkout": ("flag", "Checkout", 1),
    "smart-reco": ("flag", "Growth", 3),
    "cod-enabled": ("flag", "Payments", 1),
}

# (dependent, dependency, kind)
DEPENDS = [
    ("web-frontend", "api-gateway", "calls"), ("mobile-bff", "api-gateway", "calls"),
    ("api-gateway", "auth", "calls"), ("api-gateway", "catalog", "calls"), ("api-gateway", "search", "calls"),
    ("api-gateway", "cart", "calls"), ("api-gateway", "checkout", "calls"), ("api-gateway", "users", "calls"),
    ("api-gateway", "recommendations", "calls"), ("api-gateway", "reviews", "calls"),
    ("auth", "users", "calls"), ("auth", "sessions-cache", "reads"), ("users", "users-db", "writes"),
    ("catalog", "catalog-db", "reads"), ("catalog", "pricing", "calls"), ("catalog", "inventory", "calls"),
    ("search", "search-index", "reads"), ("search", "catalog", "calls"),
    ("search-indexer", "inventory-events", "consumes"), ("search-indexer", "search-index", "writes"),
    ("recommendations", "catalog", "calls"), ("recommendations", "data-warehouse", "reads"),
    ("reviews", "catalog-db", "writes"),
    ("pricing", "promotions", "calls"), ("pricing", "catalog-db", "reads"),
    ("cart", "cart-cache", "writes"), ("cart", "pricing", "calls"), ("cart", "inventory", "calls"),
    ("checkout", "cart", "calls"), ("checkout", "pricing", "calls"), ("checkout", "payments", "calls"),
    ("checkout", "orders", "calls"), ("checkout", "inventory", "calls"), ("checkout", "fraud-check", "calls"),
    ("checkout", "shipping", "calls"),
    ("payments", "payments-db", "writes"), ("payments", "pg-adapter", "calls"), ("payments", "fraud-check", "calls"),
    ("orders", "orders-db", "writes"), ("order-events", "orders", "produced_by"),
    ("payment-events", "payments", "produced_by"),
    ("inventory", "inventory-db", "writes"), ("inventory-events", "inventory", "produced_by"),
    ("shipping", "warehouse", "calls"), ("shipping", "order-events", "consumes"),
    ("notifications", "order-events", "consumes"), ("notifications", "payment-events", "consumes"),
    ("notifications", "email", "calls"), ("notifications", "sms", "calls"),
    ("analytics-ingest", "order-events", "consumes"), ("analytics-ingest", "payment-events", "consumes"),
    ("analytics-ingest", "inventory-events", "consumes"), ("analytics-ingest", "data-warehouse", "writes"),
    ("checkout", "new-checkout", "gated_by"), ("cart", "new-checkout", "gated_by"),
    ("recommendations", "smart-reco", "gated_by"), ("web-frontend", "smart-reco", "gated_by"),
    ("payments", "cod-enabled", "gated_by"), ("checkout", "cod-enabled", "gated_by"),
]

TEAMS = ["Growth", "Platform", "Identity", "Catalog", "Checkout", "Payments", "Fulfilment"]

# Humans who authorise agents, and which teams' resources they can authorise changes to
HUMANS = [
    {"id": "priya", "name": "Priya (Checkout lead)", "teams": ["Checkout", "Payments"]},
    {"id": "arjun", "name": "Arjun (Platform on-call)", "teams": ["Platform", "Identity", "Fulfilment", "Checkout", "Payments", "Catalog", "Growth"]},
    {"id": "neha", "name": "Neha (Catalog lead)", "teams": ["Catalog", "Growth"]},
    {"id": "rahul", "name": "Rahul (Fulfilment lead)", "teams": ["Fulfilment"]},
]

ENVS = ["prod", "staging"]


def resources():
    for env in ENVS:
        for table in (SERVICES, DATASTORES, TOPICS, FLAGS):
            for name, (kind, team, tier) in table.items():
                yield {"id": f"{name}@{env}", "name": name, "kind": kind, "team": team,
                       "tier": tier if env == "prod" else 3, "env": env}


def dependencies():
    for env in ENVS:
        for a, b, kind in DEPENDS:
            yield {"a": f"{a}@{env}", "b": f"{b}@{env}", "kind": kind}
