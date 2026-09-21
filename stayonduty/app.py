"""StayOnDuty app v1 — the friendly face of the supervision engine.

Single-user, local-first, no auth, no accounts, no subscriptions. One web
UI on top of the task ledger:

  /           home — the friendly dashboard (task cards, plain language)
  /new        "What should get done?" + which helper (Gemini/Claude/Grok/ChatGPT)
  /settings   connect helpers: one-tap Google sign-in, or guided API keys
  /?task=ID   the full story of one task, including its answer

  /connect/google   start the one-tap Google sign-in (OAuth)
  /oauth/callback   Google sends the user back here after sign-in

A background supervisor thread runs the app's tasks against the official
provider APIs with the stuck ladder, heartbeats, checkpoints, spend
caps, and the autonomy journal doing their work invisibly. The UI is
pull-only: problems are handled on their own; the user sees the calm
record when they check in.

Getting a helper is one tap: "Continue with Google" signs in with the
user's Google account and runs Gemini on its free allowance — no keys,
no codes, no billing. Claude/Grok/ChatGPT stay available through guided
3-step connects (their official APIs on the user's own key, usually
pennies per task).

Keys and tokens are never logged, never echoed back, never leave this
machine except in the Authorization header of a call to their own
provider. "Claude/Grok/ChatGPT" here means the official APIs — the
chat websites (claude.ai, grok.com, chatgpt.com) can't be plugged into
anything.

Run:  python3 -m stayonduty.app [db] [--port 8080]
"""
import html
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from stayonduty import dashboard  # noqa: E402  (friendly rendering helpers)
from stayonduty import google_oauth  # noqa: E402
from stayonduty.store import Store  # noqa: E402
from stayonduty.app_runner import (MODELS, MODEL_IDS, is_connected,  # noqa: E402
                                   resolve_model,
                                   needs_key, provider_for,
                                   supervisor_loop)

DB = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") \
    else "stayonduty-app.db"
PORT = int(sys.argv[3] if len(sys.argv) > 3 and sys.argv[2] == "--port"
           else (sys.argv[2] if len(sys.argv) > 2 and sys.argv[2].isdigit()
                 else 8080))

APP_TOKEN_BUDGET = 100_000  # pennies of protection on every app task

MODEL_LABELS = {"auto": "Auto", "gemini": "Gemini (free)",
                "claude": "Claude", "grok": "Grok", "chatgpt": "ChatGPT"}

APP_CSS = """
.nav{display:flex;gap:.6rem;align-items:center;margin-bottom:1.1rem;flex-wrap:wrap}
.nav .sp{flex:1}
.btn{display:inline-block;background:var(--green);color:#fff;border:none;
border-radius:10px;padding:.65rem 1.25rem;font-size:1rem;font-weight:600;
cursor:pointer;text-decoration:none}
.btn.sec{background:#fff;color:var(--ink);border:1px solid var(--line);
font-weight:500;padding:.5rem 1rem;font-size:.9rem}
.btn.danger{background:#fff;color:var(--red);border:1px solid #f0b9b2;
font-weight:500;padding:.4rem .9rem;font-size:.85rem}
.btn.big{font-size:1.1rem;padding:.85rem 2rem;border-radius:12px;width:100%;
margin-top:1rem}
textarea.big{width:100%;min-height:8rem;border:1px solid var(--line);
border-radius:12px;padding:.9rem;font-size:1.05rem;font-family:inherit;
resize:vertical}
textarea.big:focus{outline:2px solid var(--green);border-color:var(--green)}
label.q{display:block;font-weight:600;font-size:1.05rem;margin:1.2rem 0 .5rem}
.pills{display:flex;gap:.6rem;flex-wrap:wrap}
.pill{border:1px solid var(--line);background:#fff;border-radius:999px;
padding:.55rem 1.15rem;cursor:pointer;font-size:.95rem}
.pill input{accent-color:var(--green);margin-right:.4rem}
.hint{color:var(--muted);font-size:.88rem;margin-top:.6rem}
.keybox{background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:1rem 1.2rem;margin-bottom:.8rem}
.keybox .row{display:flex;gap:.6rem;align-items:center;flex-wrap:wrap;margin-top:.6rem}
.keybox input[type=password]{flex:1;min-width:12rem;padding:.55rem .7rem;
border:1px solid var(--line);border-radius:8px;font-size:.95rem}
.saved{color:var(--green);font-weight:600}
.notset{color:var(--muted)}
.pre{white-space:pre-wrap;margin:0;font-size:.95rem}
.banner{background:#eef7ee;border:1px solid #cbe3cb;border-radius:14px;
padding:.9rem 1.2rem;margin-bottom:.8rem;font-size:.93rem}
.banner.amber{background:#fff8e6;border-color:#ecd9a0}
.banner.blue{background:#eef4fd;border-color:#c3d6f2}
.btn.google{background:#fff;color:var(--ink);border:1px solid var(--line);
font-size:1.05rem;padding:.8rem 1.4rem;border-radius:12px;width:100%;
margin-top:.8rem;font-weight:600}
.steps{margin:.6rem 0 0;padding:0;list-style:none}
.steps li{margin:.55rem 0;font-size:.95rem}
.steps .n{display:inline-block;width:1.5rem;height:1.5rem;border-radius:50%;
background:var(--green);color:#fff;text-align:center;line-height:1.5rem;
font-size:.8rem;font-weight:700;margin-right:.5rem}
.keybox input[type=text]{flex:1;min-width:12rem;padding:.55rem .7rem;
border:1px solid var(--line);border-radius:8px;font-size:.95rem}
details.setup{margin-top:.8rem;font-size:.9rem}
details.setup summary{cursor:pointer;color:var(--green);font-weight:600}
details.setup ol{margin:.6rem 0;padding-left:1.4rem}
details.setup li{margin:.4rem 0}
code.url{word-break:break-all;background:#f4f4f4;padding:.15rem .4rem;
border-radius:6px;font-size:.85rem}
"""

