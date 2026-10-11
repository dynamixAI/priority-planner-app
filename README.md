# Agenndar

**The planner that works whether your week goes to plan or not.**

Agenndar is a weekly timetable planner built with Python (Flask). You plan
your week in colour-coded priority tiers, then the app keeps an honest record
of what actually happened: done on time, done late, pushed, or missed. That
record feeds a single score, the **Behaviour Index (BHI)**, so you can see
whether you're staying disciplined or drifting.

---

## Features

- **Weekly timetable:** time-blocked grid with custom priority tiers and
  colours. Choose hours down the side or days down the side.
- **Catch-up banner:** when a task's time passes, the app asks what happened:
  *Done On Time*, *Done Late*, *Push*, or *Missed*. Tasks still open at
  midnight are marked missed automatically.
- **Honest rescheduling:**
  - Moving a task *before* it's due is a free **replan**.
  - Moving it *after* it's due is a **push**, and counts against you.
  - A **miss is permanent**. Rescheduling creates a new follow-up task but
    never erases the original miss.
- **Behaviour Index:** a 0–100 score with three tiers: *Disciplined*,
  *Drifting*, *Avoidance*.
- **Trends:** daily completed, pushed and missed counts with a completion
  rate over 7, 14, 30 or 90 days.
- **Reminders:** browser push notifications about 15 minutes before a task.
- **Your settings:** 12- or 24-hour time, your own timezone, week start day,
  visible days, light/dark theme, overdue threshold.
- **Phone-style time picker:** scroll wheels with every minute available.
- **Installable:** works as a home-screen app (PWA).
- **Account security:** hashed passwords, CSRF protection, escalating
  temporary lockouts after failed logins, and expiring email reset links.

## How the Behaviour Index is calculated

Each judged task contributes:

| Outcome | Points |
|---|---|
| Done on time | +1.0 |
| Done late | +0.7 |
| Pushed (moved after it was due) | +0.4 |
| Missed | −0.6 |
| Still open long past its first date (overdue threshold) | −0.3 |

BHI = total points ÷ number of judged tasks × 100, kept between 0 and 100.
Free replans and tasks that aren't due yet are not judged.

---

## Tech stack

- **Backend:** Python, Flask, Gunicorn
- **Templates:** Jinja2, vanilla JavaScript, Chart.js (trends)
- **Database:** [Turso](https://turso.tech) (libSQL), over its HTTP API
- **Notifications:** Web Push (VAPID) via `pywebpush`
- **Hosting:** Render (free tier)

## Running it locally

1. Clone the repository:

   ```bash
   git clone https://github.com/dynamixAI/priority-planner-app.git
   cd priority-planner-app
   ```

2. Create a virtual environment and install the dependencies:

   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

3. Create a `.env` file with the settings below (it is git-ignored).

4. Start the app:

   ```bash
   python3 app.py
   ```

   Then open http://localhost:8080.

## Environment variables

| Variable | Required | What it's for |
|---|---|---|
| `SECRET_KEY` | Yes | Signs login sessions. Use a long random string. |
| `TURSO_DATABASE_URL` | Yes | Your Turso database URL. |
| `TURSO_AUTH_TOKEN` | Yes | Turso access token. |
| `SMTP_SERVER` | For password reset | Email server address. |
| `SMTP_PORT` | No | Email server port (default 587). |
| `SMTP_LOGIN` | For password reset | Email account login. |
| `SMTP_PASSWORD` | For password reset | Email account password. |
| `SENDER_EMAIL` | For password reset | "From" address on reset emails. |
| `VAPID_PUBLIC_KEY` | For reminders | Public key for push notifications. |
| `VAPID_PRIVATE_KEY_FILE` | For reminders | Path to the private key file (default `vapid_private.pem`). On Render, add it as a Secret File. |
| `CRON_SECRET` | For reminders | Shared secret for the reminder endpoint. If it isn't set, the endpoint refuses all requests. |

## Reminders

Reminders are sent by calling this endpoint every few minutes from any
scheduler (for example a free cron service):

```
GET /api/cron/send-reminders?secret=<CRON_SECRET>
```

## Deploying on Render

`render.yaml` describes the web service (free plan, Gunicorn). Add the
environment variables above in the service's **Environment** settings, and
the VAPID private key as a **Secret File**.

---

Built by [DynamixAI](https://github.com/dynamixAI).
