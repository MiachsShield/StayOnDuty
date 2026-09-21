"""Worker: recover -> claim -> heartbeat -> work -> complete.

Crash semantics: if the process dies, heartbeats stop, the lease expires,
and the next worker's recover_stale() puts the task back in the queue.
Memory written so far survives, so the new worker resumes instead of
restarting.
"""
from __future__ import annotations
import threading
import time
from .store import Store


class QuotaWait(Exception):
    """Raise from work_fn to park the task until a provider quota resets.

    run_one() catches this and parks the task in `waiting` instead of
    failing it. The worker exits; the watchdog promotes the task back to
    pending when the window elapses and a fresh worker resumes it.
    """

    def __init__(self, resume_at, reason="", provider=""):
        super().__init__(reason)
        self.resume_at = resume_at
        self.reason = reason
        self.provider = provider  # display name, e.g. "Grok"


class Ctx:
    """What the work function gets: task + memory + liveness."""

    def __init__(self, store, task, owner, lost, stuck):
        self.store = store
        self.task = task
        self.owner = owner
        self._lost = lost
        self._stuck = stuck

    @property
    def id(self):
        return self.task["id"]

    def alive(self):
        """False if another worker took over the lease — stop working."""
        return not self._lost.is_set()

    def stuck(self):
        """True if the watchdog flagged no-progress — time to replan."""
        return self._stuck.is_set()

    def step(self, detail):
        print(f"  [step] {detail}", flush=True)
        self.store.step(self.id, detail)

    def note(self, detail):
        """A timeline note that is NOT progress (won't clear a stuck flag)."""
        print(f"  [note] {detail}", flush=True)
        self.store.note(self.id, detail)

    def park_waiting(self, resume_at, reason="", provider=""):
        """Park this task until resume_at (quota window). Returns from the
        work function afterwards — run_one() will not complete the task."""
        when = time.strftime("%H:%M:%S", time.localtime(resume_at))
        print(f"  [waiting] {reason} — resumes ~{when}", flush=True)
        return self.store.park_waiting(self.id, self.owner, resume_at, reason,
                                       provider=provider)

    def report_usage(self, prompt_tokens=0, completion_tokens=0, usd=None):
        """Report model usage against the task's spend caps. Returns the
        usage dict — when it says on_hold, the guardrail parked the task
        and you must stop working immediately."""
        r = self.store.report_usage(self.id, self.owner, prompt_tokens,
                                    completion_tokens, usd)
        if r.get("ok"):
            print(f"  [spend] {r['tokens_used']} tokens used"
                  + (f" (${r['usd_used']:.4f})" if usd else "")
                  + (" — ON HOLD, standing down" if r.get("on_hold") else ""),
                  flush=True)
        else:
            print(f"  [spend] rejected: {r.get('reason')}", flush=True)
        return r

    def remember(self, key, value):
        self.store.remember(key, value, task_id=self.id)

    def recall(self, key):
        return self.store.recall(key, scope=f"task:{self.id}")

    def acceptance_check(self, criterion_id, passed, evidence=""):
        """Report evidence for a manual acceptance criterion
        (e.g. 'reviewed the diff against the spec')."""
        return self.store.acceptance_check(self.id, self.owner,
                                           criterion_id, passed, evidence)

    def decompose(self, subtasks):
        """Split this task into subtasks (see Store.decompose): each gets
        its own acceptance criteria, lease, memory scope, and spend caps;
        this task becomes `decomposed` and run_one will NOT try to
        complete it afterwards — it resolves on its own when every child
        is done. Returns [child ids]."""
        kids = self.store.decompose(self.id, self.owner, subtasks)
        if kids is None:
            raise RuntimeError(
                f"could not decompose {self.id} — not your in_progress"
                " task, bad subtask specs, or nesting too deep")
        return kids


