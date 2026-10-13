"""Shared setup for the tests.

Every test gets its own brand-new, empty database (a temporary SQLite file
that behaves like Turso), so tests never touch real data and can't affect
each other. The clock is frozen at Tuesday 13 Oct 2026, 10:00 UK time, so
"is this task due yet?" always has the same answer.
"""
import os
import sqlite3
import sys
from datetime import datetime

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# The app reads these when it starts. Test values only.
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("TURSO_DATABASE_URL", "libsql://test.invalid")
os.environ.setdefault("TURSO_AUTH_TOKEN", "test-token")
os.environ["CRON_SECRET"] = "test-cron-secret"
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client.apps.googleusercontent.com")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "test-google-secret")
os.environ["ALERT_EMAIL"] = "owner@example.com"

FROZEN_NOW = datetime(2026, 10, 13, 10, 0)  # Tue 13 Oct 2026, 10:00 UK time
TODAY = "2026-10-13"


class FakeTurso:
    """Stands in for the Turso connection: same execute()/commit() shape."""

    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)

    def execute(self, sql, params=()):
        return self.db.execute(sql, params)

    def commit(self):
        self.db.commit()


_current = {}


# Point the app at a throwaway database BEFORE it starts (it creates its
# tables on import).
import turso_http  # noqa: E402

_current["conn"] = FakeTurso(":memory:")
turso_http.TursoHTTPConnection = lambda *a, **k: _current["conn"]

import app as app_module  # noqa: E402
import alerts, core, pages, reminders, tasks  # noqa: E401,E402

# Freeze the clock everywhere the app asks "what time is it for this user?"
for part in (core, pages, reminders, tasks):
    part.user_now = lambda tz_name: FROZEN_NOW
app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)


@pytest.fixture
def db(tmp_path):
    """A fresh, empty database with all the app's tables."""
    _current["conn"] = FakeTurso(str(tmp_path / "test.db"))
    app_module.init_db()
    return _current["conn"]


@pytest.fixture(autouse=True)
def alerts_sent(monkeypatch):
    """Error alert emails are collected here instead of being sent."""
    sent = []
    monkeypatch.setattr(alerts, "_send_in_background",
                        lambda subject, body: sent.append((subject, body)))
    monkeypatch.setitem(alerts._state, "last_sent", 0.0)
    monkeypatch.setitem(alerts._state, "held_back", 0)
    return sent


@pytest.fixture
def client(db):
    return app_module.app.test_client()


def register(client, username="sam", password="pass1234", email=None):
    return client.post("/register", data={
        "username": username,
        "email": email or f"{username}@example.com",
        "password": password,
        "confirm_password": password,
        "agree_terms": "on",
        "timezone": "Europe/London",
    })


def login(client, username="sam", password="pass1234"):
    return client.post("/login", data={"username": username, "password": password})


@pytest.fixture
def user(client):
    """A signed-up, logged-in user called sam."""
    register(client)
    return client


def add_task(client, title="Gym", date=TODAY, time="18:00", minutes=60, **extra):
    body = {"title": title, "date": date, "time": time,
            "duration": f"{minutes} mins", "duration_minutes": minutes}
    body.update(extra)
    return client.post("/api/task/add", json=body)


def task_id(db, title):
    return db.execute("SELECT id FROM tasks WHERE title = ? ORDER BY id DESC",
                      (title,)).fetchone()[0]


def status_of(db, tid):
    return db.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
