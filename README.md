# StayOnDuty

**The agent stays on duty.** External memory + task persistence for AI agents,
so long-running work never stalls silently. The complaint it kills:
"I told it to keep doing something and when I checked back, it's not doing anything."

## Install

```bash
pip install stayonduty
```

Quick start (any agent, including the one reading this):

```python
from stayonduty import Client
client = Client("work.db")
with client.lease("my long task") as job:
    job.step("did a thing")
    job.remember("progress", 42)
```

Or as an MCP server any MCP-capable agent can discover on its own:

```bash
stayonduty-mcp --db work.db
```

Then watch it work: `python -m stayonduty.dashboard work.db --port 8080`.

## How it works

- **Task ledger** (SQLite): tasks move `pending -> in_progress -> done/failed`.
  Every transition and work step is appended to an event log.
- **Leases + heartbeats**: a worker claims a task with a lease and renews it
  via heartbeat. If heartbeats stop, the lease expires and the next worker's
  `recover_stale()` returns the task to the queue (attempts += 1).
- **Memory**: workers `remember()` durable facts (scoped globally or per
  task). Memory survives crashes, so the next worker *resumes* instead of
  restarting — it skips work already recorded as done.
- **Dashboard**: a zero-dependency web UI answering "is your agent actually
  working?" — task statuses, lease health, event timelines, memory.
  Plus a JSON API (`/api/tasks`, `/api/task?id=`) for future clients.

## Iteration 1 — crash-safe core

```
cd ~/workspace/stayonduty
python3 -m stayonduty.demo
```

Phase 1: the agent delivers 2 of 5 packages, then dies via `os._exit(1)`
(no cleanup — a true crash). Phase 2: the agent restarts, the expired lease
is recovered, and it resumes at package 3 using the memory left behind.

## Iteration 2 — liveness dashboard

```
cd ~/workspace/stayonduty
python3 -m stayonduty.dashboard demo.db --port 8080
# open http://localhost:8080/
```

Green = working, orange STALE = heartbeat missed (the exact failure the app
exists to catch), with per-task timelines and memory. Auto-refreshes every 5s.

## Iteration 3 — stuck detection (autonomy cut)

A worker can heartbeat forever while making zero progress. The watchdog
closes that hole — without ever bothering the user:

```
cd ~/workspace/stayonduty
python3 -m stayonduty.watchdog demo.db --stuck-after 600 --escalate-after 1800 --max-attempts 3 --interval 30
# demo: python3 -m stayonduty.demo_stuck
```

- **Level 1 — stuck:** no `step`/`memory` progress for N seconds → task flagged
  STUCK (purple pill), worker is nudged to replan on its next heartbeat
  (`on_stuck` callback + `ctx.stuck()` for work functions).
- **Level 2 — auto-retry:** still no progress after M more seconds → the task
  is released back to pending for a fresh attempt (attempts+1). The stuck
  worker loses its lease and stands down; a new worker picks it up.
- **Level 3 — give up with diagnostics:** attempts exhausted → marked failed
  WITH the full timeline preserved. A passive record, never a push.
- **Automatic recovery:** the next `step()` or `remember()` clears the stuck
  flag (`unstuck` event) — no manual reset needed.
- **Crash loops cost attempts too:** a task whose worker keeps dying gets
  fresh attempts on its own up to `--max-attempts`, then fails with
  diagnostics instead of looping forever.

Product rule: the dashboard is pull-only. Alerts are a passive log
("Handled on its own" / "Couldn't finish on its own") for when the user
checks in — nothing ever pushes. A problem that flags the user is a
product failure.

## Iteration 4 — provider supervision + quota waits

StayOnDuty is not a do-anything agent — it is a supervision layer over
existing AI assistants (Grok, Claude, GPT). The worker drives a
third-party model API to complete a long automatic task
("generate 50 images per these specs until usage caps"), paces work
across quota limits, and verifies each output. BYOK: you supply the
provider API key; StayOnDuty never pays for inference.

```
cd ~/workspace/stayonduty
python3 -m stayonduty.demo_provider   # dashboard live at :18081 during the run
```

- **Providers** (`stayonduty/providers/`): `base.py` defines the interface
  (`generate()` -> result with output bytes/text, `quota_status()` ->
  remaining + reset time). `xai.py` drives Grok via xAI's
  OpenAI-compatible REST API (chat + image generation, stdlib urllib).
  `fake.py` is a scripted fake for tests: real PNGs built with zlib,
  scriptable corrupt outputs and quota windows.
