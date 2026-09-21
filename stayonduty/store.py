"""StayOnDuty v0.2 — durable task + memory store (SQLite).

The whole point: an agent can die mid-task (crash, disconnect, deploy)
and a new worker picks up exactly where it left off, because the task
ledger and the agent's memory live in SQLite, not in the process.
"""
from __future__ import annotations
import json
import os
import re
import sqlite3
import subprocess
import time
import uuid

from .verify import verify_image

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',   -- pending | in_progress | in_review | waiting | budget_hold | done | failed | decomposed
  payload TEXT NOT NULL DEFAULT '{}',
  acceptance_criteria TEXT NOT NULL DEFAULT '[]',  -- json list, see _normalize_criteria
  verify_mode TEXT NOT NULL DEFAULT 'none',  -- none | independent (fresh-eyes review)
  producer_owner TEXT,                       -- who did the work (independent review)
  review_rounds INTEGER NOT NULL DEFAULT 0,  -- completed review rounds
  verified_by TEXT,                          -- verifier owner that passed it
  parent_id TEXT,                            -- decomposition: NULL = root task
  lease_owner TEXT,                          -- which worker holds it
  lease_expires_at REAL,                     -- heartbeat deadline (unix ts)
  attempts INTEGER NOT NULL DEFAULT 0,
  stuck_at REAL,                             -- when stuck was flagged (NULL = not stuck)
  stuck_level INTEGER NOT NULL DEFAULT 0,    -- 0 ok | 1 flagged, worker nudged | 2 escalated to human
  resume_at REAL,                            -- waiting tasks: unix ts when the wait window ends
  token_budget INTEGER,                      -- spend guardrail: NULL = no cap
  tokens_used INTEGER NOT NULL DEFAULT 0,
  usd_budget REAL,                           -- NULL = no cap
  usd_used REAL NOT NULL DEFAULT 0,
  hold_reason TEXT,                          -- why parked on budget_hold
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL,
  type TEXT NOT NULL,   -- created|claimed|released|step|memory|completed|failed
  detail TEXT NOT NULL DEFAULT '',
  at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS memory (
  key TEXT PRIMARY KEY,
  scope TEXT NOT NULL DEFAULT 'global',     -- global | task:<id>
  value TEXT NOT NULL,                      -- json
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id);
CREATE TABLE IF NOT EXISTS reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL,
  round INTEGER NOT NULL,            -- 1-based review round
  verifier_owner TEXT NOT NULL,      -- who reviewed (never the producer)
  verdict TEXT NOT NULL,             -- pass | fail
  reason TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reviews_task ON reviews(task_id);
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL,
  level INTEGER NOT NULL,      -- 2 = escalated to human
  message TEXT NOT NULL,
  created_at REAL NOT NULL,
  acked_at REAL                -- NULL = unacknowledged
);
-- The autonomy journal: every time StayOnDuty handles a problem on its
-- own (worker died, stuck replan, auto-retry, give-up), a row lands here.
-- This is the pull-only confidence surface: the user checks in and sees
-- exactly what was handled while they were away. Nothing here ever pushes.
CREATE TABLE IF NOT EXISTS recovery_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL,
  task_title TEXT NOT NULL DEFAULT '',
  event TEXT NOT NULL,   -- worker_stopped | verifier_stopped | task_resumed |
                         -- stuck_replanned | auto_retried | give_up
  worker TEXT NOT NULL DEFAULT '',  -- the AI/worker that stopped ("" = n/a)
  detail TEXT NOT NULL DEFAULT '',
  at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recovery_log_at ON recovery_log(at);
