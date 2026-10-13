"""Agenndar starts here. Render runs "gunicorn app:app".

This file loads each part of the app so its pages and addresses get
registered, then makes sure the database tables exist. The code lives in:

  core.py       settings, the Flask app, database access, small helpers
  database.py   the tables and the default priorities
  scoring.py    the Behaviour Index and the catch-up banner
  auth.py       sign up, log in, Google sign-in, password reset, log out
  pages.py      welcome, dashboard, trends, privacy and terms pages
  tasks.py      adding, finishing, moving and deleting tasks
  settings.py   the settings page, theme, account reset and delete
  reminders.py  push notifications and the 5-minute reminder check
  alerts.py     emails you when something goes wrong
  booking.py    booking links: other people put meetings in your planner
  backup/       the nightly database backup (run by GitHub)
"""
from core import app
from database import init_db

import alerts, auth, booking, pages, reminders, settings, tasks  # noqa: E401,F401

init_db()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True)