- **Quota-wait state:** a task deliberately waiting for a quota reset is
  parked as `waiting` with `resume_at` — this is a pause, not a stall.
  `detect_stuck()` skips waiting tasks entirely, the watchdog promotes
  them back to `pending` when the window elapses, and promotion does NOT
  consume an attempt. Dashboard shows a blue WAITING pill with the
  reset time.
- **Verifier** (`stayonduty/verify.py`, stdlib only): magic bytes
  (PNG/JPEG), dimension parsing, minimum size, blank/single-color
  heuristic. Corrupt or blank outputs are retried, not silently kept.
  A `spec_check` hook is reserved for a future model-graded
  "does it match the spec?" check.
- **Autonomy doctrine holds:** quota exhaustion is routine, not a
  failure — no flags, no pushes. Genuine failures (bad output twice)
  become passive timeline records with diagnostics.

### Pointing it at xAI (Grok)

```bash
export XAI_API_KEY="..."   # from https://console.x.ai — never hardcode it
python3 - <<'EOF'
from stayonduty.providers.xai import XAIProvider
grok = XAIProvider()  # reads XAI_API_KEY; override models via
                      # XAI_CHAT_MODEL / XAI_IMAGE_MODEL env vars
img = grok.generate("a brutalist concrete tower at dusk", kind="image")
open("tower.png", "wb").write(img.image_bytes)
EOF
```

## Layout

- `stayonduty/store.py` — SQLite store: tasks, events, memory
- `stayonduty/worker.py` — claim/heartbeat/complete loop + worker context
- `stayonduty/runner.py` — demo workload (package delivery)
- `stayonduty/demo.py` — end-to-end crash/recovery demonstration
- `stayonduty/dashboard.py` — liveness web UI + JSON API (stdlib only)
- `stayonduty/watchdog.py` — stale-lease recovery + stuck detection loop
- `stayonduty/demo_stuck.py` — end-to-end stuck/replan/escalate/recover demo
- `stayonduty/providers/base.py` — provider interface (generate/quota_status)
- `stayonduty/providers/xai.py` — Grok driver via xAI's REST API (BYOK)
- `stayonduty/providers/fake.py` — scripted fake provider for tests/demos
- `stayonduty/verify.py` — stdlib image verifier (format/dims/blank checks)
- `stayonduty/demo_provider.py` — end-to-end provider/quota/verify demo
- `stayonduty/claude_code/` — Claude Code integration: installer, hooks, README

## What's next (later iterations)

- Canary verification: fully validate output #1 before spending budget on
  a batch of N (pairs with decomposition for batch work)
- Reflexion-style durable failure lessons: failures become memory the
  next attempt reads
- More providers: Claude / GPT drivers behind the same interface
- Sync: local SQLite -> cloud backend so state follows the user (and the phone)

## Iteration 5 — agent-facing surface (the YouTube play)

StayOnDuty is infrastructure: agents use it automatically, the way videos
use YouTube. Two surfaces, same engine, zero dependencies:

**Python SDK** — any agent, any framework, two lines:

```python
from stayonduty import Client

client = Client("work.db")
with client.lease("generate 50 card faces") as job:
    start = (job.recall("last_index") or -1) + 1
    for i in range(start, 50):
        make_card_face(i)
        job.step(f"card face {i}")    # real progress the watchdog can see
        job.remember("last_index", i) # checkpoint — survives crashes
```

If the process dies, the lease expires, another worker recovers the task,
recalls `last_index`, and resumes — nothing redone, nobody paged.
`job.stuck` tells the worker when the watchdog flags no-progress (time to
replan); `job.alive` goes false if another worker took over (stop working).
Raise `QuotaWait(resume_at, ...)` to park for a provider quota window.

**MCP server** — 17 tools any MCP-capable agent discovers on its own:

```
python3 -m stayonduty.mcp_server --db work.db
```

```json
{ "mcpServers": { "stayonduty": {
    "command": "python3",
    "args": ["-m", "stayonduty.mcp_server", "--db", "/path/to/work.db"],
    "cwd": "/path/to/stayonduty" } } }
```

Tools: `register_task`, `claim_task`, `heartbeat`, `step`, `note`,
`remember`, `recall`, `park_waiting`, `complete`, `fail`, `task_status`,
`report_usage`, `raise_budget`, `acceptance_check`, `claim_review`,
`submit_verdict`, `reviews`.
The agent's standing instruction is one sentence: "For any task running
longer than a few minutes, register it with StayOnDuty — say what 'done'
looks like as acceptance criteria — heartbeat while you work, checkpoint
with step() and remember()."

**End-to-end proof** (a foreign agent over raw JSON-RPC stdio — crash,
resume from memory, completion, zero human involvement):

