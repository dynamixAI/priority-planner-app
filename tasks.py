"""Everything a task can go through: added, done, missed, moved,
edited and deleted, plus the data behind the timetable."""
import json
import time
from datetime import datetime, timedelta

from flask import jsonify, request, session

from core import (DEFAULT_ACTIVE_DAYS, MAX_TEXT_LEN, MAX_TITLE_LEN, app,
                  clean_date, clean_text, clean_time, clean_timezone,
                  fetch_all, fetch_one, get_db, user_now)
from scoring import compute_metrics, fetch_attention_tasks


# The only status changes a user can make directly. Everything else
# (missed -> done, moved tasks, made-up statuses) is refused, so a miss
# stays permanent and the score can't be edited around the app's rules.
ALLOWED_STATUS_CHANGES = {
    ("pending", "completed"),
    ("pending", "missed"),
    ("completed", "pending"),
}


@app.route("/api/tasks/week")
def api_tasks_week():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    user_id = session["user_id"]
    week_param = request.args.get("week_start")
    conn = get_db()

    user = fetch_one(conn, "SELECT week_start, active_days, timezone FROM users WHERE id = ?", (user_id,))
    week_start_pref = user["week_start"] if user and user["week_start"] else "sunday"
    active_days_list = json.loads(user["active_days"]) if user and user["active_days"] else DEFAULT_ACTIVE_DAYS

    today = user_now(clean_timezone(user.get("timezone") if user else "")).date()
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

    user = fetch_one(conn, "SELECT overdue_threshold_days, timezone FROM users WHERE id = ?", (user_id,))
    now_dt = user_now(clean_timezone(user.get("timezone") if user else ""))
    today_str = now_dt.strftime("%Y-%m-%d")

    conn.execute(
        "UPDATE tasks SET status = 'missed' WHERE user_id = ? AND status = 'pending' AND date < ?",
        (user_id, today_str)
    )
    conn.commit()

    overdue_threshold_days = user["overdue_threshold_days"] if user and user["overdue_threshold_days"] else 14
    return jsonify({
        "metrics": compute_metrics(conn, user_id, now_dt, overdue_threshold_days),
        "attention_tasks": fetch_attention_tasks(conn, user_id, today_str),
    })


@app.route("/api/tasks/list")
def api_tasks_list():
    """Tasks of one status (optionally one date), loaded only when a list
    is opened or a time is checked for overlaps, instead of on every load."""
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    status = request.args.get("status", "")
    if status not in ("completed", "pending", "rescheduled", "missed"):
        return jsonify({"error": "Unknown status"}), 400
    sql = "SELECT * FROM tasks WHERE user_id = ? AND status = ?"
    params = [session["user_id"], status]
    date = clean_date(request.args.get("date"))
    if date:
        # Overlap check, so meetings count here
        sql += " AND date = ?"
        params.append(date)
    else:
        sql += " AND meeting_type IS NULL"
    sql += " ORDER BY date ASC, time ASC"
    return jsonify({"tasks": fetch_all(get_db(), sql, params)})


