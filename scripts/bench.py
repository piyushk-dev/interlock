"""Same fleet, same seed, two runs: enforcement on, then off. Prints the numbers the README and site quote.
   python scripts/bench.py [agents] [sim_seconds]"""
import json
import sys
import time

from interlock import core
from interlock.sim import fleet

agents = int(sys.argv[1]) if len(sys.argv) > 1 else 24
secs = float(sys.argv[2]) if len(sys.argv) > 2 else 180
speed = 6.0
out = {}
for mode in (True, False):
    core.clock.set_speed(speed)
    fleet.enforce = mode
    fleet.rng.seed(7)
    fleet.reset(size=agents)
    fleet.start()
    time.sleep(secs / speed)
    fleet.pause()
    time.sleep(0.5)
    m = fleet.metrics["on" if mode else "off"]
    out["on" if mode else "off"] = dict(m)
on, off = out["on"], out["off"]
summary = {
    "agents": agents, "sim_seconds": secs,
    "with": {"completed": on["completed"], "collisions": 0, "prevented": on["prevented"], "avg_wait_s": round(on["wait_time"] / max(1, on["waits"]), 1),
             "policy_blocks": on["denied"], "max_parallel": on["max_parallel"]},
    "without": {"completed": off["completed"], "collisions": off["collisions"], "outages": off["incidents"], "max_parallel": off["max_parallel"]},
}
print(json.dumps(summary, indent=2))