```
cd ~/workspace/stayonduty
python3 -m stayonduty.demo_agent_surface
```

## Iteration 6 — spend guardrails (the $47k-runaway stories can't happen)

Per-task token and USD caps. The worker reports usage (`job.report_usage`
/ `stayonduty_report_usage`); when a cap is exceeded the task parks itself
in `budget_hold` — lease released, worker stood down, a passive dashboard
notice logged. Like `waiting`, a hold consumes no attempts and is never a
push notification; unlike `waiting`, nothing auto-resumes it — the budget
is raised pull-only (dashboard form, SDK, or MCP) and the task goes back
to the queue with its checkpointed progress intact.

```python
client = Client("work.db")
tid = client.register("generate 50 card faces", token_budget=200_000)
with client.lease(task_id=tid) as job:
    for i in range(start, 50):
        make_card_face(i)
        job.step(f"card face {i}")
        r = job.report_usage(prompt_tokens=800, completion_tokens=1200)
        if r["on_hold"]:
            break  # guardrail parked the task — stop spending
# later, after raising the cap: a fresh worker resumes from memory
client.raise_budget(tid, token_budget=500_000)
```

```
python3 -m stayonduty.demo_spend_guardrail
```

Proven: trip mid-task → airtight hold (no spend accepted, watchdog and
crash recovery ignore it) → budget raised → fresh worker resumes from
`last_index` → done. Token and USD caps, SDK + MCP + dashboard surfaces.

## Iteration 7 — acceptance criteria (a claim is not proof)

The biggest quality gap in the pro-technique review: tasks used to close
because a worker *said* "done". Now every task can register with a
definition of done, and `complete()` verifies it against the real artifact
instead of trusting the claim.

Designed for zero-friction agent adoption — the YouTube play applied to
quality: stating what "done" looks like costs the agent one plain-language
list, and StayOnDuty does the hard part (verification, refusal with exact
missing evidence, re-check on retry). The agent's life gets easier, not
harder: it never has to build its own checklist, test harness, or
"did I actually finish?" ritual again.

```python
client = Client("work.db")
with client.lease("build greeting widget",
                  acceptance=["output.txt exists",
                              {"description": "smoke passes",
                               "check": {"type": "command",
                                         "cmd": "python3 smoke.py"}}]) as job:
    ...do the work...
    verdict = job.complete("done")
    if not verdict["verified"]:
        # task stays in_progress with your lease — fix what's missing
        missing = [c["id"] for c in verdict["criteria"]
                   if c["status"] != "passed"]
```

Built-in check types (stdlib only, run at the prove-it moment, evidence
recorded): `file_exists`, `file_contains` (text or regex), `command`
(exit 0), `image_valid` (format/dimensions/non-blank). A plain string is a
*manual* criterion — the agent reports evidence with
`job.acceptance_check(id, passed, evidence)` (`stayonduty_acceptance_check`
over MCP). An unevidenced manual criterion **fails**: a sentence is not
proof.

When verification fails, the task stays `in_progress` with its lease — no
attempt consumed, no user flagged. Failed verifications are notes, not
progress, so a worker that keeps claiming "done" without fixing anything
trips stuck detection on its own. The dashboard renders the checklist with
per-criterion evidence.

```
python3 -m stayonduty.demo_acceptance
```

Proven: sloppy work → `complete()` refused with exact failing criteria →
worker fixes the artifact, reports evidence → verified done, attempts still
0. Same loop over raw MCP. No-criteria tasks complete exactly as before;
old databases migrate cleanly.

## Iteration 8 — Claude Code integration (the YouTube play, shipped)

MCP alone isn't adoption: an agent still has to *choose* the tools, and it
can still quit mid-task. One command wires all three layers:

```
python3 -m stayonduty.claude_code.install --project /path/to/project
```

- **MCP server** — `stayonduty` (17 tools, stdio, zero deps) registered in
  `.mcp.json` (project scope) or `~/.claude.json` top-level `mcpServers`
  (user scope). `--user` covers every project on the machine.
- **SessionStart hook** — injects the one-sentence standing instruction into
  every session (start/resume/clear/compact), so the agent reaches for
  `stayonduty_register_task` on its own for long work.
- **Stop hook** — when the agent tries to stop with StayOnDuty tasks still
  `in_progress` on live leases, the stop is blocked (bounded: 3 per
  session, then the watchdog owns recovery) and the agent is told to finish
  or park the work. Never traps the *user*: missing DB, no live work,
  stale leases, an already-active stop hook, or an exhausted budget all
  let the stop through.

