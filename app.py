import sqlite3
import json
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = "priority-planner-secret-key-change-if-needed"
DB_NAME = "planner.db"

DEFAULT_PRIORITIES = [
    {"id": "p1", "label": "Urgent / Core", "color": "#ef5350"},
    {"id": "p2", "label": "Teaching / Delivery", "color": "#fdd835"},
    {"id": "p3", "label": "1-to-1 Support", "color": "#fb8c00"},
    {"id": "p4", "label": "Admin / Prep", "color": "#64b5f6"},
    {"id": "break", "label": "Break / Lunch", "color": "#b0bec5"}
]

TIME_SLOTS = [
    "08:30", "09:10", "10:15", "10:40", "11:50", "12:55", "13:40", "14:50", "16:00"
]

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
                priorities_json TEXT
            );
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                detail TEXT,
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                duration TEXT NOT NULL,
                priority_id TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                FOREIGN KEY (user_id) REFERENCES users(id)
            );
        """)
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

        if not username or not email or not password:
            flash("All fields are required.", "error")
            return redirect(url_for("register"))

        hashed = generate_password_hash(password)
        try:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO users (username, email, password_hash, priorities_json) VALUES (?, ?, ?, ?)",
                    (username, email, hashed, json.dumps(DEFAULT_PRIORITIES))
                )
                conn.commit()
                session["user_id"] = cursor.lastrowid
                session["username"] = username
                flash("Account created! Please confirm or customize your priority colors.", "info")
                return redirect(url_for("priority_setup"))
        except sqlite3.IntegrityError:
            flash("Username or email already exists.", "error")
            return redirect(url_for("register"))

    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        with get_db() as conn:
            user = conn.execute("SELECT * FROM users WHERE username = ? OR email = ?", (username, username)).fetchone()
            if user and check_password_hash(user["password_hash"], password):
                session["user_id"] = user["id"]
                session["username"] = user["username"]
                return redirect(url_for("dashboard"))
            else:
                flash("Invalid credentials. Try again.", "error")
                return redirect(url_for("login"))

    return render_template("login.html")

@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        with get_db() as conn:
            user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
            if user:
                flash(f"Password recovery instructions have been sent to {email}.", "success")
            else:
                flash("If that email exists in our records, instructions have been dispatched.", "info")
        return redirect(url_for("login"))
    return render_template("forgot_password.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))

@app.route("/priority-setup", methods=["GET", "POST"])
def priority_setup():
    if "user_id" not in session:
        return redirect(url_for("login"))
    user_id = session["user_id"]

    with get_db() as conn:
        user = conn.execute("SELECT priorities_json FROM users WHERE id = ?", (user_id,)).fetchone()
        priorities = json.loads(user["priorities_json"]) if user and user["priorities_json"] else DEFAULT_PRIORITIES

    if request.method == "POST":
        new_priorities = []
        for p in priorities:
            label = request.form.get(f"label_{p['id']}", p["label"])
            color = request.form.get(f"color_{p['id']}", p["color"])
            new_priorities.append({"id": p["id"], "label": label, "color": color})
        with get_db() as conn:
            conn.execute("UPDATE users SET priorities_json = ? WHERE id = ?", (json.dumps(new_priorities), user_id))
            conn.commit()
        return redirect(url_for("dashboard"))

    return render_template("priority_setup.html", priorities=priorities)

@app.route("/dashboard")
def dashboard():
    if "user_id" not in session:
        return redirect(url_for("login"))

    user_id = session["user_id"]
    week_param = request.args.get("week_start")
    
    if week_param:
        try:
            start_date = datetime.strptime(week_param, "%Y-%m-%d").date()
        except ValueError:
            start_date = datetime.today().date() - timedelta(days=datetime.today().weekday())
    else:
        # Start of current week (Monday)
        start_date = datetime.today().date() - timedelta(days=datetime.today().weekday())

    week_dates = [start_date + timedelta(days=i) for i in range(5)] # Mon-Fri
    prev_week = (start_date - timedelta(days=7)).strftime("%Y-%m-%d")
    next_week = (start_date + timedelta(days=7)).strftime("%Y-%m-%d")
    cur_week_str = start_date.strftime("%Y-%m-%d")

    with get_db() as conn:
        user = conn.execute("SELECT priorities_json FROM users WHERE id = ?", (user_id,)).fetchone()
        priorities = json.loads(user["priorities_json"]) if user and user["priorities_json"] else DEFAULT_PRIORITIES
        
        week_date_strs = [d.strftime("%Y-%m-%d") for d in week_dates]
        placeholders = ",".join("?" for _ in week_date_strs)
        tasks = conn.execute(
            f"SELECT * FROM tasks WHERE user_id = ? AND date IN ({placeholders})",
            [user_id] + week_date_strs
        ).fetchall()

    return render_template(
        "dashboard.html",
        username=session.get("username"),
        priorities=priorities,
        tasks=[dict(t) for t in tasks],
        week_dates=week_dates,
        prev_week=prev_week,
        next_week=next_week,
        cur_week_str=cur_week_str,
        time_slots=TIME_SLOTS
    )

@app.route("/api/task/add", methods=["POST"])
def add_task():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO tasks (user_id, title, detail, date, time, duration, priority_id, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
            (session["user_id"], data.get("title"), data.get("detail"), data.get("date"), data.get("time"), data.get("duration"), data.get("priority_id"))
        )
        conn.commit()
    return jsonify({"status": "success"})

@app.route("/api/task/status", methods=["POST"])
def update_status():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    with get_db() as conn:
        conn.execute("UPDATE tasks SET status = ? WHERE id = ? AND user_id = ?", (data.get("status"), data.get("task_id"), session["user_id"]))
        conn.commit()
    return jsonify({"status": "success"})

@app.route("/api/task/reschedule", methods=["POST"])
def reschedule_task():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    with get_db() as conn:
        conn.execute(
            "UPDATE tasks SET date = ?, time = ?, status = 'rescheduled' WHERE id = ? AND user_id = ?",
            (data.get("new_date"), data.get("new_time"), data.get("task_id"), session["user_id"])
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
