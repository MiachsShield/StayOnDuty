"""Spend guardrails — the $47k-runaway stories can't happen here.

End-to-end proof that a task with a spend cap parks ITSELF when the cap
is exceeded, with zero human involvement and zero attempts consumed:

  Phase 1: worker-1 runs "generate 10 thumbnails" (cap: 1000 tokens),
           reporting 300 tokens/unit. After unit 3 the guardrail trips:
           task -> budget_hold, lease released, worker stands down.
  Phase 2: guardrail invariants — no usage accepted on hold, stuck
           detection and crash recovery ignore held tasks, the old
           worker's lease is dead.
  Phase 3: the budget is raised (pull-only, like the dashboard form) ->
           task returns to pending, notice clears, attempts untouched.
  Phase 4: worker-2 claims the SAME task, recalls last_index=3, finishes
           units 4-9, completes. Nothing redone.
  Phase 5: USD caps work the same way.
  Phase 6: the MCP surface exposes report/raise over raw JSON-RPC.

Pass criteria: task ends `done` with 3000 tokens used; the hold consumed
no attempts; no spend happened while on hold; every phase asserts.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB = os.path.join(ROOT, "demo_spend.db")

for f in (DB, DB + "-wal", DB + "-shm"):
    if os.path.exists(f):
        os.remove(f)

sys.path.insert(0, ROOT)
from stayonduty import Client  # noqa: E402
from stayonduty.store import Store  # noqa: E402

TOKENS_PER_UNIT = 300
UNITS = 10


def work_units(job, upto):
    """Do units of fake work, reporting usage; stop on guardrail."""
    start = (job.recall("last_index") or -1) + 1
    for i in range(start, upto):
        job.step(f"thumbnail {i}")
        job.remember("last_index", i)
        r = job.report_usage(prompt_tokens=200, completion_tokens=100)
        assert r["ok"], r
        if r["on_hold"]:
            job.note("guardrail tripped — standing down, no more spend")
            return False  # parked, not finished
    return True


def main():
    client = Client(DB)

    # --- Phase 1: the guardrail trips mid-task ---
    tid = client.register("generate 10 thumbnails", token_budget=1000)
    with client.lease(task_id=tid, owner="worker-1", lease_secs=30) as job:
        finished = work_units(job, UNITS)
        assert not finished, "guardrail should have tripped"
        assert job.on_hold, "lease must see the budget hold"
    # clean context exit must NOT log a bogus 'completed' event:
    kinds = [e["type"] for e in Store(DB).events(tid)]
    assert "completed" not in kinds, kinds
    assert "budget_hold" in kinds, kinds
    t = client.status(tid)["task"]
    assert t["status"] == "budget_hold", t["status"]
    assert t["tokens_used"] == 1200, t["tokens_used"]  # 4 units x 300
    assert t["attempts"] == 0, "a hold is a pause, not a retry"
    print("[demo] Phase 1: guardrail tripped at 1200/1000 tokens — "
          "task parked, worker stood down, 0 attempts consumed")

    # --- Phase 2: invariants while on hold ---
    s = Store(DB)
    try:
        # no usage accepted from anyone while held
        r = s.report_usage(tid, "worker-1", prompt_tokens=10)
        assert r == {"ok": False, "on_hold": False,
                     "reason": "not your in_progress task"}, r
        # stuck detection ignores held tasks entirely
        res = s.detect_stuck(stuck_after_secs=1, escalate_after_secs=1)
        assert not res["stuck"] and not res["requeued"], res
        # crash recovery ignores held tasks entirely
        res = s.recover_stale()
        assert res == {"released": 0, "failed": 0}, res
        # the old worker's lease is dead
        assert not s.heartbeat(tid, "worker-1", 30)
        # passive notice is logged, exactly once
        notices = [a for a in s.alerts()
                   if a["message"].startswith("\U0001f4b0")]
        assert len(notices) == 1 and notices[0]["level"] == 1, notices
        print("[demo] Phase 2: hold is airtight — no spend accepted, watchdog"
              " and recovery ignore it, one passive notice logged")
    finally:
        s.close()

    # --- Phase 3: raise the budget, task resumes ---
    assert client.raise_budget(tid, token_budget=5000) is True
    t = client.status(tid)["task"]
    assert t["status"] == "pending", t["status"]
    assert t["attempts"] == 0, t
    assert Store(DB).alerts() == [], "notice must clear on resume"
    assert client.raise_budget(tid, token_budget=1) is False, \
        "raise only works on held tasks"
    print("[demo] Phase 3: budget raised to 5000 — task back in queue,"
          " notice cleared, attempts still 0")

    # --- Phase 4: fresh worker resumes from memory, finishes ---
    with client.lease(task_id=tid, owner="worker-2", lease_secs=30) as job:
        assert job.recall("last_index") == 3
        assert work_units(job, UNITS) is True
    t = client.status(tid)["task"]
    assert t["status"] == "done", t["status"]
    assert t["tokens_used"] == 3000, t["tokens_used"]  # 10 units, none redone
    assert t["attempts"] == 0, t
    print("[demo] Phase 4: worker-2 resumed at unit 4 from memory —"
          " all 10 done, 3000 tokens total, 0 attempts")

    # --- Phase 5: USD caps ---
    tid2 = client.register("usd-capped job", usd_budget=0.05)
    with client.lease(task_id=tid2, owner="worker-3") as job:
        r = job.report_usage(usd=0.10)
        assert r["on_hold"] is True, r
    t = client.status(tid2)["task"]
    assert t["status"] == "budget_hold" and t["usd_used"] == 0.10, t
    assert client.raise_budget(tid2, usd_budget=1.0) is True
    print("[demo] Phase 5: USD cap tripped at $0.10/$0.05 — same machinery")

    # --- Phase 6: MCP surface over raw JSON-RPC ---
    proc = subprocess.Popen(
        [sys.executable, "-m", "stayonduty.mcp_server", "--db", DB],
        cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, bufsize=1)
    rid = 0

    def rpc_call(method, params=None):
        nonlocal rid
        rid += 1
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()
        out = json.loads(proc.stdout.readline())
        if out.get("error"):
            raise RuntimeError(out["error"])
        return out["result"]

    def tool(name, **args):
        res = rpc_call("tools/call", {"name": name, "arguments": args})
        if res.get("isError"):
            raise RuntimeError(f"tool {name}: {res['content']}")
        return json.loads(res["content"][0]["text"])

    try:
        rpc_call("initialize", {"protocolVersion": "2024-11-05",
                                "capabilities": {},
                                "clientInfo": {"name": "demo", "version": "0"}})
        tid3 = tool("stayonduty_register_task", title="mcp budget job",
                    token_budget=500)["task_id"]
        tool("stayonduty_claim_task", task_id=tid3, owner="mcp-agent")
        r = tool("stayonduty_report_usage", task_id=tid3, owner="mcp-agent",
                 prompt_tokens=400, completion_tokens=200)
        assert r["on_hold"] is True, r
        st = tool("stayonduty_task_status", task_id=tid3)["task"]
        assert st["status"] == "budget_hold", st["status"]
        r = tool("stayonduty_raise_budget", task_id=tid3, token_budget=2000)
        assert r["ok"] is True, r
        st = tool("stayonduty_task_status", task_id=tid3)["task"]
        assert st["status"] == "pending", st["status"]
        print("[demo] Phase 6: foreign agent drove budgets over MCP —"
              " register w/ cap, trip, raise, resume")
    finally:
        proc.terminate()
        proc.wait(timeout=5)

    print("\nPASS: spend guardrail parks runaway tasks by itself — caps hold,"
          " no attempts burned, no spend on hold, resume on budget raise.")


if __name__ == "__main__":
    t0 = time.time()
    main()
    print(f"(demo took {time.time() - t0:.1f}s)")