HEADER = """<div class="nav"><a class="btn sec" href="/">Tasks</a>
<a class="btn sec" href="/new">＋ New task</a><span class="sp"></span>
<a class="btn sec" href="/settings">⚙ Settings</a></div>
"""


def page(body):
    return dashboard.PAGE.format(
        css=dashboard.CSS + APP_CSS,
        now=time.strftime("%Y-%m-%d %H:%M:%S"),
        body=HEADER + body)


# ---------- home ----------

def _model_of(task):
    mid = (task.get("payload") or {}).get("model", "auto")
    return MODEL_LABELS.get(mid, "Auto")


def _resolved_model(task, store):
    """The concrete model id a task will run on, or None if nothing is
    connected yet."""
    return resolve_model((task.get("payload") or {}).get("model", "auto"),
                         store)


def app_task_list(store):
    """The friendly task cards, with app additions: calm 'one tap to
    start' / 'waiting to be connected' states (never claimed, never
    burned), a plain-English quota note for Gemini's free allowance, and
    a note of which helper each task uses."""
    rows = store.db.execute(
        "SELECT * FROM tasks ORDER BY updated_at DESC").fetchall()
    if not rows:
        return ("<h2 class='sec'>Your tasks</h2><div class='feed'><p class='empty'>"
                "Nothing here yet — tap <b>＋ New task</b> above and tell your"
                " helper what to do.</p></div>")
    now = time.time()
    cards = []
    for r in rows:
        t = store.get_task(r["id"])
        is_app = bool((t.get("payload") or {}).get("app"))
        mid = (t.get("payload") or {}).get("model", "auto")
        if (t["status"] == "pending" and is_app and needs_key(t, store)):
            if mid in ("auto", "gemini"):
                label, cls = "One tap to start", "st-money"
                why = ("Tap “Continue with Google” in Settings once — free,"
                       " no keys — and this starts on its own."
                       " Nothing is lost.")
            else:
                which = (f"your {_model_of(t)} helper" if mid in MODEL_IDS
                         else "a helper")
                label, cls = "Waiting to be connected", "st-money"
                why = (f"Connect {which} in Settings and this starts on its"
                       " own — nothing is lost.")
        elif (t["status"] == "waiting" and is_app
                and _resolved_model(t, store) == "gemini"):
            # Parked on Gemini's free daily allowance — say so like a
            # person would, not like an error.
            label, cls = "Taking a scheduled break", "st-wait"
            why = ("You've used today's free allowance — fresh allowance"
                   " tomorrow morning. Nothing is lost.")
        else:
            is_stale = (t["status"] == "in_progress" and t["lease_expires_at"]
                        and t["lease_expires_at"] < now)
            is_stuck = (t["status"] == "in_progress"
                        and (t["stuck_level"] or 0) > 0)
            label, cls, why = dashboard.friendly_status(
                t, is_stale, is_stuck and not is_stale)
        title = html.escape(t["title"])
        if t.get("parent_id"):
            title = f"↳ {title}"
        spend = dashboard.fmt_spend(t)
        spend_html = (f"<div class='why'>{html.escape(spend)}</div>"
                      if spend else "")
        via = (f" · via {_model_of(t)}" if is_app else "")
        extra = ""
        if t["status"] == "decomposed":
            kids = store.children(t["id"])
            done_n = sum(1 for k in kids if k["status"] == "done")
            extra = f" · {done_n} of {len(kids)} pieces done"
        cards.append(
            f"<a class='card' href='/?task={t['id']}'>"
            f"<span class='status'><span class='dot {cls}'></span>"
            f"{html.escape(label)}</span>"
            f"<div class='title'>{title}</div>"
            f"<div class='why'>{html.escape(why)}</div>{spend_html}"
            f"<div class='meta'>updated {dashboard.rel(t['updated_at'])}"
            f"{via}{extra}</div></a>")
    return "<h2 class='sec'>Your tasks</h2>" + "".join(cards)


