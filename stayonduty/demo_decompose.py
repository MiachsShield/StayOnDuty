"""Iteration 10 demo: hierarchical decomposition.

A worker claims a big task ("generate 50 card faces"), realizes it's big,
and splits it into 5 subtasks with their own acceptance criteria. The
parent goes `decomposed` — not claimable, no lease — and completes on its
own when every child is done and its own acceptance verifies.

Also shown:
  * crash-safety composes: a child worker dies mid-batch (os._exit), the
    lease expires, and a fresh worker resumes from the child's memory
  * refusal paths: non-owner, empty/bad specs, re-decompose, depth limit
  * failure propagation: one failed child -> passive failed parent record
    naming the child (no push), the good sibling stays done
  * subtree spend caps: a parent budget caps the whole subtree; raising
    the parent's budget resumes parked children
  * SDK + raw MCP paths, and verify_mode inheritance

Run: python3 -m stayonduty.demo_decompose
"""
import os
import subprocess
import sys
import time
import json
import zlib
import struct

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from stayonduty.store import Store  # noqa: E402
from stayonduty.sdk import Client  # noqa: E402
from stayonduty.worker import run_one  # noqa: E402
from stayonduty import mcp_server  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB = os.path.join(HERE, "demo_decompose.db")
OUT = os.path.join(HERE, "demo_decompose_out")

for ext in ("", "-wal", "-shm"):
    if os.path.exists(DB + ext):
        os.remove(DB + ext)
os.makedirs(OUT, exist_ok=True)
for f in os.listdir(OUT):
    os.remove(os.path.join(OUT, f))

WORKER_SCRIPT = os.path.join(HERE, "_demo_decompose_worker.py")


def count_cmd(lo, hi):
    rng = f"range({lo}, {hi + 1})"
    return (f"python3 -c \"import os,sys; d='{OUT}'; sys.exit(0 if all("
            f"os.path.exists(os.path.join(d, f'face_{{i:02d}}.png'))"
            f" for i in {rng}) else 1)\"")


def write_png(path, seed):
    w = h = 8
    raw = b"".join(
        b"\x00" + bytes(v for x in range(w) for v in
                        ((seed * 37 + x * 11 + y * 7) % 256,
                         (seed * 53 + x * 5) % 256, 128))
        for y in range(h))

    def chunk(typ, data):
        c = struct.pack(">I", len(data)) + typ + data
        return c + struct.pack(">I", zlib.crc32(typ + data))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw))
           + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)


def child_work(ctx):
    """One child: faces lo..hi, resuming from memory after a crash.

    When the module-level CRASH_AFTER is set (crash-worker subprocess),
    die suddenly after that many files — no cleanup, heartbeats stop.
    """
    lo, hi = ctx.task["payload"]["lo"], ctx.task["payload"]["hi"]
    start = (ctx.recall("last_index") or lo - 1) + 1
    done = 0
    for i in range(start, hi + 1):
        if not ctx.alive():
            return
        write_png(os.path.join(OUT, f"face_{i:02d}.png"), i)
        ctx.step(f"face {i:02d}")
        ctx.remember("last_index", i)
        done += 1
        if CRASH_AFTER and done >= CRASH_AFTER:
            print("  [CRASH] sudden death mid-child — no cleanup",
                  flush=True)
            os._exit(1)


CRASH_AFTER = 0


def write_worker_script():
    # Self-contained: it must NOT import this demo module (the demo runs
    # at import time). Everything the worker needs is inlined.
    with open(WORKER_SCRIPT, "w") as f:
        f.write(
            "import os, sys, zlib, struct\n"
            f"sys.path.insert(0, {ROOT!r})\n"
            "from stayonduty.worker import run_one\n"
            "OUT = os.environ['DEMO_OUT']\n"
            "CRASH_AFTER = int(sys.argv[3])\n"
            "def write_png(path, seed):\n"
            "    w = h = 8\n"
            "    raw = b''.join(bytes([0]) + bytes(v for x in range(w)\n"
            "        for v in ((seed*37+x*11+y*7) % 256,\n"
            "        (seed*53+x*5) % 256, 128)) for y in range(h))\n"
            "    def chunk(typ, data):\n"
            "        c = struct.pack('>I', len(data)) + typ + data\n"
            "        return c + struct.pack('>I', zlib.crc32(typ + data))\n"
            "    png = (bytes([137, 80, 78, 71, 13, 10, 26, 10])\n"
            "        + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0))\n"
            "        + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))\n"
            "    open(path, 'wb').write(png)\n"
            "def child_work(ctx):\n"
            "    lo, hi = ctx.task['payload']['lo'], ctx.task['payload']['hi']\n"
            "    start = (ctx.recall('last_index') or lo - 1) + 1\n"
            "    done = 0\n"
            "    for i in range(start, hi + 1):\n"
            "        if not ctx.alive(): return\n"
            "        write_png(os.path.join(OUT, f'face_{i:02d}.png'), i)\n"
            "        ctx.step(f'face {i:02d}')\n"
            "        ctx.remember('last_index', i)\n"
            "        done += 1\n"
            "        if CRASH_AFTER and done >= CRASH_AFTER:\n"
            "            print('  [CRASH] sudden death mid-child — no cleanup',\n"
            "                  flush=True)\n"
            "            os._exit(1)\n"
            "if __name__ == '__main__':\n"
            "    run_one(sys.argv[1], 'crasher', child_work,\n"
            "            lease_secs=3, task_id=sys.argv[2])\n")


