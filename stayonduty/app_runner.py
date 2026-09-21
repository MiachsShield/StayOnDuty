"""StayOnDuty app runner — the background worker behind the v1 web app.

A supervisor thread that keeps the app's tasks moving with zero human
supervision:

  every few seconds: recover stale leases, promote quota-waited tasks
      whose window elapsed, and run one pending app task to completion
  every ~30s:          run stuck detection (nudge -> fresh attempt ->
      give up with diagnostics)

App tasks are the ones created through the web UI ("New task"). Each
carries its model choice in the payload; the runner resolves it to a
provider using the API keys in the settings table. A task whose model
has no key is NEVER claimed — it sits in a calm "needs a key" state
until the user adds one in Settings. No crashes, no attempts burned.

Run standalone for debugging:
    python3 -m stayonduty.app_runner <db>
"""
from __future__ import annotations
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from stayonduty.store import Store  # noqa: E402
from stayonduty.worker import run_one, QuotaWait  # noqa: E402
from stayonduty.providers.base import (ProviderError, AuthError,  # noqa: E402
                                       QuotaExhausted)
from stayonduty.providers.anthropic import AnthropicProvider  # noqa: E402
from stayonduty.providers.xai import XAIProvider  # noqa: E402
from stayonduty.providers.openai import OpenAIProvider  # noqa: E402

# model id -> (settings key holding its API key, provider class, human label)
MODELS = {
    "claude": ("key_anthropic", AnthropicProvider, "Claude"),
    "grok": ("key_xai", XAIProvider, "Grok"),
    "chatgpt": ("key_openai", OpenAIProvider, "ChatGPT"),
}
MODEL_IDS = tuple(MODELS)
DEFAULT_MODEL_CHOICE = "auto"  # "auto" | one of MODEL_IDS

SYSTEM_PROMPT = (
    "You are a helpful assistant working on a task for the user. "
    "Do the task as completely as you can in one focused response, "
    "then report the result clearly. If the task cannot be done, say so "
    "plainly and explain what is missing instead of guessing."
)


def resolve_model(model_choice, store):
    """Pick a concrete model id for a task's choice.

    'auto' uses the user's default model when it has a key, otherwise the
    first model with a key. Returns the model id, or None when no key is
    configured anywhere — the task must wait, not crash.
    """
    if model_choice in MODELS:
        return model_choice if _has_key(model_choice, store) else None
    order = []
    default = store.get_setting("default_model", "auto")
    if default in MODELS:
        order.append(default)
    for mid in MODEL_IDS:
        if mid not in order:
            order.append(mid)
    for mid in order:
        if _has_key(mid, store):
            return mid
    return None


def _has_key(model_id, store):
    setting_key = MODELS[model_id][0]
    return bool((store.get_setting(setting_key) or "").strip())


def provider_for(model_id, store):
    """Build the provider for a resolved model id. The key is read from
    the settings database and passed transiently — never logged, never
    stored anywhere else."""
    setting_key, cls, label = MODELS[model_id]
    key = (store.get_setting(setting_key) or "").strip()
    if not key:
        raise AuthError(f"No API key saved for {label} — add one in Settings.")
    return cls(api_key=key), label


def needs_key(task, store):
    """True when an app task can't run yet because its model has no API
    key. These tasks are left alone (pending) — claiming them would burn
    attempts on something the user hasn't set up yet."""
    payload = task.get("payload") or {}
    if not payload.get("app"):
        return False
    return resolve_model(payload.get("model", "auto"), store) is None


def _usage_tokens(result):
    """Normalize provider usage metadata to (prompt, completion) tokens."""
    usage = (result.meta or {}).get("usage") or {}
    prompt = usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0
    completion = (usage.get("completion_tokens",
                            usage.get("output_tokens", 0)) or 0)
    try:
        return int(prompt), int(completion)
    except (TypeError, ValueError):
        return 0, 0