def home_banners(store):
    """Pull-only, calm, honest. Nothing here pushes — it's just what the
    user sees when they check in."""
    out = []
    connected = sum(1 for mid in MODEL_IDS if is_connected(mid, store))
    if connected == 0:
        out.append(
            "<div class='banner'>👋 <b>Welcome!</b> No keys, no codes —"
            " one tap and your helper is ready."
            "<a class='btn google' href='/connect/google'>"
            "Continue with Google — free, one tap</a>"
            "<p class='hint'>Signs you in with your Google account and runs"
            " Gemini on its free allowance (~1,500 requests a day — plenty)."
            " StayOnDuty is free forever; nothing is billed, ever.</p></div>")
    waiting = [t for t in
               store.db.execute(
                   "SELECT * FROM tasks WHERE status='pending'").fetchall()
               if (store.get_task(t["id"]).get("payload") or {}).get("app")
               and needs_key(store.get_task(t["id"]), store)]
    if waiting:
        n = len(waiting)
        they = "they" if n > 1 else "it"
        # If everything waiting just needs the one tap, say so plainly.
        choices = {(store.get_task(t["id"]).get("payload") or {}).get(
            "model", "auto") for t in waiting}
        if choices <= {"auto", "gemini"}:
            out.append(
                f"<div class='banner amber'>✨ <b>{n} task"
                f"{'s' if n > 1 else ''} ready when you are</b> —"
                " <a href='/connect/google'><b>tap Continue with Google"
                "</b></a> (one tap, free) and"
                f" {they} will start on"
                f" {'their' if n > 1 else 'its'} own.</div>")
        else:
            out.append(
                f"<div class='banner amber'>🔑 <b>{n} task"
                f"{'s' if n > 1 else ''} waiting to be connected</b> —"
                " <a href='/settings'><b>connect a helper in Settings"
                f"</b></a> and {they} will start on"
                f" {'their' if n > 1 else 'its'} own.</div>")
    return "".join(out)


def home(store):
    return (home_banners(store)
            + dashboard.overview_stats(store)
            + app_task_list(store)
            + dashboard.recovery_feed_html(store))


# ---------- new task ----------

