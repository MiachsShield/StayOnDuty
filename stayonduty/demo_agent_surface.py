"""End-to-end proof of the agent-facing surface.

Simulates a FOREIGN agent — one that never imports stayonduty — talking
to the MCP server over raw JSON-RPC stdio, exactly like Claude Code or
any MCP client would:

  Phase 1: agent A registers a task, claims it, delivers 2 of 5 packages,
           checkpoints memory, then CRASHES (vanishes mid-task).
  Phase 2: the lease expires; recover_stale() returns the task to the queue.
  Phase 3: agent B (a fresh worker) claims it, recalls where A got to,
           resumes at package 3, and completes. Zero human involvement.
  Phase 4: the Python SDK lease() context manager does a tiny supervised
           run on its own.

Pass criteria: task ends `done`, all 5 packages delivered exactly once
(resume, not restart), crash-recovery visible in the timeline.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB = os.path.join(ROOT, "demo_agent.db")

for f in (DB, DB + "-wal", DB + "-shm"):
    if os.path.exists(f):
        os.remove(f)


class Rpc:
    def __init__(self, proc):
        self.proc = proc
        self._id = 0

    def call(self, method, params=None):
        self._id += 1
        msg = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            msg["params"] = params
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("MCP server closed stdout")
        out = json.loads(line)
        if out.get("error"):
            raise RuntimeError(f"RPC error: {out['error']}")
        return out["result"]

    def notify(self, method, params=None):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def tool(self, name, **args):
        res = self.call("tools/call",
                        {"name": name, "arguments": args})
        if res.get("isError"):
            raise RuntimeError(f"tool {name} failed: {res['content']}")
        return json.loads(res["content"][0]["text"])


def main():
    sys.path.insert(0, ROOT)
    proc = subprocess.Popen(
        [sys.executable, "-m", "stayonduty.mcp_server", "--db", DB],
        cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, bufsize=1)
    rpc = Rpc(proc)
    try:
        # --- handshake ---
        init = rpc.call("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "demo-agent", "version": "0.1"}})
        assert init["serverInfo"]["name"] == "stayonduty", init
        rpc.notify("notifications/initialized")
        tools = rpc.call("tools/list")["tools"]
        names = {t["name"] for t in tools}
        assert len(tools) == 20, f"expected 20 tools, got {len(tools)}"
        print(f"[demo] handshake ok — {len(tools)} tools exposed")

        # --- Phase 1: agent A works, then crashes ---
        tid = rpc.tool("stayonduty_register_task",
                       title="deliver 5 packages",
                       payload={"route": "demo"})["task_id"]
        print(f"[demo] agent A registered task {tid}")
        rpc.tool("stayonduty_claim_task", task_id=tid, owner="agent-a",
                 lease_secs=3)
        delivered = []
        for i in range(2):
            delivered.append(i)
            rpc.tool("stayonduty_step", task_id=tid,
                     detail=f"package {i} delivered")
            rpc.tool("stayonduty_remember", task_id=tid,
                     key="last_index", value=i)
            rpc.tool("stayonduty_heartbeat", task_id=tid, owner="agent-a",
                     lease_secs=3)
        print("[demo] agent A delivered packages 0-1, checkpointed,"
              " then CRASHED (vanished)")
        # agent A never speaks again. lease (3s) expires...

        # --- Phase 2: recovery (what the next worker's run_one does) ---
        time.sleep(4)
        from stayonduty.store import Store
        s = Store(DB)
        res = s.recover_stale()
        assert res["released"] == 1, res
        print(f"[demo] lease expired — recover_stale released"
              f" {res['released']} task(s)")
        s.close()

        # --- Phase 3: agent B resumes from memory ---
        claimed = rpc.tool("stayonduty_claim_task", owner="agent-b",
                           lease_secs=30)["task"]
        assert claimed["id"] == tid, "fresh worker must get the same task"
        last = rpc.tool("stayonduty_recall", task_id=tid,
                        key="last_index")["value"]
        assert last == 1, f"expected last_index=1, got {last}"
        print(f"[demo] agent B claimed it, recalled last_index={last},"
              f" resuming at package 2")
        for i in range(last + 1, 5):
            delivered.append(i)
            rpc.tool("stayonduty_step", task_id=tid,
                     detail=f"package {i} delivered")
        rpc.tool("stayonduty_complete", task_id=tid, owner="agent-b",
                 result="all 5 delivered")
        assert delivered == [0, 1, 2, 3, 4], delivered
        status = rpc.tool("stayonduty_task_status", task_id=tid)
        assert status["task"]["status"] == "done", status["task"]
        kinds = [e["type"] for e in status["recent_events"]]
        assert "released" in kinds, "crash recovery must be in the timeline"
        print("[demo] agent B delivered 2-4 — all 5 exactly once, task done")

        # --- Phase 4: SDK lease context manager ---
        from stayonduty import Client
        client = Client(DB)
        with client.lease("sdk smoke task") as job:
            assert job.alive
            job.step("unit 1")
            job.remember("k", "v")
            assert job.recall("k") == "v"
        st = client.status(job.task_id)["task"]["status"]
        assert st == "done", st
        print("[demo] SDK lease() context manager: supervised run done")

        print("\nPASS: foreign agent drove StayOnDuty over MCP — crash, "
              "resume from memory, completion, zero human involvement.")
    finally:
        proc.terminate()
        proc.wait(timeout=5)


if __name__ == "__main__":
    main()
