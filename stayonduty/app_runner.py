"""StayOnDuty app runner — the background worker behind the v1 web app.

A supervisor thread that keeps the app's tasks moving with zero human
supervision:

  every few seconds: recover stale leases, promote quota-waited tasks
      whose window elapsed, and run one pending app task to completion
  every ~30s:          run stuck detection (nudge -> fresh attempt ->
      give up with diagnostics)

App tasks are the ones created through the web UI ("New task"). Each
carries its model choice in the payload; the runner resolves it to a
provider using the credentials in the settings table — a one-tap Google
sign-in for Gemini (preferred), or a pasted API key per provider. A
task whose model isn't connected is NEVER claimed — it sits in a calm
"needs a key" state until the user connects it in Settings. No crashes,
no attempts burned.

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
from stayonduty.providers.gemini import GeminiProvider  # noqa: E402
from stayonduty import google_oauth  # noqa: E402

# model id -> (settings key holding its API key, provider class, human label)
# gemini's settings key is None: it connects with one tap through Google
# OAuth ("Continue with Google"); a pasted key_gemini is the fallback.
MODELS = {
    "gemini": (None, GeminiProvider, "Gemini"),
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


def is_connected(model_id, store):
    """True when a model can actually run: a saved API key, or — for
    Gemini — a completed Google sign-in."""
    if model_id == "gemini":
        return (google_oauth.google_connected(store)
                or bool((store.get_setting("key_gemini") or "").strip()))
    setting_key = MODELS[model_id][0]
    return bool((store.get_setting(setting_key) or "").strip())


def _has_key(model_id, store):
    return is_connected(model_id, store)


def provider_for(model_id, store):
    """Build the provider for a resolved model id. Credentials are read
    from the settings database and passed transiently — never logged,
    never stored anywhere else."""
    setting_key, cls, label = MODELS[model_id]
    if model_id == "gemini":
        # One-tap Google sign-in first (token refreshed proactively),
        # pasted API key as the advanced fallback.
        if google_oauth.google_connected(store):
            return cls(access_token=google_oauth.get_valid_access_token(
                store)), label
        key = (store.get_setting("key_gemini") or "").strip()
        if not key:
            raise AuthError(
                "Gemini isn't connected — tap 'Continue with Google' in"
                " Settings (one tap, free).")
        return cls(api_key=key), label
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


def connected_models_in_order(preferred, store):
    """Model ids with working credentials: the preferred (auto-resolved)
    model first, then every other connected model. A task walks this list
    so a broken lane hands off to the next helper instead of dying."""
    order = []
    if preferred in MODELS:
        order.append(preferred)
    for mid in MODEL_IDS:
        if mid not in order:
            order.append(mid)
    return [mid for mid in order if is_connected(mid, store)]


def make_work_fn(model_ids, store):
    """One focused pass per task, with cross-model failover.

    The task is tried on each connected model in order. When a lane is
    broken (provider errors, bad key) the discussion continues with
    another LLM on its own. A quota wait stays a calm wait — we never
    burn a second model's allowance just because the first is resting.
    """
    def work(ctx):
        payload = ctx.task.get("payload") or {}
        prompt = payload.get("prompt") or ctx.task["title"]
        last_error = None
        for i, mid in enumerate(model_ids):
            _, _, label = MODELS[mid]
            next_label = MODELS[model_ids[i + 1]][2] \
                if i + 1 < len(model_ids) else None
            try:
                provider, _ = provider_for(mid, store)
            except AuthError:
                # Key vanished between the check and the build — skip
                # this lane, don't burn the task on it.
                last_error = AuthError(f"{label} credentials missing")
                if next_label:
                    ctx.note(f"{label} isn't connected right now — "
                             f"{next_label} is picking it up instead.")
                continue
            ctx.step(f"Sent your request to {label}")
            result = None
            for attempt in (1, 2, 3):
                try:
                    result = provider.generate(
                        prompt, kind="text", system=SYSTEM_PROMPT,
                        max_tokens=2000, temperature=0.7)
                    last_error = None
                    break
                except QuotaExhausted as qe:
                    # Resting lane, not a broken one: wait calmly for the
                    # free allowance instead of spending another model's.
                    wait_msg = (getattr(provider, "QUOTA_WAIT_MESSAGE", None)
                                or f"{label} is at its usage limit — resuming"
                                   " on its own when the window resets")
                    raise QuotaWait(qe.reset_at, wait_msg, provider=label)
                except AuthError:
                    # Wrong key: retrying this lane is pointless — hand
                    # the discussion to the next helper.
                    last_error = AuthError(f"{label} rejected its key")
                    if next_label:
                        ctx.note(f"{label} didn't accept its saved key — "
                                 f"{next_label} is picking it up instead. "
                                 f"The {label} key may need a check in "
                                 "Settings when you get a moment.")
                    break
                except ProviderError as e:
                    last_error = e
                    if attempt < 3:
                        ctx.note("Ran into a hiccup — trying again on its own.")
                        time.sleep(2 ** attempt)
            if result is not None:
                if i > 0:
                    ctx.note(f"{label} picked it up and finished it.")
                ctx.step(f"{label} answered")
                ctx.remember("result", result.text)
                prompt_toks, completion_toks = _usage_tokens(result)
                if prompt_toks or completion_toks:
                    ctx.report_usage(prompt_tokens=prompt_toks,
                                     completion_tokens=completion_toks)
                return
            if next_label and last_error is not None:
                ctx.note(f"{label} hit a wall — {next_label} is continuing"
                         " from here.")
        ctx.note("Every connected helper hit a wall — the full story is"
                 " saved here, and it'll be retried on its own.")
        raise last_error if last_error is not None else ProviderError(
            "no connected model available")
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
        preferred = resolve_model(
            (t.get("payload") or {}).get("model", "auto"), store)
        if preferred is None:
            continue
        model_ids = connected_models_in_order(preferred, store)
        if not model_ids:
            continue  # nothing connected — waits calmly, never burned
        work_fn = make_work_fn(model_ids, store)
        labels = " / ".join(MODELS[m][2] for m in model_ids)
        print(f"[app-runner] {t['id']} '{t['title']}' -> {labels}", flush=True)
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