def new_task_form(store, error=""):
    default = store.get_setting("default_model", "auto")
    pills = []
    for mid, lab in [("auto", "Auto <span class='hint'>(recommended)</span>"),
                     ("gemini", "Gemini <span class='hint'>(free)</span>"),
                     ("claude", "Claude"), ("grok", "Grok"),
                     ("chatgpt", "ChatGPT")]:
        checked = "checked" if default == mid else ""
        pills.append(
            f"<label class='pill'><input type='radio' name='model' value='{mid}'"
            f" {checked}>{lab}</label>")
    err = f"<div class='banner amber'>{html.escape(error)}</div>" if error else ""
    return (f"<a class='back' href='/'>← All tasks</a>"
            f"<h2 style='margin:.2rem 0'>New task</h2>{err}"
            f"<form method='post' action='/new'>"
            f"<label class='q' for='prompt'>What should get done?</label>"
            f"<textarea class='big' id='prompt' name='prompt' placeholder="
            f"'\"Summarize this report…\"  \"Draft a reply to…\"  \"Plan…\"'></textarea>"
            f"<label class='q'>Which helper should do it?</label>"
            f"<div class='pills'>{''.join(pills)}</div>"
            f"<p class='hint'>Every task gets a spending cap"
            f" ({APP_TOKEN_BUDGET // 1000}k tokens — pennies), so a runaway"
            " can't surprise you. If a helper stalls, errors, or its key"
            " hits a limit, StayOnDuty handles it on its own — you'll see"
            " the calm record here when you check in.</p>"
            f"<button class='btn big' type='submit'>Start</button></form>")


# ---------- settings ----------

# Guided 3-step connects for the key-based helpers. Each card walks a
# non-technical user to the right page, the right button, and the paste
# field — no bare key boxes. (pid, label, console URL, step 1, step 2)
PROVIDER_GUIDES = [
    ("anthropic", "Claude", "key_anthropic", "https://console.anthropic.com",
     "Open console.anthropic.com and sign in (or make a free account).",
     "Open “API keys” and tap “Create key”, then copy it."),
    ("xai", "Grok", "key_xai", "https://console.x.ai",
     "Open console.x.ai and sign in with your X account.",
     "Open “API keys” and create a key, then copy it."),
    ("openai", "ChatGPT", "key_openai", "https://platform.openai.com",
     "Open platform.openai.com and sign in (or make a free account).",
     "Tap your profile → “API keys” → “Create new secret key”, then copy it."),
]

# ?google= notices from the OAuth round-trip. All calm, all pull-only.
GOOGLE_NOTICES = {
    "connected": ("<div class='banner'>✅ <b>You're in!</b> Gemini is now"
                  " your helper, on the free allowance. No keys, no codes —"
                  " just tap ＋ New task.</div>"),
    "denied": ("<div class='banner amber'>No problem — the Google sign-in"
               " was cancelled. You can connect any time.</div>"),
    "error": ("<div class='banner amber'>That sign-in didn't go through —"
              " try again when you like. Nothing was changed.</div>"),
    "setup-needed": ("<div class='banner amber'>Google sign-in needs its"
                     " one-time setup first — follow the steps under"
                     " “Google sign-in setup” below, then tap"
                     " “Continue with Google”.</div>"),
    "disconnected": ("<div class='banner'>Google sign-in was removed."
                     " Reconnect any time with one tap.</div>"),
}


def _google_section(store):
    """One-tap connect card + the one-time host setup (collapsible)."""
    if google_oauth.google_connected(store):
        card = (
            "<div class='banner'>✅ <b>Connected with Google</b> — Gemini is"
            " your helper, running on your free allowance (~1,500 requests a"
            " day). StayOnDuty only uses this sign-in for Gemini, nothing"
            " else."
            "<form method='post' action='/settings/disconnect_google'"
            " style='margin-top:.6rem'>"
            "<button class='btn danger' type='submit'>Disconnect"
            " Google</button></form></div>")
    else:
        card = (
            "<a class='btn google' href='/connect/google'>"
            "Continue with Google — free, one tap</a>"
            "<p class='hint'>No keys, no codes — one tap and your helper is"
            " ready. Uses your own free Gemini allowance; StayOnDuty is"
            " free forever and nothing is billed.</p>")
    cid_set = bool(google_oauth.client_config(store)[0])
    sec_set = bool(google_oauth.client_config(store)[1])
    redir = html.escape(google_oauth.redirect_uri(store, PORT))
    setup = (
        "<details class='setup'><summary>Google sign-in setup"
        " (one time, for whoever hosts this app)</summary>"
        "<ol>"
        "<li>Go to <b>console.cloud.google.com</b>, pick a project (or make"
        " one).</li>"
        "<li>Search <b>“Generative Language API”</b> and click"
        " <b>Enable</b>.</li>"
        "<li>Go to <b>Google Auth platform → Audience</b>: set the user type"
        " to <b>External</b>, and add your own email under"
        " <b>Test users</b>.</li>"
        "<li>Go to <b>Google Auth platform → Clients → Create client</b>:"
        " application type <b>Web application</b>, name it"
        " <b>StayOnDuty</b>.</li>"
        "<li>Under <b>Authorized redirect URIs</b> add exactly this:"
        f" <code class='url'>{redir}</code> → <b>Create</b>.</li>"
        "<li>Copy the <b>Client ID</b> and <b>Client secret</b> into the"
        " fields below and save. Done — “Continue with Google” will work"
        " from then on.</li>"
        "</ol>"
        "<form method='post' action='/settings/google_config'>"
        "<div class='keybox'><div class='row'>"
        "<input type='text' name='client_id' placeholder="
        f"'Client ID{' (saved)' if cid_set else ''}' autocomplete='off'>"
        "</div><div class='row'>"
        "<input type='password' name='client_secret' placeholder="
        f"'Client secret{' (saved)' if sec_set else ''}' autocomplete='off'>"
        "</div><div class='row'>"
        "<input type='text' name='redirect_uri' placeholder="
        "'Redirect URI (leave blank for this address)' autocomplete='off'>"
        "<button class='btn' type='submit'>Save</button>"
        "</div>"
        "<p class='hint'>The secret is stored only in the database on this"
        " machine — same as an API key. Leave the redirect URI blank unless"
        " this app lives at a public address.</p>"
        "</div></form></details>")
    return (f"<h3>Get started in one tap</h3>{card}{setup}")


