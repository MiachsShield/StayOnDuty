"""Iteration 3 demo (autonomy cut): stuck worker -> replan nudge ->
autonomous requeue -> a fresh worker finishes. No human involved at any point.

Run:  python3 -m stayonduty.demo_stuck
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from stayonduty.store import Store  # noqa: E402

DB = "demo_stuck.db"
for ext in ("", "-wal", "-shm"):
    p = DB + ext
    if os.path.exists(p):
        os.remove(p)

store = Store(DB)
tid = store.create_task("migrate database", {"tables": 4})
store.claim_task("agent-1", lease_secs=30, task_id=tid)
print(f"[main] task {tid} claimed by agent-1", flush=True)


def worker1():
    """First worker: alive and heartbeating, but blocked — zero progress."""
    s = Store(DB)
    replanned = False
    try:
        while True:
            if not s.heartbeat(tid, "agent-1", 30):
                print("[worker-1] lost the lease — standing down", flush=True)
                return
            t = s.get_task(tid)
            if t["stuck_level"] >= 1 and not replanned:
                replanned = True
                s.note(tid, "stuck flag seen — replanning: will retry with backoff")
                print("[worker-1] STUCK flag seen -> replanning (still blocked)", flush=True)
            time.sleep(1)
    finally:
        s.close()


def worker2():
    """Fresh attempt: a new worker picks up the requeued task and finishes."""
    s = Store(DB)
    try:
        t = s.claim_task("agent-2", lease_secs=30, task_id=tid)
        if not t:
            print("[worker-2] nothing to claim", flush=True)
            return
        print("[worker-2] claimed the requeued task — fresh attempt", flush=True)
        for i in range(1, 5):
            s.heartbeat(tid, "agent-2", 30)
            s.step(tid, f"table {i}/4 migrated")
            time.sleep(1)
        s.complete_task(tid, "agent-2", "all tables migrated")
        print("[worker-2] done", flush=True)
    finally:
        s.close()


threading.Thread(target=worker1, daemon=True).start()

t0 = time.time()
w2_started = False
while time.time() - t0 < 60:
    det = store.detect_stuck(stuck_after_secs=5, escalate_after_secs=8,
                             max_attempts=3)
    for t in det["stuck"]:
        print(f"[watchdog] STUCK: '{t['title']}' — no progress for 5s, nudging worker",
              flush=True)
    for t in det["requeued"]:
        print(f"[watchdog] AUTO-RETRY: '{t['title']}' — fresh attempt"
              f" #{t['attempts'] + 1} queued on its own, no human needed", flush=True)
    for t in det["failed"]:
        print(f"[watchdog] GAVE UP: '{t['title']}' — attempts exhausted", flush=True)
    cur = store.get_task(tid)
    if cur["status"] == "pending" and cur["attempts"] >= 1 and not w2_started:
        w2_started = True
        threading.Thread(target=worker2, daemon=True).start()
    if cur["status"] == "done":
        break
    time.sleep(1)

print("\n-- timeline --")
for e in store.events(tid):
    print(f"  {e['type']:10s} {e['detail']}")
print("-- alerts (passive log, never pushed) --")
alerts = store.alerts(unacked_only=False)
for a in alerts:
    print(f"  #{a['id']} level={a['level']}: {a['message']}")

final = store.get_task(tid)
assert final["status"] == "done", "task should be done"
assert final["attempts"] == 1, "should have taken exactly one auto-retry"
assert any(e["type"] == "auto-requeue" for e in store.events(tid)), "missing auto-requeue"
assert any(a["level"] == 2 for a in alerts), "missing passive auto-retry record"
print("\nDEMO PASS: stuck -> replan -> auto-retry -> fresh worker -> done."
      " Zero human involvement.", flush=True)
store.close()
