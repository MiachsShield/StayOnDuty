"""Iteration 4 demo: StayOnDuty supervises a provider-backed image task.

Task: "generate 6 images per spec" against the fake Grok provider with
quota=3 (reset +25s) and one corrupt output mid-run.

Shows, with zero human involvement:
  - per-unit ctx.step() progress on the dashboard timeline
  - corrupt output caught by the verifier and retried
  - quota hit -> task parks as WAITING (the watchdog does NOT flag it stuck)
  - resume after the reset window, fresh worker picks up where it left off
  - task completes; waiting consumed no attempts

Run:  python3 -m stayonduty.demo_provider
Dashboard (live during the demo): http://localhost:18081/
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from stayonduty.store import Store  # noqa: E402
from stayonduty.worker import run_one, QuotaWait  # noqa: E402
from stayonduty.verify import verify_image  # noqa: E402
from stayonduty.providers.fake import FakeProvider  # noqa: E402
from stayonduty.providers.base import QuotaExhausted  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "demo_provider.db")
for ext in ("", "-wal", "-shm"):
    p = DB + ext
    if os.path.exists(p):
        os.remove(p)

TOTAL = 6
SPEC = "brutalist concrete tower at dusk, dramatic lighting"

provider = FakeProvider(quota_limit=3, reset_after_secs=25, corrupt_at={3})
print(f"[main] fake provider: quota=3 per 25s window, call #3 corrupt", flush=True)

store = Store(DB)
tid = store.create_task("generate 6 images per spec",
                        {"total": TOTAL, "spec": SPEC})
print(f"[main] task {tid} created", flush=True)

# Dashboard in a daemon thread so the timeline is browsable live.
sys.argv = ["dashboard", DB, "--port", "18081"]
import stayonduty.dashboard as dashmod  # noqa: E402
from http.server import HTTPServer  # noqa: E402


def _serve():
    try:
        HTTPServer(("127.0.0.1", 18081), dashmod.Handler).serve_forever()
    except OSError as e:
        print(f"[dashboard] could not bind :18081 ({e}) — continuing headless",
              flush=True)


threading.Thread(target=_serve, daemon=True).start()
print("[main] dashboard: http://localhost:18081/", flush=True)


def gen_verified(ctx, n):
    """One image, verified; retried once on verification failure."""
    for attempt in (1, 2):
        res = provider.generate(f"{SPEC} — variation {n}", kind="image")
        v = verify_image(res.image_bytes, min_width=32, min_height=32)
        if v.passed:
            return v
        ctx.note(f"image {n}/{TOTAL}: failed verification"
                 f" ({'; '.join(v.reasons)}) — retrying")
    raise RuntimeError(f"image {n}/{TOTAL}: output failed verification twice"
                       f" ({'; '.join(v.reasons)})")


def work(ctx):
    for n in range(1, TOTAL + 1):
        if not ctx.alive():
            return
        if ctx.recall(f"done:{n}"):
            ctx.step(f"image {n}/{TOTAL}: already verified, skipping")
            continue
        try:
            v = gen_verified(ctx, n)
        except QuotaExhausted as q:
            raise QuotaWait(q.reset_at,
                            f"image quota exhausted ({provider.quota_status()})",
                            provider=provider.display_name)
        ctx.remember(f"done:{n}", {"w": v.width, "h": v.height})
        ctx.step(f"image {n}/{TOTAL}: verified {v.width}x{v.height} {v.format}")


workers = []


def worker_alive():
    return any(t.is_alive() for t in workers)


def spawn_worker(tag):
    t = threading.Thread(
        target=lambda: run_one(DB, tag, work, lease_secs=30),
        daemon=True)
    workers.append(t)
    t.start()
    print(f"[main] {tag} started", flush=True)


t0 = time.time()
TIMEOUT = 150
while time.time() - t0 < TIMEOUT:
    det = store.detect_stuck(stuck_after_secs=10, escalate_after_secs=30,
                             max_attempts=3)
    for t in det["stuck"]:
        print(f"[watchdog] STUCK: '{t['title']}' — no progress for 10s", flush=True)
    for t in det["requeued"]:
        print(f"[watchdog] AUTO-RETRY: '{t['title']}'", flush=True)
    for t in store.promote_waiting():
        print(f"[watchdog] RESUMED: '{t['title']}' ({t['id']}) — quota window"
              f" reached, back in the queue on its own", flush=True)
    cur = store.get_task(tid)
    if cur["status"] == "pending" and not worker_alive():
        spawn_worker(f"agent-{len(workers) + 1}")
    if cur["status"] == "done":
        break
    if cur["status"] == "failed":
        break
    time.sleep(1)

print("\n-- timeline --")
events = store.events(tid)
for e in events:
    print(f"  {e['type']:10s} {e['detail']}")

final = store.get_task(tid)
types = [e["type"] for e in events]
verified_steps = sum(1 for e in events
                     if e["type"] == "step" and "verified" in e["detail"]
                     and "skipping" not in e["detail"])
checks = [
    ("task completed", final["status"] == "done"),
    (f"{TOTAL} verified per-unit steps", verified_steps == TOTAL),
    ("corrupt output caught + retried",
     any(e["type"] == "note" and "failed verification" in e["detail"]
         for e in events)),
    ("quota park recorded as waiting", "waiting" in types),
    ("resumed after reset window", "resumed" in types),
    ("watchdog never flagged it stuck", "stuck" not in types),
    ("waiting consumed no attempts", final["attempts"] == 0),
]
print("\n-- checks --")
ok = True
for label, passed in checks:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
    ok = ok and passed

print(("\nDEMO PASS: 6 images generated across quota windows — corrupt output"
       " retried, quota waits parked/resumed, zero human involvement.")
      if ok else
      "\nDEMO FAIL: see checks above.")
store.close()
sys.exit(0 if ok else 1)