-- App settings (v1): API keys and preferences live ONLY here, in the
-- local database. They are never written to code, logs, or git. Values
-- are write-only from the UI's point of view — the settings page shows
-- "saved" / "not set", never the value back.
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at REAL NOT NULL
);
"""


def _fmt_time(ts):
    """'6:42 PM' local time — the way the recovery sentence reads it."""
    return time.strftime("%I:%M %p", time.localtime(ts)).lstrip("0")


def format_recovery(entry, resume_entry=None, task_status=None):
    """Render one autonomy-journal row as the human sentence.

    Canonical paired form (worker died, StayOnDuty resumed it):
        "Muse" stopped "50 card faces" at "6:42 PM" — StayOnDuty resumed
        the task at "6:43 PM"
    """
    t = entry.get("task_title") or entry.get("task_id", "")
    w = entry.get("worker") or "its worker"
    at = _fmt_time(entry["at"])
    ev = entry["event"]
    if ev == "worker_stopped":
        if resume_entry is not None:
            return (f'"{w}" stopped "{t}" at "{at}" — StayOnDuty resumed'
                    f' the task at "{_fmt_time(resume_entry["at"])}"')
        if task_status == "failed":
            return (f'"{w}" stopped "{t}" at "{at}" — its retries ran out'
                    f" (see the give-up entry)")
        if task_status == "done":
            return (f'"{w}" stopped "{t}" at "{at}" — StayOnDuty got it'
                    f" finished anyway")
        return (f'"{w}" stopped "{t}" at "{at}" — StayOnDuty will pick'
                f" it back up on its own")
    if ev == "verifier_stopped":
        return (f'"{w}" stopped verifying "{t}" at "{at}" — StayOnDuty'
                f" reopened the review for another verifier")
    if ev == "task_resumed":
        detail = entry.get("detail") or ""
        extra = f" ({detail})" if detail else ""
        return (f'StayOnDuty resumed "{t}" at "{at}" — picked up by'
                f' "{w}"{extra}')
    if ev == "stuck_replanned":
        return (f'"{w}" stalled on "{t}" at "{at}" — StayOnDuty nudged it'
                f" to replan on its own")
    if ev == "auto_retried":
        detail = entry.get("detail") or ""
        extra = f" {detail}" if detail else ""
        return (f'"{w}" stalled on "{t}" — StayOnDuty queued a fresh'
                f" attempt on its own.{extra}")
    if ev == "give_up":
        detail = entry.get("detail") or ""
        extra = f" ({detail})" if detail else ""
        return (f'"{t}" could not finish on its own{extra} — full'
                f" diagnostics in its timeline")
    return f'{ev}: "{t}" at "{at}"'


class Store:
    def __init__(self, path="stayonduty.db"):
        self.path = path
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL;")
        self.db.execute("PRAGMA busy_timeout=5000;")  # writers wait, not explode
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self):
        """Bring pre-iteration-4 databases up to the current schema."""
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(tasks)")}
        if "stuck_at" not in cols:
            self.db.execute("ALTER TABLE tasks ADD COLUMN stuck_at REAL")
        if "stuck_level" not in cols:
            self.db.execute("ALTER TABLE tasks ADD COLUMN stuck_level INTEGER NOT NULL DEFAULT 0")
        if "resume_at" not in cols:
            self.db.execute("ALTER TABLE tasks ADD COLUMN resume_at REAL")
        if "token_budget" not in cols:
            self.db.execute("ALTER TABLE tasks ADD COLUMN token_budget INTEGER")
        if "tokens_used" not in cols:
            self.db.execute("ALTER TABLE tasks ADD COLUMN tokens_used INTEGER NOT NULL DEFAULT 0")
        if "usd_budget" not in cols:
            self.db.execute("ALTER TABLE tasks ADD COLUMN usd_budget REAL")
        if "usd_used" not in cols:
            self.db.execute("ALTER TABLE tasks ADD COLUMN usd_used REAL NOT NULL DEFAULT 0")
        if "hold_reason" not in cols:
            self.db.execute("ALTER TABLE tasks ADD COLUMN hold_reason TEXT")
        if "acceptance_criteria" not in cols:
            self.db.execute(
                "ALTER TABLE tasks ADD COLUMN acceptance_criteria"
                " TEXT NOT NULL DEFAULT '[]'")
        if "verify_mode" not in cols:
            self.db.execute(
                "ALTER TABLE tasks ADD COLUMN verify_mode"
                " TEXT NOT NULL DEFAULT 'none'")
        if "producer_owner" not in cols:
            self.db.execute("ALTER TABLE tasks ADD COLUMN producer_owner TEXT")
        if "review_rounds" not in cols:
            self.db.execute(
                "ALTER TABLE tasks ADD COLUMN review_rounds"
                " INTEGER NOT NULL DEFAULT 0")
        if "verified_by" not in cols:
            self.db.execute("ALTER TABLE tasks ADD COLUMN verified_by TEXT")
        if "parent_id" not in cols:
            self.db.execute("ALTER TABLE tasks ADD COLUMN parent_id TEXT")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS reviews ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " task_id TEXT NOT NULL, round INTEGER NOT NULL,"
            " verifier_owner TEXT NOT NULL, verdict TEXT NOT NULL,"
            " reason TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL)")
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_reviews_task ON reviews(task_id)")
        # Autonomy journal for pre-iteration-11 databases.
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS recovery_log ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " task_id TEXT NOT NULL, task_title TEXT NOT NULL DEFAULT '',"
            " event TEXT NOT NULL, worker TEXT NOT NULL DEFAULT '',"
            " detail TEXT NOT NULL DEFAULT '', at REAL NOT NULL)")
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_recovery_log_at"
            " ON recovery_log(at)")
        # App settings table for pre-v1 databases.
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS settings ("
            " key TEXT PRIMARY KEY, value TEXT NOT NULL,"
            " updated_at REAL NOT NULL)")

    # ---------- autonomy journal ----------
    def _log_recovery(self, task_id, title, event, worker="", detail="",
                      at=None):
        """One row in the pull-only 'handled on its own' feed."""
        self.db.execute(
            "INSERT INTO recovery_log"
            " (task_id, task_title, event, worker, detail, at)"
            " VALUES (?,?,?,?,?,?)",
            (task_id, title, event, worker or "", detail or "",
             at if at is not None else time.time()))

    def recovery_feed(self, limit=50):
        """Autonomy journal entries, newest first. Each row also carries
        a human sentence via format_recovery()."""
        rows = self.db.execute(
            "SELECT * FROM recovery_log ORDER BY at DESC, id DESC"
            " LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def recovery_sentences(self, limit=50):
        """Human sentences for the dashboard feed, newest first.

        Each stopped worker pairs with the NEXT resume of the same task
        after it, giving the canonical form:
            "Muse" stopped "50 card faces" at "6:42 PM" — StayOnDuty
            resumed the task at "6:43 PM"
        A stop with no later resume is worded by where the task ended up
        (still pending vs. retries exhausted), so the feed never promises
        a pickup that isn't coming.
        """
        entries = list(reversed(self.recovery_feed(limit)))  # oldest first
        resumes_by_task = {}
        for e in entries:
            if e["event"] == "task_resumed":
                resumes_by_task.setdefault(e["task_id"], []).append(e)
        resume_idx = {}
        used = set()
        out = []
        for e in entries:
            if id(e) in used:
                continue
            if e["event"] in ("worker_stopped", "verifier_stopped"):
                cands = resumes_by_task.get(e["task_id"], [])
                i = resume_idx.get(e["task_id"], 0)
                while i < len(cands) and cands[i]["at"] < e["at"]:
                    i += 1
                if i < len(cands):
                    r = cands[i]
                    resume_idx[e["task_id"]] = i + 1
                    used.add(id(r))
                    out.append(format_recovery(e, r))
                    continue
                t = self.get_task(e["task_id"])
                out.append(format_recovery(
                    e, task_status=t["status"] if t else None))
            else:
                out.append(format_recovery(e))
        return list(reversed(out))

    # ---------- internal ----------
    def _event(self, task_id, type, detail=""):
        self.db.execute(
            "INSERT INTO events (task_id, type, detail, at) VALUES (?,?,?,?)",
            (task_id, type, detail, time.time()))

    def _alert(self, task_id, level, message, now=None):
        self.db.execute(
            "INSERT INTO alerts (task_id, level, message, created_at)"
            " VALUES (?,?,?,?)",
            (task_id, level, message, now or time.time()))

    @staticmethod
    def _row_task(row):
        d = dict(row)
        d["payload"] = json.loads(d["payload"])
        d["acceptance_criteria"] = json.loads(d["acceptance_criteria"] or "[]")
        return d

    @staticmethod
    def _normalize_criteria(acceptance):
        """Plain strings become manual checks ('a claim is not proof' —
        the agent must report evidence). Dicts keep their check spec:

            {"description": "output.txt exists",
             "check": {"type": "file_exists", "path": "out/output.txt"}}
        """
        out = []
        for i, c in enumerate(acceptance or []):
            cid = f"ac{i + 1}"
            if isinstance(c, str):
                out.append({"id": cid, "description": c,
                            "check": {"type": "manual"},
                            "status": "pending", "evidence": ""})
            else:
                dd = dict(c)
                out.append({"id": dd.get("id", cid),
                            "description": dd.get("description",
                                                  dd.get("id", cid)),
                            "check": dd.get("check", {"type": "manual"}),
                            "status": "pending", "evidence": ""})
        return out

    # ---------- tasks ----------
    def create_task(self, title, payload=None, token_budget=None,
                    usd_budget=None, acceptance=None, verify="none",
                    parent_id=None):
        tid = uuid.uuid4().hex[:8]
        now = time.time()
        if verify not in ("none", "independent"):
            verify = "none"
        self.db.execute(
            "INSERT INTO tasks (id,title,status,payload,acceptance_criteria,"
            "verify_mode,parent_id,lease_owner,lease_expires_at,attempts,token_budget,"
            "usd_budget,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, title, "pending", json.dumps(payload or {}),
             json.dumps(self._normalize_criteria(acceptance)),
             verify, parent_id, None, None, 0, token_budget, usd_budget,
             now, now))
        self._event(tid, "created", title)
        self.db.commit()
        return tid

    def get_task(self, task_id):
        row = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return self._row_task(row) if row else None

    def recover_stale(self, max_attempts=3):
        """Release tasks whose worker stopped heartbeating.

        Autonomy rule: a task that keeps dying gets fresh attempts on its
        own — but not forever. Past max_attempts it is marked failed WITH
        full diagnostics, as a passive record. Never pushes the user.
        Returns {'released': n, 'failed': n}.
        """
        now = time.time()
        stale = self.db.execute(
            "SELECT * FROM tasks WHERE status='in_progress'"
            " AND lease_expires_at IS NOT NULL AND lease_expires_at < ?",
            (now,)).fetchall()
        released, failed = 0, 0
        for row in stale:
            t = self._row_task(row)
            if t["attempts"] + 1 >= max_attempts:
                self.db.execute(
                    "UPDATE tasks SET status='failed', lease_owner=NULL,"
                    " lease_expires_at=NULL, stuck_at=NULL, stuck_level=0,"
                    " updated_at=? WHERE id=?", (now, t["id"]))
                self._event(t["id"], "failed",
                            f"worker kept dying — no progress after {t['attempts'] + 1} attempts")
                self._log_recovery(t["id"], t["title"], "give_up",
                                   worker=t["lease_owner"] or "",
                                   detail=f"after {t['attempts'] + 1} attempts")
                self.db.execute(
                    "INSERT INTO alerts (task_id, level, message, created_at)"
                    " VALUES (?,?,?,?)",
                    (t["id"], 3,
                     f"'{t['title']}' could not finish on its own after "
                     f"{t['attempts'] + 1} attempts — see its timeline when you check in",
                     now))
                failed += 1
                self._maybe_resolve_parent(t["id"])
            else:
                self.db.execute(
                    "UPDATE tasks SET status='pending', lease_owner=NULL,"
                    " lease_expires_at=NULL, stuck_at=NULL, stuck_level=0,"
                    " attempts=attempts+1, updated_at=?"
                    " WHERE id=?", (now, t["id"]))
                self._event(t["id"], "released",
                            f"heartbeat missed (owner={t['lease_owner']}); attempt #{t['attempts'] + 1}")
                # Autonomy journal: the "ai stopped task at time" half of
                # the sentence. `at` is the lease deadline — the moment
                # the worker's heartbeat went missing.
                self._log_recovery(t["id"], t["title"], "worker_stopped",
                                   worker=t["lease_owner"] or "",
                                   detail="heartbeat missed;"
                                   f" attempt #{t['attempts'] + 1} queued",
                                   at=t["lease_expires_at"] or now)
                released += 1
            self.db.commit()  # per task: keep write txns short under contention
        # A verifier that died mid-review: release the review lease so
        # another verifier can claim it. The review round isn't consumed.
        for row in self.db.execute(
                "SELECT * FROM tasks WHERE status='in_review'"
                " AND lease_owner IS NOT NULL AND lease_expires_at < ?",
                (now,)).fetchall():
            t = self._row_task(row)
            self.db.execute(
                "UPDATE tasks SET lease_owner=NULL, lease_expires_at=NULL,"
                " updated_at=? WHERE id=?", (now, t["id"]))
            self._event(t["id"], "released",
                        f"verifier heartbeat missed (was {t['lease_owner']});"
                        " review available again")
            self._log_recovery(t["id"], t["title"], "verifier_stopped",
                               worker=t["lease_owner"] or "",
                               detail="review lease released for another"
                               " verifier",
                               at=t["lease_expires_at"] or now)
            self.db.commit()
            released += 1
        return {"released": released, "failed": failed}

    def live_leases(self, now=None):
        """Tasks in_progress or in_review with unexpired leases — work an
        agent is actively doing right now. Used by the Claude Code Stop
        hook to catch an agent about to orphan live work."""
        now = now if now is not None else time.time()
        rows = self.db.execute(
            "SELECT * FROM tasks WHERE status IN ('in_progress','in_review')"
            " AND lease_expires_at IS NOT NULL AND lease_expires_at > ?"
            " ORDER BY updated_at DESC", (now,)).fetchall()
        return [self._row_task(r) for r in rows]

    def claim_task(self, owner, lease_secs=30, task_id=None):
        now = time.time()
        if task_id:
            row = self.db.execute(
                "SELECT * FROM tasks WHERE id=? AND status='pending'", (task_id,)).fetchone()
        else:
            row = self.db.execute(
                "SELECT * FROM tasks WHERE status='pending' ORDER BY created_at LIMIT 1").fetchone()
        if not row:
            return None
        t = self._row_task(row)
        self.db.execute(
            "UPDATE tasks SET status='in_progress', lease_owner=?,"
            " lease_expires_at=?, stuck_at=NULL, stuck_level=0, updated_at=? WHERE id=?",
            (owner, now + lease_secs, now, t["id"]))
        self._event(t["id"], "claimed", f"owner={owner} lease={lease_secs}s")
        if t["attempts"] > 0:
            # This claim resumes work a previous worker dropped — the
            # "StayOnDuty resumed the task" half of the sentence.
            mems = self.memories(scope=f"task:{t['id']}")
            detail = f"attempt #{t['attempts'] + 1}"
            if mems:
                detail += f"; checkpoint memory found ({len(mems)} keys)"
            self._log_recovery(t["id"], t["title"], "task_resumed",
                               worker=owner, detail=detail)
        self.db.commit()
        return self.get_task(t["id"])

    def heartbeat(self, task_id, owner, lease_secs=30):
        """Renew the lease. False = someone else owns it now (stop working)."""
        now = time.time()
        cur = self.db.execute(
            "UPDATE tasks SET lease_expires_at=?, updated_at=?"
            " WHERE id=? AND status IN ('in_progress','in_review')"
            " AND lease_owner=?",
            (now + lease_secs, now, task_id, owner))
        self.db.commit()
        return cur.rowcount == 1

    def complete_task(self, task_id, owner, result=""):
        """Mark done — but only if the acceptance criteria verify.

        Returns a verdict dict {'verified', 'criteria', ...}. When
        verification fails the task STAYS in_progress with its lease: the
        worker reads the verdict, fixes what's missing, and tries again.
        Failed verifications are logged as notes, not progress — so a
        worker that keeps claiming 'done' without fixing anything will
        trip stuck detection on its own.

        When the task asked for independent verification, a passing
        deterministic gate routes it to `in_review` instead of done: a
        different worker must review it before it can complete. The
        verdict then carries 'in_review': True.
        """
        now = time.time()
        t = self.get_task(task_id)
        if not t or t["status"] != "in_progress" or t["lease_owner"] != owner:
            return {"verified": False, "criteria": [],
                    "reason": "not your in_progress task"}
        verdict = self.verify_acceptance(task_id)
        if not verdict["verified"]:
            missing = [c["id"] for c in verdict["criteria"]
                       if c["status"] != "passed"]
            self._event(task_id, "verify_failed",
                        f"completion refused — failing: {', '.join(missing)};"
                        " fix and complete again")
            self.db.commit()
            return verdict
        if t.get("verify_mode") == "independent":
            self._submit_for_review(task_id, owner, now)
            verdict["in_review"] = True
            return verdict
        cur = self.db.execute(
            "UPDATE tasks SET status='done', lease_owner=NULL,"
            " lease_expires_at=NULL, updated_at=? WHERE id=? AND lease_owner=?",
            (now, task_id, owner))
        if cur.rowcount:
            self._event(task_id, "completed", str(result))
        self.db.commit()
        if cur.rowcount:
            self._maybe_resolve_parent(task_id)
        return verdict

    # ---------- independent review ----------
    def _submit_for_review(self, task_id, producer_owner, now=None):
        """Producer passed the deterministic gate: park the task in
        `in_review` for a DIFFERENT worker to judge. Lease released."""
        now = now if now is not None else time.time()
        self.db.execute(
            "UPDATE tasks SET status='in_review', producer_owner=?,"
            " lease_owner=NULL, lease_expires_at=NULL, stuck_at=NULL,"
            " stuck_level=0, updated_at=? WHERE id=?",
            (producer_owner, now, task_id))
        self._event(task_id, "submitted_for_review",
                    f"producer={producer_owner} passed the deterministic gate;"
                    " awaiting an independent verifier")
        self.db.commit()

    def claim_review(self, verifier_owner, lease_secs=60, task_id=None):
        """Claim an in_review task for judgment. The producer can never
        review its own work — that claim is refused. Returns the task
        (with fresh deterministic re-check results) or None."""
        now = time.time()
        if task_id:
            row = self.db.execute(
                "SELECT * FROM tasks WHERE id=? AND status='in_review'"
                " AND (lease_owner IS NULL OR lease_expires_at < ?)",
                (task_id, now)).fetchone()
        else:
            row = self.db.execute(
                "SELECT * FROM tasks WHERE status='in_review'"
                " AND producer_owner != ?"
                " AND (lease_owner IS NULL OR lease_expires_at < ?)"
                " ORDER BY updated_at LIMIT 1",
                (verifier_owner, now)).fetchone()
        if not row:
            return None
        t = self._row_task(row)
        if t["producer_owner"] == verifier_owner:
            return None  # the producer cannot grade its own work
        self.db.execute(
            "UPDATE tasks SET lease_owner=?, lease_expires_at=?,"
            " updated_at=? WHERE id=?",
            (verifier_owner, now + lease_secs, now, t["id"]))
        self._event(t["id"], "review_claimed",
                    f"verifier={verifier_owner} lease={lease_secs}s")
        self.db.commit()
        return self.get_task(t["id"])

    def submit_verdict(self, task_id, verifier_owner, verdict, reason="",
                       max_rounds=3):
        """Record a review verdict. 'pass' completes the task (verified_by
        recorded). 'fail' sends it back to in_progress with the verifier's
        notes in the timeline — bounded: past max_rounds failures the task
        is failed as a passive record, never a push. Returns True/False."""
        now = time.time()
        t = self.get_task(task_id)
        if (not t or t["status"] != "in_review"
                or t["lease_owner"] != verifier_owner
                or t["producer_owner"] == verifier_owner):
            return False
        if verdict not in ("pass", "fail"):
            return False
        rnd = t["review_rounds"] + 1
        self.db.execute(
            "INSERT INTO reviews (task_id, round, verifier_owner, verdict,"
            " reason, created_at) VALUES (?,?,?,?,?,?)",
            (task_id, rnd, verifier_owner, verdict, reason, now))
        if verdict == "pass":
            self.db.execute(
                "UPDATE tasks SET status='done', lease_owner=NULL,"
                " lease_expires_at=NULL, review_rounds=?, verified_by=?,"
                " updated_at=? WHERE id=?",
                (rnd, verifier_owner, now, task_id))
            self._event(task_id, "review_passed",
                        f"round {rnd} verifier={verifier_owner}: {reason}")
        else:
            if rnd >= max_rounds:
                self.db.execute(
                    "UPDATE tasks SET status='failed', lease_owner=NULL,"
                    " lease_expires_at=NULL, review_rounds=?, updated_at=?"
                    " WHERE id=?", (rnd, now, task_id))
                self._event(task_id, "failed",
                            f"independent review failed {rnd} rounds —"
                            f" giving up: {reason}")
                self._log_recovery(task_id, t["title"], "give_up",
                                   worker=t["producer_owner"] or "",
                                   detail=f"independent review failed"
                                   f" {rnd} rounds")
                self.db.execute(
                    "INSERT INTO alerts (task_id, level, message, created_at)"
                    " VALUES (?,?,?,?)",
                    (task_id, 3,
                     f"'{t['title']}' failed independent review {rnd} times"
                     " — see its timeline when you check in", now))
            else:
                self.db.execute(
                    "UPDATE tasks SET status='pending', lease_owner=NULL,"
                    " lease_expires_at=NULL, review_rounds=?, updated_at=?"
                    " WHERE id=?", (rnd, now, task_id))
                self._event(task_id, "review_failed",
                            f"round {rnd} verifier={verifier_owner}: {reason};"
                            " re-queued — fix and resubmit")
                self._log_recovery(task_id, t["title"], "auto_retried",
                                   worker=t["producer_owner"] or "",
                                   detail=f"independent review round {rnd}"
                                   " failed — re-queued with notes")
        self.db.commit()
        if verdict == "pass" or (verdict == "fail" and rnd >= max_rounds):
            self._maybe_resolve_parent(task_id)
        return True

    def reviews(self, task_id):
        """Review history for a task, oldest first."""
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM reviews WHERE task_id=? ORDER BY round",
            (task_id,)).fetchall()]

    def release_review(self, task_id, verifier_owner):
        """Give up a claimed review without a verdict — another verifier
        may claim it. The round is not consumed."""
        cur = self.db.execute(
            "UPDATE tasks SET lease_owner=NULL, lease_expires_at=NULL,"
            " updated_at=? WHERE id=? AND status='in_review'"
            " AND lease_owner=?",
            (time.time(), task_id, verifier_owner))
        if cur.rowcount:
            self._event(task_id, "review_released",
                        f"verifier={verifier_owner} stepped down")
        self.db.commit()
        return cur.rowcount == 1

    # ---------- hierarchical decomposition ----------
    # A worker holding an in_progress task may split it into subtasks.
    # The parent becomes `decomposed` (not claimable, no lease); each
    # child gets its own lease lifecycle, memory scope, stuck ladder,
    # acceptance gate, and spend caps. When every child is terminal the
    # parent auto-resolves: all children done + the parent's own
    # acceptance verified -> done; otherwise a passive failed record.
    # A parent budget is a SUBTREE cap: usage reported on any descendant
    # counts against every budgeted ancestor, and the spending task is
    # parked on budget_hold when an ancestor's cap trips.
    MAX_DEPTH = 5

    def _depth(self, task_id):
        """How many ancestors this task has (root = 0)."""
        d, seen = 0, set()
        t = self.get_task(task_id)
        while t and t.get("parent_id") and t["id"] not in seen:
            seen.add(t["id"])
            d += 1
            t = self.get_task(t["parent_id"])
        return d

    def _ancestors(self, task_id):
        """Ancestor tasks, nearest first."""
        out, seen = [], set()
        t = self.get_task(task_id)
        while t and t.get("parent_id") and t["id"] not in seen:
            seen.add(t["id"])
            t = self.get_task(t["parent_id"])
            if t:
                out.append(t)
        return out

    def children(self, task_id):
        """Direct subtasks, oldest first."""
        return [self._row_task(r) for r in self.db.execute(
            "SELECT * FROM tasks WHERE parent_id=? ORDER BY created_at",
            (task_id,)).fetchall()]

    def _subtree_usage(self, task_id):
        """Sum tokens_used/usd_used over a task and all its descendants."""
        total_t = total_u = 0.0
        stack = [task_id]
        seen = set()
        while stack:
            tid = stack.pop()
            if tid in seen:
                continue
            seen.add(tid)
            t = self.get_task(tid)
            if not t:
                continue
            total_t += t["tokens_used"] or 0
            total_u += t["usd_used"] or 0
            for k in self.children(tid):
                stack.append(k["id"])
        return total_t, total_u

    def decompose(self, task_id, owner, subtasks):
        """Split an in_progress task into subtasks.

        Only the lease owner may decompose. Each subtask spec:
            {"title": str (required), "payload": dict,
             "acceptance": [...], "token_budget": int, "usd_budget": float,
             "verify": "none"|"independent" (default: inherit parent)}
        The parent goes `decomposed` (lease released, not claimable);
        children start pending. Returns [child ids], or None when refused
        (not owner, empty specs, bad spec, nesting too deep).
        """
        t = self.get_task(task_id)
        if (not t or t["status"] != "in_progress"
                or t["lease_owner"] != owner):
            return None
        specs = [s or {} for s in (subtasks or [])]
        if not specs or any(not s.get("title") for s in specs):
            return None
        if self._depth(task_id) >= self.MAX_DEPTH:
            return None
        now = time.time()
        kids = []
        for spec in specs:
            kid = self.create_task(
                spec["title"], payload=spec.get("payload"),
                token_budget=spec.get("token_budget"),
                usd_budget=spec.get("usd_budget"),
                acceptance=spec.get("acceptance"),
                verify=spec.get("verify", t["verify_mode"]),
                parent_id=task_id)
            # create_task commits per child; stamp them as one batch
            kids.append(kid)
        self.db.execute(
            "UPDATE tasks SET status='decomposed', lease_owner=NULL,"
            " lease_expires_at=NULL, stuck_at=NULL, stuck_level=0,"
            " updated_at=? WHERE id=?", (now, task_id))
        self._event(task_id, "decomposed",
                    f"{owner} split into {len(kids)} subtasks:"
                    f" {', '.join(kids)}")
        self.db.commit()
        return kids

    def _maybe_resolve_parent(self, task_id):
        """Called whenever a task goes terminal (done/failed).

        If its parent is decomposed and ALL its children are now
        terminal, resolve the parent: all children done + parent's own
        acceptance verified -> done; else a passive failed record naming
        the cause. Then recurse upward — a grandparent may now resolve.
        """
        t = self.get_task(task_id)
        if not t or not t.get("parent_id"):
            return
        p = self.get_task(t["parent_id"])
        if not p or p["status"] != "decomposed":
            return
        kids = self.children(p["id"])
        if not kids or any(k["status"] not in ("done", "failed")
                           for k in kids):
            return
        now = time.time()
        failed = [k for k in kids if k["status"] == "failed"]
        if failed:
            names = ", ".join(k["id"] for k in failed)
            self.db.execute(
                "UPDATE tasks SET status='failed', updated_at=? WHERE id=?",
                (now, p["id"]))
            self._event(p["id"], "failed",
                        f"{len(failed)}/{len(kids)} subtasks failed"
                        f" ({names}) — see their timelines")
            self._alert(p["id"], 3,
                        f"\"{p['title']}\" could not finish:"
                        f" {len(failed)}/{len(kids)} subtask(s) failed"
                        f" ({names}) — see its timeline when you check in",
                        now)
        else:
            verdict = self.verify_acceptance(p["id"])
            if verdict["verified"]:
                self.db.execute(
                    "UPDATE tasks SET status='done', updated_at=? WHERE id=?",
                    (now, p["id"]))
                self._event(p["id"], "completed",
                            f"all {len(kids)} subtasks done; parent"
                            " acceptance verified")
            else:
                missing = [c["id"] for c in verdict["criteria"]
                           if c["status"] != "passed"]
                self.db.execute(
                    "UPDATE tasks SET status='failed', updated_at=? WHERE id=?",
                    (now, p["id"]))
                self._event(p["id"], "failed",
                            "all subtasks done but the parent's own"
                            f" acceptance was not met — missing:"
                            f" {', '.join(missing)}")
                self._alert(p["id"], 3,
                            f"\"{p['title']}\" finished its subtasks but its"
                            " own acceptance criteria were not met"
                            f" ({', '.join(missing)}) — see its timeline when"
                            " you check in", now)
        self.db.commit()
        self._maybe_resolve_parent(p["id"])

    # ---------- acceptance criteria ----------
    def add_acceptance_criteria(self, task_id, acceptance):
        """Append criteria to a task that isn't done/failed."""
        t = self.get_task(task_id)
        if not t or t["status"] in ("done", "failed"):
            return False
        crits = t["acceptance_criteria"] + self._normalize_criteria(acceptance)
        for i, c in enumerate(crits):
            c["id"] = f"ac{i + 1}"
        self.db.execute(
            "UPDATE tasks SET acceptance_criteria=?, updated_at=? WHERE id=?",
            (json.dumps(crits), time.time(), task_id))
        self._event(task_id, "criteria_added",
                    f"{len(acceptance or [])} acceptance criteria added")
        self.db.commit()
        return True

    def acceptance_check(self, task_id, owner, criterion_id, passed,
                         evidence=""):
        """Report evidence for a manual criterion. Only the lease owner
        of an in_progress task may report."""
        now = time.time()
        t = self.get_task(task_id)
        if not t or t["status"] != "in_progress" or t["lease_owner"] != owner:
            return False
        crits = t["acceptance_criteria"]
        found = False
        for c in crits:
            if c["id"] == criterion_id:
                c["status"] = "passed" if passed else "failed"
                c["evidence"] = str(evidence)
                found = True
        if not found:
            return False
        self.db.execute(
            "UPDATE tasks SET acceptance_criteria=?, updated_at=? WHERE id=?",
            (json.dumps(crits), now, task_id))
        self._event(task_id, "acceptance_check",
                    f"{criterion_id}: {'passed' if passed else 'failed'}"
                    f" — {evidence}")
        self.db.commit()
        return True

    def verify_acceptance(self, task_id):
        """Evaluate every criterion against the real artifact.

        Built-in checks run now (files, commands, images). Manual
        criteria use the last reported status — an unevidenced manual
        criterion FAILS: a sentence is not proof. Persists statuses and
        evidence. Returns {'verified': bool, 'criteria': [...]}.
        """
        t = self.get_task(task_id)
        crits = t["acceptance_criteria"] if t else []
        for c in crits:
            if c["check"].get("type") == "manual":
                if c["status"] != "passed":
                    c["status"] = "failed"
                    if not c["evidence"]:
                        c["evidence"] = ("no evidence reported —"
                                         " a claim is not proof")
            else:
                passed, evidence = self._eval_check(c["check"])
                c["status"] = "passed" if passed else "failed"
                c["evidence"] = evidence
        if t:
            self.db.execute(
                "UPDATE tasks SET acceptance_criteria=?, updated_at=?"
                " WHERE id=?", (json.dumps(crits), time.time(), task_id))
            self.db.commit()
        return {"verified": all(c["status"] == "passed" for c in crits),
                "criteria": crits}

    def _eval_check(self, check):
        """Run one built-in check. Returns (passed, evidence).

        Checks run with the worker's own privileges — the agent could run
        these commands itself; StayOnDuty just runs them at the 'prove it'
        moment and records the evidence.
        """
        ctype = check.get("type", "manual")
        try:
            if ctype == "file_exists":
                p = check["path"]
                ok = os.path.exists(p)
                return ok, f"{p} {'exists' if ok else 'MISSING'}"
            if ctype == "file_contains":
                p, needle = check["path"], check.get("text", "")
                try:
                    with open(p, encoding="utf-8",
                              errors="replace") as f:
                        data = f.read()
                except OSError as e:
                    return False, f"cannot read {p}: {e}"
                ok = (re.search(needle, data) is not None
                      if check.get("regex") else needle in data)
                return ok, f"{p} {'contains' if ok else 'LACKS'} {needle!r}"
            if ctype == "command":
                r = subprocess.run(
                    check["cmd"], shell=True, cwd=check.get("cwd"),
                    timeout=check.get("timeout", 60),
                    capture_output=True, text=True)
                out = (r.stdout + r.stderr).strip()[-300:]
                return r.returncode == 0, f"exit {r.returncode}: {out}"
            if ctype == "image_valid":
                p = check["path"]
                try:
                    with open(p, "rb") as f:
                        data = f.read()
                except OSError as e:
                    return False, f"cannot read {p}: {e}"
                v = verify_image(
                    data, min_width=check.get("min_width", 64),
                    min_height=check.get("min_height", 64))
                ev = ((f"{v.format} {v.width}x{v.height}")
                      if v.format else "; ".join(v.reasons))
                return bool(v), ev or "; ".join(v.reasons)
            if ctype == "manual":
                return None, "manual — report via acceptance_check"
        except Exception as e:  # noqa: BLE001
            return False, f"check error: {e}"
        return False, f"unknown check type: {ctype}"

    def fail_task(self, task_id, owner, reason=""):
        now = time.time()
        cur = self.db.execute(
            "UPDATE tasks SET status='failed', lease_owner=NULL,"
            " lease_expires_at=NULL, updated_at=? WHERE id=? AND lease_owner=?",
            (now, task_id, owner))
        if cur.rowcount:
            self._event(task_id, "failed", str(reason))
        self.db.commit()
        if cur.rowcount:
            self._maybe_resolve_parent(task_id)

    def step(self, task_id, detail):
        """A unit of real progress. Clears any stuck flag automatically."""
        now = time.time()
        self._event(task_id, "step", detail)
        self.db.execute("UPDATE tasks SET updated_at=? WHERE id=?", (now, task_id))
        self._clear_stuck(task_id)
        self.db.commit()

    # ---------- quota waiting ----------
    def park_waiting(self, task_id, owner, resume_at, reason="", provider=""):
        """Deliberately pause a task until resume_at (e.g. provider quota reset).

        This is NOT a failure and NOT stuck: detect_stuck() only looks at
        in_progress tasks, so waiting tasks are never flagged, and
        promote_waiting() returns them to pending WITHOUT consuming an
        attempt — waiting is a pause, not a retry.

        A passive level-1 notification ('"<provider>" has hit usage cap') is
        logged so the user sees it when they check the dashboard; it clears
        automatically when the task resumes.
        """
        now = time.time()
        cur = self.db.execute(
            "UPDATE tasks SET status='waiting', lease_owner=NULL,"
            " lease_expires_at=NULL, stuck_at=NULL, stuck_level=0,"
            " resume_at=?, updated_at=? WHERE id=? AND lease_owner=?",
            (resume_at, now, task_id, owner))
        if cur.rowcount:
            when = time.strftime("%H:%M:%S", time.localtime(resume_at))
            self._event(task_id, "waiting", f"{reason} — resumes ~{when}")
            dup = self.db.execute(
                "SELECT 1 FROM alerts WHERE task_id=? AND level=1"
                " AND acked_at IS NULL", (task_id,)).fetchone()
            if not dup:
                name = provider or "provider"
                title = (self.get_task(task_id) or {}).get("title", task_id)
                self.db.execute(
                    "INSERT INTO alerts (task_id, level, message, created_at)"
                    " VALUES (?,?,?,?)",
                    (task_id, 1,
                     f'"{name}" has hit usage cap — "{title}" parked,'
                     f" resumes automatically ~{when}",
                     now))
        self.db.commit()
        return cur.rowcount == 1

    def promote_waiting(self, now=None):
        """Return waiting tasks whose window elapsed back to pending.

        Does not touch attempts. Returns the promoted tasks. The watchdog
        calls this every loop; it is the quiet counterpart to detect_stuck.
        """
        now = time.time() if now is None else now
        promoted = []
        for row in self.db.execute(
                "SELECT * FROM tasks WHERE status='waiting'"
                " AND resume_at IS NOT NULL AND resume_at <= ?",
                (now,)).fetchall():
            t = self._row_task(row)
            self.db.execute(
                "UPDATE tasks SET status='pending', resume_at=NULL, updated_at=?"
                " WHERE id=?", (now, t["id"]))
            self._event(t["id"], "resumed", "wait window reached — back in the queue")
            self._clear_notices(t["id"], now)
            self.db.commit()
            promoted.append(self.get_task(t["id"]))
        return promoted

    def _clear_notices(self, task_id, now=None):
        """Clear passive level-1 notices (quota / budget) once the task
        moves again. Mirrors promote_waiting's old inline behavior."""
        now = time.time() if now is None else now
        self.db.execute(
            "UPDATE alerts SET acked_at=? WHERE task_id=? AND level=1"
            " AND acked_at IS NULL", (now, task_id))

    # ---------- spend guardrails ----------
    def report_usage(self, task_id, owner, prompt_tokens=0,
                     completion_tokens=0, usd=None):
        """Report model usage against the task's spend caps.

        Only the lease owner of an in_progress task may report. Usage is
        recorded honestly even when it trips the cap. On exceed, the task
        is parked in `budget_hold` — lease released, worker stood down —
        and a passive level-1 notice is logged. Like `waiting`, a hold is
        NOT a failure and consumes NO attempt; unlike `waiting`, nothing
        auto-resumes it — the budget must be raised (raise_budget).

        Returns {'ok', 'on_hold', 'tokens_used', 'usd_used', ...}.
        The worker MUST stop when on_hold is True.
        """
        now = time.time()
        t = self.get_task(task_id)
        if not t or t["status"] != "in_progress" or t["lease_owner"] != owner:
            return {"ok": False, "on_hold": False,
                    "reason": "not your in_progress task"}
        tokens = (prompt_tokens or 0) + (completion_tokens or 0)
        new_tokens = t["tokens_used"] + tokens
        new_usd = t["usd_used"] + (usd or 0)
        over = ((t["token_budget"] is not None and new_tokens > t["token_budget"])
                or (t["usd_budget"] is not None and new_usd > t["usd_budget"]))
        # A parent budget is a subtree cap: usage on any descendant counts
        # against every budgeted ancestor, so decomposition can't weaken
        # the guardrail. The spending task is the one parked.
        cap_holder, used_tokens = t, new_tokens
        if not over:
            for a in self._ancestors(task_id):
                if a["token_budget"] is None and a["usd_budget"] is None:
                    continue
                st, su = self._subtree_usage(a["id"])
                st, su = int(st + tokens), su + (usd or 0)
                a_over = ((a["token_budget"] is not None and st > a["token_budget"])
                          or (a["usd_budget"] is not None and su > a["usd_budget"]))
                if a_over:
                    over = True
                    cap_holder, used_tokens = a, st
                    break
        if over:
            reason = (f"spend cap exceeded: {used_tokens} tokens used"
                      + (f" (${new_usd:.4f})" if usd else ""))
            if cap_holder["id"] != task_id:
                reason += f" against subtree cap on '{cap_holder['title']}'"
            self.db.execute(
                "UPDATE tasks SET status='budget_hold', lease_owner=NULL,"
                " lease_expires_at=NULL, stuck_at=NULL, stuck_level=0,"
                " tokens_used=?, usd_used=?, hold_reason=?, updated_at=?"
                " WHERE id=?",
                (new_tokens, new_usd, reason, now, task_id))
            self._event(task_id, "budget_hold",
                        reason + " — guardrail parked the task, worker stood down")
            dup = self.db.execute(
                "SELECT 1 FROM alerts WHERE task_id=? AND level=1"
                " AND acked_at IS NULL", (task_id,)).fetchone()
            if not dup:
                caps = []
                if cap_holder["token_budget"] is not None:
                    caps.append(f"{cap_holder['token_budget']} tokens")
                if cap_holder["usd_budget"] is not None:
                    caps.append(f"${cap_holder['usd_budget']:.2f}")
                on_txt = (f" on '{cap_holder['title']}'"
                          if cap_holder["id"] != task_id else "")
                self.db.execute(
                    "INSERT INTO alerts (task_id, level, message, created_at)"
                    " VALUES (?,?,?,?)",
                    (task_id, 1,
                     f"\U0001f4b0 Budget guardrail tripped — \"{t['title']}\" parked"
                     f" after {used_tokens} tokens (cap {' + '.join(caps)}{on_txt})."
                     f" Raise the budget to resume — nothing spent since.",
                     now))
            self.db.commit()
            return {"ok": True, "on_hold": True,
                    "tokens_used": new_tokens, "usd_used": new_usd,
                    "reason": reason}
        self.db.execute(
            "UPDATE tasks SET tokens_used=?, usd_used=?, updated_at=?"
            " WHERE id=?", (new_tokens, new_usd, now, task_id))
        self.db.commit()
        return {"ok": True, "on_hold": False,
                "tokens_used": new_tokens, "usd_used": new_usd}

    def raise_budget(self, task_id, token_budget=None, usd_budget=None):
        """Raise (or remove, with None) the spend caps on a task.

        A budget-held task returns to pending with attempts untouched and
        its passive notice cleared. Caps may also be raised on a
        `decomposed` parent — its budget is a subtree cap, and
        descendants parked on budget_hold against it resume automatically
        once their caps clear. Other statuses refuse (False).
        """
        now = time.time()
        t = self.get_task(task_id)
        if not t or t["status"] not in ("budget_hold", "decomposed"):
            return False
        if t["status"] == "budget_hold":
            self.db.execute(
                "UPDATE tasks SET status='pending', token_budget=?, usd_budget=?,"
                " hold_reason=NULL, updated_at=? WHERE id=?",
                (token_budget, usd_budget, now, task_id))
            self._event(task_id, "budget_raised",
                        f"caps now {token_budget} tokens / {usd_budget} usd"
                        " — back in the queue")
            self._clear_notices(task_id, now)
        else:
            self.db.execute(
                "UPDATE tasks SET token_budget=?, usd_budget=?, updated_at=?"
                " WHERE id=?",
                (token_budget, usd_budget, now, task_id))
            self._event(task_id, "budget_raised",
                        f"caps now {token_budget} tokens / {usd_budget} usd")
        # Descendants parked against this (subtree) cap resume if clear.
        for d in self._descendants(task_id):
            if d["status"] == "budget_hold" and self._caps_clear(d["id"]):
                self.db.execute(
                    "UPDATE tasks SET status='pending', hold_reason=NULL,"
                    " updated_at=? WHERE id=?", (now, d["id"]))
                self._event(d["id"], "budget_raised",
                            f"cap raised on '{t['title']}' — back in the queue")
                self._clear_notices(d["id"], now)
        self.db.commit()
        return True

    def _descendants(self, task_id):
        """All subtasks at any depth, nearest first."""
        out, stack, seen = [], list(self.children(task_id)), set()
        while stack:
            k = stack.pop(0)
            if k["id"] in seen:
                continue
            seen.add(k["id"])
            out.append(k)
            stack.extend(self.children(k["id"]))
        return out

    def _caps_clear(self, task_id):
        """True when neither the task's own caps nor any budgeted
        ancestor's subtree cap is currently exceeded."""
        t = self.get_task(task_id)
        if not t:
            return False
        if t["token_budget"] is not None and t["tokens_used"] > t["token_budget"]:
            return False
        if t["usd_budget"] is not None and t["usd_used"] > t["usd_budget"]:
            return False
        for a in self._ancestors(task_id):
            st, su = self._subtree_usage(a["id"])
            if a["token_budget"] is not None and st > a["token_budget"]:
                return False
            if a["usd_budget"] is not None and su > a["usd_budget"]:
                return False
        return True

    def note(self, task_id, detail):
        """A non-progress note (e.g. 'replanning'). Does NOT clear stuck."""
        self._event(task_id, "note", detail)
        self.db.commit()

    def _clear_stuck(self, task_id):
        cur = self.db.execute(
            "UPDATE tasks SET stuck_at=NULL, stuck_level=0"
            " WHERE id=? AND stuck_level>0", (task_id,))
        if cur.rowcount:
            self._event(task_id, "unstuck", "progress resumed — stuck flag cleared")

    # ---------- memory ----------
    def remember(self, key, value, scope="global", task_id=None):
        if task_id:
            scope = f"task:{task_id}"
            self._event(task_id, "memory", f"{key}={value}")
        self.db.execute(
            "INSERT INTO memory (key, scope, value, updated_at) VALUES (?,?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET scope=excluded.scope,"
            " value=excluded.value, updated_at=excluded.updated_at",
            (key, scope, json.dumps(value), time.time()))
        if task_id:
            self._clear_stuck(task_id)  # writing memory is progress too
        self.db.commit()

    def recall(self, key, scope="global"):
        row = self.db.execute(
            "SELECT value FROM memory WHERE key=? AND scope=?", (key, scope)).fetchone()
        return json.loads(row["value"]) if row else None

    def memories(self, scope=None):
        q = "SELECT key, scope, value FROM memory" + (" WHERE scope=?" if scope else "")
        rows = self.db.execute(q, (scope,) if scope else []).fetchall()
        return {r["key"]: json.loads(r["value"]) for r in rows}

    # ---------- app settings (API keys live ONLY here) ----------
    def set_setting(self, key, value):
        """Store a setting. Used for API keys — the value is never logged,
        never echoed back to the UI, and never leaves this database except
        in the Authorization header of a call to its own provider."""
        self.db.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
            " updated_at=excluded.updated_at",
            (key, value, time.time()))
        self.db.commit()

    def get_setting(self, key, default=None):
        row = self.db.execute(
            "SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def delete_setting(self, key):
        self.db.execute("DELETE FROM settings WHERE key=?", (key,))
        self.db.commit()

    # ---------- stuck detection ----------
    def _last_progress_at(self, task_id, fallback):
        row = self.db.execute(
            "SELECT MAX(at) FROM events WHERE task_id=? AND type IN ('step','memory')",
            (task_id,)).fetchone()
        return row[0] or fallback

    def detect_stuck(self, now=None, stuck_after_secs=600, escalate_after_secs=1800,
                     max_attempts=3):
        """Flag in_progress tasks with no real progress — and handle them.

        Autonomy first: the user is never pushed. The ladder is:
        level 1 (stuck): the worker is nudged to replan via the stuck flag.
        level 2 (auto-retry): still nothing -> the task goes back to pending
            for a fresh attempt (attempts+1). A passive alert is logged.
        level 3 (failed): attempts exhausted -> marked failed WITH full
            diagnostics in the timeline. Still no push — the user sees it
            when they check the dashboard.
        Tasks in `waiting` (parked for a quota window) are skipped entirely —
        a deliberate pause must never look like a stall. Tasks in `in_review`
        with no verifier for too long are failed as a passive record — no
        reviewer, no completion, no push.
        Returns {'stuck': [...], 'requeued': [...], 'failed': [...]}.
        """
        now = time.time() if now is None else now
        stuck, requeued, failed = [], [], []
        for row in self.db.execute(
                "SELECT * FROM tasks WHERE status='in_review'"
                " AND (lease_owner IS NULL OR lease_expires_at < ?)",
                (now,)).fetchall():
            t = self._row_task(row)
            idle = now - self._last_progress_at(t["id"], t["created_at"])
            if t["stuck_level"] == 0 and idle > stuck_after_secs:
                self.db.execute(
                    "UPDATE tasks SET stuck_at=?, stuck_level=1, updated_at=?"
                    " WHERE id=?", (now, now, t["id"]))
                self._event(t["id"], "stuck",
                            f"in review with no verifier for {int(idle)}s")
                stuck.append(self.get_task(t["id"]))
            elif t["stuck_level"] == 1 \
                    and now - (t["stuck_at"] or now) > escalate_after_secs:
                self.db.execute(
                    "UPDATE tasks SET status='failed', lease_owner=NULL,"
                    " lease_expires_at=NULL, stuck_at=NULL, stuck_level=0,"
                    " updated_at=? WHERE id=?", (now, t["id"]))
                self._event(t["id"], "failed",
                            "no independent verifier claimed it — giving up,"
                            " diagnostics in timeline")
                self._log_recovery(t["id"], t["title"], "give_up",
                                   detail="no independent verifier claimed it")
                self.db.execute(
                    "INSERT INTO alerts (task_id, level, message, created_at)"
                    " VALUES (?,?,?,?)",
                    (t["id"], 3,
                     f"'{t['title']}' waited for an independent verifier and"
                     " none came — see its timeline when you check in",
                     now))
                failed.append(self.get_task(t["id"]))
                self._maybe_resolve_parent(t["id"])
            self.db.commit()  # per task: keep write txns short under contention
        for row in self.db.execute(
                "SELECT * FROM tasks WHERE status='in_progress'").fetchall():
            t = self._row_task(row)
            idle = now - self._last_progress_at(t["id"], t["created_at"])
            if t["stuck_level"] == 0 and idle > stuck_after_secs:
                self.db.execute(
                    "UPDATE tasks SET stuck_at=?, stuck_level=1, updated_at=?"
                    " WHERE id=?", (now, now, t["id"]))
                self._event(t["id"], "stuck",
                            f"no progress for {int(idle)}s — nudging worker to replan")
                self._log_recovery(t["id"], t["title"], "stuck_replanned",
                                   worker=t["lease_owner"] or "")
                stuck.append(self.get_task(t["id"]))
            elif t["stuck_level"] == 1 and idle > stuck_after_secs \
                    and now - (t["stuck_at"] or now) > escalate_after_secs:
                if t["attempts"] + 1 >= max_attempts:
                    self.db.execute(
                        "UPDATE tasks SET status='failed', lease_owner=NULL,"
                        " lease_expires_at=NULL, stuck_at=NULL, stuck_level=0,"
                        " updated_at=? WHERE id=?", (now, t["id"]))
                    self._event(t["id"], "failed",
                                f"no progress after {t['attempts'] + 1} attempts"
                                " — giving up, diagnostics in timeline")
                    self._log_recovery(t["id"], t["title"], "give_up",
                                       worker=t["lease_owner"] or "",
                                       detail=f"no progress after"
                                       f" {t['attempts'] + 1} attempts")
                    self.db.execute(
                        "INSERT INTO alerts (task_id, level, message, created_at)"
                        " VALUES (?,?,?,?)",
                        (t["id"], 3,
                         f"'{t['title']}' could not finish on its own after "
                         f"{t['attempts'] + 1} attempts — see its timeline when you check in",
                         now))
                    failed.append(self.get_task(t["id"]))
                    self._maybe_resolve_parent(t["id"])
                else:
                    self.db.execute(
                        "UPDATE tasks SET status='pending', lease_owner=NULL,"
                        " lease_expires_at=NULL, stuck_at=NULL, stuck_level=0,"
                        " attempts=attempts+1, updated_at=? WHERE id=?",
                        (now, t["id"]))
                    self._event(t["id"], "auto-requeue",
                                f"stuck with no progress — fresh attempt"
                                f" #{t['attempts'] + 2} queued autonomously")
                    self._log_recovery(t["id"], t["title"], "auto_retried",
                                       worker=t["lease_owner"] or "",
                                       detail=f"attempt #{t['attempts'] + 2}")
                    self.db.execute(
                        "INSERT INTO alerts (task_id, level, message, created_at)"
                        " VALUES (?,?,?,?)",
                        (t["id"], 2,
                         f"'{t['title']}' was stuck; queued fresh attempt"
                         f" #{t['attempts'] + 2} on its own"
                         f" (owner was {t['lease_owner']})",
                         now))
                    requeued.append(self.get_task(t["id"]))
            self.db.commit()  # per task: keep write txns short under contention
        return {"stuck": stuck, "requeued": requeued, "failed": failed}

    def alerts(self, unacked_only=True):
        q = ("SELECT * FROM alerts WHERE acked_at IS NULL ORDER BY created_at"
             if unacked_only else "SELECT * FROM alerts ORDER BY created_at")
        return [dict(r) for r in self.db.execute(q).fetchall()]

    def ack_alert(self, alert_id):
        self.db.execute("UPDATE alerts SET acked_at=? WHERE id=?",
                        (time.time(), alert_id))
        self.db.commit()

    # ---------- inspection ----------
    def events(self, task_id):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM events WHERE task_id=? ORDER BY id", (task_id,)).fetchall()]

    def close(self):
        self.db.close()
