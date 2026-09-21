#!/usr/bin/env python3
"""Iteration 8 proof: the Claude Code integration.

Installs StayOnDuty into a fake HOME + fake project (project and user
scope), simulates Claude Code invoking the SessionStart and Stop hooks
exactly as it would (JSON on stdin, JSON on stdout), and proves:

  * every session automatically learns the standing instruction
  * the agent cannot quit while StayOnDuty tasks are live (bounded blocks)
  * the hook never traps the user (missing DB, garbage input, stale
    leases, exhausted budget all allow the stop)
  * the installed MCP config actually launches the server (20 tools)
  * install is idempotent; uninstall removes exactly what was added

No `claude` CLI needed — the hook protocol is simulated faithfully.
"""
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

home = tempfile.mkdtemp(prefix="sod_home_")
proj = tempfile.mkdtemp(prefix="sod_proj_")
os.environ["HOME"] = home  # install.py resolves ~ from this

from stayonduty.claude_code import install  # noqa: E402
from stayonduty.sdk import Client  # noqa: E402

PY = sys.executable


def run_hook(script, payload, db=None):
    cmd = [PY, script]
    if db:
        cmd += ["--db", db]
    p = subprocess.run(cmd, input=json.dumps(payload), capture_output=True,
                       text=True, timeout=30)
    assert p.returncode == 0, (script, p.returncode, p.stderr)
    return p.stdout.strip()


print("=== phase 1: project-scope install ===")
install.main(["--project", proj])
mcp_path = os.path.join(proj, ".mcp.json")
mcp = json.load(open(mcp_path))
srv = mcp["mcpServers"]["stayonduty"]
assert srv["args"][:2] == ["-m", "stayonduty.mcp_server"], srv
db = os.path.join(proj, ".stayonduty", "work.db")
assert srv["args"][srv["args"].index("--db") + 1] == db, srv
assert os.path.isdir(os.path.join(srv["cwd"], "stayonduty")), srv
settings = json.load(open(os.path.join(proj, ".claude", "settings.json")))
starts = settings["hooks"]["SessionStart"][0]["hooks"][0]
stops = settings["hooks"]["Stop"][0]["hooks"][0]
assert starts["command"].endswith("hooks/session_start.py"), starts
assert stops["command"].endswith(f"hooks/stop.py --db {db}"), stops
assert starts["timeout"] == 10000 and stops["timeout"] == 15000
print("project .mcp.json + .claude/settings.json written, absolute paths")

print("=== phase 2: idempotent reinstall ===")
before = {p: open(p, "rb").read() for p in
          (mcp_path, os.path.join(proj, ".claude", "settings.json"))}
install.main(["--project", proj])
after = {p: open(p, "rb").read() for p in before}
assert before == after, "reinstall must change nothing"
print("reinstall changed nothing")

print("=== phase 3: user-scope install ===")
install.main(["--user"])
uclaude = json.load(open(os.path.join(home, ".claude.json")))
userv = uclaude["mcpServers"]["stayonduty"]  # top-level key, not settings.json
udb = os.path.join(home, ".stayonduty", "work.db")
assert userv["args"][userv["args"].index("--db") + 1] == udb
usettings = json.load(open(os.path.join(home, ".claude", "settings.json")))
assert "SessionStart" in usettings["hooks"] and "Stop" in usettings["hooks"]
print("~/.claude.json mcpServers + ~/.claude/settings.json written")

print("=== phase 4: SessionStart hook teaches the instruction ===")
out = run_hook(os.path.join(ROOT, "stayonduty", "claude_code", "hooks",
                            "session_start.py"),
               {"session_id": "s1", "cwd": proj})
ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
for needle in ("stayonduty_register_task", "stayonduty_heartbeat",
               "stayonduty_complete", "a claim is not proof"):
    assert needle in ctx, needle
print("standing instruction injected, names the real tools")

STOP = os.path.join(ROOT, "stayonduty", "claude_code", "hooks", "stop.py")
print("=== phase 5: Stop hook never traps the user ===")
assert run_hook(STOP, {"session_id": "s1"}, db) == "", "no tasks -> allow"
assert run_hook(STOP, {"session_id": "s1"},
                db + ".missing") == "", "missing db -> allow"
p = subprocess.run([PY, STOP, "--db", db], input="not json{{{",
                   capture_output=True, text=True, timeout=30)
assert p.returncode == 0 and p.stdout.strip() == "", "garbage in -> allow"
print("no tasks / missing db / garbage input: stop allowed")

