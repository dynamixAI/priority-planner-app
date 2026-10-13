"""Emails you when something goes wrong, so you hear about it before users do.

Anything the app logs as an error becomes an email to ALERT_EMAIL: a page
crashing, a reminder or reset email failing to send, Google sign-in failing.
At most one email every 15 minutes; errors in between are counted and
mentioned in the next one. Off unless ALERT_EMAIL is set on Render.
"""
import logging
import os
import smtplib
import threading
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText

from flask import has_request_context, request

from core import app

ALERT_EMAIL = os.environ.get("ALERT_EMAIL", "").strip()
QUIET_SECONDS = 15 * 60

_state = {"last_sent": 0.0, "held_back": 0}
_lock = threading.Lock()
_log = logging.getLogger("agenndar.alerts")


def send_email(to_email, subject, body, reply_to=None):
    sender = os.environ["SENDER_EMAIL"]
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to_email
    if reply_to:
        msg["Reply-To"] = reply_to
    port = int(os.environ.get("SMTP_PORT", 587))
    with smtplib.SMTP(os.environ["SMTP_SERVER"], port, timeout=15) as server:
        server.starttls()
        server.login(os.environ["SMTP_LOGIN"], os.environ["SMTP_PASSWORD"])
        server.sendmail(sender, [to_email], msg.as_string())


def _send_in_background(subject, body):
    """Sends without making the visitor's page wait for the mail server."""
    def run():
        try:
            send_email(ALERT_EMAIL, subject, body)
        except Exception:
            _log.exception("Could not send the error alert email")
    threading.Thread(target=run, daemon=True).start()


def where():
    """The address being used, without private parts: a password-reset
    link shows as /reset-password/<token>, never the token itself."""
    if not has_request_context():
        return "background job"
    rule = request.url_rule.rule if request.url_rule else "(unknown address)"
    return "%s %s" % (request.method, rule)


class AlertHandler(logging.Handler):
    def emit(self, record):
        try:
            with _lock:
                now = time.time()
                if now - _state["last_sent"] < QUIET_SECONDS:
                    _state["held_back"] += 1
                    return
                held_back, _state["held_back"] = _state["held_back"], 0
                _state["last_sent"] = now

            what = record.getMessage()
            if has_request_context():
                what = what.replace(request.path, where().split(" ", 1)[1])
            details = "(no details)"
            if record.exc_info:
                details = logging.Formatter().formatException(record.exc_info)
            when = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")
            lines = [
                "Something went wrong in Agenndar.",
                "",
                "What:  " + what,
                "Where: " + where(),
                "When:  " + when,
            ]
            if held_back:
                lines.append("Also: %d more errors since the last alert email."
                             % held_back)
            lines += ["", "Details for fixing it:", details, "",
                      "Full logs: Render dashboard > priority-planner-app > Logs"]
            _send_in_background("Agenndar error: " + where(), "\n".join(lines))
        except Exception:
            self.handleError(record)


if ALERT_EMAIL:
    app.logger.addHandler(AlertHandler(level=logging.ERROR))
