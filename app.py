import sqlite3
import json
import time
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = "priority-planner-secret-key-prod-random-seed"
DB_NAME = "planner.db"

DEFAULT_PRIORITIES = [
    {"id": "p_q1", "label": "Q1: Urgent & Important", "color": "#ef4444"},
    {"id": "p_q2", "label": "Q2: Not Urgent, but Important", "color": "#3b82f6"},
    {"id": "p_q3", "label": "Q3: Urgent, Not Important", "color": "#f59e0b"},
    {"id": "p_q4", "label": "Q4: Not Urgent & Not Important", "color": "#10b981"}
]

TIME_SLOTS = [f"{h:02d}:00" for h in range(24)]
DEFAULT_ACTIVE_DAYS = ["0", "1", "2", "3", "4", "5", "6"]  # 0=Sunday, 6=Saturday

def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                priorities_json TEXT,
                theme TEXT DEFAULT 'light',
                week_start TEXT DEFAULT 'sunday',
                active_days TEXT DEFAULT '["0","1","2","3","4","5","6"]',
                failed_attempts INTEGER DEFAULT 0,
                lock_until REAL DEFAULT 0,
                is_permanently_locked INTEGER DEFAULT 0
            );
        """)
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(users)")
        cols = [c[1] for c in cursor.fetchall()]
        if "theme" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN theme TEXT DEFAULT 'light'")
        if "week_start" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN week_start TEXT DEFAULT 'sunday'")
        if "active_days" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN active_days TEXT DEFAULT '[\"0\",\"1\",\"2\",\"3\",\"4\",\"5\",\"6\"]'")
        if "failed_attempts" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN failed_attempts INTEGER DEFAULT 0")
        if "lock_until" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN lock_until REAL DEFAULT 0")
        if "is_permanently_locked" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN is_permanently_locked INTEGER DEFAULT 0")

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
                priority_id TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                FOREIGN KEY (user_id) REFERENCES users(id)
            );
        """)
        cursor.execute("PRAGMA table_info(tasks)")
        task_cols = [c[1] for c in cursor.fetchall()]
        if "location" not in task_cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN location TEXT")

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

        if not username or not email or not password or not confirm_password:
            flash("All fields are required.", "error")
            return redirect(url_for("register"))

        if password != confirm_password:
            flash("Passwords do not match.", "error")
            return redirect(url_for("register"))

        hashed = generate_password_hash(password)
        try:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO users (username, email, password_hash, priorities_json, theme, week_start, active_days, failed_attempts, lock_until, is_permanently_locked) VALUES (?, ?, ?, ?, 'light', 'sunday', ?, 0, 0, 0)",
                    (username, email, hashed, json.dumps(DEFAULT_PRIORITIES), json.dumps(DEFAULT_ACTIVE_DAYS))
                )
                conn.commit()
                session["user_id"] = cursor.lastrowid
                session["username"] = username
                return redirect(url_for("priority_setup"))
        except sqlite3.IntegrityError:
            flash("Username or email already registered.", "error")
            return redirect(url_for("register"))

    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        now = time.time()

        with get_db() as conn:
            user = conn.execute("SELECT * FROM users WHERE username = ? OR email = ?", (username, username.lower())).fetchone()
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

                conn.execute("UPDATE users SET failed_attempts = ?, lock_until = ?, is_permanently_locked = ? WHERE id = ?", (attempts, lock_until, perm_lock, user["id"]))
                conn.commit()
                flash("Invalid password.", "error")
                return redirect(url_for("login"))

    return render_template("login.html")

@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        with get_db() as conn:
            user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
            if user:
                conn.execute("UPDATE users SET failed_attempts = 0, lock_until = 0, is_permanently_locked = 0 WHERE id = ?", (user["id"],))
                conn.commit()
        flash("If that email is on file, verification instructions have been sent.", "info")
        return redirect(url_for("login"))
    return render_template("forgot_password.html")

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
    with get_db() as conn:
        conn.execute("UPDATE users SET theme = ? WHERE id = ?", (new_theme, session["user_id"]))
        conn.commit()
    return jsonify({"status": "success", "theme": new_theme})

@app.route("/settings", methods=["GET", "POST"])
@app.route("/priority-setup", methods=["GET", "POST"])
def priority_setup():
    if "user_id" not in session:
        return redirect(url_for("login"))
    user_id = session["user_id"]

    if request.method == "POST":
        labels = request.form.getlist("labels[]")
        colors = request.form.getlist("colors[]")
        week_start = request.form.get("week_start", "sunday")
        active_days = request.form.getlist("active_days[]")
        if not active_days:
            active_days = DEFAULT_ACTIVE_DAYS

        new_priorities = []
        for i, (label, color) in enumerate(zip(labels, colors)):
            clean_label = label.strip()
            if clean_label:
                new_priorities.append({
                    "id": f"p_{i+1}",
                    "label": clean_label,
                    "color": color
                })

        if not new_priorities:
            new_priorities = DEFAULT_PRIORITIES

        with get_db() as conn:
            conn.execute(
                "UPDATE users SET priorities_json = ?, week_start = ?, active_days = ? WHERE id = ?",
                (json.dumps(new_priorities), week_start, json.dumps(active_days), user_id)
            )
            conn.commit()
        return redirect(url_for("dashboard"))

    with get_db() as conn:
        user = conn.execute("SELECT priorities_json, theme, week_start, active_days FROM users WHERE id = ?", (user_id,)).fetchone()
        priorities = json.loads(user["priorities_json"]) if user and user["priorities_json"] else DEFAULT_PRIORITIES
        theme = user["theme"] if user and user["theme"] else "light"
        week_start = user["week_start"] if user and user["week_start"] else "sunday"
        active_days = json.loads(user["active_days"]) if user and user["active_days"] else DEFAULT_ACTIVE_DAYS

    return render_template("priority_setup.html", priorities=priorities, theme=theme, week_start=week_start, active_days=active_days)

