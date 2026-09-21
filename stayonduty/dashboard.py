"""StayOnDuty dashboard — "is your agent actually working?"

Stdlib only, no dependencies. Shows every task, its status, lease health,
event timeline, and memory. This is what you check instead of wondering.

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
body{font-family:system-ui,sans-serif;max-width:900px;margin:2em auto;padding:0 1em;color:#222}
h1{font-size:1.4em} table{border-collapse:collapse;width:100%}
th,td{border:1px solid #ddd;padding:.5em;text-align:left;font-size:.9em}
th{background:#f5f5f5} .pill{display:inline-block;padding:.15em .6em;border-radius:1em;
font-size:.8em;font-weight:bold;color:#fff}
.pending{background:#999}.in_progress{background:#2a9d48}.done{background:#34618f}
.failed{background:#c0392b}.stale{background:#e67e22}.stuck{background:#8e44ad}
.waiting{background:#5b6abf}.budget_hold{background:#b7791f}.in_review{background:#7c5cbf}
.decomposed{background:#0e8a8a}
a{color:#34618f} .meta{color:#666;font-size:.85em} ul.timeline{list-style:none;padding:0}
ul.timeline li{padding:.3em 0;border-bottom:1px solid #eee;font-size:.9em}
.refresh{color:#666;font-size:.8em}
.alert{background:#fdecea;border:1px solid #c0392b;border-radius:.5em;
padding:.7em 1em;margin-bottom:1em}
.notice{background:#eaf2fd;border:1px solid #2b6cb0;border-radius:.5em;
padding:.7em 1em;margin-bottom:1em}
.alert a,.notice a{float:right;font-size:.85em}
.recovery{background:#eef7ee;border:1px solid #2a9d48;border-radius:.5em;
padding:.7em 1em;margin-bottom:1em}
.recovery h2{font-size:1.05em;margin:.1em 0 .4em}
.recovery ul{list-style:none;padding:0;margin:0}
.recovery li{padding:.3em 0;border-bottom:1px solid #d9e8d9;font-size:.9em}
.recovery li:last-child{border-bottom:none}
.recovery .empty{color:#666;font-size:.9em}
"""

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="5">
<title>StayOnDuty</title><style>{css}</style></head><body>
<h1>🟢 StayOnDuty — is your agent working?</h1>
<p class="meta">{now} · auto-refreshes every 5s · <a href="/api/tasks">JSON API</a></p>
{body}</body></html>"""


def pill(status, stale=False, stuck=False, resume_at=None):
    if stale:
        return '<span class="pill stale">STALE — heartbeat missed</span>'
    if stuck:
        return '<span class="pill stuck">STUCK — no progress</span>'
    if status == "waiting":
        when = (time.strftime("%H:%M", time.localtime(resume_at))
                if resume_at else "?")
        return (f'<span class="pill waiting">WAITING — quota resets ~{when}</span>')
    if status == "budget_hold":
        return '<span class="pill budget_hold">BUDGET HOLD — spend cap reached</span>'
    if status == "decomposed":
        return '<span class="pill decomposed">DECOMPOSED — subtasks running</span>'
    return (f'<span class="pill {status}">'
            f'{status.replace("_", " ").upper()}</span>')


def fmt_spend(t):
    """Compact 'used / cap' for the task list, e.g. '1.2k / 5k tok'."""
    if t.get("token_budget") is None and t.get("usd_budget") is None:
        return "—"
    parts = []
    if t.get("token_budget") is not None:
        parts.append(f"{_short(t['tokens_used'])} / {_short(t['token_budget'])} tok")
    if t.get("usd_budget") is not None:
        parts.append(f"${t['usd_used']:.2f} / ${t['usd_budget']:.2f}")
    return " · ".join(parts)


def _short(n):
    n = n or 0
    return f"{n / 1000:.1f}k" if n >= 1000 else str(int(n))


def alerts_banner(store):
    # Passive log only — the system handles problems itself; this is just
    # what the user sees when they check in. Nothing here ever pushes.
    items = []
    for a in store.alerts():
        when = time.strftime("%H:%M:%S", time.localtime(a["created_at"]))
        if a["level"] >= 3:
            label = "⚠️ <b>Couldn't finish on its own</b>"
            cls = "alert"
        elif a["level"] == 1 and a["message"].startswith("\U0001f4b0"):
            label = "💰 <b>Budget guardrail tripped</b>"
            cls = "notice"
        elif a["level"] == 1:
            label = "⏳ <b>Waiting on provider</b>"
            cls = "notice"
        else:
            label = "🔁 <b>Handled on its own</b>"
            cls = "alert"
        items.append(
            f"<div class='{cls}'>{label} ({when}): "
            f"{html.escape(a['message'])} "
            f"<a href='/ack?id={a['id']}'>acknowledge</a></div>")
    return "".join(items)


def recovery_feed_html(store, limit=20):
    """Pull-only 'handled on its own' record — proof the autonomy
    doctrine held while the user was away. Never pushes; just shows."""
    sentences = store.recovery_sentences(limit=limit)
    if not sentences:
        body = ("<p class='empty'>Nothing needed handling yet. When a worker"
                " dies, stalls, or a review fails, StayOnDuty fixes it on"
                " its own — and the record lands here.</p>")
    else:
        items = "".join(f"<li>🔁 {html.escape(s)}</li>" for s in sentences)
        body = f"<ul>{items}</ul>"
    return (f"<div class='recovery'><h2>Handled on its own</h2>{body}</div>")


def task_list(store):
    rows = store.db.execute(
        "SELECT * FROM tasks ORDER BY updated_at DESC").fetchall()
    if not rows:
        return "<p>No tasks yet.</p>"
    now = time.time()
    trs = []
    for r in rows:
        t = dict(r)
        is_stale = (t["status"] == "in_progress" and t["lease_expires_at"]
                    and t["lease_expires_at"] < now)
        is_stuck = (t["status"] == "in_progress" and (t["stuck_level"] or 0) > 0)
        if t["status"] == "in_progress" and t["lease_expires_at"]:
            lease = (f"expires in {int(t['lease_expires_at'] - now)}s"
                     if not is_stale else "EXPIRED")
        else:
            lease = "—"
        last = time.strftime("%H:%M:%S", time.localtime(t["updated_at"]))
        title = html.escape(t['title'])
        if t.get("parent_id"):
            title = f"↳ {title}"
        extra = ""
        if t["status"] == "decomposed":
            kids = store.children(t["id"])
            done_n = sum(1 for k in kids if k["status"] == "done")
            extra = f" <span class='meta'>{done_n}/{len(kids)} done</span>"
        trs.append(
            f"<tr><td><a href='/?task={t['id']}'>{t['id']}</a></td>"
            f"<td>{title}</td>"
            f"<td>{pill(t['status'], is_stale, is_stuck and not is_stale, t.get('resume_at'))}{extra}</td>"
            f"<td>{html.escape(fmt_spend(t))}</td>"
            f"<td>{html.escape(t['lease_owner'] or '—')}</td><td>{lease}</td>"
            f"<td>{t['attempts']}</td><td>{last}</td></tr>")
    return ("<table><tr><th>ID</th><th>Task</th><th>Status</th><th>Spend</th>"
            "<th>Worker</th>"
            "<th>Lease</th><th>Attempts</th><th>Last update</th></tr>"
            + "".join(trs) + "</table>")


def task_detail(store, task_id):
    t = store.get_task(task_id)
    if not t:
        return "<p>Unknown task. <a href='/'>Back</a></p>"
    now = time.time()
    stale = (t["status"] == "in_progress" and t["lease_expires_at"]
             and t["lease_expires_at"] < now)
    is_stuck = (t["status"] == "in_progress" and (t["stuck_level"] or 0) > 0)
    events = "".join(
        f"<li><span class='meta'>{time.strftime('%H:%M:%S', time.localtime(e['at']))}</span> "
        f"<b>{html.escape(e['type'])}</b> {html.escape(e['detail'])}</li>"
        for e in store.events(task_id))
    mems = store.memories(scope=f"task:{task_id}")
    mem_html = ("".join(f"<li><b>{html.escape(k)}</b> = {html.escape(str(v))}</li>"
                        for k, v in mems.items()) or "<li><i>empty</i></li>")
    spend_html = ""
    if t.get("token_budget") is not None or t.get("usd_budget") is not None:
        spend_html = (f"<p><b>Spend:</b> {html.escape(fmt_spend(t))}"
                      + (f" <span class='meta'>— {html.escape(t.get('hold_reason') or '')}</span>"
                         if t["status"] == "budget_hold" else "") + "</p>")
    hold_form = ""
    if t["status"] == "budget_hold":
        hold_form = (
            f"<form method='post' action='/raise_budget'>"
            f"<input type='hidden' name='id' value='{html.escape(t['id'])}'>"
            f"Token cap: <input name='token_budget' size='8' placeholder='none'> "
            f"USD cap: <input name='usd_budget' size='8' placeholder='none'> "
            f"<button type='submit'>Raise budget &amp; resume</button>"
            f" <span class='meta'>(empty = no cap)</span></form>")
    crit_html = ""
    crits = t.get("acceptance_criteria") or []
    if crits:
        items = []
        for c in crits:
            icon = ("✅" if c["status"] == "passed"
                    else "❌" if c["status"] == "failed" else "○")
            ev = (f" <span class='meta'>— {html.escape(c['evidence'])}</span>"
                  if c.get("evidence") else "")
            items.append(f"<li>{icon} <b>{html.escape(c['id'])}</b> "
                         f"{html.escape(c['description'])}{ev}</li>")
        crit_html = (f"<h3>Acceptance criteria</h3><ul>{''.join(items)}</ul>")
    review_html = ""
    revs = store.reviews(task_id)
    if revs or t.get("verify_mode") == "independent":
        ritems = []
        for r in revs:
            icon = "✅" if r["verdict"] == "pass" else "❌"
            ritems.append(
                f"<li>{icon} <b>round {r['round']}</b> — "
                f"verifier {html.escape(r['verifier_owner'] or '')}: "
                f"{html.escape(r['verdict'])}"
                f" <span class='meta'>— {html.escape(r['reason'])}</span></li>")
        body = "".join(ritems) or "<li><i>awaiting a verifier</i></li>"
        vby = (f" <span class='meta'>— verified by "
               f"{html.escape(t['verified_by'])}</span>"
               if t.get("verified_by") else "")
        review_html = (f"<h3>Independent review{vby}</h3><ul>{body}</ul>")
    parent_html = ""
    if t.get("parent_id"):
        p = store.get_task(t["parent_id"])
        if p:
            parent_html = (
                f"<p><span class='meta'>subtask of</span> "
                f"<a href='/?task={html.escape(p['id'])}'>"
                f"{html.escape(p['title'])}</a> "
                f"{pill(p['status'])}</p>")
    kids = store.children(task_id)
    sub_html = ""
    if kids:
        items = []
        for k in kids:
            items.append(
                f"<li>{pill(k['status'])} "
                f"<a href='/?task={html.escape(k['id'])}'>"
                f"{html.escape(k['title'])}</a></li>")
        sub_html = (f"<h3>Subtasks ({sum(1 for k in kids if k['status'] == 'done')}"
                    f"/{len(kids)} done)</h3><ul>{''.join(items)}</ul>")
    return (f"<p><a href='/'>← all tasks</a></p>"
            f"<h2>{html.escape(t['title'])}</h2>"
            f"<p>{pill(t['status'], stale, is_stuck and not stale, t.get('resume_at'))} "
            f"<span class='meta'>attempts: {t['attempts']}</span></p>"
            f"{parent_html}{sub_html}"
            f"{spend_html}{hold_form}{crit_html}{review_html}"
            f"<h3>Timeline</h3><ul class='timeline'>{events}</ul>"
            f"<h3>Memory</h3><ul>{mem_html}</ul>")


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
                                        + recovery_feed_html(store)
                                        + task_list(store)))
            else:
                self._send(404, "text/plain", "not found")
        finally:
            store.close()


if __name__ == "__main__":
    print(f"StayOnDuty dashboard: http://localhost:{PORT}/  (db: {DB})", flush=True)
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
