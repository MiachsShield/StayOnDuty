"""StayOnDuty watchdog — run next to your workers.

Every interval it: recovers stale leases, flags tasks with no real progress
(level 1: nudge the worker to replan), and escalates to a human when a task
stays stuck (level 2: alert row, shown on the dashboard).

Run:  python3 -m stayonduty.watchdog <db> [--stuck-after 600]
      [--escalate-after 1800] [--max-attempts 3] [--interval 30]
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from stayonduty.store import Store  # noqa: E402


def arg(name, default):
    return type(default)(sys.argv[sys.argv.index(name) + 1]) \
        if name in sys.argv else default


DB = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") \
    else "stayonduty.db"
STUCK_AFTER = arg("--stuck-after", 600)
ESCALATE_AFTER = arg("--escalate-after", 1800)
MAX_ATTEMPTS = arg("--max-attempts", 3)
INTERVAL = arg("--interval", 30)

store = Store(DB)
print(f"[watchdog] watching {DB} every {INTERVAL}s "
      f"(stuck after {STUCK_AFTER}s, auto-retry after {ESCALATE_AFTER}s, "
      f"max {MAX_ATTEMPTS} attempts — user is never pushed)", flush=True)
try:
    while True:
        res = store.recover_stale(max_attempts=MAX_ATTEMPTS)
        if res["released"]:
            print(f"[watchdog] recovered {res['released']} stale lease(s)", flush=True)
        if res["failed"]:
            print(f"[watchdog] {res['failed']} task(s) failed after {MAX_ATTEMPTS}"
                  f" attempts — diagnostics in timeline (passive, no push)", flush=True)
        for t in store.promote_waiting():
            print(f"[watchdog] RESUMED: '{t['title']}' ({t['id']}) — quota wait"
                  f" window reached, back in the queue on its own", flush=True)
        det = store.detect_stuck(stuck_after_secs=STUCK_AFTER,
                                 escalate_after_secs=ESCALATE_AFTER,
                                 max_attempts=MAX_ATTEMPTS)
        for t in det["stuck"]:
            print(f"[watchdog] STUCK: '{t['title']}' ({t['id']}) — no progress,"
                  f" worker nudged to replan", flush=True)
        for t in det["requeued"]:
            print(f"[watchdog] AUTO-RETRY: '{t['title']}' ({t['id']}) — fresh attempt"
                  f" #{t['attempts'] + 1} queued on its own, no human needed", flush=True)
        for t in det["failed"]:
            print(f"[watchdog] GAVE UP: '{t['title']}' ({t['id']}) — attempts exhausted,"
                  f" diagnostics in timeline (passive, no push)", flush=True)
        time.sleep(INTERVAL)
except KeyboardInterrupt:
    print("\n[watchdog] stopped", flush=True)
finally:
    store.close()
