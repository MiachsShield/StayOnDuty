"""StayOnDuty SDK — the two-line way for any agent to stay on duty.

Any agent, in any framework, gets crash-safe, stuck-detecting supervision
without hand-rolling a watchdog:

    from stayonduty import Client

    client = Client("work.db")
    with client.lease("generate 50 card faces",
                      acceptance=["50 pngs in out/",
                                  "each at least 256x256"]) as job:
        start = (job.recall("last_index") or -1) + 1
        for i in range(start, 50):
            make_card_face(i)            # your work here
            job.step(f"card face {i}")   # progress the watchdog can see
            job.remember("last_index", i)

If this process dies mid-loop, the lease expires, another worker recovers
the task, recalls ``last_index``, and resumes at the next card — nothing
redone, nobody paged. When the worker calls ``complete()``, StayOnDuty
verifies the acceptance criteria against the real artifact instead of
trusting the claim: a failed check refuses completion and tells the
worker exactly what's missing. A bare sentence is not proof.

Run the watchdog alongside for stuck detection:

    python3 -m stayonduty.watchdog work.db --interval 30

For agents that can't import Python (or prefer tool discovery), the same
surface is exposed as an MCP server: ``python3 -m stayonduty.mcp_server``.
"""
from __future__ import annotations

import threading
import time
import uuid

from .store import Store
from .worker import QuotaWait

__all__ = ["Client", "Lease", "ReviewClaim", "QuotaWait"]


