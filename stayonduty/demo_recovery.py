"""Iteration 11 demo: the autonomy journal — "handled on its own".

Proves the confidence surface: every time StayOnDuty handles a problem
itself, a human sentence lands in a pull-only feed:

    "Muse" stopped "generate 12 card faces" at "6:42 PM" — StayOnDuty
    resumed the task at "6:43 PM"

Covers: worker crash -> recovery -> resume (paired sentence), the stuck
ladder (nudge -> auto-retry -> resume), give-up after exhausted attempts,
review-fail requeue, plus the dashboard feed, /api/recovery, the MCP
tool, and the SDK surface. No human involved at any point.

Run:  python3 -m stayonduty.demo_recovery
"""
import json
import os
import sys
import threading
import time
import urllib.request
from http.server import HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from stayonduty.store import Store  # noqa: E402

DB = "demo_recovery.db"
OUT = "demo_recovery_out"
for ext in ("", "-wal", "-shm"):
    p = DB + ext
    if os.path.exists(p):
        os.remove(p)
os.makedirs(OUT, exist_ok=True)

store = Store(DB)

# ---------- part 1: worker dies mid-task, StayOnDuty resumes it ----------
print("=== part 1: crash -> recovery -> resume ===", flush=True)
faces = [{"description": f"face {i:02d} rendered",
          "check": {"type": "file_exists",
                    "path": f"{OUT}/face_{i:02d}.txt"}} for i in range(12)]
tid = store.create_task("generate 12 card faces", acceptance=faces)
pid = os.fork()
if pid == 0:  # child: the doomed worker
    try:
        s = Store(DB)
        s.claim_task("Muse", lease_secs=30, task_id=tid)
        for i in range(4):  # partial work, then dies
            with open(f"{OUT}/face_{i:02d}.txt", "w") as f:
                f.write(f"face {i:02d}")
            s.step(tid, f"face {i:02d} done")
        s.remember("faces_done", 4, scope=f"task:{tid}")
        s.close()
    finally:
        os._exit(1)  # crash: no release, no more heartbeats
os.waitpid(pid, 0)
print("[main] worker 'Muse' crashed after 4/12 faces", flush=True)
# Simulate time passing: the lease expires with nobody renewing it.
store.db.execute("UPDATE tasks SET lease_expires_at=? WHERE id=?",
                 (time.time() - 5, tid))
store.db.commit()
res = store.recover_stale()
assert res == {"released": 1, "failed": 0}, res
print("[main] recover_stale released the dead worker's task", flush=True)

t = store.claim_task("grok-2", lease_secs=30, task_id=tid)
assert t and t["attempts"] == 1
done = store.recall("faces_done", scope=f"task:{tid}")
print(f"[worker grok-2] resumed from checkpoint: faces_done={done}", flush=True)
assert done == 4, "fresh worker must resume from memory, not redo work"
for i in range(done, 12):
    with open(f"{OUT}/face_{i:02d}.txt", "w") as f:
        f.write(f"face {i:02d}")
    store.heartbeat(tid, "grok-2", 30)
    store.step(tid, f"face {i:02d} done")
store.complete_task(tid, "grok-2", "all 12 faces rendered")
assert store.get_task(tid)["status"] == "done"
print("[worker grok-2] finished 12/12 — task done", flush=True)

# ---------- part 2: the stuck ladder writes sentences too ----------
print("=== part 2: stuck -> replan nudge -> auto-retry -> resume ===",
      flush=True)
tid2 = store.create_task("backfill search index")
t0 = time.time()
store.claim_task("Muse", lease_secs=3600, task_id=tid2)  # alive, no progress
r = store.detect_stuck(now=t0 + 700)
assert len(r["stuck"]) == 1 and not r["requeued"], r
print("[main] stuck detected — worker nudged to replan", flush=True)
r = store.detect_stuck(now=t0 + 2600)
assert len(r["requeued"]) == 1, r
print("[main] still stuck — fresh attempt queued autonomously", flush=True)
t = store.claim_task("astra", lease_secs=30, task_id=tid2)
assert t and t["attempts"] == 1
store.complete_task(tid2, "astra", "index backfilled")
print("[worker astra] fresh attempt completed it", flush=True)

# ---------- part 3: give-up after exhausted attempts ----------
print("=== part 3: worker keeps dying -> passive give-up ===", flush=True)
tid3 = store.create_task("render 4k trailer")
for _ in range(3):
    store.claim_task("Muse", lease_secs=30, task_id=tid3)
    store.db.execute("UPDATE tasks SET lease_expires_at=? WHERE id=?",
                     (time.time() - 5, tid3))
    store.db.commit()
    store.recover_stale()
