"""Demo workload: deliver packages 1..5. Crash-safe via memory.

Run: python3 -m stayonduty.runner <db> [task_id]
Env CRASH_AFTER=N -> os._exit(1) after N deliveries (true sudden death:
no cleanup, no complete, heartbeats just stop).
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from stayonduty.worker import run_one  # noqa: E402


def work(ctx):
    items = ctx.task["payload"]["items"]
    crash_after = int(os.environ.get("CRASH_AFTER", "0"))
    done = 0
    for item in items:
        if not ctx.alive():
            return
        if ctx.recall(f"done:{item}"):
            ctx.step(f"package {item}: already delivered, skipping")
            continue
        ctx.step(f"package {item}: delivering...")
        time.sleep(0.4)
        done += 1
        ctx.remember(f"done:{item}", True)
        ctx.step(f"package {item}: delivered")
        if crash_after and done >= crash_after:
            print("  [CRASH] sudden death via os._exit(1) — no cleanup", flush=True)
            os._exit(1)


if __name__ == "__main__":
    db = sys.argv[1]
    task_id = sys.argv[2] if len(sys.argv) > 2 else None
    run_one(db, owner="agent-1", work_fn=work, lease_secs=3, task_id=task_id)
