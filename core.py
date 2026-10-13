"""Shared by every part of Agenndar: settings, the Flask app itself,
database access, time zones and input checks."""
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo, available_timezones

from dotenv import load_dotenv
from flask import Flask
from flask_wtf import CSRFProtect

from turso_http import TursoHTTPConnection


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

# Sign in with Google. The button only shows once both are set on Render.
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()


def google_enabled():
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


@app.context_processor
def inject_google_flag():
    return {"google_enabled": google_enabled()}


TIME_SLOTS = [f"{h:02d}:00" for h in range(24)]


DEFAULT_TIMEZONE = "Europe/London"
DEFAULT_TIME_FORMAT = "24h"
VALID_TIME_FORMATS = ("12h", "24h")
# Region/City names only (e.g. Europe/London) plus UTC — skips legacy aliases.
VALID_TIMEZONES = sorted(
    tz for tz in available_timezones()
    if "/" in tz and not tz.startswith(("Etc/", "SystemV/", "US/", "posix/", "right/"))
) + ["UTC"]


def clean_timezone(tz_name):
    return tz_name if tz_name in VALID_TIMEZONES else DEFAULT_TIMEZONE


def user_now(tz_name):
    """The current wall-clock time in the user's own timezone, as a naive
    datetime. Task dates/times are stored as the user's local wall-clock
    values, so every "has this happened yet?" check must compare against the
    user's local time — not the server's (Render runs on UTC, which is an
    hour behind the UK during British Summer Time)."""
    try:
        tz = ZoneInfo(tz_name or DEFAULT_TIMEZONE)
    except Exception:
        tz = ZoneInfo(DEFAULT_TIMEZONE)
    return datetime.now(tz).replace(tzinfo=None)
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


def format_time(hhmm, time_format):
    """'14:30' -> '2:30 PM' for 12-hour users; unchanged for 24-hour users."""
    if time_format != "12h":
        return hhmm
    try:
        return datetime.strptime(hhmm, "%H:%M").strftime("%I:%M %p").lstrip("0")
    except ValueError:
        return hhmm


MAX_TITLE_LEN = 200
MAX_TEXT_LEN = 2000
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")


def clean_date(value):
    """'2026-10-11' if valid, else None."""
    if not isinstance(value, str) or not _DATE_RE.match(value):
        return None
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None
    return value


def clean_time(value):
    """'09:05' (zero-padded) if a valid time, else None."""
    if not isinstance(value, str) or not _TIME_RE.match(value):
        return None
    h, m = (int(x) for x in value.split(":"))
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return f"{h:02d}:{m:02d}"


def clean_text(value, limit):
    return value.strip()[:limit] if isinstance(value, str) else ""
