#!/usr/bin/env python3
"""Claude Code Stop hook: don't let the agent quit on live StayOnDuty work.

Fires when Claude Code wants to stop. If StayOnDuty tasks are in_progress
with live leases, the stop is blocked (bounded: 3 blocks per session) and
the agent is told to finish or park them properly. The block budget and the
never-trap rule keep this from fighting the user: a missing DB, no live
tasks, an already-active stop hook, or an exhausted budget all ALLOW the
stop — lease expiry and the watchdog own recovery from there.

Usage (written by the installer; absolute paths required):
    python3 /abs/stayonduty/claude_code/hooks/stop.py --db /abs/work.db
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

BLOCK_BUDGET = 3  # blocks per session before we stand down


def _allow():
    # No output, exit 0: Claude Code proceeds with the stop.
    sys.exit(0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    args = ap.parse_args()

    try:
        data = json.load(sys.stdin)
    except Exception:
        data = {}
    if data.get("stop_hook_active"):
        _allow()  # Claude Code's own loop protection: don't pile on.
    session_id = data.get("session_id") or "unknown"

    try:
        from stayonduty.store import Store
        store = Store(args.db)
        try:
            live = store.live_leases()
        finally:
            store.close()
    except Exception:
        _allow()  # infra problem: never break the user's session over it.
    if not live:
        _allow()

    state_path = args.db + ".stop_blocks.json"
    try:
        with open(state_path) as f:
            state = json.load(f)
    except Exception:
        state = {}
    remaining = state.get(session_id, BLOCK_BUDGET)
    if remaining <= 0:
        _allow()  # budget exhausted: watchdog owns recovery from here.
    state[session_id] = remaining - 1
    try:
        with open(state_path, "w") as f:
            json.dump(state, f)
    except Exception:
        pass

    items = ", ".join(f"{t['id']} ({t['title'][:60]})" for t in live[:5])
    more = f" +{len(live) - 5} more" if len(live) > 5 else ""
    reason = (
        f"StayOnDuty: {len(live)} task(s) still in progress with live "
        f"leases: {items}{more}. Do not orphan live work: finish each one "
        "(stayonduty_complete — acceptance criteria will verify), park it "
        "properly (stayonduty_fail or stayonduty_park_waiting with a note "
        "so the watchdog recovers it), or reply 'not mine' if these belong "
        "to another worker and then stop.")
    print(json.dumps({"decision": "block", "reason": reason}))


if __name__ == "__main__":
    main()