@app.route("/api/task/add", methods=["POST"])
def add_task():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}

    # Check everything before saving: a bad request gets a clear message
    # instead of crashing the server or storing a broken task.
    title = clean_text(data.get("title"), MAX_TITLE_LEN)
    date = clean_date(data.get("date"))
    time_val = clean_time(data.get("time") or "09:00")
    if not title:
        return jsonify({"error": "Please enter a task title."}), 400
    if not date:
        return jsonify({"error": "Please choose a valid date."}), 400
    if not time_val:
        return jsonify({"error": "Please choose a valid start time."}), 400

    try:
        duration_minutes = int(data.get("duration_minutes", 60))
    except (ValueError, TypeError):
        duration_minutes = 60
    duration_minutes = max(5, min(1440, duration_minutes))

    meeting_type = data.get("meeting_type") or None
    if meeting_type not in (None, "in_person", "phone", "online"):
        return jsonify({"error": "Please choose how the meeting happens."}), 400

    conn = get_db()
    priority_id = data.get("priority_id")
    if priority_id is not None and not fetch_one(
        conn, "SELECT id FROM priorities WHERE id = ? AND user_id = ? AND is_active = 1",
        (priority_id, session["user_id"])
    ):
        return jsonify({"error": "Please choose a valid priority."}), 400

    conn.execute(
        "INSERT INTO tasks (user_id, title, detail, location, date, time, duration, duration_minutes, priority_id, status, origin_id, created_at, meeting_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, ?, ?)",
        (session["user_id"], title, clean_text(data.get("detail"), MAX_TEXT_LEN),
         clean_text(data.get("location"), MAX_TITLE_LEN), date, time_val,
         clean_text(data.get("duration"), 40), duration_minutes, priority_id,
         time.time(), meeting_type)
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

    if task.get("meeting_type"):
        return jsonify({"error": "For meetings, choose Happened, I missed it or They didn't show."}), 400
    if task["status"] in ("rescheduled", "replanned") and target_status == "completed":
        return jsonify({"error": "This task was moved to a new time — complete it there instead."}), 400
    if (task["status"], target_status) not in ALLOWED_STATUS_CHANGES:
        return jsonify({"error": "That change isn't allowed for this task."}), 400

    # "Done Late" from the catch-up banner sends late=true. Any other
    # change (done on time, undo, missed) clears the late flag.
    late = 1 if (target_status == "completed" and data.get("late")) else 0
    conn.execute(
        "UPDATE tasks SET status = ?, completed_late = ? WHERE id = ? AND user_id = ?",
        (target_status, late, task_id, session["user_id"])
    )
    conn.commit()
    return jsonify({"status": "success"})


@app.route("/api/task/reschedule", methods=["POST"])
def reschedule_task():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    task_id = data.get("task_id")
    new_date = clean_date(data.get("new_date"))
    new_time = clean_time(data.get("new_time"))
    if not new_date or not new_time:
        return jsonify({"error": "Please pick a valid date and start time."}), 400

    conn = get_db()
    # The task and its owner's timezone in one database trip.
    task = fetch_one(
        conn,
        "SELECT t.*, u.timezone AS owner_timezone FROM tasks t "
        "JOIN users u ON u.id = t.user_id WHERE t.id = ? AND t.user_id = ?",
        (task_id, session["user_id"])
    )
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
        # Each missed task gets at most ONE follow-up: any later task in the
        # same chain means it has already been rescheduled.
        existing_followup = fetch_one(
            conn,
            "SELECT id FROM tasks WHERE user_id = ? AND origin_id = ? AND id > ?",
            (session["user_id"], origin, task["id"])
        )
        if existing_followup:
            return jsonify({"error": "This missed task already has a follow-up scheduled."}), 400
        if task.get("followup_dismissed"):
            return jsonify({"error": "You chose not to reschedule this task."}), 400
        result_label = "missed_kept"
        # leave the original row's status untouched — it stays 'missed'
    else:
        # Was this task already due at the moment we're rescheduling it?
        # Only an already-overdue push counts against PUSHED/BHI — moving
        # something before it's due is a free replan, not a penalized push.
        now_local = user_now(clean_timezone(task.get("owner_timezone") or ""))
        try:
            scheduled_dt = datetime.strptime(f"{task['date']} {task['time']}", "%Y-%m-%d %H:%M")
        except ValueError:
            scheduled_dt = now_local  # fail safe: treat as due if unparsable

        is_overdue = scheduled_dt <= now_local
        original_new_status = "rescheduled" if is_overdue else "replanned"
        result_label = "pushed" if is_overdue else "replanned"
        conn.execute("UPDATE tasks SET status = ? WHERE id = ? AND user_id = ?", (original_new_status, task_id, session["user_id"]))

    # The guest's cancel link moves to the new copy
    conn.execute(
        "INSERT INTO tasks (user_id, title, detail, location, date, time, duration, duration_minutes, priority_id, status, origin_id, meeting_type, booked_by, booked_via, booking_ref) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)",
        (session["user_id"], task["title"], task["detail"], task["location"],
         new_date, new_time, task["duration"], task["duration_minutes"] or 60, task["priority_id"], origin,
         task.get("meeting_type"), task.get("booked_by"), task.get("booked_via"),
         task.get("booking_ref"))
    )
    if task.get("booking_ref"):
        conn.execute("UPDATE tasks SET booking_ref = NULL WHERE id = ?", (task["id"],))
    conn.commit()
    return jsonify({"status": "success", "result": result_label})


@app.route("/api/task/dismiss-followup", methods=["POST"])
def dismiss_followup():
    # The user chose NOT to reschedule a missed task. Remember that choice so
    # the catch-up banner stops asking about it. The task stays 'missed' — the
    # BHI penalty is permanent either way.
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    task_id = data.get("task_id")

    conn = get_db()
    task = fetch_one(conn, "SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, session["user_id"]))
    if not task:
        return jsonify({"error": "Not found"}), 404
    if task["status"] != "missed":
        return jsonify({"error": "Only missed tasks can be dismissed."}), 400

    conn.execute(
        "UPDATE tasks SET followup_dismissed = 1 WHERE id = ? AND user_id = ?",
        (task_id, session["user_id"])
    )
    conn.commit()
    return jsonify({"status": "success"})


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


@app.route("/api/task/detail", methods=["POST"])
def update_task_detail():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    task_id = data.get("task_id")
    new_detail = clean_text(data.get("detail", ""), MAX_TEXT_LEN)

    conn = get_db()
    task = fetch_one(conn, "SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, session["user_id"]))
    if not task:
        return jsonify({"error": "Not found"}), 404

    conn.execute("UPDATE tasks SET detail = ? WHERE id = ? AND user_id = ?", (new_detail, task_id, session["user_id"]))
    conn.commit()
    return jsonify({"status": "success"})


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
