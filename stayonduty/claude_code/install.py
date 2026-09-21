#!/usr/bin/env python3
"""Install StayOnDuty into Claude Code. One command, no interaction:

    python3 -m stayonduty.claude_code.install --project /path/to/project

Project scope (default): writes <project>/.mcp.json (MCP server, committed
with the repo) and <project>/.claude/settings.json (hooks).
User scope (--user): writes ~/.claude.json top-level mcpServers and
~/.claude/settings.json — every project on this machine.

What gets installed:
  * MCP server "stayonduty": 14 tools over stdio, zero dependencies.
  * SessionStart hook: injects the one-sentence standing instruction into
    every session so the agent actually reaches for the tools.
  * Stop hook: blocks the agent from stopping while StayOnDuty tasks are
    in_progress with live leases (bounded: 3 blocks per session, then the
    watchdog owns recovery — it never traps the user).

Idempotent: re-running changes nothing when already installed.
--uninstall removes exactly what install added. Existing settings files are
backed up to .bak before modification. The `claude` CLI is not required.
"""
import argparse
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_PARENT = os.path.dirname(os.path.dirname(HERE))  # has stayonduty/
HOOKS_DIR = os.path.join(HERE, "hooks")
SESSION_START = os.path.join(HOOKS_DIR, "session_start.py")
STOP_HOOK = os.path.join(HOOKS_DIR, "stop.py")
HOOK_MARKER = "stayonduty/claude_code/hooks/"


def _load(path):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def _save(path, obj, backup=True):
    if backup and os.path.exists(path):
        shutil.copy2(path, path + ".bak")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)
        f.write("\n")


def _mcp_entry(db_path):
    return {
        "command": sys.executable,
        "args": ["-m", "stayonduty.mcp_server", "--db", db_path],
        "cwd": PKG_PARENT,
    }


def _hook_entries(db_path):
    py = sys.executable
    return {
        "SessionStart": [{
            "matcher": "*",
            "hooks": [{
                "type": "command",
                "command": f"{py} {SESSION_START}",
                "timeout": 10000,
            }],
        }],
        "Stop": [{
            "matcher": "*",
            "hooks": [{
                "type": "command",
                "command": f"{py} {STOP_HOOK} --db {db_path}",
                "timeout": 15000,
            }],
        }],
    }


def _merge_mcp(path, db_path, uninstall):
    """Merge the stayonduty server into an mcpServers file. Returns changed."""
    obj = _load(path)
    servers = obj.setdefault("mcpServers", {})
    if uninstall:
        if "stayonduty" not in servers:
            return False
        del servers["stayonduty"]
        if not servers:
            del obj["mcpServers"]
    else:
        entry = _mcp_entry(db_path)
        if servers.get("stayonduty") == entry:
            return False
        servers["stayonduty"] = entry
    if not obj and path.endswith(".mcp.json") and os.path.exists(path):
        os.remove(path)  # nothing left: leave no trace
        return True
    _save(path, obj)
    return True


def _is_ours(entry):
    return HOOK_MARKER in json.dumps(entry)


def _merge_hooks(path, db_path, uninstall, no_hooks):
    """Merge our hook entries into a settings.json. Returns changed."""
    obj = _load(path)
    hooks = obj.setdefault("hooks", {})
    changed = False
    if uninstall or no_hooks:
        for event in list(hooks):
            kept = [e for e in hooks[event] if not _is_ours(e)]
            if len(kept) != len(hooks[event]):
                changed = True
            if kept:
                hooks[event] = kept
            else:
                del hooks[event]
        if not hooks and "hooks" in obj:
            del obj["hooks"]
    else:
        for event, entries in _hook_entries(db_path).items():
            existing = hooks.setdefault(event, [])
            for entry in entries:
                if entry not in existing:
                    existing.append(entry)
                    changed = True
    if not changed:
        return False
    if not obj and os.path.exists(path):
        os.remove(path)
        return True
    _save(path, obj)
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Install StayOnDuty into Claude Code.")
    ap.add_argument("--project", default=os.getcwd(),
                    help="Project dir for project scope (default: cwd).")
    ap.add_argument("--user", action="store_true",
                    help="User scope: all projects on this machine.")
    ap.add_argument("--db",
                    help="StayOnDuty DB path (default: "
                         "<project>/.stayonduty/work.db or "
                         "~/.stayonduty/work.db).")
    ap.add_argument("--no-hooks", action="store_true",
                    help="Install the MCP server only, no hooks.")
    ap.add_argument("--uninstall", action="store_true",
                    help="Remove what install added.")
    args = ap.parse_args(argv)

    home = os.path.expanduser("~")
    if args.user:
        mcp_path = os.path.join(home, ".claude.json")
        settings_path = os.path.join(home, ".claude", "settings.json")
        db_path = args.db or os.path.join(home, ".stayonduty", "work.db")
        scope = "user"
    else:
        project = os.path.abspath(args.project)
        mcp_path = os.path.join(project, ".mcp.json")
        settings_path = os.path.join(project, ".claude", "settings.json")
        db_path = args.db or os.path.join(project, ".stayonduty", "work.db")
        scope = f"project ({project})"
    db_path = os.path.abspath(db_path)
    if not args.uninstall:
        os.makedirs(os.path.dirname(db_path), exist_ok=True)

    verb = "Uninstalled from" if args.uninstall else "Installed into"
    changed_mcp = _merge_mcp(mcp_path, db_path, args.uninstall)
    changed_hooks = _merge_hooks(settings_path, db_path,
                                 args.uninstall, args.no_hooks)
    if changed_mcp:
        print(f"  {'-' if args.uninstall else '+'} MCP server 'stayonduty'"
              f" in {mcp_path}")
    if changed_hooks:
        print(f"  {'-' if args.uninstall else '+'} hooks"
              f" in {settings_path}")
    if not changed_mcp and not changed_hooks:
        print("  (already installed — nothing changed)")
    print(f"{verb} Claude Code [{scope}], db: {db_path}")
    if not args.uninstall and not args.no_hooks:
        print("  Verify: `claude mcp list` should show stayonduty; a new"
              " session gets the StayOnDuty instruction automatically.")


if __name__ == "__main__":
    main()