# ---------------------------------------------------------------- main flow
print("== phase 1: worker-A claims the big task and decomposes it ==")
client = Client(DB)
with client.lease(title="generate 50 card faces",
                  payload={"total": 50},
                  owner="worker-A",
                  acceptance=[{
                      "description": "50 face PNGs in out/",
                      "check": {"type": "command",
                                "cmd": count_cmd(0, 49)}}],
                  token_budget=100_000) as lease:
    kids = lease.decompose([
        {"title": f"faces {lo:02d}-{hi:02d}",
         "payload": {"lo": lo, "hi": hi},
         "acceptance": [{
             "description": f"faces {lo:02d}-{hi:02d} on disk",
             "check": {"type": "command", "cmd": count_cmd(lo, hi)}}]}
        for lo, hi in [(0, 9), (10, 19), (20, 29), (30, 39), (40, 49)]])
    parent_id = lease.task_id
print(f"  parent {parent_id} -> 5 children: {kids}")

s = Store(DB)
p = s.get_task(parent_id)
assert p["status"] == "decomposed", p["status"]
assert all(s.get_task(k)["status"] == "pending" for k in kids)
assert all(s.get_task(k)["parent_id"] == parent_id for k in kids)
print("  parent is DECOMPOSED (not claimable, lease released)")

print("== phase 2: child-0's worker DIES mid-batch ==")
write_worker_script()
env = dict(os.environ, DEMO_OUT=OUT)
proc = subprocess.run(
    [sys.executable, WORKER_SCRIPT, DB, kids[0], "4"],
    capture_output=True, text=True, cwd=ROOT, env=env)
print(proc.stdout, end="")
if proc.returncode != 1:
    print(proc.stderr[-2000:] if proc.stderr else "", end="")
made = sorted(f for f in os.listdir(OUT) if f.endswith(".png"))
print(f"  faces on disk after the crash: {len(made)} (child-0 did 4, then died)")
assert len(made) == 4, made
time.sleep(4)  # let the 3s lease expire so recovery picks it up

print("== phase 3: fresh workers finish every child ==")
n = 0
while True:
    rid = run_one(DB, f"pool-{n % 3}", child_work, lease_secs=10)
    n += 1
    if rid is None:
        break
    assert n < 30, "workers spun too long"
made = sorted(f for f in os.listdir(OUT) if f.endswith(".png"))
assert len(made) == 50, len(made)
p = s.get_task(parent_id)
assert p["status"] == "done", p["status"]
assert all(c["status"] == "passed"
           for c in p["acceptance_criteria"]), p["acceptance_criteria"]
print(f"  all 5 children done -> parent auto-resolved: {p['status']}")
print("  parent acceptance verified against the real 50 files")

print("== phase 4: refusal paths ==")
t = s.create_task("refusal probe")
s.claim_task("intruder", 30, task_id=t)
assert s.decompose(t, "someone-else", [{"title": "x"}]) is None
assert s.decompose(t, "intruder", []) is None
assert s.decompose(t, "intruder", [{"nope": 1}]) is None
assert s.decompose(t, "intruder", [{"title": "ok"}]) is not None
assert s.decompose(t, "intruder", [{"title": "again"}]) is None  # decomposed now
print("  non-owner / empty / bad-spec / re-decompose all refused")
d = s.create_task("deep")
s.claim_task("w", 30, task_id=d)
for i in range(5):
    (d,) = s.decompose(d, "w", [{"title": f"level {i}"}])
    s.claim_task("w", 30, task_id=d)
assert s.decompose(d, "w", [{"title": "too deep"}]) is None
print("  nesting depth capped at 5")

print("== phase 5: one failed child -> passive failed parent ==")
p3 = s.create_task("fragile tree")
s.claim_task("w", 30, task_id=p3)
c1, c2 = s.decompose(p3, "w", [{"title": "good child"},
                               {"title": "doomed child"}])
