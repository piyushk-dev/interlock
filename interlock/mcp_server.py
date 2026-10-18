"""Interlock as an MCP server. A thin client over the HTTP API, so every agent (Claude Code,
Cursor, your own workers) shares one Interlock and one graph.

  INTERLOCK_URL=http://localhost:8790 INTERLOCK_AGENT=claude-code python -m interlock.mcp_server
"""
import json
import os
import urllib.request

from mcp.server.mcpserver import MCPServer

URL = os.getenv("INTERLOCK_URL", "http://localhost:8790").rstrip("/")
AGENT = os.getenv("INTERLOCK_AGENT", "claude-code")

server = MCPServer(name="interlock", instructions=(
    "Before you deploy, restart, migrate, rotate secrets, flip a flag, scale or delete anything in production, "
    "call request_change and only proceed if the status is 'granted'. Heartbeat while you work and release when done. "
    "If the status is 'waiting', 'needs_approval' or 'denied', tell the user the reason Interlock gave."))


def _call(path, body=None):
    req = urllib.request.Request(URL + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"}, method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


@server.tool()
def request_change(action: str, target: str, reason: str = "") -> dict:
    """Ask before changing production. action: deploy | restart | migrate | rotate_secret | flip_flag | scale | delete | diagnose.
    target: a resource like payments or orders-db (add @staging for staging)."""
    return _call("/api/request", {"agent": AGENT, "action": action, "target": target, "reason": reason})


@server.tool()
def heartbeat(intent: str) -> dict:
    """Renew the lock while you are still working, so it doesn't lapse."""
    return _call("/api/heartbeat", {"intent": intent})


@server.tool()
def release(intent: str) -> dict:
    """Release the lock when your change is done. Waiting agents wake up."""
    return _call("/api/release", {"intent": intent})


@server.tool()
def why_blocked(intent: str) -> dict:
    """Everything about one request: impact zone, decisions, who it waits for, who authorised it."""
    return _call(f"/api/why?intent={intent}")


@server.tool()
def status() -> dict:
    """Changes in flight right now and pending approvals."""
    s = _call("/api/state")
    return {"enforce": s["enforce"], "active": s["snapshot"]["active"], "pending": s["pending"]}


@server.tool()
def list_resources(query: str = "") -> list:
    """Find resource ids (services, databases, topics, flags)."""
    return _call(f"/api/resources?q={query}")


if __name__ == "__main__":
    server.run("stdio")
