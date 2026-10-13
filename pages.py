"""The main pages: welcome, dashboard, trends, privacy and terms."""
import json
import time
from datetime import datetime, timedelta

from flask import (jsonify, redirect, render_template, request,
                   send_from_directory, session, url_for)

from core import (DEFAULT_ACTIVE_DAYS, DEFAULT_TIME_FORMAT, TIME_SLOTS,
                  VALID_TIME_FORMATS, app, clean_timezone, fetch_all,
                  fetch_one, get_db, user_now)
from scoring import compute_metrics, fetch_attention_tasks


@app.route("/")
def home():
    if "user_id" not in session:
        return render_template("welcome.html")
    return redirect(url_for("dashboard"))


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


@app.route("/dashboard")
def dashboard():
    if "user_id" not in session:
        return redirect(url_for("login"))

    user_id = session["user_id"]
    week_param = request.args.get("week_start")
    conn = get_db()

    user = fetch_one(conn, "SELECT theme, week_start, active_days, overdue_threshold_days, grid_orientation, time_format, timezone, tour_seen FROM users WHERE id = ?", (user_id,))
    # Note the visit for the admin stats, at most once every 10 minutes.
    if time.time() - session.get("seen_at", 0) > 600:
        session["seen_at"] = time.time()
        conn.execute("UPDATE users SET last_seen_at = ? WHERE id = ?",
                     (session["seen_at"], user_id))
        conn.commit()
    user_tz = clean_timezone(user.get("timezone") if user else "")
    time_format = user.get("time_format") if user and user.get("time_format") in VALID_TIME_FORMATS else DEFAULT_TIME_FORMAT
    now_dt = user_now(user_tz)
    today = now_dt.date()

    # Auto-missed grace window: a task stays open all day on its scheduled
    # date, even after its time slot passes — it only flips to 'missed' once
    # the calendar date itself has moved on. Runs on every dashboard load
    # since there's no background job in this setup; if you never open the
    # app, a task won't flip until the next time you do. "Today" is the
    # user's own date, so the flip happens at THEIR midnight.
    today_str = today.strftime("%Y-%m-%d")
    conn.execute(
        "UPDATE tasks SET status = 'missed' WHERE user_id = ? AND status = 'pending' "
        "AND date < ? AND meeting_type IS NULL",
        (user_id, today_str)
    )
    conn.commit()

    priorities = fetch_all(
        conn, "SELECT * FROM priorities WHERE user_id = ? AND is_active = 1 ORDER BY sort_order ASC", (user_id,)
    )
    theme = user["theme"] if user and user["theme"] else "light"
    week_start_pref = user["week_start"] if user and user["week_start"] else "sunday"
    grid_orientation = user.get("grid_orientation") if user and user.get("grid_orientation") else "time_rows"
    active_days_list = json.loads(user["active_days"]) if user and user["active_days"] else DEFAULT_ACTIVE_DAYS

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

    overdue_threshold_days = user["overdue_threshold_days"] if user and user["overdue_threshold_days"] else 14
    metrics = compute_metrics(conn, user_id, now_dt, overdue_threshold_days)
    attention_tasks = fetch_attention_tasks(conn, user_id, today_str)
    # Started meetings with no outcome yet
    meetings_waiting = (fetch_one(conn, (
        "SELECT COUNT(*) AS n FROM tasks WHERE user_id = ? AND status = 'pending' "
        "AND meeting_type IS NOT NULL AND (date < ? OR (date = ? AND time <= ?))"),
        (user_id, today_str, today_str, now_dt.strftime("%H:%M"))) or {}).get("n") or 0

    week_dates_iso = [d.strftime("%Y-%m-%d") for d in visible_week_dates]

    return render_template(
        "dashboard.html",
        username=session.get("username"),
        theme=theme,
        priorities=priorities,
        tasks=tasks,
        attention_tasks=attention_tasks,
        meetings_waiting=meetings_waiting,
        week_dates=visible_week_dates,
        week_dates_iso=week_dates_iso,
        week_start_pref=week_start_pref,
        grid_orientation=grid_orientation,
        time_format=time_format,
        user_timezone=user_tz,
        active_days=active_days_list,
        prev_week=prev_week,
        next_week=next_week,
        cur_week_str=cur_week_str,
        time_slots=TIME_SLOTS,
        metrics=metrics,
        show_tour=bool(user) and not user.get("tour_seen")
    )


@app.route("/api/tour/seen", methods=["POST"])
def tour_seen():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    conn = get_db()
    conn.execute("UPDATE users SET tour_seen = 1 WHERE id = ?", (session["user_id"],))
    conn.commit()
    return jsonify({"status": "success"})


@app.route("/trends")
def trends():
    if "user_id" not in session:
        return redirect(url_for("login"))
    user_id = session["user_id"]
    conn = get_db()

    user = fetch_one(conn, "SELECT theme, timezone FROM users WHERE id = ?", (user_id,))
    theme = user["theme"] if user and user["theme"] else "light"
    user_tz = clean_timezone(user.get("timezone") if user else "")

    try:
        days = int(request.args.get("days", 14))
    except ValueError:
        days = 14
    if days not in (7, 14, 30, 90):
        days = 14

    today = user_now(user_tz).date()
    first_day = (today - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    # Only the chart's own date range is loaded, not every task ever made.
    range_tasks = fetch_all(
        conn,
        "SELECT date, status FROM tasks WHERE user_id = ? AND date BETWEEN ? AND ? "
        "AND meeting_type IS NULL",
        (user_id, first_day, today.strftime("%Y-%m-%d"))
    )

    chart_labels, chart_completed, chart_pushed, chart_missed, chart_rate = [], [], [], [], []
    for i in range(days - 1, -1, -1):
        day = today - timedelta(days=i)
        day_cursor = day.strftime("%Y-%m-%d")
        chart_labels.append(day.strftime("%d %b"))
        day_tasks = [t for t in range_tasks if t["date"] == day_cursor]
        c_cnt = sum(1 for t in day_tasks if t["status"] == "completed")
        p_cnt = sum(1 for t in day_tasks if t["status"] == "rescheduled")
        m_cnt = sum(1 for t in day_tasks if t["status"] == "missed")
        # Same rule as the BHI: free replans and still-open tasks aren't
        # judged, so the rate is done out of done + pushed + missed.
        judged = c_cnt + p_cnt + m_cnt
        rate = round((c_cnt / judged * 100), 1) if judged > 0 else 0
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