def _provider_cards(store):
    """Guided 3-step connect cards for Claude / Grok / ChatGPT."""
    blocks = []
    for pid, label, skey, console_url, step1, step2 in PROVIDER_GUIDES:
        saved = bool((store.get_setting(skey) or "").strip())
        status = ("<span class='saved'>● Saved</span>" if saved
                  else "<span class='notset'>○ Not set</span>")
        remove = (f"<form method='post' action='/settings/remove_key'"
                  f" style='display:inline'>"
                  f"<input type='hidden' name='provider' value='{pid}'>"
                  f"<button class='btn danger' type='submit'>Remove</button>"
                  f"</form>" if saved else "")
        blocks.append(
            f"<div class='keybox'><b>{label}</b> {status}"
            f"<ol class='steps'>"
            f"<li><span class='n'>1</span>{html.escape(step1)}</li>"
            f"<li><span class='n'>2</span>{html.escape(step2)}</li>"
            f"<li><span class='n'>3</span>Paste the key here and tap Save."
            f"<form method='post' action='/settings/key'>"
            f"<div class='row'>"
            f"<input type='hidden' name='provider' value='{pid}'>"
            f"<input type='password' name='key' placeholder="
            f"'{('Replace key' if saved else 'Paste key')}'"
            f" autocomplete='off'>"
            f"<button class='btn' type='submit'>Save</button>{remove}"
            f"</div></form></li></ol>"
            f"<p class='hint'><a href='{console_url}'>{console_url}</a> —"
            f" {label}'s official API on your own key (usually pennies per"
            f" task).</p></div>")
    return "".join(blocks)


def settings_page(store, notice=""):
    banner = GOOGLE_NOTICES.get(notice or "", "")
    default = store.get_setting("default_model", "auto")
    pills = []
    for mid, lab in [("auto", "Auto"),
                     ("gemini", "Gemini <span class='hint'>(free)</span>"),
                     ("claude", "Claude"), ("grok", "Grok"),
                     ("chatgpt", "ChatGPT")]:
        checked = "checked" if default == mid else ""
        pills.append(
            f"<label class='pill'><input type='radio' name='default_model'"
            f" value='{mid}' {checked}>{lab}</label>")
    return (f"<a class='back' href='/'>← All tasks</a>"
            f"<h2 style='margin:.2rem 0'>Settings</h2>{banner}"
            f"{_google_section(store)}"
            f"<div class='banner'>StayOnDuty is <b>free forever</b> — no"
            " subscriptions, no fees. Below are the other helpers, each on"
            " <b>your own API key</b> at their normal rates (usually pennies"
            " per task). Your keys are stored <b>only in the database on"
            " this machine</b> — never anywhere else."
            " <br><br>One honest note: these are the <b>official APIs</b>."
            " The claude.ai, grok.com, and chatgpt.com chat websites can't"
            " be plugged into anything, so a website login won't work here.</div>"
            f"<h3>Other helpers</h3>{_provider_cards(store)}"
            f"<h3>Default helper</h3>"
            f"<form method='post' action='/settings/default'>"
            f"<div class='pills'>{''.join(pills)}</div>"
            f"<p class='hint'>“Auto” uses your default when it's connected,"
            " otherwise the first connected helper.</p>"
            f"<button class='btn' type='submit'>Save default</button></form>")


