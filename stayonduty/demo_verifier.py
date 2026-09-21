#!/usr/bin/env python3
"""StayOnDuty iteration 9 — independent verifier demo.

Proves the producer can never grade its own work:

  1. Producer finishes a verify="independent" task -> parked in `in_review`.
  2. The producer trying to review its own work is refused (store AND
     worker level).
  3. A different verifier judges it: the deterministic gate passed, but
     the judge FAILs the producer's self-review ("a claim is not proof").
     Task re-queues with the verdict in its timeline; round 1 recorded.
  4. Producer fixes it with real evidence, resubmits -> verifier PASSes.
     Task done, verified_by recorded, producer != verifier.
  5. Bounded: 3 failed rounds -> failed as a passive record, never a push.
  6. Raw-MCP + SDK ReviewClaim surfaces work the same way.

The judge here is the scripted FakeProvider (keys off the SELF-REVIEW
marker the review prompt instructs judges to emit). A production
deployment wires provider_review_policy(XAIProvider(...)).
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stayonduty import Client  # noqa: E402
from stayonduty.providers.fake import FakeProvider  # noqa: E402
from stayonduty.worker import provider_review_policy, run_one  # noqa: E402
from stayonduty.store import Store  # noqa: E402

TMP = tempfile.mkdtemp(prefix="sod-verify-")
DB = os.path.join(TMP, "work.db")
OUT = os.path.join(TMP, "out")
os.makedirs(OUT, exist_ok=True)

CRITERIA = [
    {"description": "report.md exists",
     "check": {"type": "file_exists", "path": os.path.join(OUT, "report.md")}},
    {"description": "report has a conclusion",
     "check": {"type": "file_contains", "path": os.path.join(OUT, "report.md"),
               "text": "Conclusion"}},
    "a second engineer reviewed the changes",
]

PRODUCER = "producer-1"
VERIFIER = "verifier-1"
policy = provider_review_policy(FakeProvider())


def noop(ctx):
    raise RuntimeError("should never produce in this demo step")


def work_round_0(ctx):
    with open(os.path.join(OUT, "report.md"), "w") as f:
        f.write("# Weekly report\n\nConclusion: all green.\n")
    ctx.step("wrote report.md")
    ctx.acceptance_check(
        "ac3", True,
        "SELF-REVIEW: I read it over myself, looks good")


def work_round_1(ctx):
    ctx.acceptance_check(
        "ac3", True,
        "Maya (second engineer) reviewed the diff; 2 findings fixed")
    ctx.step("addressed review findings")


print("== 1. producer finishes; task parks in in_review ==")
client = Client(DB)
tid = client.register("ship the weekly report", acceptance=CRITERIA,
                      verify="independent")
run_one(DB, PRODUCER, work_round_0, lease_secs=5)
t = client.status(tid)["task"]
assert t["status"] == "in_review", t["status"]
assert t["producer_owner"] == PRODUCER
print(f"   task {tid} in_review, producer={t['producer_owner']}")

print("== 2. producer cannot review its own work ==")
s = Store(DB)
assert s.claim_review(PRODUCER, task_id=tid) is None  # store level
run_one(DB, PRODUCER, noop, lease_secs=5, review_policy=policy)  # worker level
t = client.status(tid)["task"]
assert t["status"] == "in_review" and t["lease_owner"] is None, t
s.close()
print("   refused at store and worker level; review still unclaimed")

print("== 3. independent verifier FAILs the self-review ==")
run_one(DB, VERIFIER, noop, lease_secs=5, review_policy=policy)
t = client.status(tid)["task"]
assert t["status"] == "pending", t["status"]  # re-queued, not done
revs = client.reviews(tid)
assert len(revs) == 1 and revs[0]["verdict"] == "fail", revs
assert revs[0]["verifier_owner"] == VERIFIER
print(f"   round 1: FAIL by {VERIFIER}: {revs[0]['reason'][:80]}")

print("== 4. producer fixes with real evidence; verifier PASSes ==")
run_one(DB, PRODUCER, work_round_1, lease_secs=5)
t = client.status(tid)["task"]
assert t["status"] == "in_review", t["status"]
run_one(DB, VERIFIER, noop, lease_secs=5, review_policy=policy)
t = client.status(tid)["task"]
assert t["status"] == "done", t["status"]
assert t["verified_by"] == VERIFIER and t["producer_owner"] == PRODUCER
assert t["verified_by"] != t["producer_owner"]
revs = client.reviews(tid)
assert len(revs) == 2 and revs[1]["verdict"] == "pass", revs
print(f"   round 2: PASS by {VERIFIER}; task done, verified_by recorded")

print("== 5. bounded: 3 failed rounds -> passive failure, no push ==")
tid2 = client.register("sloppy task", acceptance=["done right"],
                       verify="independent")
s = Store(DB)
for rnd in range(3):
    s.claim_task("p", 60, task_id=tid2)
    s.acceptance_check(tid2, "p", "ac1", True, "SELF-REVIEW: trust me")
    v = s.complete_task(tid2, "p")
    assert v.get("in_review"), v
    s.claim_review("v", 60, task_id=tid2)
    assert s.submit_verdict(tid2, "v", "fail", f"round {rnd}: still no proof")
t = s.get_task(tid2)
assert t["status"] == "failed" and t["review_rounds"] == 3, t
alerts = [a for a in s.alerts() if a["task_id"] == tid2]
assert len(alerts) == 1 and "independent review 3 times" in alerts[0]["message"]
s.close()
print("   failed after 3 rounds; passive alert logged, nobody paged")

print("== 6. SDK ReviewClaim + raw MCP surfaces ==")
tid3 = client.register("sdk-reviewed task", acceptance=["checked"],
                       verify="independent")
s = Store(DB)
s.claim_task("p", 60, task_id=tid3)
s.acceptance_check(tid3, "p", "ac1", True, "Maya reviewed; findings fixed")
assert s.complete_task(tid3, "p").get("in_review")
s.close()
try:
    with client.claim_review(tid3, "p"):
        raise AssertionError("producer must not claim own review")
except RuntimeError:
    print("   SDK: producer claim refused")
with client.claim_review(tid3, "sdk-verifier") as rc:
    assert rc.verdict(True, "evidence names a real reviewer") is True
t = client.status(tid3)["task"]
assert t["status"] == "done" and t["verified_by"] == "sdk-verifier"
print("   SDK: ReviewClaim verdict accepted; task done")

from stayonduty import mcp_server  # noqa: E402
r = mcp_server._call_tool(
    DB, "stayonduty_register_task",
    {"title": "mcp-reviewed task", "verify": "independent"})
tid4 = r["content"][0]["text"]
assert '"task_id"' in tid4
print("   MCP: register with verify='independent' ok;",
      len(mcp_server.TOOLS), "tools total")

print()
print("ALL VERIFIER CHECKS PASSED")
