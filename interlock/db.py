import os

from falkordb import FalkorDB

GRAPH_NAME = os.getenv("INTERLOCK_GRAPH", "interlock")

_db = None


def graph():
    global _db
    if _db is None:
        url = os.getenv("FALKORDB_URL")
        if url:
            _db = FalkorDB.from_url(url)
        else:
            _db = FalkorDB(
                host=os.getenv("FALKORDB_HOST", "127.0.0.1"),
                port=int(os.getenv("FALKORDB_PORT", "6379")),
                username=os.getenv("FALKORDB_USERNAME") or None,
                password=os.getenv("FALKORDB_PASSWORD") or None,
            )
    return _db.select_graph(GRAPH_NAME)


def rows(query, params=None, read_only=True):
    """Run Cypher and return a list of dicts keyed by the RETURN aliases."""
    g = graph()
    res = g.ro_query(query, params or {}) if read_only else g.query(query, params or {})
    header = [h[1] if isinstance(h, (list, tuple)) else h for h in res.header]
    return [dict(zip(header, r)) for r in res.result_set]


def write(query, params=None):
    return graph().query(query, params or {})
