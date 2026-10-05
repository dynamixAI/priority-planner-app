import os
import json
import time
import secrets
import hashlib
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timedelta
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, send_from_directory
from flask_wtf import CSRFProtect
from werkzeug.security import generate_password_hash, check_password_hash
from turso_http import TursoHTTPConnection
from pywebpush import webpush, WebPushException

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY")
if not app.secret_key:
    raise RuntimeError("SECRET_KEY is not set — add it to your .env (local) or Render environment variables.")

# Protects every POST/PUT/DELETE route in the app. Real <form> submissions
# need a hidden csrf_token field; fetch()-based JSON requests need the same
# token sent as an X-CSRFToken header instead — both are added in the
# templates next, since this alone doesn't yet update any of them.
csrf = CSRFProtect(app)

TURSO_DATABASE_URL = os.environ["TURSO_DATABASE_URL"]
TURSO_AUTH_TOKEN = os.environ["TURSO_AUTH_TOKEN"]

VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_KEY_FILE = os.environ.get("VAPID_PRIVATE_KEY_FILE", "vapid_private.pem")
VAPID_CLAIMS = {"sub": "mailto:" + os.environ.get("SENDER_EMAIL", "admin@example.com")}

DEFAULT_PRIORITIES = [
    {"label": "Urgent & Important", "color": "#ef4444"},
    {"label": "Not Urgent, but Important", "color": "#3b82f6"},
    {"label": "Urgent, Not Important", "color": "#f59e0b"},
    {"label": "Not Urgent & Not Important", "color": "#10b981"}
]

TIME_SLOTS = [f"{h:02d}:00" for h in range(24)]
DEFAULT_ACTIVE_DAYS = ["0", "1", "2", "3", "4", "5", "6"]


def get_db():
    return TursoHTTPConnection(TURSO_DATABASE_URL, TURSO_AUTH_TOKEN)


def rows_to_dicts(cur, rows):
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in rows]


def fetch_all(conn, sql, params=()):
    cur = conn.execute(sql, params)
    return rows_to_dicts(cur, cur.fetchall())


def fetch_one(conn, sql, params=()):
    rows = fetch_all(conn, sql, params)
    return rows[0] if rows else None


def send_reset_email(to_email, reset_link):
    smtp_server = os.environ["SMTP_SERVER"]
    smtp_port = int(os.environ.get("SMTP_PORT", 587))
    smtp_login = os.environ["SMTP_LOGIN"]
    smtp_password = os.environ["SMTP_PASSWORD"]
    sender_email = os.environ["SENDER_EMAIL"]

    body = (
        "Hi,\n\n"
        "We received a request to reset your Agenndar password. "
        "Click the link below to choose a new one:\n\n"
        f"{reset_link}\n\n"
        "This link expires in 1 hour. If you didn't request this, you can safely ignore this email."
    )
    msg = MIMEText(body)
    msg["Subject"] = "Reset your Agenndar password"
    msg["From"] = sender_email
    msg["To"] = to_email

    with smtplib.SMTP(smtp_server, smtp_port) as server:
        server.starttls()
        server.login(smtp_login, smtp_password)
        server.sendmail(sender_email, [to_email], msg.as_string())


def seed_default_priorities(conn, user_id):
    for i, p in enumerate(DEFAULT_PRIORITIES):
        conn.execute(
            "INSERT INTO priorities (user_id, label, color, sort_order, is_active) VALUES (?, ?, ?, ?, 1)",
            (user_id, p["label"], p["color"], i)
        )
    conn.commit()


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            theme TEXT DEFAULT 'light',
            week_start TEXT DEFAULT 'sunday',
            active_days TEXT DEFAULT '["0","1","2","3","4","5","6"]',
            failed_attempts INTEGER DEFAULT 0,
            lock_until REAL DEFAULT 0,
            is_permanently_locked INTEGER DEFAULT 0,
            overdue_threshold_days INTEGER DEFAULT 14,
            reset_token TEXT,
            reset_token_expiry REAL DEFAULT 0,
            grid_orientation TEXT DEFAULT 'time_rows'
        );
    """)
    # Guarded migrations for databases created before these columns existed —
    # ALTER TABLE ADD COLUMN has no "IF NOT EXISTS" in SQLite/libSQL, so we
    # just try each and ignore the error if the column is already there.
    for stmt in [
        "ALTER TABLE users ADD COLUMN overdue_threshold_days INTEGER DEFAULT 14;",
        "ALTER TABLE users ADD COLUMN reset_token TEXT;",
        "ALTER TABLE users ADD COLUMN reset_token_expiry REAL DEFAULT 0;",
        "ALTER TABLE users ADD COLUMN grid_orientation TEXT DEFAULT 'time_rows';",
        "ALTER TABLE tasks ADD COLUMN reminder_sent INTEGER DEFAULT 0;",
        "ALTER TABLE users ADD COLUMN terms_accepted_at REAL DEFAULT 0;",
        "ALTER TABLE users ADD COLUMN marketing_opt_in INTEGER DEFAULT 0;",
    ]:
        try:
            conn.execute(stmt)
            conn.commit()
        except Exception:
            pass
    conn.execute("""
        CREATE TABLE IF NOT EXISTS priorities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            label TEXT NOT NULL,
            color TEXT NOT NULL,
            sort_order INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS push_subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            endpoint TEXT NOT NULL UNIQUE,
            p256dh TEXT NOT NULL,
            auth TEXT NOT NULL,
            created_at REAL DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
    """)
    conn.commit()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            detail TEXT,
            location TEXT,
            date TEXT NOT NULL,
            time TEXT NOT NULL,
            duration TEXT NOT NULL,
            duration_minutes INTEGER DEFAULT 60,
            priority_id INTEGER,
            status TEXT DEFAULT 'pending',
            origin_id INTEGER,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (priority_id) REFERENCES priorities(id),
            FOREIGN KEY (origin_id) REFERENCES tasks(id)
        );
    """)
    # One-time migration for databases created before duration_minutes existed:
    # add the column (no default, so existing rows land as NULL), then backfill
    # from the old text durations. The UPDATE only touches NULL rows, so it's
    # safe — and necessary — to run on every startup.
    try:
        conn.execute("ALTER TABLE tasks ADD COLUMN duration_minutes INTEGER;")
        conn.commit()
    except Exception:
        pass
    try:
        conn.execute("""
            UPDATE tasks SET duration_minutes = CASE duration
                WHEN '30 mins' THEN 30
                WHEN '45 mins' THEN 45
                WHEN '1 hour' THEN 60
                WHEN '1.5 hours' THEN 90
                WHEN '2 hours' THEN 120
                ELSE 60
            END
            WHERE duration_minutes IS NULL;
        """)
        conn.commit()
    except Exception:
        pass
    conn.commit()


