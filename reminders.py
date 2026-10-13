"""Reminders: phones and browsers sign up for push notifications, and
cron-job.org calls the reminder check every 5 minutes."""
import hmac
import json
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import jsonify, request, session
from pywebpush import WebPushException, webpush

from core import (VAPID_CLAIMS, VAPID_PRIVATE_KEY_FILE, VAPID_PUBLIC_KEY, app,
                  clean_timezone, fetch_all, fetch_one, get_db, format_time,
                  user_now)


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
    # Refuse outright if no secret is configured — otherwise an empty
    # ?secret= would match an empty setting and anyone could trigger this.
    # compare_digest avoids leaking the secret through response timing.
    expected = os.environ.get("CRON_SECRET", "")
    secret = request.args.get("secret", "")
    if not expected or not hmac.compare_digest(secret, expected):
        return jsonify({"error": "Forbidden"}), 403

    conn = get_db()

    # Tasks starting within the next 15 minutes, still pending, not yet reminded.
    # Each task's time is the OWNER's local time, so "now" is worked out per
    # user from their timezone. Only dates within a day of UTC today can be
    # starting in the next 15 minutes anywhere in the world, so the query is
    # limited to those instead of scanning every pending task ever created.
    utc_today = datetime.now(ZoneInfo("UTC")).date()
    due_soon = fetch_all(
        conn,
        "SELECT t.*, u.timezone AS user_timezone, u.time_format AS user_time_format "
        "FROM tasks t JOIN users u ON u.id = t.user_id "
        "WHERE t.status = 'pending' AND (t.reminder_sent = 0 OR t.reminder_sent IS NULL) "
        "AND t.date BETWEEN ? AND ?",
        ((utc_today - timedelta(days=1)).strftime("%Y-%m-%d"), (utc_today + timedelta(days=1)).strftime("%Y-%m-%d"))
    )

    sent_count = 0
    checked_count = 0

    for task in due_soon:
        try:
            task_dt = datetime.strptime(f"{task['date']} {task['time']}", "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        now = user_now(clean_timezone(task.get("user_timezone")))
        if not (now <= task_dt <= now + timedelta(minutes=15)):
            continue

        checked_count += 1
        subs = fetch_all(conn, "SELECT * FROM push_subscriptions WHERE user_id = ?", (task["user_id"],))
        if not subs:
            # No device has notifications turned on yet. Leave the task
            # unreminded so a later run can still deliver it if the user
            # turns notifications on before the task starts.
            continue

        payload = json.dumps({
            "title": "Agenndar",
            "body": f"'{task['title']}' starts at {format_time(task['time'], task.get('user_time_format'))}",
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
                    vapid_claims=VAPID_CLAIMS.copy(),
                    timeout=10  # never let one slow push service hang the app
                )
                any_delivered = True
            except WebPushException as e:
                # A 404/410 means the browser revoked this subscription (e.g. the
                # person cleared site data or uninstalled). Clean it up silently.
                status = getattr(e.response, "status_code", None)
                if status in (404, 410):
                    conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (sub["endpoint"],))
                    conn.commit()
            except Exception:
                # Timeouts and network errors: log it and try again next run.
                app.logger.exception("Push notification failed to send")

        # Only tick the task off once a device actually received it. If every
        # send failed (e.g. a temporary push-service error), the next run
        # tries again — the 15-minute window gives it a few more chances.
        if any_delivered:
            conn.execute("UPDATE tasks SET reminder_sent = 1 WHERE id = ?", (task["id"],))
            conn.commit()
            sent_count += 1

    return jsonify({"checked": checked_count, "sent": sent_count})
