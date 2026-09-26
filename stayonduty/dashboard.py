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
:root{--green:#2f9e4f;--green-d:#257f40;--ink:#23272e;--muted:#6b7280;--card:#ffffff;
--bg:#f6f4ef;--line:#e8e2d6;--amber:#b7791f;--red:#c0392b;--purple:#8e44ad;--blue:#34618f;
--shadow:0 1px 2px rgba(35,39,46,.06),0 6px 18px rgba(35,39,46,.07)}
*{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
background:var(--bg);color:var(--ink);margin:0;padding:0;line-height:1.55;
-webkit-font-smoothing:antialiased}
.wrap{max-width:960px;margin:0 auto;padding:1.4rem 1rem 3rem}
header.top{display:flex;align-items:center;margin-bottom:.15rem}
.brand{display:flex;align-items:center;gap:.7rem;text-decoration:none;color:inherit}
.logo{width:48px;height:48px;flex:none;filter:drop-shadow(0 3px 8px rgba(47,158,79,.35))}
.brandname{font-size:1.5rem;font-weight:800;letter-spacing:-.02em}
.sub{color:var(--muted);margin:.2rem 0 1.3rem;font-size:.95rem}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:.8rem;margin-bottom:1.4rem}
.stat{background:var(--card);border:1px solid var(--line);border-radius:16px;
padding:.9rem 1.1rem;box-shadow:var(--shadow)}
.stat .n{font-size:1.7rem;font-weight:800;letter-spacing:-.02em}
.stat .l{color:var(--muted);font-size:.82rem;white-space:nowrap}
h2.sec{font-size:1.08rem;margin:1.7rem 0 .7rem;letter-spacing:-.01em}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;
padding:1.05rem 1.25rem;margin-bottom:.75rem;display:block;color:inherit;
text-decoration:none;box-shadow:var(--shadow);
transition:transform .12s ease,box-shadow .12s ease}
a.card:hover{border-color:var(--green);transform:translateY(-1px);
box-shadow:0 2px 4px rgba(35,39,46,.06),0 10px 24px rgba(35,39,46,.1)}
.card .title{font-weight:700;font-size:1.04rem;margin:.45rem 0 .2rem;letter-spacing:-.01em}
.card .why{color:var(--muted);font-size:.88rem}
.card .meta{color:var(--muted);font-size:.8rem;margin-top:.5rem}
.dot{display:inline-block;width:.55em;height:.55em;border-radius:50%;flex:none}
.dot.st-working{background:var(--green)} .dot.st-wait{background:#9aa1ad}
.dot.st-done{background:var(--blue)} .dot.st-bad{background:var(--red)}
.dot.st-self{background:#e67e22} .dot.st-think{background:var(--purple)}
.dot.st-money{background:var(--amber)}
.status{display:inline-flex;align-items:center;gap:.45em;font-weight:700;
font-size:.85rem;border-radius:999px;padding:.3rem .8rem;background:#eef0f3}
.status.st-working{background:#e4f3e8} .status.st-wait{background:#eef0f3}
.status.st-done{background:#e7f0f9} .status.st-bad{background:#fdecea}
.status.st-self{background:#fdf0e0} .status.st-think{background:#f2eaf9}
.status.st-money{background:#fdf5e0}
.feed{background:var(--card);border:1px solid var(--line);border-radius:16px;
padding:1rem 1.25rem;margin-bottom:.75rem;box-shadow:var(--shadow)}
.feed ul{list-style:none;padding:0;margin:.4rem 0 0}
.feed li{padding:.55rem 0;border-top:1px solid var(--line);font-size:.92rem}
.feed li:first-child{border-top:none}
.empty{color:var(--muted);font-size:.92rem}
.empty-state{text-align:center;padding:1.6rem 1rem}
.empty-state svg{width:64px;height:64px;margin-bottom:.4rem}
.empty-state p{color:var(--muted);font-size:.95rem;margin:.4rem 0 0}
.empty-state.slim{padding:.9rem .5rem}
.empty-state.slim svg{width:48px;height:48px}
.note{background:#fff8e6;border:1px solid #ecd9a0;border-radius:16px;
padding:.9rem 1.2rem;margin-bottom:.75rem;font-size:.92rem;box-shadow:var(--shadow)}
.note.bad{background:#fdecea;border-color:#f0b9b2}
.note a{float:right;font-size:.85em}
h3{font-size:1.02rem;margin:1.5rem 0 .55rem;letter-spacing:-.01em}
ul.plain{list-style:none;padding:0;margin:0}
ul.plain li{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:.65rem .95rem;margin-bottom:.5rem;font-size:.9rem;box-shadow:var(--shadow)}
ul.plain .t{color:var(--muted);font-size:.78rem;margin-right:.5rem}
form.budget{background:var(--card);border:1px solid var(--line);border-radius:16px;
padding:1rem 1.25rem;margin:1rem 0;box-shadow:var(--shadow)}
form.budget input{padding:.45rem .6rem;border:1px solid var(--line);border-radius:8px;
font-size:.9rem;width:7em}
form.budget button{background:var(--green);color:#fff;border:none;border-radius:8px;
padding:.5rem 1rem;font-size:.9rem;cursor:pointer}
.back{display:inline-block;margin-bottom:.6rem;color:var(--green);text-decoration:none;
font-weight:700}
.foot{color:var(--muted);font-size:.78rem;margin-top:2.2rem;text-align:center}
.foot a{color:var(--muted)}
.answer{background:#f0f7f1;border:1px solid #cfe6d2;border-radius:16px;
padding:1.15rem 1.3rem;margin-bottom:.75rem;box-shadow:var(--shadow);font-size:1rem}
.answer .pre{white-space:pre-wrap;margin:0}
@media(max-width:640px){.stats{gap:.55rem}.stat{padding:.75rem .8rem}
.stat .n{font-size:1.35rem}.stat .l{font-size:.76rem}.brandname{font-size:1.32rem}}
@keyframes fadein{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
.wrap{animation:fadein .22s ease}
ul.timeline{position:relative;list-style:none;padding:0;margin:0}
ul.timeline:before{content:"";position:absolute;left:1.35rem;top:1rem;bottom:1rem;
width:2px;background:var(--line);border-radius:2px}
ul.timeline li{position:relative;background:var(--card);border:1px solid var(--line);
border-radius:12px;padding:.6rem .95rem .6rem 2.7rem;margin-bottom:.5rem;
font-size:.9rem;box-shadow:var(--shadow)}
ul.timeline li:before{content:"";position:absolute;left:1.02rem;top:1.02rem;
width:.65rem;height:.65rem;border-radius:50%;background:var(--green);
border:2px solid var(--card);box-shadow:0 0 0 2px var(--green)}
@media (prefers-color-scheme:dark){
:root{--ink:#e9ebee;--muted:#9aa0a8;--card:#1f2227;--bg:#141518;--line:#2e3239;
--shadow:0 1px 2px rgba(0,0,0,.35),0 6px 18px rgba(0,0,0,.4);color-scheme:dark}
body{background:var(--bg)}
.status.st-working{background:#1d3524}.status.st-wait{background:#262a31}
.status.st-done{background:#1c2b3d}.status.st-bad{background:#3d2220}
.status.st-self{background:#3a2a1c}.status.st-think{background:#2f2339}
.status.st-money{background:#3a3120}
.note{background:#2b2517;border-color:#5a4d24}
.note.bad{background:#3a2220;border-color:#6e3a34}
.answer{background:#1c2a1f;border-color:#2f5b38}
}
"""

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="10">
<title>StayOnDuty</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 48 48'%3E%3Crect width='48' height='48' rx='12' fill='%232f9e4f'/%3E%3Cpath d='M14 24.5l9 9L34 19' stroke='white' stroke-width='6' fill='none' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E">
<style>{css}</style></head><body>
<div class="wrap">
<header class="top"><a class="brand" href="/"><svg class="logo" viewBox="0 0 48 48" aria-hidden="true"><rect width="48" height="48" rx="12" fill="#2f9e4f"/><path d="M14 24.5l9 9L34 19" stroke="#fff" stroke-width="6" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg><span class="brandname">StayOnDuty</span></a></header>
<div class="sub">Your AI helpers keep working — even when you're not watching.</div>
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
            f"<div class='l'>working now</div></div>"
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
        body = ("<div class='empty-state slim'>"
                "<svg viewBox='0 0 64 64' aria-hidden='true'>"
                "<circle cx='32' cy='32' r='26' fill='#e9f4eb'/>"
                "<path d='M22 33l7 7 13-15' stroke='#2f9e4f' stroke-width='6'"
                " fill='none' stroke-linecap='round' stroke-linejoin='round'/>"
                "</svg><p>All quiet. If a helper ever goes silent, hits a"
                " snag, or a check fails, StayOnDuty fixes it by itself —"
                " and the story lands here.</p></div>")
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
            f"<span class='status {cls}'><span class='dot {cls}'></span>"
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
                f"<span class='status {kcls}'><span class='dot {kcls}'></span>"
                f"{html.escape(klabel)}</span>"
                f"<div class='title'>{html.escape(k['title'])}</div></a>")
        sub_html = (f"<h3>Smaller pieces ({sum(1 for k in kids if k['status'] == 'done')}"
                    f" of {len(kids)} done)</h3>{''.join(items)}")
    return (f"<a class='back' href='/'>← All tasks</a>"
            f"<h2 style='margin:.2rem 0'>{html.escape(t['title'])}</h2>"
            f"<p><span class='status {cls}'><span class='dot {cls}'></span>"
            f"{html.escape(label)}</span><br>"
            f"<span class='why'>{html.escape(why)}</span></p>"
            f"{parent_html}{sub_html}"
            f"{spend_html}{hold_form}{crit_html}{review_html}"
            f"<h3>What happened</h3><ul class='plain timeline'>{events}</ul>"
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