init_db()


@app.route("/")
def home():
    if "user_id" not in session:
        return render_template("welcome.html")
    return redirect(url_for("dashboard"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        agree_terms = request.form.get("agree_terms")
        marketing_opt_in = 1 if request.form.get("marketing_opt_in") else 0

        if not username or not email or not password or not confirm_password:
            flash("All fields are required.", "error")
            return redirect(url_for("register"))
        if password != confirm_password:
            flash("Passwords do not match.", "error")
            return redirect(url_for("register"))
        if not agree_terms:
            flash("You must agree to the Terms of Service and Privacy Policy to create an account.", "error")
            return redirect(url_for("register"))

        conn = get_db()
        existing = fetch_one(conn, "SELECT id FROM users WHERE username = ? OR email = ?", (username, email))
        if existing:
            flash("Username or email already registered.", "error")
            return redirect(url_for("register"))

        hashed = generate_password_hash(password)
        conn.execute(
            "INSERT INTO users (username, email, password_hash, theme, week_start, active_days, failed_attempts, lock_until, is_permanently_locked, terms_accepted_at, marketing_opt_in) VALUES (?, ?, ?, 'light', 'sunday', ?, 0, 0, 0, ?, ?)",
            (username, email, hashed, json.dumps(DEFAULT_ACTIVE_DAYS), time.time(), marketing_opt_in)
        )
        conn.commit()

        new_user = fetch_one(conn, "SELECT id FROM users WHERE username = ?", (username,))
        user_id = new_user["id"]
        seed_default_priorities(conn, user_id)

        session["user_id"] = user_id
        session["username"] = username
        return redirect(url_for("priority_setup"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        now = time.time()

        conn = get_db()
        user = fetch_one(conn, "SELECT * FROM users WHERE username = ? OR email = ?", (username, username.lower()))
        if not user:
            flash("Invalid credentials.", "error")
            return redirect(url_for("login"))

        if user["is_permanently_locked"]:
            flash("Account locked due to excessive failed attempts.", "error")
            return redirect(url_for("login"))

        if user["lock_until"] and now < user["lock_until"]:
            mins_left = int((user["lock_until"] - now) // 60) + 1
            flash(f"Account locked. Try again in {mins_left} min(s).", "error")
            return redirect(url_for("login"))

        if check_password_hash(user["password_hash"], password):
            conn.execute("UPDATE users SET failed_attempts = 0, lock_until = 0 WHERE id = ?", (user["id"],))
            conn.commit()
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            return redirect(url_for("dashboard"))
        else:
            attempts = user["failed_attempts"] + 1
            lock_until = 0
            perm_lock = 0
            if attempts >= 9:
                perm_lock = 1
            elif attempts >= 8:
                lock_until = now + 300
            elif attempts >= 5:
                lock_until = now + 120

            conn.execute(
                "UPDATE users SET failed_attempts = ?, lock_until = ?, is_permanently_locked = ? WHERE id = ?",
                (attempts, lock_until, perm_lock, user["id"])
            )
            conn.commit()
            flash("Invalid password.", "error")
            return redirect(url_for("login"))

    return render_template("login.html")


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        conn = get_db()
        user = fetch_one(conn, "SELECT * FROM users WHERE email = ?", (email,))
        if user:
            raw_token = secrets.token_urlsafe(32)
            token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
            expiry = time.time() + 3600  # 1 hour

            conn.execute(
                "UPDATE users SET reset_token = ?, reset_token_expiry = ? WHERE id = ?",
                (token_hash, expiry, user["id"])
            )
            conn.commit()

            reset_link = url_for("reset_password", token=raw_token, _external=True)
            try:
                send_reset_email(user["email"], reset_link)
            except Exception:
                pass  # never reveal whether sending succeeded — same message either way

        flash("If that email is on file, reset instructions have been sent.", "info")
        return redirect(url_for("login"))
    return render_template("forgot_password.html")


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    conn = get_db()
    user = fetch_one(conn, "SELECT * FROM users WHERE reset_token = ?", (token_hash,))

    if not user or not user["reset_token_expiry"] or time.time() > user["reset_token_expiry"]:
        flash("That reset link is invalid or has expired. Please request a new one.", "error")
        return redirect(url_for("forgot_password"))

    if request.method == "POST":
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not password or password != confirm_password:
            flash("Passwords do not match.", "error")
            return redirect(url_for("reset_password", token=token))

        hashed = generate_password_hash(password)
        conn.execute(
            "UPDATE users SET password_hash = ?, reset_token = NULL, reset_token_expiry = 0, "
            "failed_attempts = 0, lock_until = 0, is_permanently_locked = 0 WHERE id = ?",
            (hashed, user["id"])
        )
        conn.commit()

        flash("Your password has been reset. You can now log in.", "info")
        return redirect(url_for("login"))

    return render_template("reset_password.html")


@app.route("/service-worker.js")
def service_worker():
    # Served from the root path (not /static/) so its default scope covers
    # the whole site, not just /static/ — otherwise it can never control
    # pages like /dashboard, and anything waiting on it hangs forever.
    response = send_from_directory("static", "service-worker.js")
    response.headers["Service-Worker-Allowed"] = "/"
    return response


@app.route("/privacy")
def privacy():
    return render_template("privacy.html", updated_date=datetime.today().strftime("%d %B %Y"))


@app.route("/terms")
def terms():
    return render_template("terms.html", updated_date=datetime.today().strftime("%d %B %Y"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


@app.route("/api/theme", methods=["POST"])
def update_theme():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    new_theme = data.get("theme", "light")
    conn = get_db()
    conn.execute("UPDATE users SET theme = ? WHERE id = ?", (new_theme, session["user_id"]))
    conn.commit()
    return jsonify({"status": "success", "theme": new_theme})


@app.route("/settings", methods=["GET", "POST"])
@app.route("/priority-setup", methods=["GET", "POST"])
def priority_setup():
    if "user_id" not in session:
        return redirect(url_for("login"))
    user_id = session["user_id"]
    conn = get_db()

    if request.method == "POST":
        ids = request.form.getlist("ids[]")
        labels = request.form.getlist("labels[]")
        colors = request.form.getlist("colors[]")
        week_start = request.form.get("week_start", "sunday")
        active_days = request.form.getlist("active_days[]")
        if not active_days:
            active_days = DEFAULT_ACTIVE_DAYS

        try:
            overdue_threshold_days = int(request.form.get("overdue_threshold_days", 14))
        except ValueError:
            overdue_threshold_days = 14
        overdue_threshold_days = max(1, min(365, overdue_threshold_days))

        grid_orientation = request.form.get("grid_orientation", "time_rows")
        if grid_orientation not in ("time_rows", "day_rows"):
            grid_orientation = "time_rows"

        # Guard: a stale or empty form submission must never wipe existing tiers.
        if not any(label.strip() for label in labels):
            flash("No priority tiers were submitted — nothing was changed. Refresh the page and try again.", "error")
            return redirect(url_for("priority_setup"))

        existing = fetch_all(conn, "SELECT id FROM priorities WHERE user_id = ? AND is_active = 1", (user_id,))
        existing_ids = {str(p["id"]) for p in existing}
        kept_ids = set()

        for i, (pid, label, color) in enumerate(zip(ids, labels, colors)):
            clean_label = label.strip()
            if not clean_label:
                continue
            if pid and pid in existing_ids:
                conn.execute(
                    "UPDATE priorities SET label = ?, color = ?, sort_order = ? WHERE id = ? AND user_id = ?",
                    (clean_label, color, i, pid, user_id)
                )
                kept_ids.add(pid)
            else:
                conn.execute(
                    "INSERT INTO priorities (user_id, label, color, sort_order, is_active) VALUES (?, ?, ?, ?, 1)",
                    (user_id, clean_label, color, i)
                )

        for rid in existing_ids - kept_ids:
            conn.execute("UPDATE priorities SET is_active = 0 WHERE id = ? AND user_id = ?", (rid, user_id))

        conn.execute(
            "UPDATE users SET week_start = ?, active_days = ?, overdue_threshold_days = ?, grid_orientation = ? WHERE id = ?",
            (week_start, json.dumps(active_days), overdue_threshold_days, grid_orientation, user_id)
        )
        conn.commit()

        remaining = fetch_all(conn, "SELECT id FROM priorities WHERE user_id = ? AND is_active = 1", (user_id,))
        if not remaining:
            seed_default_priorities(conn, user_id)

        return redirect(url_for("dashboard"))

    user = fetch_one(conn, "SELECT theme, week_start, active_days, overdue_threshold_days, grid_orientation FROM users WHERE id = ?", (user_id,))
    priorities = fetch_all(
        conn, "SELECT * FROM priorities WHERE user_id = ? AND is_active = 1 ORDER BY sort_order ASC", (user_id,)
    )
    theme = user["theme"] if user and user["theme"] else "light"
    week_start = user["week_start"] if user and user["week_start"] else "sunday"
    active_days = json.loads(user["active_days"]) if user and user["active_days"] else DEFAULT_ACTIVE_DAYS
    overdue_threshold_days = user["overdue_threshold_days"] if user and user["overdue_threshold_days"] else 14
    grid_orientation = user.get("grid_orientation") if user and user.get("grid_orientation") else "time_rows"

    return render_template(
        "priority_setup.html", priorities=priorities, theme=theme, week_start=week_start,
        active_days=active_days, overdue_threshold_days=overdue_threshold_days,
        grid_orientation=grid_orientation
    )


@app.route("/dashboard")
def dashboard():
    if "user_id" not in session:
        return redirect(url_for("login"))

    user_id = session["user_id"]
    week_param = request.args.get("week_start")
    conn = get_db()

    # Auto-missed grace window: a task stays open all day on its scheduled
    # date, even after its time slot passes — it only flips to 'missed' once
    # the calendar date itself has moved on. Runs on every dashboard load
    # since there's no background job in this setup; if you never open the
    # app, a task won't flip until the next time you do.
    today_str = datetime.today().date().strftime("%Y-%m-%d")
    conn.execute(
        "UPDATE tasks SET status = 'missed' WHERE user_id = ? AND status = 'pending' AND date < ?",
        (user_id, today_str)
    )
    conn.commit()

    user = fetch_one(conn, "SELECT theme, week_start, active_days, overdue_threshold_days, grid_orientation FROM users WHERE id = ?", (user_id,))
    priorities = fetch_all(
        conn, "SELECT * FROM priorities WHERE user_id = ? AND is_active = 1 ORDER BY sort_order ASC", (user_id,)
    )
    theme = user["theme"] if user and user["theme"] else "light"
    week_start_pref = user["week_start"] if user and user["week_start"] else "sunday"
    grid_orientation = user.get("grid_orientation") if user and user.get("grid_orientation") else "time_rows"
    active_days_list = json.loads(user["active_days"]) if user and user["active_days"] else DEFAULT_ACTIVE_DAYS

    today = datetime.today().date()
    if week_param:
        try:
            start_date = datetime.strptime(week_param, "%Y-%m-%d").date()
        except ValueError:
            start_date = today
    else:
        if week_start_pref == "monday":
            start_date = today - timedelta(days=today.weekday())
        else:
            days_since_sunday = (today.weekday() + 1) % 7
            start_date = today - timedelta(days=days_since_sunday)

    full_week_dates = [start_date + timedelta(days=i) for i in range(7)]

    def day_to_code(d):
        return str((d.weekday() + 1) % 7)

    visible_week_dates = [d for d in full_week_dates if day_to_code(d) in active_days_list]
    if not visible_week_dates:
        visible_week_dates = full_week_dates

    prev_week = (start_date - timedelta(days=7)).strftime("%Y-%m-%d")
    next_week = (start_date + timedelta(days=7)).strftime("%Y-%m-%d")
    cur_week_str = start_date.strftime("%Y-%m-%d")

    week_date_strs = [d.strftime("%Y-%m-%d") for d in visible_week_dates]
    if week_date_strs:
        placeholders = ",".join("?" for _ in week_date_strs)
        tasks = fetch_all(
            conn, f"SELECT * FROM tasks WHERE user_id = ? AND date IN ({placeholders}) ORDER BY time ASC",
            [user_id] + week_date_strs
        )
    else:
        tasks = []

    all_user_tasks = fetch_all(conn, "SELECT * FROM tasks WHERE user_id = ? ORDER BY date ASC, time ASC", (user_id,))
    total_tasks = len(all_user_tasks)
    done_tasks = sum(1 for t in all_user_tasks if t["status"] == "completed")
    pushed_tasks = sum(1 for t in all_user_tasks if t["status"] == "rescheduled")
    missed_tasks = sum(1 for t in all_user_tasks if t["status"] == "missed")
    undone_tasks = sum(1 for t in all_user_tasks if t["status"] == "pending")
    replanned_tasks = sum(1 for t in all_user_tasks if t["status"] == "replanned")

    now_dt = datetime.now()

    def is_not_yet_due(t):
        # A pending task whose scheduled time hasn't arrived yet shouldn't be
        # judged at all — you can't have missed or completed something before
        # its own moment has come. It only enters the score once it's overdue.
        if t["status"] != "pending":
            return False
        try:
            scheduled_dt = datetime.strptime(f"{t['date']} {t['time']}", "%Y-%m-%d %H:%M")
        except ValueError:
            return False
        return scheduled_dt > now_dt

    not_yet_due_tasks = sum(1 for t in all_user_tasks if is_not_yet_due(t))

    # Exclude 'replanned' rows (free moves) and not-yet-due pending tasks from
    # the BHI denominator. They still count toward total_tasks / "Total Logged"
    # above, since that tracks every row ever entered regardless of timing.
    bhi_total = total_tasks - replanned_tasks - not_yet_due_tasks

    # Overdue-threshold penalty: a still-open task, chronically outstanding
    # since its FIRST scheduled date (even across pushes/replans), costs a
    # fixed 0.3 points once it crosses the user's own configured day count.
    task_by_id = {t["id"]: t for t in all_user_tasks}
    overdue_threshold_days = user["overdue_threshold_days"] if user and user["overdue_threshold_days"] else 14

    def get_origin_date(t):
        if t["origin_id"] and t["origin_id"] in task_by_id:
            return task_by_id[t["origin_id"]]["date"]
        return t["date"]

    def is_flagged_overdue(t):
        if t["status"] != "pending":
            return False
        try:
            origin_date = datetime.strptime(get_origin_date(t), "%Y-%m-%d").date()
        except ValueError:
            return False
        return (today - origin_date).days >= overdue_threshold_days

    overdue_flagged_tasks = sum(1 for t in all_user_tasks if is_flagged_overdue(t))

    if bhi_total > 0:
        score_raw = (
            (done_tasks * 1.0) + (pushed_tasks * 0.4)
            - (missed_tasks * 0.6) - (overdue_flagged_tasks * 0.3)
        ) / bhi_total * 100
        bhi_score = max(0, min(100, round(score_raw, 1)))
    else:
        bhi_score = 100.0

    if bhi_score >= 80:
        bhi_tier, bhi_color = "Disciplined", "#16a34a"
    elif bhi_score >= 50:
        bhi_tier, bhi_color = "Drifting", "#eab308"
    else:
        bhi_tier, bhi_color = "Avoidance", "#ef4444"

    week_dates_iso = [d.strftime("%Y-%m-%d") for d in visible_week_dates]

    return render_template(
        "dashboard.html",
        username=session.get("username"),
        theme=theme,
        priorities=priorities,
        tasks=tasks,
        all_tasks=all_user_tasks,
        week_dates=visible_week_dates,
        week_dates_iso=week_dates_iso,
        week_start_pref=week_start_pref,
        grid_orientation=grid_orientation,
        active_days=active_days_list,
        prev_week=prev_week,
        next_week=next_week,
        cur_week_str=cur_week_str,
        time_slots=TIME_SLOTS,
        metrics={
            "total_logged": total_tasks,
            "total": total_tasks,
            "done": done_tasks,
            "undone": undone_tasks,
            "pushed": pushed_tasks,
            "missed": missed_tasks,
            "bhi_score": bhi_score,
            "bhi_tier": bhi_tier,
            "bhi_color": bhi_color
        }
    )


@app.route("/trends")
def trends():
    if "user_id" not in session:
        return redirect(url_for("login"))
    user_id = session["user_id"]
    conn = get_db()

    user = fetch_one(conn, "SELECT theme FROM users WHERE id = ?", (user_id,))
    theme = user["theme"] if user and user["theme"] else "light"

    try:
        days = int(request.args.get("days", 14))
    except ValueError:
        days = 14
    if days not in (7, 14, 30, 90):
        days = 14

    all_user_tasks = fetch_all(conn, "SELECT * FROM tasks WHERE user_id = ? ORDER BY date ASC, time ASC", (user_id,))
    today = datetime.today().date()

    chart_labels, chart_completed, chart_pushed, chart_missed, chart_rate = [], [], [], [], []
    for i in range(days - 1, -1, -1):
        day_cursor = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        chart_labels.append((today - timedelta(days=i)).strftime("%d %b"))
        day_tasks = [t for t in all_user_tasks if t["date"] == day_cursor]
        c_cnt = sum(1 for t in day_tasks if t["status"] == "completed")
        p_cnt = sum(1 for t in day_tasks if t["status"] == "rescheduled")
        m_cnt = sum(1 for t in day_tasks if t["status"] == "missed")
        t_cnt = len(day_tasks)
        rate = round((c_cnt / t_cnt * 100), 1) if t_cnt > 0 else 0
        chart_completed.append(c_cnt)
        chart_pushed.append(p_cnt)
        chart_missed.append(m_cnt)
        chart_rate.append(rate)

    return render_template(
        "trends.html",
        theme=theme,
        days=days,
        chart_data={
            "labels": chart_labels,
            "completed": chart_completed,
            "pushed": chart_pushed,
            "missed": chart_missed,
            "rate": chart_rate
        }
    )


@app.route("/api/tasks/week")
def api_tasks_week():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    user_id = session["user_id"]
    week_param = request.args.get("week_start")
    conn = get_db()

    user = fetch_one(conn, "SELECT week_start, active_days FROM users WHERE id = ?", (user_id,))
    week_start_pref = user["week_start"] if user and user["week_start"] else "sunday"
    active_days_list = json.loads(user["active_days"]) if user and user["active_days"] else DEFAULT_ACTIVE_DAYS

    today = datetime.today().date()
    if week_param:
        try:
            start_date = datetime.strptime(week_param, "%Y-%m-%d").date()
        except ValueError:
            start_date = today
    else:
        if week_start_pref == "monday":
            start_date = today - timedelta(days=today.weekday())
        else:
            days_since_sunday = (today.weekday() + 1) % 7
            start_date = today - timedelta(days=days_since_sunday)

    full_week_dates = [start_date + timedelta(days=i) for i in range(7)]

    def day_to_code(d):
        return str((d.weekday() + 1) % 7)

    visible_week_dates = [d for d in full_week_dates if day_to_code(d) in active_days_list]
    if not visible_week_dates:
        visible_week_dates = full_week_dates

    prev_week = (start_date - timedelta(days=7)).strftime("%Y-%m-%d")
    next_week = (start_date + timedelta(days=7)).strftime("%Y-%m-%d")
    cur_week_str = start_date.strftime("%Y-%m-%d")

    week_date_strs = [d.strftime("%Y-%m-%d") for d in visible_week_dates]
    if week_date_strs:
        placeholders = ",".join("?" for _ in week_date_strs)
        tasks = fetch_all(
            conn, f"SELECT * FROM tasks WHERE user_id = ? AND date IN ({placeholders}) ORDER BY time ASC",
            [user_id] + week_date_strs
        )
    else:
        tasks = []

    day_labels = [d.strftime("%A") for d in visible_week_dates]
    day_short = [d.strftime("%d %b") for d in visible_week_dates]
    week_title = f'Week of {visible_week_dates[0].strftime("%B %d, %Y")}' if visible_week_dates else ""

    return jsonify({
        "tasks": tasks,
        "week_dates_iso": week_date_strs,
        "day_labels": day_labels,
        "day_short": day_short,
        "week_title": week_title,
        "prev_week": prev_week,
        "next_week": next_week,
        "cur_week_str": cur_week_str
    })


@app.route("/api/metrics")
def api_metrics():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    user_id = session["user_id"]
    conn = get_db()

    today_str = datetime.today().date().strftime("%Y-%m-%d")
    conn.execute(
        "UPDATE tasks SET status = 'missed' WHERE user_id = ? AND status = 'pending' AND date < ?",
        (user_id, today_str)
    )
    conn.commit()

    user = fetch_one(conn, "SELECT overdue_threshold_days FROM users WHERE id = ?", (user_id,))
    all_user_tasks = fetch_all(conn, "SELECT * FROM tasks WHERE user_id = ? ORDER BY date ASC, time ASC", (user_id,))

    total_tasks = len(all_user_tasks)
    done_tasks = sum(1 for t in all_user_tasks if t["status"] == "completed")
    pushed_tasks = sum(1 for t in all_user_tasks if t["status"] == "rescheduled")
    missed_tasks = sum(1 for t in all_user_tasks if t["status"] == "missed")
    undone_tasks = sum(1 for t in all_user_tasks if t["status"] == "pending")
    replanned_tasks = sum(1 for t in all_user_tasks if t["status"] == "replanned")

    today = datetime.today().date()
    now_dt = datetime.now()

    def is_not_yet_due(t):
        if t["status"] != "pending":
            return False
        try:
            scheduled_dt = datetime.strptime(f"{t['date']} {t['time']}", "%Y-%m-%d %H:%M")
        except ValueError:
            return False
        return scheduled_dt > now_dt

    not_yet_due_tasks = sum(1 for t in all_user_tasks if is_not_yet_due(t))
    bhi_total = total_tasks - replanned_tasks - not_yet_due_tasks

    task_by_id = {t["id"]: t for t in all_user_tasks}
    overdue_threshold_days = user["overdue_threshold_days"] if user and user["overdue_threshold_days"] else 14

    def get_origin_date(t):
        if t["origin_id"] and t["origin_id"] in task_by_id:
            return task_by_id[t["origin_id"]]["date"]
        return t["date"]

    def is_flagged_overdue(t):
        if t["status"] != "pending":
            return False
        try:
            origin_date = datetime.strptime(get_origin_date(t), "%Y-%m-%d").date()
        except ValueError:
            return False
        return (today - origin_date).days >= overdue_threshold_days

    overdue_flagged_tasks = sum(1 for t in all_user_tasks if is_flagged_overdue(t))

    if bhi_total > 0:
        score_raw = (
            (done_tasks * 1.0) + (pushed_tasks * 0.4)
            - (missed_tasks * 0.6) - (overdue_flagged_tasks * 0.3)
        ) / bhi_total * 100
        bhi_score = max(0, min(100, round(score_raw, 1)))
    else:
        bhi_score = 100.0

    if bhi_score >= 80:
        bhi_tier, bhi_color = "Disciplined", "#16a34a"
    elif bhi_score >= 50:
        bhi_tier, bhi_color = "Drifting", "#eab308"
    else:
        bhi_tier, bhi_color = "Avoidance", "#ef4444"

    return jsonify({
        "all_tasks": all_user_tasks,
        "metrics": {
            "total_logged": total_tasks,
            "total": total_tasks,
            "done": done_tasks,
            "undone": undone_tasks,
            "pushed": pushed_tasks,
            "missed": missed_tasks,
            "bhi_score": bhi_score,
            "bhi_tier": bhi_tier,
            "bhi_color": bhi_color
        }
    })


@app.route("/api/task/add", methods=["POST"])
def add_task():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    time_val = data.get("time", "09:00")
    if len(time_val) == 4:
        time_val = "0" + time_val

    try:
        duration_minutes = int(data.get("duration_minutes", 60))
    except (ValueError, TypeError):
        duration_minutes = 60
    duration_minutes = max(5, min(1440, duration_minutes))

    conn = get_db()
    conn.execute(
        "INSERT INTO tasks (user_id, title, detail, location, date, time, duration, duration_minutes, priority_id, status, origin_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL)",
        (session["user_id"], data.get("title"), data.get("detail"), data.get("location", ""),
         data.get("date"), time_val, data.get("duration"), duration_minutes, data.get("priority_id"))
    )
    conn.commit()
    return jsonify({"status": "success"})


@app.route("/api/task/status", methods=["POST"])
def update_status():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    task_id = data.get("task_id")
    target_status = data.get("status")

    conn = get_db()
    task = fetch_one(conn, "SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, session["user_id"]))
    if not task:
        return jsonify({"error": "Not found"}), 404

    if task["status"] in ("rescheduled", "replanned") and target_status == "completed":
        return jsonify({"error": "This task was moved to a new time — complete it there instead."}), 400

    conn.execute("UPDATE tasks SET status = ? WHERE id = ? AND user_id = ?", (target_status, task_id, session["user_id"]))
    conn.commit()
    return jsonify({"status": "success"})


@app.route("/api/task/reschedule", methods=["POST"])
def reschedule_task():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    task_id = data.get("task_id")
    new_date = data.get("new_date")
    new_time = data.get("new_time")

    conn = get_db()
    task = fetch_one(conn, "SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, session["user_id"]))
    if not task:
        return jsonify({"error": "Not found"}), 404

    if task["status"] in ("rescheduled", "replanned"):
        return jsonify({"error": "Task has already been rescheduled."}), 400
    if task["status"] == "completed":
        return jsonify({"error": "This task is already completed and can't be rescheduled."}), 400

    origin = task["origin_id"] if task["origin_id"] else task["id"]

    if task["status"] == "missed":
        # A miss is permanent — rescheduling creates a fresh follow-up task,
        # it does NOT undo or soften the original miss's BHI penalty.
        result_label = "missed_kept"
        # leave the original row's status untouched — it stays 'missed'
    else:
        # Was this task already due at the moment we're rescheduling it?
        # Only an already-overdue push counts against PUSHED/BHI — moving
        # something before it's due is a free replan, not a penalized push.
        try:
            scheduled_dt = datetime.strptime(f"{task['date']} {task['time']}", "%Y-%m-%d %H:%M")
        except ValueError:
            scheduled_dt = datetime.now()  # fail safe: treat as due if unparsable

        is_overdue = scheduled_dt <= datetime.now()
        original_new_status = "rescheduled" if is_overdue else "replanned"
        result_label = "pushed" if is_overdue else "replanned"
        conn.execute("UPDATE tasks SET status = ? WHERE id = ? AND user_id = ?", (original_new_status, task_id, session["user_id"]))

    conn.execute(
        "INSERT INTO tasks (user_id, title, detail, location, date, time, duration, duration_minutes, priority_id, status, origin_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
        (session["user_id"], task["title"], task["detail"], task["location"],
         new_date, new_time, task["duration"], task["duration_minutes"] or 60, task["priority_id"], origin)
    )
    conn.commit()
    return jsonify({"status": "success", "result": result_label})


@app.route("/api/task/delete", methods=["POST"])
def delete_task():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    task_id = data.get("task_id")
    conn = get_db()

    # A task can be the origin that other rows in its reschedule chain point
    # back to. Deleting it while descendants still reference it violates the
    # origin_id foreign key — so detach any descendants first (they become
    # standalone tasks, losing only their link back to this one) before the
    # actual delete.
    conn.execute(
        "UPDATE tasks SET origin_id = NULL WHERE origin_id = ? AND user_id = ?",
        (task_id, session["user_id"])
    )
    conn.execute("DELETE FROM tasks WHERE id = ? AND user_id = ?", (task_id, session["user_id"]))
    conn.commit()
    return jsonify({"status": "success"})


@app.route("/api/account/reset", methods=["POST"])
def reset_account():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    user_id = session["user_id"]
    conn = get_db()

    conn.execute("DELETE FROM tasks WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM priorities WHERE user_id = ?", (user_id,))
    conn.execute(
        "UPDATE users SET week_start = 'sunday', active_days = ?, overdue_threshold_days = 14, grid_orientation = 'time_rows' WHERE id = ?",
        (json.dumps(DEFAULT_ACTIVE_DAYS), user_id)
    )
    conn.commit()

    seed_default_priorities(conn, user_id)

    return jsonify({"status": "success"})


@app.route("/api/account/delete", methods=["POST"])
def delete_account():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    user_id = session["user_id"]
    data = request.get_json()
    password = data.get("password", "")

    conn = get_db()
    user = fetch_one(conn, "SELECT * FROM users WHERE id = ?", (user_id,))
    if not user or not check_password_hash(user["password_hash"], password):
        return jsonify({"error": "Incorrect password."}), 400

    conn.execute("DELETE FROM tasks WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM priorities WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM push_subscriptions WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()

    session.clear()
    return jsonify({"status": "success"})


@app.route("/api/task/detail", methods=["POST"])
def update_task_detail():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    task_id = data.get("task_id")
    new_detail = data.get("detail", "")

    conn = get_db()
    task = fetch_one(conn, "SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, session["user_id"]))
    if not task:
        return jsonify({"error": "Not found"}), 404

    conn.execute("UPDATE tasks SET detail = ? WHERE id = ? AND user_id = ?", (new_detail, task_id, session["user_id"]))
    conn.commit()
    return jsonify({"status": "success"})


@app.route("/api/push/public-key")
def push_public_key():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify({"publicKey": VAPID_PUBLIC_KEY})


@app.route("/api/push/subscribe", methods=["POST"])
def push_subscribe():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    endpoint = data.get("endpoint")
    keys = data.get("keys", {})
    p256dh = keys.get("p256dh")
    auth = keys.get("auth")

    if not endpoint or not p256dh or not auth:
        return jsonify({"error": "Invalid subscription"}), 400

    conn = get_db()
    existing = fetch_one(conn, "SELECT id FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
    if existing:
        conn.execute(
            "UPDATE push_subscriptions SET user_id = ?, p256dh = ?, auth = ? WHERE endpoint = ?",
            (session["user_id"], p256dh, auth, endpoint)
        )
    else:
        conn.execute(
            "INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth, created_at) VALUES (?, ?, ?, ?, ?)",
            (session["user_id"], endpoint, p256dh, auth, time.time())
        )
    conn.commit()
    return jsonify({"status": "success"})


@app.route("/api/push/unsubscribe", methods=["POST"])
def push_unsubscribe():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    endpoint = data.get("endpoint")
    if not endpoint:
        return jsonify({"error": "Missing endpoint"}), 400

    conn = get_db()
    conn.execute(
        "DELETE FROM push_subscriptions WHERE endpoint = ? AND user_id = ?",
        (endpoint, session["user_id"])
    )
    conn.commit()
    return jsonify({"status": "success"})


@app.route("/api/cron/send-reminders")
def send_reminders():
    secret = request.args.get("secret", "")
    if secret != os.environ.get("CRON_SECRET", ""):
        return jsonify({"error": "Forbidden"}), 403

    conn = get_db()
    now = datetime.now()
    window_end = now + timedelta(minutes=15)

    # Tasks starting within the next 15 minutes, still pending, not yet reminded.
    due_soon = fetch_all(
        conn,
        "SELECT * FROM tasks WHERE status = 'pending' AND (reminder_sent = 0 OR reminder_sent IS NULL)"
    )

    sent_count = 0
    checked_count = 0

    for task in due_soon:
        try:
            task_dt = datetime.strptime(f"{task['date']} {task['time']}", "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        if not (now <= task_dt <= window_end):
            continue

        checked_count += 1
        subs = fetch_all(conn, "SELECT * FROM push_subscriptions WHERE user_id = ?", (task["user_id"],))
        if not subs:
            # No device subscribed for this user — still mark as sent so we
            # don't keep rechecking it every run for the rest of its window.
            conn.execute("UPDATE tasks SET reminder_sent = 1 WHERE id = ?", (task["id"],))
            conn.commit()
            continue

        payload = json.dumps({
            "title": "Agenndar",
            "body": f"'{task['title']}' starts at {task['time']}",
            "url": "/dashboard"
        })

        any_delivered = False
        for sub in subs:
            try:
                webpush(
                    subscription_info={
                        "endpoint": sub["endpoint"],
                        "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}
                    },
                    data=payload,
                    vapid_private_key=VAPID_PRIVATE_KEY_FILE,
                    vapid_claims=VAPID_CLAIMS.copy()
                )
                any_delivered = True
            except WebPushException as e:
                # A 404/410 means the browser revoked this subscription (e.g. the
                # person cleared site data or uninstalled). Clean it up silently.
                status = getattr(e.response, "status_code", None)
                if status in (404, 410):
                    conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (sub["endpoint"],))
                    conn.commit()

        conn.execute("UPDATE tasks SET reminder_sent = 1 WHERE id = ?", (task["id"],))
        conn.commit()
        if any_delivered:
            sent_count += 1

    return jsonify({"checked": checked_count, "sent": sent_count})


@app.route("/api/task/priority", methods=["POST"])
def update_task_priority():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    task_id = data.get("task_id")
    priority_id = data.get("priority_id")

    conn = get_db()
    task = fetch_one(conn, "SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, session["user_id"]))
    if not task:
        return jsonify({"error": "Not found"}), 404

    priority = fetch_one(
        conn, "SELECT id FROM priorities WHERE id = ? AND user_id = ? AND is_active = 1",
        (priority_id, session["user_id"])
    )
    if not priority:
        return jsonify({"error": "Invalid priority"}), 400

    conn.execute("UPDATE tasks SET priority_id = ? WHERE id = ? AND user_id = ?", (priority_id, task_id, session["user_id"]))
    conn.commit()
    return jsonify({"status": "success"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