def make_work_fn(provider, label):
    """One focused provider pass per task: ask, record, report spend."""
    def work(ctx):
        payload = ctx.task.get("payload") or {}
        prompt = payload.get("prompt") or ctx.task["title"]
        ctx.step(f"Sent your request to {label}")
        result = None
        last_error = None
        for attempt in (1, 2, 3):
            try:
                result = provider.generate(
                    prompt, kind="text", system=SYSTEM_PROMPT,
                    max_tokens=2000, temperature=0.7)
                last_error = None
                break
            except QuotaExhausted as qe:
                raise QuotaWait(
                    qe.reset_at,
                    f"{label} is at its usage limit — resuming on its own"
                    " when the window resets",
                    provider=label)
            except AuthError:
                # Wrong key: retrying is pointless. The note tells the
                # user exactly what to do; the task fails as a passive
                # record, never a push.
                ctx.note(f"{label} didn't accept the API key — check it in"
                         " Settings, then start the task again.")
                raise
            except ProviderError as e:
                last_error = e
                if attempt < 3:
                    ctx.note("Ran into a hiccup — trying again on its own.")
                    time.sleep(2 ** attempt)
        if result is None:
            ctx.note("Kept hitting a wall — leaving the full story here.")
            raise last_error
        ctx.step(f"{label} answered")
        ctx.remember("result", result.text)
        prompt_toks, completion_toks = _usage_tokens(result)
        if prompt_toks or completion_toks:
            ctx.report_usage(prompt_tokens=prompt_toks,
                             completion_tokens=completion_toks)
    return work


def _claimable_app_tasks(store):
    """Pending root tasks created by the app, oldest first."""
    rows = store.db.execute(
        "SELECT * FROM tasks WHERE status='pending' AND parent_id IS NULL"
        " ORDER BY created_at").fetchall()
    out = []
    for r in rows:
        t = store.get_task(r["id"])
        if t and (t.get("payload") or {}).get("app"):
            out.append(t)
    return out


def run_one_app_task(db_path, store):
    """Claim and run a single app task that has everything it needs.
    Returns the task id, or None when there was nothing runnable."""
    for t in _claimable_app_tasks(store):
        if needs_key(t, store):
            continue  # waits calmly for a key — never claimed, never burned
        model_id = resolve_model((t.get("payload") or {}).get("model", "auto"),
                                 store)
        if model_id is None:
            continue
        try:
            provider, label = provider_for(model_id, store)
        except AuthError:
            continue  # key vanished between check and build — stays pending
        work_fn = make_work_fn(provider, label)
        print(f"[app-runner] {t['id']} '{t['title']}' -> {label}", flush=True)
        return run_one(db_path, owner="app-runner", work_fn=work_fn,
                       lease_secs=30, task_id=t["id"])
    return None


def supervisor_loop(db_path, stop=None, tick_secs=5):
    """The background loop. Runs until stop is set (or Ctrl-C standalone).

    Every tick: recover stale leases, promote elapsed quota waits, run
    one app task. Every ~30s: stuck detection with app-sized thresholds
    (a single provider call under 15 minutes never looks stuck).
    """
    store = Store(db_path)
    stop = stop or threading.Event()
    tick = 0
    print(f"[app-runner] supervising {db_path} — user is never notified",
          flush=True)
    try:
        while not stop.wait(tick_secs):
            tick += 1
            try:
                res = store.recover_stale(max_attempts=3)
                if res["released"] or res["failed"]:
                    print(f"[app-runner] recovered {res['released']} stale,"
                          f" {res['failed']} failed", flush=True)
                for t in store.promote_waiting():
                    print(f"[app-runner] resumed '{t['title']}' — quota"
                          f" window elapsed", flush=True)
                run_one_app_task(db_path, store)
                if tick % 6 == 0:
                    det = store.detect_stuck(stuck_after_secs=900,
                                             escalate_after_secs=3600,
                                             max_attempts=3)
                    for t in det["stuck"]:
                        print(f"[app-runner] stuck: '{t['title']}' — nudged",
                              flush=True)
                    for t in det["requeued"]:
                        print(f"[app-runner] auto-retry queued:"
                              f" '{t['title']}'", flush=True)
                    for t in det["failed"]:
                        print(f"[app-runner] gave up: '{t['title']}' —"
                              f" diagnostics saved", flush=True)
            except Exception as e:  # noqa: BLE001 — the loop must not die
                # Never log settings values here; e carries no keys by
                # construction (providers redact them).
                print(f"[app-runner] loop hiccup ({type(e).__name__}):"
                      f" {e}", flush=True)
    except KeyboardInterrupt:
        print("\n[app-runner] stopped", flush=True)
    finally:
        store.close()


if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else "stayonduty-app.db"
    supervisor_loop(db)
