"""Booking links and the Meetings page."""
import re
import secrets
import threading
import time
from datetime import datetime, timedelta

from flask import (flash, jsonify, redirect, render_template, request, session,
                   url_for)

from alerts import send_email
from core import (MAX_TEXT_LEN, MAX_TITLE_LEN, app, clean_date, clean_text,
                  clean_time, clean_timezone, fetch_all, fetch_one, format_time,
                  get_db, user_now)
from reminders import send_push_to_user

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
            "Saturday", "Sunday"]
LENGTH_CHOICES = [15, 30, 45, 60, 90, 120]
SLOT_STEP = 30
DAILY_LIMIT = 20  # per public link
MEETING_TYPES = {
    "in_person": "In-person meeting",
    "phone": "Phone call",
    "online": "Online meeting",
    "task": "Task",
}
PUBLIC_TYPES = ("in_person", "phone", "online")
# outcome: (label, task status)
OUTCOMES = {
    "held": ("Happened", "completed"),
    "missed": ("Missed by you", "missed"),
    "no_show": ("They didn't show", "missed"),
}
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_booking_lock = threading.Lock()


def to_minutes(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def hhmm(minutes):
    return "%02d:%02d" % (minutes // 60, minutes % 60)


def duration_text(minutes):
    if minutes < 60:
        return "%d mins" % minutes
    h, m = divmod(minutes, 60)
    if m == 0:
        return "1 hour" if h == 1 else "%d hours" % h
    return "%dh %dm" % (h, m)


def overlaps(start, end, ranges):
    return any(s < end and start < e for s, e in ranges)


def full_url(endpoint, **values):
    # Render sits behind a proxy, so Flask sees http
    url = url_for(endpoint, _external=True, **values)
    if not request.host.startswith(("localhost", "127.0.0.1")):
        url = url.replace("http://", "https://", 1)
    return url


def nice_date(date_str):
    return datetime.strptime(date_str, "%Y-%m-%d").strftime("%a %d %b %Y")


def booking_settings(conn, user_id):
    user = fetch_one(conn, (
        "SELECT username, email, timezone, time_format, booking_name, "
        "booking_lengths, booking_days_ahead, booking_notice_hours "
        "FROM users WHERE id = ?"), (user_id,)) or {}
    lengths = []
    for part in (user.get("booking_lengths") or "30,60").split(","):
        if part.strip().isdigit() and int(part) in LENGTH_CHOICES:
            lengths.append(int(part))
    hours = {}
    for row in fetch_all(conn, (
            "SELECT weekday, start_time, end_time FROM availability "
            "WHERE user_id = ?"), (user_id,)):
        hours[row["weekday"]] = (row["start_time"], row["end_time"])
    timezone = clean_timezone(user.get("timezone") or "")
    return {
        # shown to guests only if the owner set it
        "name": (user.get("booking_name") or "").strip(),
        "email": user.get("email") or "",
        "timezone": timezone,
        "tz_label": "UTC" if timezone == "UTC"
        else timezone.split("/")[-1].replace("_", " ") + " time",
        "time_format": user.get("time_format") or "24h",
        "lengths": lengths or [30, 60],
        "days_ahead": user.get("booking_days_ahead") or 14,
        "notice_hours": user.get("booking_notice_hours")
        if user.get("booking_notice_hours") is not None else 2,
        "hours": hours,
    }


def busy_by_date(conn, user_id, first_date, last_date):
    """{date: [(start, end) in minutes]}"""
    rows = fetch_all(conn, (
        "SELECT date, time, duration_minutes FROM tasks "
        "WHERE user_id = ? AND date BETWEEN ? AND ? "
        "AND status IN ('pending', 'completed', 'missed')"),
        (user_id, first_date, last_date))
    busy = {}
    for row in rows:
        try:
            start = to_minutes(row["time"])
        except (ValueError, AttributeError):
            continue
        end = start + (row["duration_minutes"] or 60)
        busy.setdefault(row["date"], []).append((start, end))
    return busy


def free_slots(settings, busy, length, now):
    """{date: [start times]}"""
    earliest = now + timedelta(hours=settings["notice_hours"])
    days = {}
    for offset in range(settings["days_ahead"] + 1):
        day = now.date() + timedelta(days=offset)
        window = settings["hours"].get(day.weekday())
        if not window:
            continue
        day_str = day.strftime("%Y-%m-%d")
        taken = busy.get(day_str, [])
        start, end = to_minutes(window[0]), to_minutes(window[1])
        times = []
        t = start
        while t + length <= end:
            slot_at = datetime.strptime(day_str + " " + hhmm(t), "%Y-%m-%d %H:%M")
            if slot_at >= earliest and not overlaps(t, t + length, taken):
                times.append(hhmm(t))
            t += SLOT_STEP
        if times:
            days[day_str] = times
    return days


def link_gone():
    return render_template(
        "book_message.html", heading="This link doesn't work",
        message="It may have been turned off or replaced. Please ask the "
                "person who sent it for a new one."), 404


def _notify_owner_later(user_id, title, body):
    def run():
        try:
            send_push_to_user(get_db(), user_id,
                              {"title": title, "body": body, "url": "/dashboard"})
        except Exception:
            app.logger.exception("Booking notification failed to send")
    threading.Thread(target=run, daemon=True).start()


def with_name(settings):
    return " with " + settings["name"] if settings["name"] else ""


def _email_later(to_email, subject, body, reply_to=None):
    def run():
        try:
            send_email(to_email, subject, body, reply_to)
        except Exception:
            app.logger.exception("Booking confirmation email failed to send")
    threading.Thread(target=run, daemon=True).start()


def make_link(conn, user_id, kind):
    conn.execute("UPDATE booking_links SET active = 0 WHERE user_id = ? AND kind = ?",
                 (user_id, kind))
    conn.execute(
        "INSERT INTO booking_links (user_id, kind, token, active, created_at) "
        "VALUES (?, ?, ?, 1, ?)",
        (user_id, kind, secrets.token_urlsafe(12), time.time()))


@app.route("/settings/booking", methods=["GET", "POST"])
def booking_setup():
    if "user_id" not in session:
        return redirect(url_for("login"))
    user_id = session["user_id"]
    conn = get_db()

    if request.method == "POST":
        action = request.form.get("action", "save")
        if action in ("new_public", "new_assistant"):
            make_link(conn, user_id, action[4:])
            conn.commit()
            flash("New link made. Any old link of that kind no longer works.", "info")
        elif action in ("off_public", "off_assistant"):
            conn.execute(
                "UPDATE booking_links SET active = 0 WHERE user_id = ? AND kind = ?",
                (user_id, action[4:]))
            conn.commit()
            flash("Link turned off. Nobody can book with it now.", "info")
        else:
            problems = save_booking_settings(conn, user_id)
            if problems:
                flash("Check the hours for: " + ", ".join(problems)
                      + ". The end time must be after the start time.", "error")
            else:
                flash("Booking settings saved.", "info")
        return redirect(url_for("booking_setup"))

    settings = booking_settings(conn, user_id)
    links = {}
    for row in fetch_all(conn, (
            "SELECT kind, token FROM booking_links WHERE user_id = ? AND active = 1"),
            (user_id,)):
        links[row["kind"]] = full_url("book", token=row["token"])
    theme = (fetch_one(conn, "SELECT theme FROM users WHERE id = ?", (user_id,))
             or {}).get("theme") or "light"
    return render_template(
        "booking_settings.html", settings=settings, links=links, theme=theme,
        weekdays=WEEKDAYS, length_choices=LENGTH_CHOICES)


def save_booking_settings(conn, user_id):
    """Returns the days with invalid hours, if any."""
    problems, rows = [], []
    for i, day in enumerate(WEEKDAYS):
        if not request.form.get("day%d_on" % i):
            continue
        start = clean_time(request.form.get("day%d_start" % i))
        end = clean_time(request.form.get("day%d_end" % i))
        if not start or not end or to_minutes(end) <= to_minutes(start):
            problems.append(day)
        else:
            rows.append((user_id, i, start, end))
    if problems:
        return problems

    lengths = [str(n) for n in LENGTH_CHOICES
               if request.form.get("len%d" % n)] or ["30"]
    try:
        days_ahead = max(1, min(60, int(request.form.get("days_ahead", 14))))
    except ValueError:
        days_ahead = 14
    try:
        notice = max(0, min(72, int(request.form.get("notice_hours", 2))))
    except ValueError:
        notice = 2
    name = clean_text(request.form.get("booking_name"), 60)

    conn.execute("DELETE FROM availability WHERE user_id = ?", (user_id,))
    for row in rows:
        conn.execute("INSERT INTO availability (user_id, weekday, start_time, "
                     "end_time) VALUES (?, ?, ?, ?)", row)
    conn.execute(
        "UPDATE users SET booking_name = ?, booking_lengths = ?, "
        "booking_days_ahead = ?, booking_notice_hours = ? WHERE id = ?",
        (name, ",".join(lengths), days_ahead, notice, user_id))
    conn.commit()
    return []


@app.route("/book/<token>", methods=["GET", "POST"])
def book(token):
    conn = get_db()
    link = fetch_one(conn, (
        "SELECT * FROM booking_links WHERE token = ? AND active = 1"), (token,))
    if not link:
        return link_gone()
    owner_id = link["user_id"]
    trusted = link["kind"] == "assistant"
    settings = booking_settings(conn, owner_id)
    now = user_now(settings["timezone"])

    if not trusted and not settings["hours"]:
        return render_template(
            "book_message.html", heading="Not taking bookings right now",
            message="This booking page isn't open yet. Please check back "
                    "later."), 200

    lengths = LENGTH_CHOICES if trusted else settings["lengths"]
    first = now.strftime("%Y-%m-%d")
    last = (now + timedelta(days=settings["days_ahead"])).strftime("%Y-%m-%d")
    busy = busy_by_date(conn, owner_id, first, last)
    slots = {str(n): free_slots(settings, busy, n, now) for n in lengths}

    form = request.form if request.method == "POST" else {}
    error = None
    if request.method == "POST":
        if form.get("website"):
            # honeypot field
            return render_template(
                "book_message.html", heading="You're booked",
                message="Thanks, your booking has been received.")
        error, booking = check_booking(conn, link, settings, lengths, slots, now)
        if not error:
            with _booking_lock:
                taken = busy_by_date(conn, owner_id, booking["date"],
                                     booking["date"]).get(booking["date"], [])
                start = to_minutes(booking["time"])
                if overlaps(start, start + booking["length"], taken):
                    error = ("Sorry, that time has just been taken. "
                             "Please pick another.")
                else:
                    save_booking(conn, link, settings, booking)
            if not error:
                return booked_page(link, settings, booking)

    return render_template(
        "book.html", owner=settings["name"], trusted=trusted, lengths=lengths,
        slots=slots, tz_label=settings["tz_label"],
        time_format=settings["time_format"], today=first,
        types=MEETING_TYPES if trusted else
        {k: MEETING_TYPES[k] for k in PUBLIC_TYPES},
        form=form, error=error), (400 if error else 200)


def check_booking(conn, link, settings, lengths, slots, now):
    form = request.form
    trusted = link["kind"] == "assistant"
    b = {
        "date": clean_date(form.get("date")),
        "time": clean_time(form.get("time")),
        "kind": form.get("kind", ""),
        "name": clean_text(form.get("name"), 100),
        "email": clean_text(form.get("email"), 200).lower(),
        "phone": clean_text(form.get("phone"), 40),
        "place": clean_text(form.get("place"), MAX_TITLE_LEN),
        "topic": clean_text(form.get("topic"), 120),
        "notes": clean_text(form.get("notes"), MAX_TEXT_LEN),
    }
    try:
        b["length"] = int(form.get("length", 0))
    except ValueError:
        b["length"] = 0

    allowed_types = MEETING_TYPES if trusted else PUBLIC_TYPES
    if b["length"] not in lengths:
        return "Please choose how long the meeting is.", b
    if not b["date"] or not b["time"]:
        return "Please pick a day and time.", b
    if b["kind"] not in allowed_types:
        return "Please choose how you'll meet.", b
    if b["kind"] == "task":
        if not b["topic"]:
            return "Please say what the task is.", b
    elif not b["name"]:
        return "Please enter a name.", b
    if b["email"] and not _EMAIL_RE.match(b["email"]):
        return "Please check the email address.", b
    if not trusted and not b["email"]:
        return "Please enter your email so you get a confirmation.", b
    if b["kind"] == "phone" and not b["phone"]:
        return "Please enter a phone number for the call.", b

    if trusted:
        when = datetime.strptime(b["date"] + " " + b["time"], "%Y-%m-%d %H:%M")
        if when < now or when > now + timedelta(days=365):
            return "Please pick a time in the next year that hasn't passed.", b
    else:
        if b["time"] not in slots.get(str(b["length"]), {}).get(b["date"], []):
            return "Sorry, that time isn't available. Please pick another.", b
        since = time.time() - 86400
        recent = fetch_one(conn, (
            "SELECT COUNT(*) AS n FROM tasks WHERE user_id = ? "
            "AND booked_via = 'public' AND created_at > ?"),
            (link["user_id"], since))
        if (recent or {}).get("n", 0) >= DAILY_LIMIT:
            return ("This booking page is very busy today. Please try again "
                    "tomorrow."), b
    return None, b


def save_booking(conn, link, settings, b):
    label = MEETING_TYPES[b["kind"]]
    if b["kind"] == "task":
        title = b["topic"]
    else:
        title = "%s with %s" % (label, b["name"])
        if b["topic"]:
            title += ": " + b["topic"]
    who = "your assistant link" if link["kind"] == "assistant" \
        else "your booking link"
    lines = ["Booked through %s" % who]
    if b["name"]:
        lines.append("With: " + b["name"])
    if b["email"]:
        lines.append("Email: " + b["email"])
    if b["phone"]:
        lines.append("Phone: " + b["phone"])
    if b["notes"]:
        lines.append("Notes: " + b["notes"])
    location = b["place"] or ("Call " + b["phone"] if b["kind"] == "phone" else "")

    priority = fetch_one(conn, (
        "SELECT id FROM priorities WHERE user_id = ? AND is_active = 1 "
        "ORDER BY sort_order ASC LIMIT 1"), (link["user_id"],))
    b["ref"] = secrets.token_urlsafe(16)
    conn.execute(
        "INSERT INTO tasks (user_id, title, detail, location, date, time, duration, "
        "duration_minutes, priority_id, status, created_at, booked_by, booked_via, "
        "booking_ref, meeting_type, booked_email, booking_seen) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)",
        (link["user_id"], title[:MAX_TITLE_LEN], "\n".join(lines)[:MAX_TEXT_LEN],
         location, b["date"], b["time"], duration_text(b["length"]), b["length"],
         (priority or {}).get("id"), time.time(), b["name"] or None,
         link["kind"], b["ref"], None if b["kind"] == "task" else b["kind"],
         b["email"] or None, 1 if b["kind"] == "task" else 0))
    conn.commit()

    when = "%s at %s" % (nice_date(b["date"])[:-5],
                         format_time(b["time"], settings["time_format"]))
    _notify_owner_later(link["user_id"], "New booking",
                        "%s - %s, %s" % (b["name"] or b["topic"], label, when))


def when_text(settings, date, time_str):
    return "%s at %s (%s)" % (
        nice_date(date), format_time(time_str, settings["time_format"]),
        settings["tz_label"])


def booked_page(link, settings, b):
    when = when_text(settings, b["date"], b["time"])
    label = MEETING_TYPES[b["kind"]]
    if b["email"] and link["kind"] == "public":
        cancel = full_url("cancel_booking", ref=b["ref"])
        _email_later(b["email"], "Booked: %s%s" % (label, with_name(settings)),
                     "\n".join([
                         "Hi %s," % b["name"], "",
                         "Your %s%s is booked for %s." % (
                             label.lower(), with_name(settings), when),
                         "", "Can't make it? Cancel here: " + cancel, "",
                         "You can reply to this email to get in touch."]),
                     reply_to=settings["email"])
        message = "Booked for %s. A confirmation is on its way to %s." % (
            when, b["email"])
    else:
        message = "Booked for %s." % when
    return render_template("book_message.html", heading="You're booked",
                           message=message)


def guest_email(task):
    if task.get("booked_email"):
        return task["booked_email"]
    found = re.search(r"^Email: (\S+@\S+)$", task.get("detail") or "", re.M)
    return found.group(1) if found else None


def email_guest_about_move(conn, task, new_date, new_time, note):
    """Emails the person who booked a meeting its new time."""
    to = guest_email(task)
    if not to or not task.get("booking_ref"):
        return False
    settings = booking_settings(conn, task["user_id"])
    label = MEETING_TYPES.get(task.get("meeting_type"), "Meeting")
    lines = ["Hi %s," % (task.get("booked_by") or "there"), ""]
    if note:
        lines += [note, ""]
    lines += [
        "Your %s%s has moved to %s." % (
            label.lower(), with_name(settings),
            when_text(settings, new_date, new_time)),
        "", "Can't make it? Cancel here: "
        + full_url("cancel_booking", ref=task["booking_ref"]), "",
        "You can reply to this email to get in touch."]
    _email_later(to, "New time: %s%s" % (label, with_name(settings)),
                 "\n".join(lines), reply_to=settings["email"])
    return True


def email_guest_about_cancel(conn, task, note):
    """Emails the person who booked a meeting that it's off."""
    to = guest_email(task)
    if not to or not task.get("booking_ref"):
        return False
    settings = booking_settings(conn, task["user_id"])
    label = MEETING_TYPES.get(task.get("meeting_type"), "Meeting")
    lines = ["Hi %s," % (task.get("booked_by") or "there"), ""]
    if note:
        lines += [note, ""]
    lines.append("Your %s%s on %s has been cancelled." % (
        label.lower(), with_name(settings),
        when_text(settings, task["date"], task["time"])))
    link = fetch_one(conn, "SELECT token FROM booking_links WHERE user_id = ? "
                           "AND kind = 'public' AND active = 1", (task["user_id"],))
    if link:
        lines += ["", "You can book a new time here: "
                  + full_url("book", token=link["token"])]
    lines += ["", "You can reply to this email to get in touch."]
    _email_later(to, "Cancelled: %s%s" % (label, with_name(settings)),
                 "\n".join(lines), reply_to=settings["email"])
    return True


@app.route("/booking/<ref>/cancel", methods=["GET", "POST"])
def cancel_booking(ref):
    conn = get_db()
    task = fetch_one(conn, "SELECT * FROM tasks WHERE booking_ref = ?", (ref,))
    if not task:
        return link_gone()
    settings = booking_settings(conn, task["user_id"])
    when = "%s at %s" % (nice_date(task["date"]),
                         format_time(task["time"], settings["time_format"]))
    if task["status"] == "cancelled":
        return render_template(
            "book_message.html", heading="Booking cancelled",
            message="This booking has already been cancelled.")
    if task["status"] != "pending":
        return render_template(
            "book_message.html", heading="This booking can't be cancelled here",
            message="It has already been moved or finished. Please reply to "
                    "your confirmation email instead.")
    if request.method == "POST":
        if task.get("meeting_type"):
            # kept on the calendar so the owner can see what happened
            conn.execute("UPDATE tasks SET status = 'cancelled', booking_seen = 0 "
                         "WHERE id = ?", (task["id"],))
        else:
            conn.execute("UPDATE tasks SET origin_id = NULL WHERE origin_id = ?",
                         (task["id"],))
            conn.execute("DELETE FROM tasks WHERE id = ?", (task["id"],))
        conn.commit()
        who = task["booked_by"] or "Someone"
        _notify_owner_later(task["user_id"], "Booking cancelled",
                            "%s cancelled %s" % (who, when))
        if settings["email"]:
            _email_later(settings["email"], "Cancelled: " + task["title"],
                         "%s cancelled %s, %s.\n\nThat time is free again." % (
                             who, task["title"], when_text(
                                 settings, task["date"], task["time"])))
        return render_template(
            "book_message.html", heading="Booking cancelled",
            message="Your booking for %s has been cancelled." % when)
    return render_template(
        "book_message.html", heading="Cancel this booking?",
        message="%s, %s." % (task["title"], when),
        cancel_form=True)


def has_started(task, now):
    return datetime.strptime(task["date"] + " " + task["time"],
                             "%Y-%m-%d %H:%M") <= now


@app.route("/meetings")
def meetings():
    if "user_id" not in session:
        return redirect(url_for("login"))
    user_id = session["user_id"]
    conn = get_db()
    user = fetch_one(conn, "SELECT theme, timezone, time_format FROM users "
                           "WHERE id = ?", (user_id,)) or {}
    now = user_now(clean_timezone(user.get("timezone") or ""))

    open_ones = fetch_all(conn, (
        "SELECT * FROM tasks WHERE user_id = ? AND meeting_type IS NOT NULL "
        "AND status = 'pending' ORDER BY date ASC, time ASC"), (user_id,))
    cancelled = fetch_all(conn, (
        "SELECT * FROM tasks WHERE user_id = ? AND meeting_type IS NOT NULL "
        "AND status = 'cancelled' AND date >= ? ORDER BY date ASC, time ASC"),
        (user_id, now.strftime("%Y-%m-%d")))
    new_ids = [t["id"] for t in open_ones + cancelled
               if t.get("booking_seen") == 0]
    if new_ids:
        conn.execute("UPDATE tasks SET booking_seen = 1 WHERE user_id = ? "
                     "AND booking_seen = 0", (user_id,))
        conn.commit()
    waiting = [t for t in open_ones if has_started(t, now)]
    upcoming = [t for t in open_ones if not has_started(t, now)]
    earlier = fetch_all(conn, (
        "SELECT * FROM tasks WHERE user_id = ? AND meeting_type IS NOT NULL "
        "AND meeting_outcome IS NOT NULL ORDER BY date DESC, time DESC LIMIT 30"),
        (user_id,))
    record = {key: 0 for key in OUTCOMES}
    for row in fetch_all(conn, (
            "SELECT meeting_outcome AS outcome, COUNT(*) AS n FROM tasks "
            "WHERE user_id = ? AND meeting_outcome IS NOT NULL "
            "GROUP BY meeting_outcome"), (user_id,)):
        if row["outcome"] in record:
            record[row["outcome"]] = row["n"]
    has_link = fetch_one(conn, "SELECT id FROM booking_links WHERE user_id = ? "
                               "AND active = 1", (user_id,))

    time_format = user.get("time_format") or "24h"

    def when(task):
        end = to_minutes(task["time"]) + (task["duration_minutes"] or 60)
        return "%s, %s to %s" % (
            nice_date(task["date"])[:-5],
            format_time(task["time"], time_format),
            format_time(hhmm(min(end, 1439)), time_format))

    return render_template(
        "meetings.html", theme=user.get("theme") or "light", waiting=waiting,
        upcoming=upcoming, earlier=earlier, record=record, outcomes=OUTCOMES,
        types=MEETING_TYPES, when=when, has_link=bool(has_link),
        new_ids=new_ids, cancelled=cancelled)


@app.route("/api/meeting/outcome", methods=["POST"])
def meeting_outcome():
    # outcome None = undo
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    outcome = data.get("outcome")
    conn = get_db()
    task = fetch_one(conn, (
        "SELECT t.*, u.timezone AS owner_timezone FROM tasks t "
        "JOIN users u ON u.id = t.user_id WHERE t.id = ? AND t.user_id = ?"),
        (data.get("task_id"), session["user_id"]))
    if not task or not task.get("meeting_type"):
        return jsonify({"error": "Not found"}), 404
    if task["status"] in ("rescheduled", "replanned"):
        return jsonify({"error": "This meeting was moved to a new time."}), 400

    if outcome is None:
        conn.execute("UPDATE tasks SET status = 'pending', meeting_outcome = NULL "
                     "WHERE id = ?", (task["id"],))
    elif outcome in OUTCOMES:
        now = user_now(clean_timezone(task.get("owner_timezone") or ""))
        if not has_started(task, now):
            return jsonify({"error": "This meeting hasn't started yet."}), 400
        conn.execute("UPDATE tasks SET status = ?, meeting_outcome = ? WHERE id = ?",
                     (OUTCOMES[outcome][1], outcome, task["id"]))
    else:
        return jsonify({"error": "Unknown outcome."}), 400
    conn.commit()
    return jsonify({"status": "success"})