def _heartbeat_loop(path, task_id, owner, lease_secs, stop, lost, stuck,
                    on_stuck, ctx):
    s = Store(path)
    notified = False
    try:
        while not stop.wait(lease_secs / 3):
            if not s.heartbeat(task_id, owner, lease_secs):
                lost.set()
                return
            if not notified:
                t = s.get_task(task_id)
                if t and t["stuck_level"] >= 1:
                    notified = True
                    stuck.set()  # work_fn can also poll ctx.stuck()
                    s.note(task_id, "stuck flag seen — replanning approach")
                    print("[stuck] no progress detected — replanning", flush=True)
                    if on_stuck:
                        try:
                            on_stuck(ctx)
                        except Exception as e:  # noqa: BLE001
                            print(f"[stuck] on_stuck failed: {e}", flush=True)
    finally:
        s.close()


def build_review_prompt(view):
    """Format an independent-review brief for a provider.judge() call.

    view: {'title','producer_owner','round','criteria': [{'id',
    'description','recheck','evidence'}]}. The prompt instructs the judge
    to trust only demonstrated evidence and to flag SELF-REVIEW.
    """
    lines = [
        f"Task: {view['title']}",
        f"Producer: {view['producer_owner']} (you are NOT the producer;"
        " you are the independent reviewer)",
        f"Review round: {view['round']}",
        "",
        "For each acceptance criterion, the deterministic re-check result",
        "and the producer's recorded evidence are shown. Judge ONLY what",
        "the evidence demonstrates. A bare assertion ('I checked', 'looks",
        "good', 'LGTM', 'trust me') with no artifact, command output, or",
        "named reviewer is self-review — when you see it, start that line",
        "of your reasoning with the marker SELF-REVIEW followed by a colon.",
        "",
    ]
    for c in view["criteria"]:
        lines.append(f"- [{c['id']}] {c['description']}")
        lines.append(f"  deterministic re-check: {c['recheck']}")
        lines.append(f"  producer evidence: {c['evidence'] or '(none recorded)'}")
    lines += ["", "First line of your reply: PASS or FAIL. Then your reasoning."]
    return "\n".join(lines)


def provider_review_policy(provider):
    """Turn a provider with judge() into a run_one review_policy."""
    from .providers.base import parse_judge_verdict

    def policy(view):
        res = provider.judge(build_review_prompt(view))
        return parse_judge_verdict(res.text)

    return policy


def _run_review_once(store, path, owner, review_policy, lease_secs):
    """Claim one in_review task (never our own) and judge it. Returns the
    task id, or None when no review was available."""
    rev = store.claim_review(owner, lease_secs * 2)
    if not rev:
        return None
    print(f"[review] {rev['id']} '{rev['title']}'"
          f" (producer={rev['producer_owner']},"
          f" round {rev['review_rounds'] + 1})", flush=True)
    stop, lost, stuck = threading.Event(), threading.Event(), threading.Event()
    hb = threading.Thread(
        target=_heartbeat_loop,
        args=(path, rev["id"], owner, lease_secs * 2, stop, lost, stuck,
              None, None),
        daemon=True)
    hb.start()
    try:
        recheck = store.verify_acceptance(rev["id"])
        view = {
            "id": rev["id"],
            "title": rev["title"],
            "producer_owner": rev["producer_owner"],
            "round": rev["review_rounds"] + 1,
            "criteria": [{
                "id": c["id"],
                "description": c["description"],
                "recheck": c["status"],
                "evidence": c["evidence"],
            } for c in recheck["criteria"]],
        }
        ok, reason = review_policy(view)
    except Exception as e:  # noqa: BLE001
        store.release_review(rev["id"], owner)
        print(f"[review] {rev['id']} policy errored ({e}) — released for"
              " another verifier", flush=True)
        return rev["id"]
    finally:
        stop.set()
        hb.join()
    if lost.is_set():
        print("[review] lost lease mid-review; leaving it", flush=True)
        return rev["id"]
    verdict = "pass" if ok else "fail"
    if store.submit_verdict(rev["id"], owner, verdict, str(reason or "")):
        print(f"[review] {rev['id']} -> {verdict.upper()}:"
              f" {str(reason)[:140]}", flush=True)
    else:
        print(f"[review] {rev['id']} verdict not accepted (lease lost?)",
              flush=True)
    return rev["id"]


