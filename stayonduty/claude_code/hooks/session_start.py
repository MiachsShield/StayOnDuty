#!/usr/bin/env python3
"""Claude Code SessionStart hook: teach every session the standing instruction.

Claude Code runs this at session start / resume / clear / compact, feeds it
JSON on stdin, and injects the returned additionalContext into the session.
Stdlib only. Never fails: worst case it prints nothing and the session
starts without the instruction.
"""
import json
import sys

INSTRUCTION = (
    "StayOnDuty is available as MCP tools (stayonduty_*). For any task that "
    "runs longer than a few minutes: register it with stayonduty_register_task "
    "— say what 'done' looks like as acceptance criteria — then heartbeat "
    "while you work (stayonduty_heartbeat) and checkpoint progress with "
    "stayonduty_step() and stayonduty_remember(). If this session dies, "
    "another worker resumes from your checkpoints; nothing is lost. Report "
    "token spend with stayonduty_report_usage when the task has a budget. "
    "stayonduty_complete() verifies acceptance criteria against the real "
    "artifact — a claim is not proof. For high-stakes tasks register with "
    "verify='independent': completion parks in review until a DIFFERENT "
    "session judges it with stayonduty_claim_review/stayonduty_submit_verdict "
    "(you can never review your own work — claim others' reviews when idle). "
    "For big tasks, split them with stayonduty_decompose into subtasks "
    "with their own acceptance criteria — the parent completes when the "
    "children verify. "
    "If a session dies mid-task, StayOnDuty recovers it on its own — "
    "read stayonduty_recovery to see what was handled while you were gone. "
    "Park provider quota waits with "
    "stayonduty_park_waiting; they resume automatically."
)


def main():
    try:
        json.load(sys.stdin)
    except Exception:
        pass
    try:
        print(json.dumps(
            {"hookSpecificOutput": {"additionalContext": INSTRUCTION}}))
    except Exception:
        pass


if __name__ == "__main__":
    main()
