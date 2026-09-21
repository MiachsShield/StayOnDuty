"""StayOnDuty dashboard — "is your agent actually working?"

Stdlib only, no dependencies. Written for humans, not engineers:
plain-language statuses, friendly cards, zero jargon. The JSON API
underneath is unchanged.

Run:  python3 -m stayonduty.dashboard <db> [--port 8080]
"""
import html
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from stayonduty.store import Store  # noqa: E402

DB = sys.argv[1] if len(sys.argv) > 1 else "stayonduty.db"
PORT = int(sys.argv[3] if len(sys.argv) > 3 and sys.argv[2] == "--port"
           else (sys.argv[2] if len(sys.argv) > 2 and sys.argv[2].isdigit() else 8080))

CSS = """
:root{--green:#2f9e4f;--ink:#23272e;--muted:#6b7280;--card:#ffffff;
--bg:#f6f4ef;--line:#e8e2d6;--amber:#b7791f;--red:#c0392b;--purple:#8e44ad}
*{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
background:var(--bg);color:var(--ink);margin:0;padding:0;line-height:1.5}
.wrap{max-width:960px;margin:0 auto;padding:1.5rem 1rem 3rem}
header.top{display:flex;align-items:center;gap:.8rem;margin-bottom:.4rem}
.logo{width:44px;height:44px;border-radius:14px;background:var(--green);
display:flex;align-items:center;justify-content:center;font-size:24px;flex:none}
h1{font-size:1.5rem;margin:0}
.sub{color:var(--muted);margin:.1rem 0 1.4rem;font-size:.95rem}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:.8rem;margin-bottom:1.4rem}
.stat{background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:.9rem 1.1rem}
.stat .n{font-size:1.7rem;font-weight:700}
.stat .l{color:var(--muted);font-size:.85rem}
h2.sec{font-size:1.05rem;margin:1.6rem 0 .7rem;color:var(--ink)}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:1rem 1.2rem;margin-bottom:.7rem;display:block;color:inherit;text-decoration:none}
a.card:hover{border-color:var(--green)}
.card .title{font-weight:600;font-size:1.02rem;margin:.35rem 0 .2rem}
.card .why{color:var(--muted);font-size:.88rem}
.card .meta{color:var(--muted);font-size:.8rem;margin-top:.45rem}
.dot{display:inline-block;width:.55em;height:.55em;border-radius:50%;margin-right:.45em}
.st-working{background:var(--green)} .st-wait{background:#9aa1ad}
.st-done{background:#34618f} .st-bad{background:var(--red)}
.st-self{background:#e67e22} .st-think{background:var(--purple)}
.st-money{background:var(--amber)}
.status{font-weight:600;font-size:.9rem}
.feed{background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:1rem 1.2rem;margin-bottom:.7rem}
.feed ul{list-style:none;padding:0;margin:.4rem 0 0}
.feed li{padding:.5rem 0;border-top:1px solid var(--line);font-size:.92rem}
.feed li:first-child{border-top:none}
.empty{color:var(--muted);font-size:.92rem}
.note{background:#fff8e6;border:1px solid #ecd9a0;border-radius:14px;
padding:.9rem 1.2rem;margin-bottom:.7rem;font-size:.92rem}
.note.bad{background:#fdecea;border-color:#f0b9b2}
.note a{float:right;font-size:.85em}
h3{font-size:1rem;margin:1.4rem 0 .5rem}
ul.plain{list-style:none;padding:0;margin:0}
ul.plain li{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:.6rem .9rem;margin-bottom:.45rem;font-size:.9rem}
ul.plain .t{color:var(--muted);font-size:.78rem;margin-right:.5rem}
form.budget{background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:1rem 1.2rem;margin:1rem 0}
form.budget input{padding:.45rem .6rem;border:1px solid var(--line);border-radius:8px;
font-size:.9rem;width:7em}
form.budget button{background:var(--green);color:#fff;border:none;border-radius:8px;
padding:.5rem 1rem;font-size:.9rem;cursor:pointer}
.back{display:inline-block;margin-bottom:.6rem;color:var(--green);text-decoration:none;
font-weight:600}
.foot{color:var(--muted);font-size:.78rem;margin-top:2rem;text-align:center}
.foot a{color:var(--muted)}
@media(max-width:640px){.stats{grid-template-columns:repeat(3,1fr);gap:.5rem}
.stat{padding:.7rem .8rem}.stat .n{font-size:1.3rem}}
"""

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="10">
<title>StayOnDuty</title><style>{css}</style></head><body>
<div class="wrap">
<header class="top"><div class="logo">💚</div>
<div><h1>StayOnDuty</h1>
<div class="sub">Your AI helpers keep working — even when you're not watching.</div>
</div></header>
{body}
<div class="foot">{now} · refreshes automatically · <a href="/api/tasks">data API</a></div>
</div></body></html>"""

# Plain-language status: (label, dot-class, one-line explanation)
def friendly_status(t, stale=False, stuck=False):
    s = t["status"]
    if stale:
        return ("Picking back up on its own", "st-self",
                "Its helper went quiet, so StayOnDuty is handing the task to a fresh one. Nothing is lost.")
    if stuck:
        return ("Working through a snag on its own", "st-think",
                "Progress stalled, so StayOnDuty nudged it to rethink its approach.")
    if s == "in_progress":
        return ("Working", "st-working", "An AI helper is actively on it right now.")
    if s == "pending":
        return ("In line", "st-wait", "Waiting for a helper to pick it up.")
    if s == "done":
        vby = t.get("verified_by")
        why = ("Finished and double-checked by a second helper."
               if vby else "Finished.")
        return ("Done", "st-done", why)
    if s == "failed":
        return ("Couldn't finish", "st-bad",
                "It ran out of tries. The full story is saved below — nothing was deleted.")
    if s == "waiting":
        when = (time.strftime("%-I:%M %p", time.localtime(t.get("resume_at")))
                if t.get("resume_at") else "soon")
        return ("Taking a scheduled break", "st-wait",
                f"Pausing until about {when}, then resuming on its own.")
    if s == "budget_hold":
        return ("Paused — spending limit reached", "st-money",
                "It hit the spending limit you set. Raise it below to let it continue.")
    if s == "in_review":
        return ("Getting a second opinion", "st-think",
                "The work is done — a different helper is now checking it before it's called finished.")
    if s == "decomposed":
        return ("Split into smaller pieces", "st-working",
                "This big task was broken into smaller ones running below.")
    return (s.replace("_", " ").title(), "st-wait", "")


def rel(ts):
    """'3 min ago' style relative time."""
    if not ts:
        return "—"
    d = time.time() - ts
    if d < 60:
        return "just now"
    if d < 3600:
        return f"{int(d / 60)} min ago"
    if d < 86400:
        return f"{int(d / 3600)} hr ago"
    return time.strftime("%b %-d, %-I:%M %p", time.localtime(ts))


def fmt_spend(t):
    if t.get("token_budget") is None and t.get("usd_budget") is None:
        return ""
    parts = []
    if t.get("token_budget") is not None:
        parts.append(f"{_short(t['tokens_used'])} of {_short(t['token_budget'])} tokens used")
    if t.get("usd_budget") is not None:
        parts.append(f"${t['usd_used']:.2f} of ${t['usd_budget']:.2f} used")
    return " · ".join(parts)


def _short(n):
    n = n or 0
    return f"{n / 1000:.1f}k" if n >= 1000 else str(int(n))


def overview_stats(store):
    rows = store.db.execute("SELECT status FROM tasks").fetchall()
    now = time.time()
    working = sum(1 for r in rows if r["status"] == "in_progress")
    inline = sum(1 for r in rows if r["status"] in ("pending", "waiting"))
    done = sum(1 for r in rows if r["status"] == "done")
    handled = len(store.recovery_sentences(limit=1000))
    return (f"<div class='stats'>"
            f"<div class='stat'><div class='n'>{working}</div>"
            f"<div class='l'>working right now</div></div>"
            f"<div class='stat'><div class='n'>{inline}</div>"
            f"<div class='l'>in line</div></div>"
            f"<div class='stat'><div class='n'>{done}</div>"
            f"<div class='l'>finished</div></div></div>")


def _friendly_alert(a):
    """Translate backend alert copy into plain language for the banner."""
    msg = a["message"]
    if a["level"] >= 3:
        return ("😞 <b>Couldn't finish on its own</b>",
                "bad", html.escape(msg))
    if msg.startswith("\U0001f4b0"):
        # "Budget guardrail tripped — "X" parked after N tokens (cap $Y). ..."
        import re
        m = re.search(r'"([^"]+)"', msg)
        title = m.group(1) if m else "A task"
        return ("💰 <b>Spending limit reached</b>", "",
                f"{html.escape(title)} paused because it hit its spending limit. "
                f"Raise the limit on its page and it will continue on its own — "
                f"nothing has been spent since it paused.")
    return ("⏳ <b>Waiting, will resume alone</b>", "", html.escape(msg))


def alerts_banner(store):
    # Passive log only — the system handles problems itself; this is just
    # what the user sees when they check in. Nothing here ever pushes.
    items = []
    for a in store.alerts():
        when = rel(a["created_at"])
        label, cls, text = _friendly_alert(a)
        items.append(
            f"<div class='note {cls}'>{label} ({when}): {text} "
            f"<a href='/ack?id={a['id']}'>dismiss</a></div>")
    return "".join(items)


def recovery_feed_html(store, limit=20):
    """Pull-only 'handled on its own' record — proof everything kept
    moving while the user was away. Never pushes; just shows."""
    sentences = store.recovery_sentences(limit=limit)
    if not sentences:
        body = ("<p class='empty'>Nothing needed handling yet. If a helper ever "
                "goes quiet, gets stuck, or a check fails, StayOnDuty fixes it "
                "by itself — and the story lands here.</p>")
    else:
        items = "".join(f"<li>🔁 {html.escape(s)}</li>" for s in sentences)
        body = f"<ul>{items}</ul>"
    return (f"<h2 class='sec'>Handled on its own</h2>"
            f"<div class='feed'>{body}</div>")


def task_list(store):
    rows = store.db.execute(
        "SELECT * FROM tasks ORDER BY updated_at DESC").fetchall()
    if not rows:
        return ("<h2 class='sec'>Your tasks</h2><div class='feed'><p class='empty'>"
                "No tasks yet. When an AI helper starts work through StayOnDuty, "
                "it'll show up here.</p></div>")
    now = time.time()
    cards = []
    for r in rows:
        t = dict(r)
        is_stale = (t["status"] == "in_progress" and t["lease_expires_at"]
                    and t["lease_expires_at"] < now)
        is_stuck = (t["status"] == "in_progress" and (t["stuck_level"] or 0) > 0)
        label, cls, why = friendly_status(t, is_stale, is_stuck and not is_stale)
        title = html.escape(t["title"])
        if t.get("parent_id"):
            title = f"↳ {title}"
        spend = fmt_spend(t)
        spend_html = f"<div class='why'>{html.escape(spend)}</div>" if spend else ""
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
            f"<div class='meta'>updated {rel(t['updated_at'])}{extra}</div></a>")
    return "<h2 class='sec'>Your tasks</h2>" + "".join(cards)


EVENT_WORDS = {
    "created": "Task created",
    "claimed": "A helper picked it up",
    "step": "Made progress",
    "note": "Note",
    "memory": "Remembered something",
    "heartbeat": "Checked in",
    "stuck": "Hit a snag — nudged to rethink it alone",
    "unstuck": "Back on track",
    "requeued": "Started a fresh attempt on its own",
    "auto_retried": "Started a fresh attempt on its own",
    "failed": "Couldn't finish",
    "completed": "Finished",
    "review_claimed": "A second helper started checking the work",
    "review_passed": "Second helper approved it",
    "review_failed": "Second helper sent it back for fixes",
    "budget_hold": "Paused at the spending limit",
    "budget_raised": "Spending limit raised — resumed",
    "decomposed": "Split into smaller pieces",
    "quota_wait": "Taking a scheduled break",
    "resumed": "Resumed",
}


def _friendly_detail(etype, detail):
    """Strip backend-y detail text down to what a human needs."""
    import re
    d = detail or ""
    if etype == "claimed":
        return ""  # "owner=sdk-… lease=30s" means nothing to a human
    if etype == "budget_hold":
        return ("Hit the spending limit. It paused itself — "
                "nothing has been spent since.")
    if etype in ("requeued", "auto_retried"):
        return ""
    # Drop key=value fragments like "attempts=3".
    d = re.sub(r"\b\w+=[\w\-\.]+", "", d).strip(" —-,;")
    return d


def task_detail(store, task_id):
    t = store.get_task(task_id)
    if not t:
        return "<p>Couldn't find that task. <a href='/'>Back to all tasks</a></p>"
    now = time.time()
    stale = (t["status"] == "in_progress" and t["lease_expires_at"]
             and t["lease_expires_at"] < now)
    is_stuck = (t["status"] == "in_progress" and (t["stuck_level"] or 0) > 0)
    label, cls, why = friendly_status(t, stale, is_stuck and not stale)
    lis = []
    for e in store.events(task_id):
        word = EVENT_WORDS.get(e["type"], e["type"].replace("_", " ").title())
        when = time.strftime("%-I:%M %p", time.localtime(e["at"]))
        fd = _friendly_detail(e["type"], e["detail"])
        detail = f" — {html.escape(fd)}" if fd else ""
        lis.append(f"<li><span class='t'>{when}</span><b>{html.escape(word)}</b>{detail}</li>")
    events = "".join(lis) or "<li><i>Nothing recorded yet.</i></li>"
    mems = store.memories(scope=f"task:{task_id}")
    mem_html = ("".join(f"<li><b>{html.escape(k)}</b> = {html.escape(str(v))}</li>"
                        for k, v in mems.items()) or "<li><i>Nothing saved yet.</i></li>")
    spend_html = ""
    if t.get("token_budget") is not None or t.get("usd_budget") is not None:
        spend_html = f"<p>{html.escape(fmt_spend(t))}</p>"
    hold_form = ""
    if t["status"] == "budget_hold":
        hold_form = (
            f"<form class='budget' method='post' action='/raise_budget'>"
            f"<input type='hidden' name='id' value='{html.escape(t['id'])}'>"
            f"<b>Raise the spending limit</b><br>"
            f"<span class='why'>This task paused because it hit the limit you set. "
            f"Enter a new limit (or leave blank for no limit) and it will continue on its own.</span><br><br>"
            f"Tokens: <input name='token_budget' placeholder='no limit'> "
            f"Dollars: <input name='usd_budget' placeholder='no limit'> "
            f"<button type='submit'>Resume</button></form>")
    crit_html = ""
    crits = t.get("acceptance_criteria") or []
    if crits:
        items = []
        for c in crits:
            icon = ("✅" if c["status"] == "passed"
                    else "❌" if c["status"] == "failed" else "○")
            ev = (f" <span class='t'>— {html.escape(c['evidence'])}</span>"
                  if c.get("evidence") else "")
            items.append(f"<li>{icon} <b>{html.escape(c['description'])}</b> "
                         f"<span class='t'>{html.escape(c['id'])}</span>{ev}</li>")
        crit_html = (f"<h3>How \"done\" is checked</h3>"
                     f"<p class='t'>Acceptance criteria</p>"
                     f"<ul class='plain'>{''.join(items)}</ul>")
    review_html = ""
    revs = store.reviews(task_id)
    if revs or t.get("verify_mode") == "independent":
        ritems = []
        for r in revs:
            icon = "✅" if r["verdict"] == "pass" else "🔁"
            ritems.append(
                f"<li>{icon} Check #{r['round']}: {html.escape(r['verdict'])}"
                f" <span class='t'>— {html.escape(r['reason'])}</span></li>")
        body = "".join(ritems) or "<li><i>Waiting for a second helper to check it.</i></li>"
        review_html = f"<h3>Second opinions</h3><ul class='plain'>{body}</ul>"
    parent_html = ""
    if t.get("parent_id"):
        p = store.get_task(t["parent_id"])
        if p:
            plabel = friendly_status(p)[0]
            parent_html = (
                f"<p class='why'>Part of <a href='/?task={html.escape(p['id'])}'>"
                f"{html.escape(p['title'])}</a> ({html.escape(plabel)})</p>")
    kids = store.children(task_id)
    sub_html = ""
    if kids:
        items = []
        for k in kids:
            klabel, kcls, _ = friendly_status(k)
            items.append(
                f"<a class='card' href='/?task={html.escape(k['id'])}'>"
                f"<span class='status'><span class='dot {kcls}'></span>"
                f"{html.escape(klabel)}</span>"
                f"<div class='title'>{html.escape(k['title'])}</div></a>")
        sub_html = (f"<h3>Smaller pieces ({sum(1 for k in kids if k['status'] == 'done')}"
                    f" of {len(kids)} done)</h3>{''.join(items)}")
    return (f"<a class='back' href='/'>← All tasks</a>"
            f"<h2 style='margin:.2rem 0'>{html.escape(t['title'])}</h2>"
            f"<p><span class='status'><span class='dot {cls}'></span>"
            f"{html.escape(label)}</span><br>"
            f"<span class='why'>{html.escape(why)}</span></p>"
            f"{parent_html}{sub_html}"
            f"{spend_html}{hold_form}{crit_html}{review_html}"
            f"<h3>What happened</h3><ul class='plain'>{events}</ul>"
            f"<h3>Things it remembers</h3><ul class='plain'>{mem_html}</ul>")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, ctype, body):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        store = Store(DB)
        try:
            u = urlparse(self.path)
            if u.path == "/raise_budget":
                length = int(self.headers.get("Content-Length", 0))
                form = parse_qs(self.rfile.read(length).decode())
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
                self.send_response(302)
                self.send_header("Location",
                                 f"/?task={tid}" if ok else "/")
                self.end_headers()
            else:
                self._send(404, "text/plain", "not found")
        finally:
            store.close()

    def do_GET(self):
        store = Store(DB)
        try:
            u = urlparse(self.path)
            if u.path == "/api/tasks":
                tasks = [store.get_task(r["id"]) for r in
                         store.db.execute("SELECT id FROM tasks ORDER BY updated_at DESC")]
                self._send(200, "application/json", json.dumps(tasks, default=str))
            elif u.path == "/api/task":
                q = parse_qs(u.query)
                t = store.get_task(q.get("id", [""])[0])
                if not t:
                    self._send(404, "application/json", '{"error":"unknown task"}')
                else:
                    t["events"] = store.events(t["id"])
                    t["memory"] = store.memories(scope=f"task:{t['id']}")
                    self._send(200, "application/json", json.dumps(t, default=str))
            elif u.path == "/api/alerts":
                self._send(200, "application/json",
                            json.dumps(store.alerts(), default=str))
            elif u.path == "/api/recovery":
                # Autonomy journal: raw rows + rendered human sentences.
                feed = store.recovery_feed()
                self._send(200, "application/json", json.dumps(
                    {"entries": feed,
                     "sentences": store.recovery_sentences()},
                    default=str))
            elif u.path == "/ack":
                q = parse_qs(u.query)
                try:
                    store.ack_alert(int(q.get("id", ["0"])[0]))
                except (ValueError, TypeError):
                    pass
                self.send_response(302)
                self.send_header("Location", "/")
                self.end_headers()
            elif u.path == "/" and "task" in parse_qs(u.query):
                body = task_detail(store, parse_qs(u.query)["task"][0])
                self._send(200, "text/html",
                            PAGE.format(css=CSS, now=time.strftime("%Y-%m-%d %H:%M:%S"),
                                        body=body))
            elif u.path == "/":
                self._send(200, "text/html",
                            PAGE.format(css=CSS, now=time.strftime("%Y-%m-%d %H:%M:%S"),
                                        body=alerts_banner(store)
                                        + overview_stats(store)
                                        + task_list(store)
                                        + recovery_feed_html(store)))
            else:
                self._send(404, "text/plain", "not found")
        finally:
            store.close()


if __name__ == "__main__":
    print(f"StayOnDuty dashboard: http://localhost:{PORT}/  (db: {DB})", flush=True)
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