def run_one(path, owner, work_fn, lease_secs=30, task_id=None, on_stuck=None,
            review_policy=None):
    """Recover stale leases, claim one task, run it. Returns task id or None.

    on_stuck(ctx) fires once when the watchdog flags no-progress, so the
    worker can replan. The flag clears automatically on the next step().

    review_policy(view) -> (ok, reason): when given, the worker first
    tries to claim an in_review task it did NOT produce and judges it —
    the independent-verifier path. The producer can never review its
    own work. Use provider_review_policy(provider) to wire a judge.

    If work_fn raises QuotaWait (or parks via ctx.park_waiting), the task
    goes to `waiting` until the quota window elapses instead of
    completing or failing; the watchdog promotes it back to pending.
    """
    store = Store(path)
    try:
        res = store.recover_stale()
        if res["released"] or res["failed"]:
            print(f"[recover] released {res['released']} stale task(s),"
                  f" {res['failed']} failed (attempts exhausted)", flush=True)
        if review_policy is not None:
            rid = _run_review_once(store, path, owner, review_policy,
                                   lease_secs)
            if rid:
                return rid
        task = store.claim_task(owner, lease_secs, task_id)
        if not task:
            print("[idle] nothing to do", flush=True)
            return None
        print(f"[claim] {task['id']} '{task['title']}' (attempt #{task['attempts'] + 1})",
              flush=True)
        stop, lost, stuck = threading.Event(), threading.Event(), threading.Event()
        ctx = Ctx(store, task, owner, lost, stuck)
        hb = threading.Thread(target=_heartbeat_loop,
                              args=(path, task["id"], owner, lease_secs, stop,
                                    lost, stuck, on_stuck, ctx),
                              daemon=True)
        hb.start()
        try:
            work_fn(ctx)
        except QuotaWait as qw:
            store.park_waiting(task["id"], owner, qw.resume_at, qw.reason,
                               provider=qw.provider)
            when = time.strftime("%H:%M:%S", time.localtime(qw.resume_at))
            print(f"[waiting] {task['id']}: {qw.reason} — resumes ~{when}",
                  flush=True)
        except Exception as e:  # noqa: BLE001
            store.fail_task(task["id"], owner, repr(e))
            print(f"[fail] {task['id']}: {e}", flush=True)
        else:
            cur = store.get_task(task["id"])
            if cur and cur["status"] == "waiting":
                print(f"[waiting] {task['id']} parked by worker"
                      f" — watchdog will resume it on schedule", flush=True)
            elif cur and cur["status"] == "decomposed":
                print(f"[decomposed] {task['id']} split into subtasks —"
                      f" the parent resolves on its own when they finish",
                      flush=True)
            elif lost.is_set():
                print("[warn] lost lease mid-task; leaving it for another worker",
                      flush=True)
            else:
                verdict = store.complete_task(task["id"], owner, "done")
                if verdict.get("in_review"):
                    print(f"[review] {task['id']} passed the gate — parked in"
                          " in_review for an independent verifier", flush=True)
                elif verdict["verified"]:
                    print(f"[done] {task['id']}", flush=True)
                else:
                    missing = [c["id"] for c in verdict["criteria"]
                               if c["status"] != "passed"]
                    print(f"[verify] {task['id']} not done — failing: "
                          f"{', '.join(missing)}; leaving it for another"
                          f" worker with the evidence in the timeline",
                          flush=True)
        finally:
            stop.set()
            hb.join()
        return task["id"]
    finally:
        store.close()
