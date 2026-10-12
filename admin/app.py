"""Agenndar admin: a small, separate app for the owner to see usage stats.

It runs as its own Render service from this same repo and reads the same
Turso database with a READ-ONLY token, so it can't change anything users see.

Render settings for this service:
  Build command:  pip install -r admin/requirements.txt
  Start command:  gunicorn admin.app:app
Environment variables:
  SECRET_KEY          a long random string (different from the main app's)
  ADMIN_PASSWORD      the password you'll use to log in here
  TURSO_DATABASE_URL  same as the main app
  TURSO_ADMIN_TOKEN   a read-only Turso token for that database
"""
import hmac
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, redirect, render_template, request, session, url_for
from flask_wtf import CSRFProtect

from turso_http import TursoHTTPConnection

app = Flask(__name__)
app.secret_key = os.environ["SECRET_KEY"]
app.config.update(
    SESSION_COOKIE_SECURE=os.environ.get("ADMIN_INSECURE_COOKIE") != "1",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=7 * 86400,
)
csrf = CSRFProtect(app)

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
TZ = ZoneInfo("Europe/London")

# Wrong-password lockout, kept in memory (one worker): after 5 misses in
# 15 minutes, logins pause for 15 minutes.
_misses = []
LOCK_WINDOW = 15 * 60
MAX_MISSES = 5


def get_db():
    return TursoHTTPConnection(os.environ["TURSO_DATABASE_URL"],
                               os.environ["TURSO_ADMIN_TOKEN"])


def rows(conn, sql, params=()):
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


@app.after_request
def no_index(resp):
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        now = time.time()
        _misses[:] = [t for t in _misses if now - t < LOCK_WINDOW]
        if len(_misses) >= MAX_MISSES:
            error = "Too many wrong tries. Wait 15 minutes and try again."
        elif ADMIN_PASSWORD and hmac.compare_digest(
                request.form.get("password", ""), ADMIN_PASSWORD):
            _misses.clear()
            session.clear()
            session.permanent = True
            session["admin"] = True
            return redirect(url_for("stats"))
        else:
            _misses.append(now)
            time.sleep(1)  # slows down guessing
            error = "Wrong password."
    return render_template("login.html", error=error,
                           configured=bool(ADMIN_PASSWORD))


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


def local_day(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d")


def ago(ts, now):
    if not ts:
        return "—"
    mins = int((now - ts) // 60)
    if mins < 2:
        return "just now"
    if mins < 60:
        return f"{mins} min ago"
    if mins < 1440:
        return f"{mins // 60} h ago"
    return f"{mins // 1440} d ago"


@app.route("/")
def stats():
    if not session.get("admin"):
        return redirect(url_for("login"))
    conn = get_db()
    now = time.time()
    day, week, month = now - 86400, now - 7 * 86400, now - 30 * 86400

    users = rows(conn, "SELECT id, username, terms_accepted_at, last_seen_at, "
                       "has_password, tour_seen FROM users")
    # Tasks people added themselves (rescheduled copies have an origin_id).
    added = {r["user_id"]: r["n"] for r in rows(
        conn, "SELECT user_id, COUNT(*) AS n FROM tasks WHERE origin_id IS NULL "
              "GROUP BY user_id")}
    recent_tasks = rows(conn, "SELECT created_at FROM tasks WHERE created_at >= ?",
                        (month - 86400,))
    reminder_users = rows(conn, "SELECT COUNT(DISTINCT user_id) AS n "
                                "FROM push_subscriptions")[0]["n"] or 0

    joined = lambda u: u.get("terms_accepted_at") or 0
    seen = lambda u: u.get("last_seen_at") or 0

    days = [local_day(now - i * 86400) for i in range(29, -1, -1)]
    signups_by_day = dict.fromkeys(days, 0)
    for u in users:
        if joined(u) and local_day(joined(u)) in signups_by_day:
            signups_by_day[local_day(joined(u))] += 1
    tasks_by_day = dict.fromkeys(days, 0)
    for t in recent_tasks:
        if local_day(t["created_at"]) in tasks_by_day:
            tasks_by_day[local_day(t["created_at"])] += 1

    stats = {
        "total": len(users),
        "new_week": sum(1 for u in users if joined(u) >= week),
        "new_month": sum(1 for u in users if joined(u) >= month),
        "active_day": sum(1 for u in users if seen(u) >= day),
        "active_week": sum(1 for u in users if seen(u) >= week),
        "active_month": sum(1 for u in users if seen(u) >= month),
        "google": sum(1 for u in users if u.get("has_password") == 0),
        "tour": sum(1 for u in users if u.get("tour_seen")),
        "with_tasks": sum(1 for u in users if added.get(u["id"])),
        "reminders": reminder_users,
    }
    recent = sorted(users, key=joined, reverse=True)[:20]
    table = [{
        "username": u["username"],
        "joined": datetime.fromtimestamp(joined(u), TZ).strftime("%d %b %Y")
                  if joined(u) else "Before tracking",
        "method": "Google" if u.get("has_password") == 0 else "Password",
        "tasks": added.get(u["id"], 0),
        "last_seen": ago(seen(u), now),
    } for u in recent]
    chart = {
        "labels": [datetime.strptime(d, "%Y-%m-%d").strftime("%d %b") for d in days],
        "signups": [signups_by_day[d] for d in days],
        "tasks": [tasks_by_day[d] for d in days],
    }
    return render_template("stats.html", stats=stats, rows=table, chart=chart,
                           updated=datetime.now(TZ).strftime("%d %b, %H:%M"))