assert store.get_task(tid3)["status"] == "failed"
print("[main] 3 dead attempts — failed as a passive record", flush=True)

# ---------- part 4: review-fail requeue lands in the journal ----------
print("=== part 4: independent review fails -> requeued on its own ===",
      flush=True)
tid4 = store.create_task("write release notes",
                         acceptance=["notes mention v2"],
                         verify="independent")
store.claim_task("Muse", lease_secs=30, task_id=tid4)
store.acceptance_check(tid4, "Muse", "ac1", False, "a claim is not proof")
# Producer cannot self-complete unevidenced manual criteria; force the
# review path with a real evidence note instead.
store.acceptance_check(tid4, "Muse", "ac1", True,
                       "notes file lists v2 changes line by line")
store.complete_task(tid4, "Muse", "notes drafted")
assert store.get_task(tid4)["status"] == "in_review"
store.claim_review("grok-2", task_id=tid4)
assert store.submit_verdict(tid4, "grok-2", "fail", "missing changelog")
assert store.get_task(tid4)["status"] == "pending"
print("[main] review failed — requeued with verifier notes, no human",
      flush=True)

# ---------- the sentences ----------
print("=== the autonomy journal ===", flush=True)
sentences = store.recovery_sentences()
for s in sentences:
    print("  🔁", s, flush=True)

canon = [s for s in sentences
         if '"Muse" stopped "generate 12 card faces" at "' in s
         and "— StayOnDuty resumed the task at \"" in s]
assert len(canon) == 1, f"canonical paired sentence missing: {sentences}"
print("[check] canonical stopped/resumed sentence present", flush=True)
assert any('"Muse" stalled on "backfill search index"' in s
           and "nudged it to replan" in s for s in sentences)
assert any("queued a fresh attempt on its own" in s for s in sentences)
assert any('"render 4k trailer" could not finish on its own' in s
           for s in sentences)
# Defensive wording for a stop whose task later failed without another
# resume: unit-check the renderer directly.
from stayonduty.store import format_recovery  # noqa: E402
dead_end = format_recovery(
    {"task_title": "x", "worker": "Muse", "event": "worker_stopped",
     "at": time.time(), "detail": ""},
    task_status="failed")
assert "its retries ran out" in dead_end, dead_end
print("[check] failed-task stop wording renders correctly", flush=True)
# Both trailer recoveries pair chronologically with their resumes.
assert sum(1 for s in sentences
           if '"Muse" stopped "render 4k trailer"' in s
           and "StayOnDuty resumed the task" in s) == 2
assert any("independent review round 1 failed" in s for s in sentences)
print("[check] stuck / auto-retry / give-up / review sentences present",
      flush=True)

# ---------- dashboard feed + /api/recovery ----------
print("=== dashboard + api ===", flush=True)
from stayonduty import dashboard as dash  # noqa: E402
dash.DB = DB
feed_html = dash.recovery_feed_html(store)
assert "Handled on its own" in feed_html
assert "StayOnDuty resumed the task" in feed_html
print("[check] dashboard 'Handled on its own' section renders", flush=True)

srv = HTTPServer(("127.0.0.1", 0), dash.Handler)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/api/recovery") as resp:
    api = json.loads(resp.read())
assert "sentences" in api and "entries" in api
assert any("StayOnDuty resumed the task" in s for s in api["sentences"])
print("[check] /api/recovery serves entries + sentences", flush=True)
with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as resp:
    home = resp.read().decode()
assert "Handled on its own" in home
print("[check] dashboard home page includes the feed", flush=True)
srv.shutdown()

# ---------- MCP + SDK ----------
print("=== mcp + sdk ===", flush=True)
from stayonduty.mcp_server import _call_tool, TOOLS  # noqa: E402
assert len(TOOLS) == 20, len(TOOLS)
res = _call_tool(DB, "stayonduty_recovery", {"limit": 50})
payload = json.loads(res["content"][0]["text"])
assert any("StayOnDuty resumed the task" in s
           for s in payload["sentences"]), payload
print("[check] stayonduty_recovery MCP tool (20 tools total)", flush=True)

from stayonduty.sdk import Client  # noqa: E402
sdk_sentences = Client(DB).recovery_feed()
assert any("StayOnDuty resumed the task" in s for s in sdk_sentences)
print("[check] SDK Client.recovery_feed()", flush=True)

store.close()
print("\nAll recovery-feed checks passed: every autonomous fix wrote its"
      " sentence, and the dashboard, API, MCP, and SDK all serve it.",
      flush=True)