# pid -> settings key, for the key-based helpers (kept as the advanced
# fallback behind the guided 3-step cards).
PROVIDER_KEYS = {pid: skey for pid, _label, skey, _u, _s1, _s2
                 in PROVIDER_GUIDES}


# ---------- task detail (+ the answer) ----------

def task_page(store, task_id):
    t = store.get_task(task_id)
    if not t:
        return "<p>Couldn't find that task. <a href='/'>Back to all tasks</a></p>"
    body = ""
    if (t.get("payload") or {}).get("app"):
        answer = store.memories(scope=f"task:{task_id}").get("result")
        if answer:
            body += ("<h3>The answer</h3><div class='feed'>"
                     f"<p class='pre'>{html.escape(str(answer))}</p></div>")
    return body + dashboard.task_detail(store, task_id)


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # never log requests — URLs and bodies may name tasks

    def _send(self, code, ctype, body):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _form(self):
        length = int(self.headers.get("Content-Length", 0))
        return parse_qs(self.rfile.read(length).decode())

    def _redirect(self, to):
        self.send_response(302)
        self.send_header("Location", to)
        self.end_headers()

    def do_POST(self):
        store = Store(DB)
        try:
            u = urlparse(self.path)
            form = self._form()
            if u.path == "/new":
                prompt = form.get("prompt", [""])[0].strip()
                model = form.get("model", ["auto"])[0]
                if model not in MODEL_LABELS:
                    model = "auto"
                if not prompt:
                    self._send(200, "text/html", page(
                        new_task_form(store, "Tell your helper what to do"
                                             " first — one sentence is plenty.")))
                    return
                title = prompt if len(prompt) <= 80 else prompt[:77] + "…"
                store.create_task(
                    title,
                    payload={"app": True, "model": model, "prompt": prompt},
                    token_budget=APP_TOKEN_BUDGET)
                self._redirect("/")
            elif u.path == "/settings/key":
                pid = form.get("provider", [""])[0]
                key = form.get("key", [""])[0].strip()
                if pid in PROVIDER_KEYS and key:
                    # The value is written to the local DB only — never
                    # logged, never echoed, never anywhere else.
                    store.set_setting(PROVIDER_KEYS[pid], key)
                self._redirect("/settings")
            elif u.path == "/settings/remove_key":
                pid = form.get("provider", [""])[0]
                if pid in PROVIDER_KEYS:
                    store.delete_setting(PROVIDER_KEYS[pid])
                self._redirect("/settings")
            elif u.path == "/settings/google_config":
                # One-time host setup. The secret is only overwritten when
                # a new one is typed — a blank field keeps the saved one.
                cid = form.get("client_id", [""])[0].strip()
                sec = form.get("client_secret", [""])[0].strip()
                redir = form.get("redirect_uri", [""])[0].strip()
                if cid:
                    store.set_setting(google_oauth.S_CLIENT_ID, cid)
                if sec:
                    store.set_setting(google_oauth.S_CLIENT_SECRET, sec)
                store.set_setting(google_oauth.S_REDIRECT_URI, redir)
                self._redirect("/settings")
            elif u.path == "/settings/disconnect_google":
                google_oauth.disconnect(store)
                self._redirect("/settings?google=disconnected")
            elif u.path == "/settings/default":
                default = form.get("default_model", ["auto"])[0]
                if default in MODEL_LABELS:
                    store.set_setting("default_model", default)
                self._redirect("/settings")
            elif u.path == "/raise_budget":
                tid = form.get("id", [""])[0]

                def num(v, cast):
                    v = (v or "").strip()
                    if not v:
                        return None
                    try:
                        return cast(v)
                    except ValueError:
                        return None

                ok = store.raise_budget(
                    tid,
                    num(form.get("token_budget", [""])[0], int),
                    num(form.get("usd_budget", [""])[0], float))
                self._redirect(f"/?task={tid}" if ok else "/")
            else:
                self._send(404, "text/plain", "not found")
        finally:
            store.close()

    def do_GET(self):
        store = Store(DB)
        try:
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == "/api/tasks":
                tasks = []
                for r in store.db.execute(
                        "SELECT id FROM tasks ORDER BY updated_at DESC"):
                    t = store.get_task(r["id"])
                    if (t.get("payload") or {}).get("app"):
                        t["needs_key"] = needs_key(t, store)
                        t["model_label"] = _model_of(t)
                    tasks.append(t)
                self._send(200, "application/json",
                            json.dumps(tasks, default=str))
            elif u.path == "/api/task":
                t = store.get_task(q.get("id", [""])[0])
                if not t:
                    self._send(404, "application/json",
                               '{"error":"unknown task"}')
                else:
                    t["events"] = store.events(t["id"])
                    t["memory"] = store.memories(scope=f"task:{t['id']}")
                    self._send(200, "application/json",
                               json.dumps(t, default=str))
            elif u.path == "/api/recovery":
                self._send(200, "application/json", json.dumps(
                    {"entries": store.recovery_feed(),
                     "sentences": store.recovery_sentences()},
                    default=str))
            elif u.path == "/api/app":
                # Runner liveness + which helpers are ready. Credentials are
                # NEVER exposed — only booleans.
                self._send(200, "application/json", json.dumps({
                    "runner": True,
                    "models": {
                        mid: is_connected(mid, store)
                        for mid in MODEL_IDS},
                    "google_connected": google_oauth.google_connected(store),
                    "default_model": store.get_setting(
                        "default_model", "auto")}))
            elif u.path == "/connect/google":
                # One-tap sign-in: bounce the user to Google's consent
                # screen. Nothing is stored until they approve there.
                client_id, _sec = google_oauth.client_config(store)
                if not client_id:
                    self._redirect("/settings?google=setup-needed")
                else:
                    state = google_oauth.new_state()
                    self._redirect(google_oauth.auth_url(
                        client_id,
                        google_oauth.redirect_uri(store, PORT), state))
            elif u.path == "/oauth/callback":
                # Google sends the user back here after the consent screen.
                if q.get("error"):
                    # access_denied and friends — the user said no.
                    self._redirect("/settings?google=denied")
                elif not google_oauth.consume_state(
                        q.get("state", [""])[0]) or not q.get("code"):
                    self._send(200, "text/html", page(
                        "<div class='banner amber'>That sign-in link had"
                        " expired — tap “Continue with Google” again and"
                        " you'll be right through.</div>"
                        "<p><a href='/settings'>← Back to Settings</a></p>"))
                else:
                    client_id, client_secret = google_oauth.client_config(
                        store)
                    redir = google_oauth.redirect_uri(store, PORT)
                    try:
                        tokens = google_oauth.exchange_code(
                            client_id, client_secret, redir,
                            q["code"][0])
                    except Exception:  # noqa: BLE001 — calm retry, no leak
                        self._redirect("/settings?google=error")
                    else:
                        google_oauth.store_tokens(store, tokens)
                        if (store.get_setting("default_model", "auto")
                                == "auto"):
                            store.set_setting("default_model", "gemini")
                        self._redirect("/settings?google=connected")
            elif u.path == "/new":
                self._send(200, "text/html", page(new_task_form(store)))
            elif u.path == "/settings":
                self._send(200, "text/html", page(
                    settings_page(store, q.get("google", [""])[0])))
            elif u.path == "/" and "task" in q:
                self._send(200, "text/html",
                            page(task_page(store, q["task"][0])))
            elif u.path == "/":
                self._send(200, "text/html", page(home(store)))
            else:
                self._send(404, "text/plain", "not found")
        finally:
            store.close()


def main():
    print(f"StayOnDuty app: http://localhost:{PORT}/  (db: {DB})", flush=True)
    print("Free forever. 'Continue with Google' in Settings connects Gemini"
          " with one tap — or bring your own AI key. Nothing is billed by"
          " StayOnDuty.", flush=True)
    stop = threading.Event()
    t = threading.Thread(target=supervisor_loop, args=(DB, stop),
                         daemon=True)
    t.start()
    try:
        HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nStayOnDuty app stopped", flush=True)
    finally:
        stop.set()


if __name__ == "__main__":
    main()