Idempotent (re-running changes nothing), backs up existing settings to
`.bak`, and `--uninstall` removes exactly what install added while leaving
other servers/hooks untouched.

```
python3 -m stayonduty.demo_claude_code
```

Proven (10 phases, simulated Claude Code hook protocol, no `claude` CLI
needed): install at both scopes → idempotent → every session learns the
instruction naming the real tools → stop allowed with no tasks / missing
DB / garbage input → 3 bounded blocks naming the live task → budget
exhausted hands off to the watchdog → stale leases don't block or consume
budget → installed config actually launches the MCP server (17 tools) →
uninstall leaves pre-existing servers and hooks untouched. All prior demos
still green.

## Iteration 9 — independent verifier (the producer never grades its own work)

Iteration 7's hole: acceptance checks trusted an *honest* worker — the
producer reported its own evidence. Now `verify="independent"` routes a
passing completion into `in_review`, where a DIFFERENT worker must judge
it before the task can complete. Claim-your-own-review is refused at the
store, SDK, worker, and MCP layers.

```python
client = Client("work.db")
with client.lease("ship the weekly report",
                  acceptance=["report.md exists",
                              "a second engineer reviewed the changes"],
                  verify="independent") as job:
    ...do the work...
    job.acceptance_check("ac2", True, "Maya reviewed the diff; 2 findings fixed")
    job.complete("done")   # -> in_review, not done

# a different worker (never the producer):
with client.claim_review(task_id, "verifier-7") as review:
    ...read the artifacts with fresh eyes...
    review.verdict(True, "evidence names a real reviewer")
```

- **Fail** sends the task back to the queue with the verifier's notes in
  the timeline (no attempt consumed); **3 failed rounds** fails the task
  as a passive record — never a push.
- **Judgment is pluggable**: `worker.provider_review_policy(provider)`
  turns any provider's `judge()` (PASS/FAIL + reasoning convention) into
  a review policy for `run_one(..., review_policy=...)`. `XAIProvider`
  implements `judge()` over its chat model (untested live — no key here,
  same standing as its `generate()`); `FakeProvider` has a scripted
  judge for demos. A bare producer assertion is flagged SELF-REVIEW.
- **Worker daemons** with a review policy judge pending reviews before
  taking new work; without one they leave reviews for agent sessions
  (MCP: `stayonduty_claim_review`, `stayonduty_submit_verdict`,
  `stayonduty_reviews` — server now v0.8.0, 17 tools).
- **Watchdog coverage**: dead verifier leases are released for another
  verifier; a review nobody claims is failed passively by stuck detection.
  The Stop hook treats live review leases as live work.
- Dashboard: `IN REVIEW` pill + per-round review history on the task page.

```
python3 -m stayonduty.demo_verifier
```

Proven: self-review refused at store and worker level → judge FAILs the
producer's bare assertion (deterministic gate had passed) → fix with real
evidence → PASS, `verified_by` recorded, producer ≠ verifier → 3 failed
rounds fail passively → SDK `ReviewClaim` and raw MCP paths agree. All
prior demos green.

## Iteration 10 — hierarchical decomposition (big tasks split themselves)

The shape of "keep working on this" for genuinely big jobs: a worker
holding a task may split it into subtasks, each with its own acceptance
criteria, lease lifecycle, memory scope, stuck ladder, and spend caps.

```python
with client.lease("generate 50 card faces",
                  acceptance=["50 face PNGs in out/"],
                  token_budget=100_000) as job:
    kids = job.decompose([
        {"title": "faces 00-09", "payload": {"lo": 0, "hi": 9},
         "acceptance": ["faces 00-09 on disk"]},
        # ... 4 more batches
    ])
# parent -> DECOMPOSED (not claimable, lease released).
# Each child is claimed and worked normally — crash-safe, stuck-watched,
# acceptance-gated, independently reviewable.
# When every child is done, the parent auto-completes — but only if its
# OWN acceptance verifies against the real artifacts.
```

- **Resolution**: all children done + parent acceptance verified → done.
  Any child failed → parent fails as a passive record naming the failed
  children (never a push); the good siblings stay done.
- **Budgets are subtree caps**: a parent budget counts usage from every
  descendant — decomposition can't weaken the guardrail. The spending
  child parks on `budget_hold`; `raise_budget` on the decomposed parent
  resumes parked descendants once the cap clears.
- **verify_mode is inherited** unless a subtask overrides it — an
  independently-verified parent gets independently-verified children.
- **Refusals**: non-owner, empty/bad specs, re-decomposing, nesting
  deeper than 5 — all refused, no partial trees.
