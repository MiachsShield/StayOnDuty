"""StayOnDuty app v1 — the friendly face of the supervision engine.

Single-user, local-first, no auth, no accounts, no subscriptions. One web
UI on top of the task ledger:

  /           home — the friendly dashboard (task cards, plain language)
  /new        "What should get done?" + which helper (Claude/Grok/ChatGPT)
  /settings   paste API keys (stored ONLY in the local SQLite database)
  /?task=ID   the full story of one task, including its answer

A background supervisor thread runs the app's tasks against the official
provider APIs on the user's own key (BYOK), with the stuck ladder,
heartbeats, checkpoints, spend caps, and the autonomy journal doing
their work invisibly. The UI is pull-only: problems are handled on
their own; the user sees the calm record when they check in.

Keys are never logged, never echoed back, never leave this machine
except in the Authorization header of a call to their own provider.
"Claude/Grok/ChatGPT" here means the official APIs — the chat websites
(claude.ai, grok.com, chatgpt.com) can't be plugged into anything.

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
from stayonduty.store import Store  # noqa: E402
from stayonduty.app_runner import (MODELS, MODEL_IDS, resolve_model,  # noqa: E402
                                   needs_key, provider_for,
                                   supervisor_loop)

DB = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") \
    else "stayonduty-app.db"
PORT = int(sys.argv[3] if len(sys.argv) > 3 and sys.argv[2] == "--port"
           else (sys.argv[2] if len(sys.argv) > 2 and sys.argv[2].isdigit()
                 else 8080))

APP_TOKEN_BUDGET = 100_000  # pennies of protection on every app task

MODEL_LABELS = {"auto": "Auto", "claude": "Claude", "grok": "Grok",
                "chatgpt": "ChatGPT"}

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


def app_task_list(store):
    """The friendly task cards, with two app additions: a calm
    'waiting for your API key' state (never claimed, never burned) and
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
        if (t["status"] == "pending" and is_app and needs_key(t, store)):
            mid = (t.get("payload") or {}).get("model", "auto")
            which = (f"your {_model_of(t)} key" if mid in MODEL_IDS
                     else "an API key")
            label, cls = "Waiting for your API key", "st-money"
            why = (f"Add {which} in Settings and this starts on its own — "
                   "nothing is lost.")
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
    keys = sum(1 for mid in MODEL_IDS
               if (store.get_setting(MODELS[mid][0]) or "").strip())
    if keys == 0:
        out.append(
            "<div class='banner'>👋 <b>Welcome!</b> StayOnDuty is free"
            " forever — you just bring your own AI key. Add one in"
            " <a href='/settings'><b>Settings</b></a>, then tap"
            " <b>＋ New task</b>.</div>")
    waiting = [t for t in
               store.db.execute(
                   "SELECT * FROM tasks WHERE status='pending'").fetchall()
               if (store.get_task(t["id"]).get("payload") or {}).get("app")
               and needs_key(store.get_task(t["id"]), store)]
    if waiting:
        n = len(waiting)
        out.append(
            f"<div class='banner amber'>🔑 <b>{n} task{'s' if n > 1 else ''}"
            " waiting for an API key</b> — add one in"
            " <a href='/settings'><b>Settings</b></a> and"
            f" {'they' if n > 1 else 'it'} will start on"
            " {'their' if n > 1 else 'its'} own.</div>")
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

PROVIDER_INFO = {
    "anthropic": ("Claude", "key_anthropic",
                  "Anthropic's official API — get a key at console.anthropic.com"),
    "xai": ("Grok", "key_xai",
            "xAI's official API — get a key at console.x.ai"),
    "openai": ("ChatGPT", "key_openai",
               "OpenAI's official API — get a key at platform.openai.com"),
}


def settings_page(store):
    blocks = []
    for pid, (label, skey, desc) in PROVIDER_INFO.items():
        saved = bool((store.get_setting(skey) or "").strip())
        status = ("<span class='saved'>● Saved</span>" if saved
                  else "<span class='notset'>○ Not set</span>")
        remove = (f"<form method='post' action='/settings/remove_key'"
                  f" style='display:inline'>"
                  f"<input type='hidden' name='provider' value='{pid}'>"
                  f"<button class='btn danger' type='submit'>Remove</button>"
                  f"</form>" if saved else "")
        blocks.append(
            f"<div class='keybox'><b>{label}</b> {status}<br>"
            f"<span class='hint'>{html.escape(desc)}</span>"
            f"<form method='post' action='/settings/key'><div class='row'>"
            f"<input type='hidden' name='provider' value='{pid}'>"
            f"<input type='password' name='key' placeholder="
            f"'{('Replace key' if saved else 'Paste key')}' autocomplete='off'>"
            f"<button class='btn' type='submit'>Save</button>{remove}"
            f"</div></form></div>")
    default = store.get_setting("default_model", "auto")
    pills = []
    for mid, lab in [("auto", "Auto"), ("claude", "Claude"), ("grok", "Grok"),
                     ("chatgpt", "ChatGPT")]:
        checked = "checked" if default == mid else ""
        pills.append(
            f"<label class='pill'><input type='radio' name='default_model'"
            f" value='{mid}' {checked}>{lab}</label>")
    return (f"<a class='back' href='/'>← All tasks</a>"
            f"<h2 style='margin:.2rem 0'>Settings</h2>"
            f"<div class='banner'>StayOnDuty is <b>free forever</b> — no"
            " subscriptions, no fees. You bring your own AI key: the helper"
            " you pick is billed by its provider at their normal rates"
            " (usually pennies per task), and your key is stored <b>only in"
            " the database on this machine</b> — never anywhere else."
            " <br><br>One honest note: these are the <b>official APIs</b>."
            " The claude.ai, grok.com, and chatgpt.com chat websites can't"
            " be plugged into anything, so a website login won't work here"
            " — you need an API key from the provider.</div>"
            f"<h3>API keys</h3>{''.join(blocks)}"
            f"<h3>Default helper</h3>"
            f"<form method='post' action='/settings/default'>"
            f"<div class='pills'>{''.join(pills)}</div>"
            f"<p class='hint'>\"Auto\" uses your default when it has a key,"
            " otherwise the first helper with a saved key.</p>"
            f"<button class='btn' type='submit'>Save default</button></form>")


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
                if pid in PROVIDER_INFO and key:
                    # The value is written to the local DB only — never
                    # logged, never echoed, never anywhere else.
                    store.set_setting(PROVIDER_INFO[pid][1], key)
                self._redirect("/settings")
            elif u.path == "/settings/remove_key":
                pid = form.get("provider", [""])[0]
                if pid in PROVIDER_INFO:
                    store.delete_setting(PROVIDER_INFO[pid][1])
                self._redirect("/settings")
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
                # Runner liveness + which helpers are ready. Keys are
                # NEVER exposed — only booleans.
                self._send(200, "application/json", json.dumps({
                    "runner": True,
                    "models": {
                        mid: bool((store.get_setting(MODELS[mid][0])
                                   or "").strip())
                        for mid in MODEL_IDS},
                    "default_model": store.get_setting(
                        "default_model", "auto")}))
            elif u.path == "/new":
                self._send(200, "text/html", page(new_task_form(store)))
            elif u.path == "/settings":
                self._send(200, "text/html", page(settings_page(store)))
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
    print("Free forever. Bring your own AI key in Settings — "
          "nothing is billed by StayOnDuty.", flush=True)
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