@app.route("/dashboard")
def dashboard():
    if "user_id" not in session:
        return redirect(url_for("login"))

    user_id = session["user_id"]
    week_param = request.args.get("week_start")

    with get_db() as conn:
        user = conn.execute("SELECT priorities_json, theme, week_start, active_days FROM users WHERE id = ?", (user_id,)).fetchone()
        priorities = json.loads(user["priorities_json"]) if user and user["priorities_json"] else DEFAULT_PRIORITIES
        theme = user["theme"] if user and user["theme"] else "light"
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

    with get_db() as conn:
        week_date_strs = [d.strftime("%Y-%m-%d") for d in visible_week_dates]
        placeholders = ",".join("?" for _ in week_date_strs) if week_date_strs else "''"
        tasks = conn.execute(
            f"SELECT * FROM tasks WHERE user_id = ? AND date IN ({placeholders}) ORDER BY time ASC",
            [user_id] + week_date_strs if week_date_strs else [user_id]
        ).fetchall()

        all_user_tasks = conn.execute("SELECT * FROM tasks WHERE user_id = ? ORDER BY date ASC, time ASC", (user_id,)).fetchall()
        total_tasks = len(all_user_tasks)
        done_tasks = sum(1 for t in all_user_tasks if t["status"] == "completed")
        pushed_tasks = sum(1 for t in all_user_tasks if t["status"] == "rescheduled")
        missed_tasks = sum(1 for t in all_user_tasks if t["status"] == "missed")
        undone_tasks = sum(1 for t in all_user_tasks if t["status"] == "pending")

        # Defensible Behavioral Health Index Formula
        # Positive credit for done, mild deduction for push, heavier deduction for miss
        if total_tasks > 0:
            score_raw = ((done_tasks * 1.0) + (pushed_tasks * 0.4) - (missed_tasks * 0.6)) / total_tasks * 100
            bhi_score = max(0, min(100, round(score_raw, 1)))
        else:
            bhi_score = 100.0

        if bhi_score >= 80:
            bhi_tier = "Disciplined"
            bhi_color = "#16a34a"
        elif bhi_score >= 50:
            bhi_tier = "Drifting"
            bhi_color = "#eab308"
        else:
            bhi_tier = "Avoidance"
            bhi_color = "#ef4444"

        # Chronological 14-day history for Chart.js
        chart_labels = []
        chart_completed = []
        chart_pushed = []
        chart_missed = []
        chart_rate = []
        for i in range(13, -1, -1):
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
        "dashboard.html",
        username=session.get("username"),
        theme=theme,
        priorities=priorities,
        tasks=[dict(t) for t in tasks],
        all_tasks=[dict(t) for t in all_user_tasks],
        week_dates=visible_week_dates,
        prev_week=prev_week,
        next_week=next_week,
        cur_week_str=cur_week_str,
        time_slots=TIME_SLOTS,
        metrics={
            "total": total_tasks,
            "done": done_tasks,
            "undone": undone_tasks,
            "pushed": pushed_tasks,
            "missed": missed_tasks,
            "bhi_score": bhi_score,
            "bhi_tier": bhi_tier,
            "bhi_color": bhi_color
        },
        chart_data={
            "labels": chart_labels,
            "completed": chart_completed,
            "pushed": chart_pushed,
            "missed": chart_missed,
            "rate": chart_rate
        }
    )

@app.route("/api/task/add", methods=["POST"])
def add_task():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    time_val = data.get("time", "09:00")
    if len(time_val) == 4:
        time_val = "0" + time_val

    with get_db() as conn:
        conn.execute(
            "INSERT INTO tasks (user_id, title, detail, location, date, time, duration, priority_id, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
            (session["user_id"], data.get("title"), data.get("detail"), data.get("location", ""), data.get("date"), time_val, data.get("duration"), data.get("priority_id"))
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

    with get_db() as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, session["user_id"])).fetchone()
        if not task:
            return jsonify({"error": "Not found"}), 404

        # Strict Accountability: If already missed, do not allow switching back to completed
        if task["status"] == "missed" and target_status == "completed":
            return jsonify({"error": "Missed tasks are permanently locked and cannot be marked done."}), 400

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

    with get_db() as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, session["user_id"])).fetchone()
        if not task:
            return jsonify({"error": "Not found"}), 404

        # Option A Strict Accountability:
        # If task is already missed, retain the original record as missed and create a new replacement task
        if task["status"] == "missed":
            conn.execute(
                "INSERT INTO tasks (user_id, title, detail, location, date, time, duration, priority_id, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
                (session["user_id"], task["title"], task["detail"], task["location"], new_date, new_time, task["duration"], task["priority_id"])
            )
        else:
            # Active task pushed ahead of time
            conn.execute(
                "UPDATE tasks SET date = ?, time = ?, status = 'rescheduled' WHERE id = ? AND user_id = ?",
                (new_date, new_time, task_id, session["user_id"])
            )
        conn.commit()
    return jsonify({"status": "success"})

@app.route("/api/task/delete", methods=["POST"])
def delete_task():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    with get_db() as conn:
        conn.execute("DELETE FROM tasks WHERE id = ? AND user_id = ?", (data.get("task_id"), session["user_id"]))
        conn.commit()
    return jsonify({"status": "success"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
