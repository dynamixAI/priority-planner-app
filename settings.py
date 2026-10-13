"""The settings page, light/dark theme, and resetting or deleting an
account."""
import json

from flask import (flash, jsonify, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash

from core import (DEFAULT_ACTIVE_DAYS, DEFAULT_TIME_FORMAT, VALID_TIMEZONES,
                  VALID_TIME_FORMATS, app, clean_timezone, fetch_all,
                  fetch_one, get_db)
from database import seed_default_priorities


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

        time_format = request.form.get("time_format", DEFAULT_TIME_FORMAT)
        if time_format not in VALID_TIME_FORMATS:
            time_format = DEFAULT_TIME_FORMAT
        user_tz = clean_timezone(request.form.get("timezone", ""))

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
            "UPDATE users SET week_start = ?, active_days = ?, overdue_threshold_days = ?, grid_orientation = ?, time_format = ?, timezone = ? WHERE id = ?",
            (week_start, json.dumps(active_days), overdue_threshold_days, grid_orientation, time_format, user_tz, user_id)
        )
        conn.commit()

        remaining = fetch_all(conn, "SELECT id FROM priorities WHERE user_id = ? AND is_active = 1", (user_id,))
        if not remaining:
            seed_default_priorities(conn, user_id)

        return redirect(url_for("dashboard"))

    user = fetch_one(conn, "SELECT theme, week_start, active_days, overdue_threshold_days, grid_orientation, time_format, timezone, has_password FROM users WHERE id = ?", (user_id,))
    priorities = fetch_all(
        conn, "SELECT * FROM priorities WHERE user_id = ? AND is_active = 1 ORDER BY sort_order ASC", (user_id,)
    )
    theme = user["theme"] if user and user["theme"] else "light"
    time_format = user.get("time_format") if user and user.get("time_format") in VALID_TIME_FORMATS else DEFAULT_TIME_FORMAT
    user_tz = clean_timezone(user.get("timezone") if user else "")
    week_start = user["week_start"] if user and user["week_start"] else "sunday"
    active_days = json.loads(user["active_days"]) if user and user["active_days"] else DEFAULT_ACTIVE_DAYS
    overdue_threshold_days = user["overdue_threshold_days"] if user and user["overdue_threshold_days"] else 14
    grid_orientation = user.get("grid_orientation") if user and user.get("grid_orientation") else "time_rows"

    return render_template(
        "priority_setup.html", priorities=priorities, theme=theme, week_start=week_start,
        active_days=active_days, overdue_threshold_days=overdue_threshold_days,
        grid_orientation=grid_orientation, time_format=time_format,
        user_timezone=user_tz, timezones=VALID_TIMEZONES,
        has_password=not user or user.get("has_password") != 0
    )


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
    data = request.get_json() or {}
    password = data.get("password", "")

    conn = get_db()
    user = fetch_one(conn, "SELECT * FROM users WHERE id = ?", (user_id,))
    if not user:
        return jsonify({"error": "Incorrect password."}), 400
    if user.get("has_password") == 0:
        # Google account with no password: an explicit confirmation is enough.
        if data.get("confirm") is not True:
            return jsonify({"error": "Please confirm you want to delete your account."}), 400
    elif not check_password_hash(user["password_hash"], password):
        return jsonify({"error": "Incorrect password."}), 400

    conn.execute("DELETE FROM tasks WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM priorities WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM push_subscriptions WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()

    session.clear()
    return jsonify({"status": "success"})