class Lease:
    """A claimed task with a live heartbeat. Use as a context manager."""

    def __init__(self, client, task_id=None, title=None, payload=None,
                 owner=None, lease_secs=30, token_budget=None,
                 usd_budget=None, acceptance=None, verify="none"):
        self._client = client
        self._path = client.db_path
        self.task_id = task_id
        self.title = title
        self.payload = payload or {}
        self.owner = owner or f"sdk-{uuid.uuid4().hex[:6]}"
        self.lease_secs = lease_secs
        self.token_budget = token_budget
        self.usd_budget = usd_budget
        self.acceptance = acceptance
        self.verify = verify
        self._store = Store(self._path)
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread = None
        self._decomposed = False

    # ---------- context manager ----------
    def __enter__(self):
        if self.task_id is None:
            if not self.title:
                raise ValueError("lease() needs a title or a task_id")
            self.task_id = self._store.create_task(
                self.title, self.payload,
                token_budget=self.token_budget, usd_budget=self.usd_budget,
                acceptance=self.acceptance, verify=self.verify)
        task = self._store.claim_task(self.owner, self.lease_secs,
                                      task_id=self.task_id)
        if not task:
            raise RuntimeError(
                f"could not claim task {self.task_id} — already in progress?")
        self._thread = threading.Thread(target=self._beat, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None and not self._decomposed:
                self.complete("done")
            elif exc_type is QuotaWait:
                qw = exc
                self.park_until(qw.resume_at, qw.reason, qw.provider)
            elif not self._decomposed:
                self.fail(repr(exc))
        finally:
            self._stop.set()
            if self._thread:
                self._thread.join()
            self._store.close()
        return False  # never swallow the exception

    # ---------- heartbeat ----------
    def _beat(self):
        s = Store(self._path)
        try:
            while not self._stop.wait(self.lease_secs / 3):
                if not s.heartbeat(self.task_id, self.owner, self.lease_secs):
                    self._lost.set()
                    return
        finally:
            s.close()

    @property
    def alive(self):
        """False if another worker took over the lease — stop working."""
        return not self._lost.is_set()

    @property
    def stuck(self):
        """True if the watchdog flagged no-progress — time to replan."""
        t = self._store.get_task(self.task_id)
        return bool(t and t["stuck_level"] >= 1)

    def decompose(self, subtasks):
        """Split this leased task into subtasks and stand down.

        Each subtask is a dict: {"title", "payload", "acceptance",
        "token_budget", "usd_budget", "verify" (default: inherit this
        task's mode)}. The parent becomes `decomposed` — not claimable —
        and completes on its own when every child is done and its own
        acceptance verifies. Returns [child ids]. Exiting the context
        manager afterwards is safe: the exit hook skips complete()/fail()
        for a decomposed task.
        """
        kids = self._store.decompose(self.task_id, self.owner, subtasks)
        if kids is None:
            raise RuntimeError(
                f"could not decompose {self.task_id} — not your"
                " in_progress task, bad subtask specs, or nesting too deep")
        self._decomposed = True
        self._stop.set()  # decomposition releases the lease; stop beating
        return kids

    @property
    def on_hold(self):
        """True if the spend guardrail parked the task — stop working."""
        t = self._store.get_task(self.task_id)
        return bool(t and t["status"] == "budget_hold")

    # ---------- agent actions ----------
    def step(self, detail):
        """Record a unit of real progress (clears any stuck flag)."""
        self._store.step(self.task_id, detail)

    def note(self, detail):
        """A timeline note that is NOT progress (won't clear stuck)."""
        self._store.note(self.task_id, detail)

    def remember(self, key, value):
        """Durable memory, scoped to this task. Survives crashes."""
        self._store.remember(key, value, task_id=self.task_id)

    def recall(self, key):
        """Read back task-scoped memory (None if never written)."""
        return self._store.recall(key, scope=f"task:{self.task_id}")

    def park_until(self, resume_at, reason="", provider=""):
        """Deliberately pause until resume_at (quota window). Not a failure,
        not stuck — the watchdog resumes the task automatically."""
        self._store.park_waiting(self.task_id, self.owner, resume_at,
                                 reason, provider=provider)

    def report_usage(self, prompt_tokens=0, completion_tokens=0, usd=None):
        """Report model usage against the task's spend caps. Returns the
        usage dict — when it says on_hold, the guardrail parked the task
        and you must stop working immediately."""
        return self._store.report_usage(self.task_id, self.owner,
                                        prompt_tokens, completion_tokens,
                                        usd)

    def complete(self, result=""):
        """Ask to complete the task. Returns the verification verdict
        {'verified', 'criteria'} — when acceptance criteria fail, the
        task stays in_progress with your lease: fix what's missing and
        complete again."""
        return self._store.complete_task(self.task_id, self.owner, result)

    def acceptance_check(self, criterion_id, passed, evidence=""):
        """Report evidence for a manual acceptance criterion
        (e.g. 'reviewed the diff against the spec')."""
        return self._store.acceptance_check(self.task_id, self.owner,
                                            criterion_id, passed, evidence)

    def add_criteria(self, acceptance):
        """Append acceptance criteria to this task."""
        return self._store.add_acceptance_criteria(self.task_id, acceptance)

    def fail(self, reason=""):
        self._store.fail_task(self.task_id, self.owner, reason)


class ReviewClaim:
    """A claimed independent review with a live heartbeat.

    The verifier is always a different worker than the producer —
    claim_review() refuses the producer. Use as a context manager, then
    call verdict(True/False, reason). Exiting without a verdict releases
    the review for another verifier; the round is not consumed.
    """

    def __init__(self, client, task_id, verifier, lease_secs=60):
        self._client = client
        self._path = client.db_path
        self.task_id = task_id
        self.verifier = verifier
        self.lease_secs = lease_secs
        self._store = Store(self._path)
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread = None
        self.task = None

    def __enter__(self):
        self.task = self._store.claim_review(self.verifier, self.lease_secs,
                                             task_id=self.task_id)
        if not self.task:
            raise RuntimeError(
                f"could not claim review of {self.task_id} — not awaiting"
                " review, or you produced this work")
        self._thread = threading.Thread(target=self._beat, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is not None:
                self.release()  # errored mid-review: let another verifier try
        finally:
            self._stop.set()
            if self._thread:
                self._thread.join()
            self._store.close()
        return False

    def _beat(self):
        s = Store(self._path)
        try:
            while not self._stop.wait(self.lease_secs / 3):
                if not s.heartbeat(self.task_id, self.verifier,
                                   self.lease_secs):
                    self._lost.set()
                    return
        finally:
            s.close()

    def verdict(self, passed, reason=""):
        """Submit the verdict. True = pass (task done, verified_by you);
        False = fail (task re-queued with your notes in the timeline).
        Bounded: past 3 failed rounds the task fails as a passive
        record, never a push."""
        return self._store.submit_verdict(
            self.task_id, self.verifier,
            "pass" if passed else "fail", reason)

    def release(self):
        """Step down without a verdict — another verifier may claim it."""
        return self._store.release_review(self.task_id, self.verifier)


class Client:
    """Entry point. One client per database."""

    def __init__(self, db_path="stayonduty.db"):
        self.db_path = db_path
        # Touch the DB so a typo'd path fails fast, here, not mid-task.
        Store(db_path).close()

    def register(self, title, payload=None, token_budget=None,
                 usd_budget=None, acceptance=None, verify="none"):
        """Create a task without claiming it. Returns the task id.

        token_budget / usd_budget set spend guardrails: when reported
        usage exceeds either cap, the task parks itself in `budget_hold`
        (no attempt consumed) until the budget is raised.

        acceptance is a list of what 'done' looks like — plain strings
        ('50 pngs in out/') or dicts with check specs
        ({'description': ..., 'check': {'type': 'file_exists',
        'path': ...}}). complete() verifies them against the real
        artifact instead of trusting the claim.

        verify='independent': after the producer passes the deterministic
        gate, the task parks in `in_review` until a DIFFERENT worker
        reviews it (claim_review / verdict). The producer can never
        grade its own work.
        """
        s = Store(self.db_path)
        try:
            return s.create_task(title, payload,
                                 token_budget=token_budget,
                                 usd_budget=usd_budget,
                                 acceptance=acceptance,
                                 verify=verify)
        finally:
            s.close()

    def raise_budget(self, task_id, token_budget=None, usd_budget=None):
        """Raise (or remove, with None) the spend caps. Works on
        budget-held tasks (they return to the queue) and on decomposed
        parents (subtree caps — parked descendants resume when clear).
        Returns False for other statuses."""
        s = Store(self.db_path)
        try:
            return s.raise_budget(task_id, token_budget, usd_budget)
        finally:
            s.close()

    def lease(self, title=None, task_id=None, payload=None,
              owner=None, lease_secs=30, token_budget=None, usd_budget=None,
              acceptance=None, verify="none"):
        """Claim a task (or register + claim) with a live heartbeat.

        Use as a context manager — see the module docstring.
        token_budget/usd_budget/acceptance apply when a new task is
        registered. verify='independent' routes a passing completion
        through independent review before the task can be done.
        """
        return Lease(self, task_id=task_id, title=title, payload=payload,
                     owner=owner, lease_secs=lease_secs,
                     token_budget=token_budget, usd_budget=usd_budget,
                     acceptance=acceptance, verify=verify)

    def claim_review(self, task_id, verifier, lease_secs=60):
        """Claim the independent review of a task you did NOT produce.
        Returns a ReviewClaim (context manager) or raises RuntimeError.
        """
        return ReviewClaim(self, task_id, verifier, lease_secs=lease_secs)

    def decompose(self, task_id, owner, subtasks):
        """Split an in_progress task you hold into subtasks.

        subtasks: [{"title", "payload", "acceptance", "token_budget",
        "usd_budget", "verify" (default: inherit parent)}]. The parent
        becomes `decomposed` and auto-completes when every child is done
        and its own acceptance verifies. Returns [child ids] or None
        when refused.
        """
        s = Store(self.db_path)
        try:
            return s.decompose(task_id, owner, subtasks)
        finally:
            s.close()

    def children(self, task_id):
        """Direct subtasks of a task, oldest first."""
        s = Store(self.db_path)
        try:
            return s.children(task_id)
        finally:
            s.close()

    def reviews(self, task_id):
        """Review history for a task, oldest first."""
        s = Store(self.db_path)
        try:
            return s.reviews(task_id)
        finally:
            s.close()

    def recovery_feed(self, limit=20):
        """The 'handled on its own' journal as human sentences, newest
        first — what StayOnDuty fixed while nobody was watching."""
        s = Store(self.db_path)
        try:
            return s.recovery_sentences(limit=limit)
        finally:
            s.close()

    def status(self, task_id):
        """Task row + recent timeline, for inspection, not supervision."""
        s = Store(self.db_path)
        try:
            task = s.get_task(task_id)
            events = s.events(task_id)[-20:] if task else []
            return {"task": task, "recent_events": events}
        finally:
            s.close()

    def run_supervised(self, title, work_fn, payload=None, owner=None,
                       lease_secs=30, on_stuck=None, token_budget=None,
                       usd_budget=None, acceptance=None, verify="none",
                       review_policy=None):
        """One-shot: register, claim, heartbeat, run work_fn(ctx), complete.

        work_fn receives a worker.Ctx (step/note/remember/recall/stuck/
        park_waiting/report_usage/acceptance_check). Raises QuotaWait to
        park for a quota window. This is worker.run_one() with the task
        pre-registered for you.

        review_policy(view) -> (ok, reason): when given, this worker also
        judges in_review tasks it didn't produce before taking new work
        (independent-verifier path). See worker.provider_review_policy().
        """
        from .worker import run_one
        tid = self.register(title, payload, token_budget=token_budget,
                            usd_budget=usd_budget, acceptance=acceptance,
                            verify=verify)
        return run_one(self.db_path, owner or f"sdk-{uuid.uuid4().hex[:6]}",
                       work_fn, lease_secs=lease_secs, task_id=tid,
                       on_stuck=on_stuck, review_policy=review_policy)