- **Surfaces**: SDK `Client.decompose` / `Client.children` /
  `Lease.decompose`; `run_one` work functions get `ctx.decompose()`
  (run_one skips its completion attempt for a decomposed parent);
  MCP `stayonduty_decompose` + `stayonduty_children` (server now v0.10.0,
  20 tools with `stayonduty_recovery`); dashboard shows a `DECOMPOSED` pill with n/m-done progress,
  subtask lists, and parent links. Claude Code's standing instruction
  teaches agents to decompose big tasks.

```
python3 -m stayonduty.demo_decompose
```

Proven: 5 children from one parent → child-0's worker dies mid-batch
(os._exit) → fresh worker resumes from the child's memory → all children
done → parent auto-resolves with its acceptance verified → refusals,
failed-child propagation, subtree caps, verify inheritance, and SDK/MCP
paths green. All prior demos green.
## Iteration 11 — the autonomy journal ("handled on its own")

The confidence surface: every time StayOnDuty handles a problem itself, a
human sentence lands in a pull-only feed — the proof the autonomy doctrine
held while nobody was watching:

```
"Muse" stopped "generate 12 card faces" at "6:42 PM" — StayOnDuty resumed the task at "6:43 PM"
"Muse" stalled on "backfill search index" — StayOnDuty queued a fresh attempt on its own
"render 4k trailer" could not finish on its own (after 3 attempts) — full diagnostics in its timeline
```

- **Journaled events**: worker crash / lease expiry (`worker_stopped`),
  verifier death (`verifier_stopped`), fresh-worker resume (`task_resumed`,
  noting checkpoint memory when found), stuck replan nudge
  (`stuck_replanned`), autonomous requeue (`auto_retried`), and give-up
  (`give_up`) — from crash recovery, the stuck ladder, and review failures.
- **Pairing**: each stop pairs with the next resume of the same task, so
  the feed reads as Robert's sentence. A stop with no later resume is
  worded by where the task ended up — never promises a pickup that isn't
  coming.
- **Surfaces**: dashboard "Handled on its own" section (pull-only, no
  ack links), `/api/recovery` (rows + sentences), MCP
  `stayonduty_recovery` (server now v0.10.0, 20 tools), and SDK
  `Client.recovery_feed()`. The watchdog's existing
  `recover_stale`/`detect_stuck` loop fills the journal automatically.

```
python3 -m stayonduty.demo_recovery
```

## The app (v1)

StayOnDuty as a thing a non-technical person can actually use: a small
web app on your own machine. No accounts, no subscriptions, no fees —
free forever.

```bash
cd ~/workspace/stayonduty
python3 -m stayonduty.app
# open http://localhost:8080/
```

**What you do:**

1. **Settings** — paste an API key for Claude, Grok, and/or ChatGPT.
   That's the BYOK part: *you bring your own key*, and the AI provider
   bills you directly at their normal rates (usually pennies per task).
   StayOnDuty never charges you anything and never sees your bill.
   Your keys are stored **only in the database on this machine** — never
   in code, logs, or anywhere else.
2. **＋ New task** — type what should get done in plain words ("Summarize
   this report", "Draft a reply to…"), pick which helper does it
   (or leave it on Auto), and tap Start.
3. **Check in whenever you like** — the home screen shows each task as
   a card in plain language: Working, In line, Done, Taking a scheduled
   break. If a helper stalls, errors, or hits a usage limit, StayOnDuty
   handles it on its own (replan → fresh attempt → give up with the full
   story saved). "Handled on its own" is the proof, in sentences.

**Two honest notes:**

- "Claude / Grok / ChatGPT" here means their **official APIs** on your
  key — not the chat websites. claude.ai, grok.com, and chatgpt.com
  can't be plugged into anything, so a website login won't work; you
  need an API key from the provider (the Settings page says where).
- Every task gets a spending cap (100k tokens — pennies), so a runaway
  can't surprise you. If a task hits it, it pauses itself and waits for
  you to raise the limit on its page. Nothing is ever spent after a
  pause, and nothing ever notifies or nags you — the app is pull-only.

**For the technical:** the app is `stayonduty/app.py` (web UI, stdlib
only) plus `stayonduty/app_runner.py` (background supervisor thread:
stale-lease recovery, quota-wait promotion, stuck detection, and one
focused provider pass per task). Providers live in
`stayonduty/providers/` — `anthropic.py` (Claude, Messages API),
`xai.py` (Grok), and `openai.py` (ChatGPT) are all plain-HTTPS, zero
new dependencies. The app's database defaults to `stayonduty-app.db`
in the current directory; the SDK, MCP server, dashboard, and all
demos are unchanged.