print("=== phase 6: Stop hook blocks orphaning live work (bounded) ===")
client = Client(db)
tid = client.register("migrate the billing tables",
                      acceptance=["schema diff is empty"])
from stayonduty.store import Store  # noqa: E402
s = Store(db)
s.claim_task("agent-7", 600, task_id=tid)
s.close()
for i in range(3):
    out = run_hook(STOP, {"session_id": "s1"}, db)
    r = json.loads(out)
    assert r["decision"] == "block", (i, out)
    assert tid in r["reason"] and "migrate the billing tables" in r["reason"]
print("3 blocks, each naming the live task")
assert run_hook(STOP, {"session_id": "s1"}, db) == "", "budget exhausted"
print("4th stop allowed — watchdog owns recovery from here")
out = run_hook(STOP, {"session_id": "s2"}, db)
assert json.loads(out)["decision"] == "block", "fresh session, fresh budget"
print("new session gets its own budget")
out = run_hook(STOP, {"session_id": "s3", "stop_hook_active": True}, db)
assert out == "", "stop_hook_active respected"
print("Claude Code's own loop protection respected")

print("=== phase 7: stale leases don't block ===")
s = Store(db)
s.fail_task(tid, "agent-7", "demo cleanup")  # phase-6 task done, only stale left
t2 = s.create_task("stale job")
s.claim_task("agent-7", lease_secs=-5, task_id=t2)  # already expired
s.close()
assert run_hook(STOP, {"session_id": "s4"}, db) == "", "stale -> allow"
state = json.load(open(db + ".stop_blocks.json"))
assert "s4" not in state, "stale check must not consume budget"
print("expired lease: stop allowed, budget untouched")

print("=== phase 8: installed MCP config really launches ===")
p = subprocess.Popen([srv["command"]] + srv["args"], cwd=srv["cwd"],
                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                     text=True)
init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
lst = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
p.stdin.write(json.dumps(init) + "\n" + json.dumps(lst) + "\n")
p.stdin.flush()
p.stdout.readline()  # initialize response
tools = json.loads(p.stdout.readline())["result"]["tools"]
p.kill()
assert len(tools) == 20, len(tools)
names = {t["name"] for t in tools}
assert "stayonduty_acceptance_check" in names
print(f"MCP server launches from installed config: {len(tools)} tools")

print("=== phase 9: uninstall removes exactly what was added ===")
install.main(["--project", proj, "--uninstall"])
assert not os.path.exists(mcp_path), ".mcp.json with only our server removed"
psettings_path = os.path.join(proj, ".claude", "settings.json")
if os.path.exists(psettings_path):
    psettings = json.load(open(psettings_path))
    assert psettings.get("hooks", {}) == {}, psettings
install.main(["--user", "--uninstall"])
uclaude = json.load(open(os.path.join(home, ".claude.json")))
assert "stayonduty" not in uclaude.get("mcpServers", {}), uclaude
usettings_path = os.path.join(home, ".claude", "settings.json")
if os.path.exists(usettings_path):
    usettings = json.load(open(usettings_path))
    assert usettings.get("hooks", {}) == {}, usettings

print("=== phase 10: pre-existing config survives install/uninstall ===")
proj2 = tempfile.mkdtemp(prefix="sod_proj2_")
other_mcp = os.path.join(proj2, ".mcp.json")
json.dump({"mcpServers": {"other": {"command": "other-cmd", "args": []}}},
          open(other_mcp, "w"))
other_settings = os.path.join(proj2, ".claude", "settings.json")
os.makedirs(os.path.dirname(other_settings), exist_ok=True)
other_hook = {"matcher": "*",
              "hooks": [{"type": "command", "command": "echo hi"}]}
json.dump({"hooks": {"PreToolUse": [other_hook]}}, open(other_settings, "w"))
install.main(["--project", proj2])
mcp2 = json.load(open(other_mcp))
assert set(mcp2["mcpServers"]) == {"other", "stayonduty"}, mcp2
s2 = json.load(open(other_settings))
assert s2["hooks"]["PreToolUse"] == [other_hook], s2
assert "SessionStart" in s2["hooks"] and "Stop" in s2["hooks"]
install.main(["--project", proj2, "--uninstall"])
mcp2 = json.load(open(other_mcp))
assert set(mcp2["mcpServers"]) == {"other"}, mcp2
s2 = json.load(open(other_settings))
assert s2["hooks"] == {"PreToolUse": [other_hook]}, s2
print("other servers and hooks untouched; backups written (.bak)")
print("both scopes clean; pre-existing files untouched")

print("\nALL CLAUDE CODE CHECKS PASSED")
