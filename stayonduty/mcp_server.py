"""StayOnDuty MCP server — supervision as discoverable tools.

Any MCP-capable agent (Claude Code, etc.) can find and use StayOnDuty on
its own, with no human wiring anything up. Add to the MCP client config:

    {
      "mcpServers": {
        "stayonduty": {
          "command": "python3",
          "args": ["-m", "stayonduty.mcp_server", "--db", "/path/to/work.db"],
          "cwd": "/path/to/stayonduty"
        }
      }
    }

The agent's standing instruction becomes one sentence: "For any task that
runs longer than a few minutes, register it with StayOnDuty — say what
'done' looks like as acceptance criteria — heartbeat while you work, and
checkpoint progress with step() and remember(). If you die, the next worker
resumes from your checkpoints. When you call complete, StayOnDuty verifies
your criteria against the real artifact instead of trusting your claim."

Protocol: JSON-RPC 2.0 over stdio, newline-delimited (one message per
line). Zero dependencies — stdout is the wire, so all logging goes to
stderr. Implements initialize, notifications/initialized, ping,
tools/list, tools/call.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
import uuid

from .store import Store

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "stayonduty", "version": "0.10.0"}


def _tool(name, description, properties, required):
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


_STR = {"type": "string"}
_NUM = {"type": "number"}
_OBJ = {"type": "object"}

TOOLS = [
    _tool(
        "stayonduty_register_task",
        "Register a long-running task with StayOnDuty before starting work. "
        "Returns a task_id. If the agent dies, another worker resumes from "
        "checkpoints instead of restarting. token_budget/usd_budget set "
        "spend guardrails — when reported usage exceeds a cap, the task "
        "parks itself until the budget is raised. acceptance is what 'done' "
        "looks like: plain strings ('50 pngs in out/') or objects with "
        "check specs (file_exists, file_contains, command, image_valid). "
        "complete() verifies them against the real artifact — a bare "
        "claim is not proof. verify='independent' routes a passing "
        "completion into independent review: the task parks in in_review "
        "until a DIFFERENT worker judges it, so the producer never "
        "grades its own work.",
        {"title": _STR,
         "payload": {**_OBJ, "description": "Opaque JSON the task carries"},
         "token_budget": {**_NUM, "description": "Max tokens, null = no cap"},
         "usd_budget": {**_NUM, "description": "Max USD, null = no cap"},
         "verify": {**_STR, "description": "'none' or 'independent'"},
         "acceptance": {
             "type": "array",
             "description": "Acceptance criteria: strings or "
                            "{description, check} objects",
             "items": {}}},
        ["title"]),
    _tool(
        "stayonduty_claim_task",
        "Claim a task (or the oldest pending one) with a lease. Heartbeat "
        "to keep the lease; if heartbeats stop, the task is recovered and "
        "another worker resumes it.",
        {"task_id": {**_STR, "description": "Omit to claim oldest pending"},
         "owner": _STR,
         "lease_secs": {**_NUM, "description": "Lease length, default 30"}},
        []),
    _tool(
        "stayonduty_heartbeat",
        "Renew the lease on a claimed task. Returns alive=false if another "
        "worker took over — the agent must stop working immediately.",
        {"task_id": _STR, "owner": _STR,
         "lease_secs": _NUM},
        ["task_id", "owner"]),
    _tool(
        "stayonduty_step",
        "Record one unit of REAL progress (clears any stuck flag). Call "
        "this per completed unit of work, not per heartbeat.",
        {"task_id": _STR, "detail": _STR},
        ["task_id", "detail"]),
    _tool(
        "stayonduty_note",
        "Append a timeline note that is NOT progress (won't clear stuck). "
        "Use for replans, observations, decisions.",
        {"task_id": _STR, "detail": _STR},
        ["task_id", "detail"]),
    _tool(
        "stayonduty_remember",
        "Write durable memory (scoped to the task if task_id given). "
        "Survives crashes — this is how a resumed worker skips done work.",
        {"key": _STR, "value": {"description": "Any JSON value"},
         "task_id": _STR},
        ["key", "value"]),
    _tool(
        "stayonduty_recall",
        "Read back durable memory written by stayonduty_remember. A fresh "
        "worker calls this first to find where the last one got to.",
        {"key": _STR, "task_id": _STR},
        ["key"]),
    _tool(
        "stayonduty_park_waiting",
        "Deliberately pause until a unix timestamp (e.g. a provider quota "
        "reset). NOT a failure, NOT stuck — the watchdog resumes the task "
        "automatically and nobody is paged.",
        {"task_id": _STR, "owner": _STR, "resume_at": _NUM,
         "reason": _STR, "provider": _STR},
        ["task_id", "owner", "resume_at"]),
    _tool(
        "stayonduty_complete",
        "Ask to complete the task. Only the lease owner can complete it. "
        "If the task has acceptance criteria, they are verified against "
        "the real artifact first — a failed check REFUSES completion and "
        "the task stays in progress with your lease, so fix what's "
        "missing and complete again. Returns the verification verdict.",
        {"task_id": _STR, "owner": _STR, "result": _STR},
        ["task_id", "owner"]),
    _tool(
        "stayonduty_fail",
        "Mark the task failed with a reason. Prefer this over silently "
        "abandoning — the timeline keeps the diagnostics.",
        {"task_id": _STR, "owner": _STR, "reason": _STR},
        ["task_id", "owner"]),
    _tool(
        "stayonduty_task_status",
        "Inspect a task: status, lease, attempts, spend vs budget, and "
        "recent timeline. For checking in, not for supervision.",
        {"task_id": _STR},
        ["task_id"]),
    _tool(
        "stayonduty_report_usage",
        "Report model usage (tokens and/or USD) against the task's spend "
        "caps. Returns on_hold=true when a cap is exceeded — the task is "
        "parked automatically (no attempt consumed) and the agent must "
        "stop working immediately.",
        {"task_id": _STR, "owner": _STR,
         "prompt_tokens": {**_NUM, "description": "Input tokens this call"},
         "completion_tokens": {**_NUM, "description": "Output tokens"},
         "usd": {**_NUM, "description": "Cost in USD, if known"}},
        ["task_id", "owner"]),
    _tool(
        "stayonduty_raise_budget",
        "Raise (or remove, with null) the spend caps on a budget-held task. "
        "The task returns to the queue and its passive notice clears.",
        {"task_id": _STR,
         "token_budget": {**_NUM, "description": "New token cap, null = none"},
         "usd_budget": {**_NUM, "description": "New USD cap, null = none"}},
        ["task_id"]),
    _tool(
        "stayonduty_acceptance_check",
        "Report evidence for a manual acceptance criterion (a plain-string "
        "criterion, or anything only you can judge). Only the lease owner "
        "of an in-progress task may report. Unevidenced manual criteria "
        "FAIL verification — a claim is not proof.",
        {"task_id": _STR, "owner": _STR,
         "criterion_id": {**_STR, "description": "e.g. 'ac1'"},
         "passed": {"type": "boolean"},
         "evidence": {**_STR, "description": "What you checked, concretely"}},
        ["task_id", "owner", "criterion_id", "passed"]),
    _tool(
        "stayonduty_claim_review",
        "Claim the independent review of a task awaiting review. You can "
        "only review work you did NOT produce — the producer's own claim "
        "is refused. Heartbeat to keep the review lease; judge the "
        "artifacts and the recorded evidence with fresh eyes, then "
        "stayonduty_submit_verdict.",
        {"task_id": {**_STR, "description": "Omit to take the oldest review"},
         "verifier": _STR,
         "lease_secs": {**_NUM, "description": "Review lease, default 60"}},
        ["verifier"]),
    _tool(
        "stayonduty_submit_verdict",
        "Submit your independent verdict on a claimed review. verdict is "
        "'pass' (task completes, recorded as verified by you) or 'fail' "
        "(task goes back to the queue with your reason in its timeline; "
        "bounded at 3 failed rounds, then it fails as a passive record). "
        "Judge only what the evidence demonstrates — a bare producer "
        "assertion is not proof.",
        {"task_id": _STR, "verifier": _STR,
         "verdict": {**_STR, "description": "'pass' or 'fail'"},
         "reason": _STR},
        ["task_id", "verifier", "verdict"]),
    _tool(
        "stayonduty_reviews",
        "Review history for a task: rounds, verifiers, verdicts, reasons.",
        {"task_id": _STR},
        ["task_id"]),
    _tool(
        "stayonduty_decompose",
        "Split an in_progress task you hold into subtasks with their own "
        "acceptance criteria. subtasks is a list of {title, payload, "
        "acceptance, token_budget, usd_budget, verify} — verify defaults "
        "to the parent's mode. The parent becomes decomposed (not "
        "claimable, no lease) and completes on its own when every child "
        "is done and its own acceptance verifies. A parent budget is a "
        "subtree cap: usage on any descendant counts against it. Returns "
        "the child ids.",
        {"task_id": _STR, "owner": _STR,
         "subtasks": {"type": "array",
                      "description": "Subtask specs, each with a title",
                      "items": {}}},
        ["task_id", "owner", "subtasks"]),
    _tool(
        "stayonduty_children",
        "Direct subtasks of a task, oldest first — with their statuses, "
        "for watching a decomposition converge.",
        {"task_id": _STR},
        ["task_id"]),
    _tool(
        "stayonduty_recovery",
        "The 'handled on its own' journal: every time StayOnDuty recovered "
        "a dead worker, replanned a stuck task, queued a fresh attempt, or "
        "gave up — as human sentences ('\"Muse\" stopped \"50 card faces\" "
        "at \"6:42 PM\" — StayOnDuty resumed the task'). Read this to see "
        "what was handled while nobody was watching. Pull-only; never pushes.",
        {"limit": {"type": "integer",
                   "description": "Max entries, newest first (default 20)."}},
        []),
]


def _ok(result):
    return {"content": [{"type": "text", "text": json.dumps(result)}]}


def _err(message):
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _call_tool(db_path, name, args):
    s = Store(db_path)
    try:
        if name == "stayonduty_register_task":
            tid = s.create_task(args["title"], args.get("payload"),
                                token_budget=args.get("token_budget"),
                                usd_budget=args.get("usd_budget"),
                                acceptance=args.get("acceptance"),
                                verify=args.get("verify", "none"))
            return _ok({"task_id": tid})
        if name == "stayonduty_claim_task":
            task = s.claim_task(args.get("owner") or
                                f"mcp-{uuid.uuid4().hex[:6]}",
                                args.get("lease_secs", 30),
                                task_id=args.get("task_id"))
            if not task:
                return _err("no pending task to claim")
            return _ok({"task": task})
        if name == "stayonduty_heartbeat":
            alive = s.heartbeat(args["task_id"], args["owner"],
                                args.get("lease_secs", 30))
            return _ok({"alive": alive})
        if name == "stayonduty_step":
            s.step(args["task_id"], args["detail"])
            return _ok({"ok": True})
        if name == "stayonduty_note":
            s.note(args["task_id"], args["detail"])
            return _ok({"ok": True})
        if name == "stayonduty_remember":
            s.remember(args["key"], args["value"],
                       task_id=args.get("task_id"))
            return _ok({"ok": True})
        if name == "stayonduty_recall":
            val = s.recall(args["key"],
                           scope=f"task:{args['task_id']}"
                           if args.get("task_id") else "global")
            return _ok({"value": val})
        if name == "stayonduty_park_waiting":
            ok = s.park_waiting(args["task_id"], args["owner"],
                                args["resume_at"], args.get("reason", ""),
                                provider=args.get("provider", ""))
            return _ok({"parked": ok})
        if name == "stayonduty_complete":
            verdict = s.complete_task(args["task_id"], args["owner"],
                                      args.get("result", ""))
            return _ok({"verdict": verdict})
        if name == "stayonduty_fail":
            s.fail_task(args["task_id"], args["owner"],
                        args.get("reason", ""))
            return _ok({"ok": True})
        if name == "stayonduty_task_status":
            task = s.get_task(args["task_id"])
            if not task:
                return _err("unknown task_id")
            return _ok({"task": task,
                        "recent_events": s.events(args["task_id"])[-20:]})
        if name == "stayonduty_report_usage":
            return _ok(s.report_usage(
                args["task_id"], args["owner"],
                args.get("prompt_tokens", 0),
                args.get("completion_tokens", 0),
                args.get("usd")))
        if name == "stayonduty_raise_budget":
            ok = s.raise_budget(args["task_id"],
                                args.get("token_budget"),
                                args.get("usd_budget"))
            if not ok:
                return _err("task is not on budget hold")
            return _ok({"ok": True})
        if name == "stayonduty_acceptance_check":
            ok = s.acceptance_check(args["task_id"], args["owner"],
                                    args["criterion_id"],
                                    args["passed"],
                                    args.get("evidence", ""))
            if not ok:
                return _err("unknown criterion, or not your in_progress task")
            return _ok({"ok": True})
        if name == "stayonduty_claim_review":
            task = s.claim_review(args["verifier"],
                                  args.get("lease_secs", 60),
                                  task_id=args.get("task_id"))
            if not task:
                return _err("no review available — unknown task, not awaiting"
                            " review, or you produced this work")
            return _ok({"task": task,
                        "reviews": s.reviews(task["id"])})
        if name == "stayonduty_submit_verdict":
            ok = s.submit_verdict(args["task_id"], args["verifier"],
                                  args["verdict"], args.get("reason", ""))
            if not ok:
                return _err("verdict not accepted — unknown task, not your"
                            " review lease, or invalid verdict")
            return _ok({"ok": True})
        if name == "stayonduty_reviews":
            return _ok({"reviews": s.reviews(args["task_id"])})
        if name == "stayonduty_decompose":
            kids = s.decompose(args["task_id"], args["owner"],
                               args["subtasks"])
            if kids is None:
                return _err("decompose refused — not your in_progress task,"
                            " bad subtask specs, or nesting too deep")
            return _ok({"child_ids": kids})
        if name == "stayonduty_children":
            return _ok({"children": s.children(args["task_id"])})
        if name == "stayonduty_recovery":
            return _ok({"sentences": s.recovery_sentences(
                limit=args.get("limit", 20) or 20)})
        return _err(f"unknown tool: {name}")
    except KeyError as e:
        return _err(f"missing required argument: {e}")
    finally:
        s.close()


def _handle(db_path, msg):
    """Return a response dict, or None for notifications."""
    method = msg.get("method", "")
    mid = msg.get("id")
    params = msg.get("params") or {}

    def resp(result=None, error=None):
        r = {"jsonrpc": "2.0", "id": mid}
        if error is not None:
            r["error"] = error
        else:
            r["result"] = result
        return r

    if method == "initialize":
        return resp({
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        })
    if method == "ping":
        return resp({})
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return resp({"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments") or {}
        log(f"tools/call {name}")
        try:
            return resp(_call_tool(db_path, name, args))
        except Exception:  # noqa: BLE001
            log(traceback.format_exc())
            return resp(_err("internal error — see server stderr"))
    if mid is None:
        return None  # unknown notification: ignore
    return resp(error={"code": -32601, "message": f"unknown method: {method}"})


def log(text):
    print(f"[mcp] {text}", file=sys.stderr, flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description="StayOnDuty MCP server (stdio)")
    ap.add_argument("--db", default=os.environ.get("STAYONDUTY_DB",
                                                   "stayonduty.db"))
    args = ap.parse_args(argv)
    log(f"serving {SERVER_INFO['name']} v{SERVER_INFO['version']}"
        f" on db {args.db}")
    Store(args.db).close()  # fail fast on a bad path
    stdin = sys.stdin
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            out = _handle(args.db, msg)
        except Exception:  # noqa: BLE001
            log(traceback.format_exc())
            out = ({"jsonrpc": "2.0", "id": msg.get("id"),
                    "error": {"code": -32603, "message": "internal error"}}
                   if msg.get("id") is not None else None)
        if out is not None:
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