s.claim_task("w", 30, task_id=c1)
s.complete_task(c1, "w", "fine")
s.claim_task("w", 30, task_id=c2)
s.fail_task(c2, "w", "broken beyond repair")
fp = s.get_task(p3)
assert fp["status"] == "failed", fp["status"]
assert s.get_task(c1)["status"] == "done"  # good sibling untouched
notes = [a for a in s.alerts(unacked_only=False)
         if a["task_id"] == p3 and a["level"] == 3]
assert notes and c2 in notes[0]["message"], notes
print(f"  parent failed passively; alert names the failed child ({c2})")
print("  (no push — visible on the dashboard when Robert checks in)")

print("== phase 6: subtree spend caps ==")
p4 = s.create_task("budgeted tree", token_budget=100)
s.claim_task("w", 30, task_id=p4)
b1, b2 = s.decompose(p4, "w", [{"title": "spender-1"},
                               {"title": "spender-2"}])
s.claim_task("w", 30, task_id=b1)
r = s.report_usage(b1, "w", prompt_tokens=60)
assert not r["on_hold"], r
r = s.report_usage(b1, "w", prompt_tokens=50)
assert r["on_hold"] and "subtree cap" in r["reason"], r
assert s.get_task(b1)["status"] == "budget_hold"
print(f"  child parked: {r['reason']}")
s.raise_budget(p4, token_budget=10_000)
assert s.get_task(b1)["status"] == "pending"
print("  raising the parent's cap resumed the parked child")

print("== phase 7: verify inheritance + SDK/MCP paths ==")
p5 = s.create_task("reviewed tree", verify="independent")
s.claim_task("w", 30, task_id=p5)
v1, v2 = s.decompose(p5, "w", [{"title": "inherits"},
                               {"title": "opts out", "verify": "none"}])
assert s.get_task(v1)["verify_mode"] == "independent"
assert s.get_task(v2)["verify_mode"] == "none"
print("  children inherit verify_mode unless overridden")
r = mcp_server._call_tool(
    DB, "stayonduty_decompose",
    {"task_id": v1, "owner": "w",
     "subtasks": [{"title": "grandchild"}]})
# v1 is pending (not in_progress), so MCP decompose is refused — good
assert r.get("isError"), r
s.claim_task("w", 30, task_id=v1)
r = mcp_server._call_tool(
    DB, "stayonduty_decompose",
    {"task_id": v1, "owner": "w",
     "subtasks": [{"title": "grandchild"}]})
kids7 = json.loads(r["content"][0]["text"])["child_ids"]
assert len(kids7) == 1
r = mcp_server._call_tool(DB, "stayonduty_children", {"task_id": v1})
assert len(json.loads(r["content"][0]["text"])["children"]) == 1
print("  MCP stayonduty_decompose / stayonduty_children work")
assert len(client.children(parent_id)) == 5
assert client.decompose(parent_id, "nobody", [{"title": "x"}]) is None
print("  SDK Client.decompose / Client.children work")

os.remove(WORKER_SCRIPT)
s.close()

print("== phase 8: a run_one work_fn can decompose too ==")
s = Store(DB)
ma = os.path.join(OUT, "marker_a")
mb = os.path.join(OUT, "marker_b")
p8 = s.create_task(
    "worker-split job",
    acceptance=[{"description": "both markers on disk",
                 "check": {"type": "command",
                           "cmd": f"python3 -c \"import os,sys; sys.exit(0 if os.path.exists('{ma}') and os.path.exists('{mb}') else 1)\""}}])


def split_work(ctx):
    ctx.decompose([
        {"title": "half A", "payload": {"tag": "a"},
         "acceptance": [{"description": "marker a",
                         "check": {"type": "file_exists", "path": ma}}]},
        {"title": "half B", "payload": {"tag": "b"},
         "acceptance": [{"description": "marker b",
                         "check": {"type": "file_exists", "path": mb}}]},
    ])


rid = run_one(DB, "splitter", split_work, lease_secs=10, task_id=p8)
assert rid == p8
assert s.get_task(p8)["status"] == "decomposed"


def marker_work(ctx):
    tag = ctx.task["payload"]["tag"]
    with open(os.path.join(OUT, f"marker_{tag}"), "w") as f:
        f.write("x")
    ctx.step(f"marker {tag}")


for k in s.children(p8):
    run_one(DB, "pool", marker_work, lease_secs=10, task_id=k["id"])
assert s.get_task(p8)["status"] == "done"
print("  ctx.decompose inside run_one: parent split, children finished,")
print("  parent auto-resolved — run_one did not try to complete it")
s.close()
print("\nPASS: decomposition — split, crash-resume, refusals, failure")
print("      propagation, subtree caps, and agent surfaces all green.")
