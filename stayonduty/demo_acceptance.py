#!/usr/bin/env python3
"""Iteration 7 proof: acceptance criteria + artifact verification.

Scenario: an agent registers 'build greeting widget' with a definition of
done. It does sloppy work, calls complete() — StayOnDuty verifies against
the real artifact, REFUSES completion, and tells the worker exactly what's
missing. The worker fixes it, reports evidence, completes — verified.
Zero attempts consumed by the refusal, zero human involvement.

Also: the same loop over raw MCP, plus a no-criteria regression.
"""
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stayonduty.sdk import Client          # noqa: E402
from stayonduty.store import Store          # noqa: E402
from stayonduty.dashboard import task_detail  # noqa: E402
from stayonduty.mcp_server import _handle as mcp_handle  # noqa: E402

DB = "/tmp/acc_demo.db"
for f in (DB, DB + "-wal", DB + "-shm"):
    if os.path.exists(f):
        os.remove(f)

tmp = tempfile.mkdtemp(prefix="sod_acc_")
out = os.path.join(tmp, "output.txt")
smoke = os.path.join(tmp, "smoke.py")
with open(smoke, "w") as f:
    f.write("import sys\n"
            "data = open(r'" + out + "', encoding='utf-8', errors='replace').read()\n"
            "sys.exit(0 if 'hello' in data else 1)\n")

client = Client(DB)

print("=== phase 1: sloppy work meets verification ===")
tid = client.register(
    "build greeting widget",
    acceptance=[
        {"description": "output.txt exists",
         "check": {"type": "file_exists", "path": out}},
        {"description": "output.txt says hello",
         "check": {"type": "file_contains", "path": out, "text": "hello"}},
        {"description": "smoke script passes",
         "check": {"type": "command", "cmd": f"python3 {smoke}",
                   "cwd": tmp}},
        "widget reviewed",   # plain string -> manual, evidence required
    ])
s = Store(DB)
assert s.get_task(tid)["status"] == "pending"

with client.lease(task_id=tid, owner="agent-1") as job:
    # sloppy: writes the wrong content, skips the review
    with open(out, "w") as f:
        f.write("hi\n")
    job.step("wrote output.txt")
    verdict = job.complete("widget built!")
    assert verdict["verified"] is False, verdict
    by_id = {c["id"]: c for c in verdict["criteria"]}
    assert by_id["ac1"]["status"] == "passed", by_id
    assert by_id["ac2"]["status"] == "failed", by_id
    assert by_id["ac3"]["status"] == "failed", by_id
    assert by_id["ac4"]["status"] == "failed", by_id
    assert "claim is not proof" in by_id["ac4"]["evidence"], by_id["ac4"]
    print("complete() REFUSED — failing:",
          [c["id"] for c in verdict["criteria"]
           if c["status"] != "passed"])

    # worker reads the verdict, fixes the artifact, reports evidence
    with open(out, "w") as f:
        f.write("hello world\n")
    job.step("fixed greeting")
    assert job.acceptance_check("ac4", True,
                                "read the diff: single-line change, matches spec")
    verdict = job.complete("widget built, verified")
    assert verdict["verified"] is True, verdict
    assert all(c["status"] == "passed" for c in verdict["criteria"])

t = s.get_task(tid)
assert t["status"] == "done", t
assert t["attempts"] == 0, "refusal must not consume attempts"
types = [e["type"] for e in s.events(tid)]
assert "verify_failed" in types, types
assert "completed" in types, types
print("task done, attempts:", t["attempts"], "— refusal cost nothing")

print("=== phase 2: dashboard shows the checklist ===")
html_out = task_detail(s, tid)
assert "Acceptance criteria" in html_out
for cid in ("ac1", "ac2", "ac3", "ac4"):
    assert cid in html_out, cid
assert "✅" in html_out
print("checklist rendered with per-criterion evidence")

print("=== phase 3: same loop over raw MCP ===")


def rpc(method, params=None):
    return mcp_handle(DB, {"jsonrpc": "2.0", "id": 1, "method": method,
                           "params": params or {}})


r = rpc("tools/call", {"name": "stayonduty_register_task",
                       "arguments": {
                           "title": "mcp widget",
                           "acceptance": [
                               {"description": "output.txt exists",
                                "check": {"type": "file_exists",
                                          "path": out}},
                               "reviewed by builder"]}})
mtid = json.loads(r["result"]["content"][0]["text"])["task_id"]
rpc("tools/call", {"name": "stayonduty_claim_task",
                   "arguments": {"owner": "mcp-agent", "task_id": mtid}})
v = rpc("tools/call", {"name": "stayonduty_complete",
                       "arguments": {"task_id": mtid, "owner": "mcp-agent",
                                     "result": "done"}})
verdict = json.loads(v["result"]["content"][0]["text"])["verdict"]
assert verdict["verified"] is False, verdict  # manual criterion unevidenced
r = rpc("tools/call", {"name": "stayonduty_acceptance_check",
                       "arguments": {"task_id": mtid, "owner": "mcp-agent",
                                     "criterion_id": "ac2", "passed": True,
                                     "evidence": "opened it, looks right"}})
assert json.loads(r["result"]["content"][0]["text"])["ok"] is True
v = rpc("tools/call", {"name": "stayonduty_complete",
                       "arguments": {"task_id": mtid, "owner": "mcp-agent",
                                     "result": "done"}})
verdict = json.loads(v["result"]["content"][0]["text"])["verdict"]
assert verdict["verified"] is True, verdict
print("MCP: refused without evidence, completed with evidence")

print("=== phase 4: no-criteria regression ===")
t2 = client.register("plain task")
with client.lease(task_id=t2, owner="agent-1") as job:
    verdict = job.complete("done")
    assert verdict["verified"] is True
assert s.get_task(t2)["status"] == "done"
print("no-criteria tasks complete exactly as before")

print("=== phase 5: migration check on old database ===")
subprocess.run([sys.executable, "-c",
                "import sys; sys.path.insert(0,'.');"
                "from stayonduty.store import Store;"
                "s=Store('/tmp/spend_demo.db'); s.close();"
                "print('old db (iteration 6) opens, migrates cleanly')"],
               check=True, cwd=os.path.dirname(
                   os.path.dirname(os.path.abspath(__file__))))
s2 = Store("/tmp/spend_demo.db")
old_tid = s2.create_task("migration probe")
assert s2.get_task(old_tid)["acceptance_criteria"] == []
s2.close()

print("\nALL ACCEPTANCE CHECKS PASSED")
