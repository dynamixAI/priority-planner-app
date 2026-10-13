"""Accounts: sign up, log in, Google sign-in, password reset and
log out."""
import hashlib
import hmac
import json
import os
import re
import secrets
import smtplib
import threading
import time
import urllib.parse
import urllib.request
from email.mime.text import MIMEText

from flask import (flash, redirect, render_template, request, session,
                   url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from core import (DEFAULT_ACTIVE_DAYS, DEFAULT_TIME_FORMAT, GOOGLE_CLIENT_ID,
                  GOOGLE_CLIENT_SECRET, app, clean_timezone, fetch_one, get_db,
                  google_enabled)
from database import seed_default_priorities


# Failed-login lockout: after 5 wrong passwords in a row, each further
# failure locks the account for longer — but never permanently, so a typo
# streak (or a stranger guessing your username) can't lock you out for good.
# Indexed by failure count; anything past the end uses the last value.
LOCK_SECONDS = {5: 120, 6: 300, 7: 900}
MAX_LOCK_SECONDS = 3600


def lock_seconds_for(attempts):
    if attempts < 5:
        return 0
    return LOCK_SECONDS.get(attempts, MAX_LOCK_SECONDS)


def send_reset_email(to_email, reset_link):
    smtp_server = os.environ["SMTP_SERVER"]
    smtp_port = int(os.environ.get("SMTP_PORT", 587))
    smtp_login = os.environ["SMTP_LOGIN"]
    smtp_password = os.environ["SMTP_PASSWORD"]
    sender_email = os.environ["SENDER_EMAIL"]

    body = (
        "Hi,\n\n"
        "We received a request to reset your Agenndar password. "
        "Click the link below to choose a new one:\n\n"
        f"{reset_link}\n\n"
        "This link expires in 1 hour. If you didn't request this, you can safely ignore this email."
    )
    msg = MIMEText(body)
    msg["Subject"] = "Reset your Agenndar password"
    msg["From"] = sender_email
    msg["To"] = to_email

    # A time limit, so an unreachable mail server can never hang the app.
    with smtplib.SMTP(smtp_server, smtp_port, timeout=15) as server:
        server.starttls()
        server.login(smtp_login, smtp_password)
        server.sendmail(sender_email, [to_email], msg.as_string())


def send_reset_email_in_background(to_email, reset_link):
    """Send the reset email without making the request wait for it, so a
    slow or blocked mail server can't freeze the page for everyone else.
    Failures go to the server logs (Render's Logs tab)."""
    def run():
        try:
            send_reset_email(to_email, reset_link)
        except Exception:
            app.logger.exception("Password reset email failed to send")
    threading.Thread(target=run, daemon=True).start()


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        agree_terms = request.form.get("agree_terms")
        marketing_opt_in = 1 if request.form.get("marketing_opt_in") else 0
        # Filled in by the browser on the sign-up page; falls back to UK time.
        user_tz = clean_timezone(request.form.get("timezone", ""))

        if not username or not email or not password or not confirm_password:
            flash("All fields are required.", "error")
            return redirect(url_for("register"))
        if password != confirm_password:
            flash("Passwords do not match.", "error")
            return redirect(url_for("register"))
        if not agree_terms:
            flash("You must agree to the Terms of Service and Privacy Policy to create an account.", "error")
            return redirect(url_for("register"))

        conn = get_db()
        existing = fetch_one(conn, "SELECT id FROM users WHERE username = ? OR email = ?", (username, email))
        if existing:
            flash("Username or email already registered.", "error")
            return redirect(url_for("register"))

        hashed = generate_password_hash(password)
        conn.execute(
            "INSERT INTO users (username, email, password_hash, theme, week_start, active_days, failed_attempts, lock_until, is_permanently_locked, terms_accepted_at, marketing_opt_in, time_format, timezone) VALUES (?, ?, ?, 'light', 'sunday', ?, 0, 0, 0, ?, ?, ?, ?)",
            (username, email, hashed, json.dumps(DEFAULT_ACTIVE_DAYS), time.time(), marketing_opt_in, DEFAULT_TIME_FORMAT, user_tz)
        )
        conn.commit()

        new_user = fetch_one(conn, "SELECT id FROM users WHERE username = ?", (username,))
        user_id = new_user["id"]
        seed_default_priorities(conn, user_id)

        session["user_id"] = user_id
        session["username"] = username
        return redirect(url_for("priority_setup"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        now = time.time()

        conn = get_db()
        user = fetch_one(conn, "SELECT * FROM users WHERE username = ? OR email = ?", (username, username.lower()))
        # Same message for an unknown username and a wrong password, so the
        # login form can't be used to discover which accounts exist.
        if not user:
            flash("Invalid username or password.", "error")
            return redirect(url_for("login"))

        # Locks are always temporary. The old permanent-lock flag is no
        # longer checked; it's cleared on the next successful login.
        if user["lock_until"] and now < user["lock_until"]:
            mins_left = int((user["lock_until"] - now) // 60) + 1
            flash(f"Too many attempts. Try again in {mins_left} min(s).", "error")
            return redirect(url_for("login"))

        if check_password_hash(user["password_hash"], password):
            conn.execute(
                "UPDATE users SET failed_attempts = 0, lock_until = 0, "
                "is_permanently_locked = 0 WHERE id = ?",
                (user["id"],)
            )
            conn.commit()
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            return redirect(url_for("dashboard"))
        else:
            attempts = (user["failed_attempts"] or 0) + 1
            wait = lock_seconds_for(attempts)
            lock_until = now + wait if wait else 0

            conn.execute(
                "UPDATE users SET failed_attempts = ?, lock_until = ? WHERE id = ?",
                (attempts, lock_until, user["id"])
            )
            conn.commit()
            flash("Invalid username or password.", "error")
            return redirect(url_for("login"))

    return render_template("login.html")


# ---- Sign in with Google ----
# Standard OAuth "authorization code" flow: send the user to Google, Google
# sends them back to /auth/google/callback with a one-time code, and we swap
# that code (plus our client secret) for their verified email address.
# Only the email is requested; we never see their Google password.

def google_redirect_uri():
    uri = url_for("google_callback", _external=True)
    # Render serves the app over HTTPS from behind a proxy, so Flask sees
    # plain http; Google needs the exact https address registered for it.
    if not request.host.startswith(("localhost", "127.0.0.1")):
        uri = uri.replace("http://", "https://", 1)
    return uri


def google_fetch_json(url, form=None, token=None):
    body = urllib.parse.urlencode(form).encode() if form is not None else None
    req = urllib.request.Request(url, data=body)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def unique_username(conn, email):
    base = re.sub(r"[^a-z0-9_]", "", email.split("@")[0].lower())[:20] or "user"
    name, n = base, 1
    while fetch_one(conn, "SELECT id FROM users WHERE username = ?", (name,)):
        n += 1
        name = f"{base}{n}"
    return name


@app.route("/auth/google")
def google_login():
    if not google_enabled():
        flash("Google sign-in isn't available right now.", "error")
        return redirect(url_for("login"))
    state = secrets.token_urlsafe(24)
    session["google_state"] = state
    # The sign-in page adds the device's timezone, for new accounts.
    session["google_tz"] = clean_timezone(request.args.get("tz", ""))
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": google_redirect_uri(),
        "response_type": "code",
        "scope": "openid email",
        "state": state,
        "prompt": "select_account",
    }
    return redirect("https://accounts.google.com/o/oauth2/v2/auth?"
                    + urllib.parse.urlencode(params))


@app.route("/auth/google/callback")
def google_callback():
    expected = session.pop("google_state", None)
    user_tz = session.pop("google_tz", None) or clean_timezone("")
    # The state value proves this reply belongs to a sign-in we started.
    if not expected or not hmac.compare_digest(expected, request.args.get("state", "")):
        flash("Google sign-in timed out. Please try again.", "error")
        return redirect(url_for("login"))
    code = request.args.get("code")
    if request.args.get("error") or not code:
        flash("Google sign-in was cancelled.", "error")
        return redirect(url_for("login"))

    try:
        tokens = google_fetch_json("https://oauth2.googleapis.com/token", form={
            "code": code,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": google_redirect_uri(),
            "grant_type": "authorization_code",
        })
        info = google_fetch_json("https://openidconnect.googleapis.com/v1/userinfo",
                                 token=tokens["access_token"])
    except Exception:
        app.logger.exception("Google sign-in failed")
        flash("Couldn't sign in with Google. Please try again.", "error")
        return redirect(url_for("login"))

    email = (info.get("email") or "").strip().lower()
    if not email or info.get("email_verified") is not True:
        flash("Your Google account's email address isn't verified.", "error")
        return redirect(url_for("login"))

    conn = get_db()
    user = fetch_one(conn, "SELECT id, username FROM users WHERE LOWER(email) = ?", (email,))
    if user:
        # Existing account with the same email: sign in to it.
        conn.execute(
            "UPDATE users SET failed_attempts = 0, lock_until = 0, "
            "is_permanently_locked = 0 WHERE id = ?",
            (user["id"],)
        )
        conn.commit()
        session["user_id"] = user["id"]
        session["username"] = user["username"]
        return redirect(url_for("dashboard"))

    # New user: create the account. There's no password yet (has_password=0);
    # they can set one any time with "Forgot password?".
    username = unique_username(conn, email)
    conn.execute(
        "INSERT INTO users (username, email, password_hash, theme, week_start, "
        "active_days, failed_attempts, lock_until, is_permanently_locked, "
        "terms_accepted_at, marketing_opt_in, time_format, timezone, has_password) "
        "VALUES (?, ?, ?, 'light', 'sunday', ?, 0, 0, 0, ?, 0, ?, ?, 0)",
        (username, email, generate_password_hash(secrets.token_urlsafe(32)),
         json.dumps(DEFAULT_ACTIVE_DAYS), time.time(), DEFAULT_TIME_FORMAT, user_tz)
    )
    conn.commit()
    new_user = fetch_one(conn, "SELECT id FROM users WHERE username = ?", (username,))
    seed_default_priorities(conn, new_user["id"])
    session["user_id"] = new_user["id"]
    session["username"] = username
    return redirect(url_for("priority_setup"))


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        conn = get_db()
        user = fetch_one(conn, "SELECT * FROM users WHERE email = ?", (email,))
        if user:
            raw_token = secrets.token_urlsafe(32)
            token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
            expiry = time.time() + 3600  # 1 hour

            conn.execute(
                "UPDATE users SET reset_token = ?, reset_token_expiry = ? WHERE id = ?",
                (token_hash, expiry, user["id"])
            )
            conn.commit()

            reset_link = url_for("reset_password", token=raw_token, _external=True)
            # Sent in the background: the user sees the same message either
            # way (so this page can't reveal which emails are registered),
            # and any failure is written to Render's Logs tab.
            send_reset_email_in_background(user["email"], reset_link)

        flash("If that email is on file, reset instructions have been sent.", "info")
        return redirect(url_for("login"))
    return render_template("forgot_password.html")


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    conn = get_db()
    user = fetch_one(conn, "SELECT * FROM users WHERE reset_token = ?", (token_hash,))

    if not user or not user["reset_token_expiry"] or time.time() > user["reset_token_expiry"]:
        flash("That reset link is invalid or has expired. Please request a new one.", "error")
        return redirect(url_for("forgot_password"))

    if request.method == "POST":
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not password or password != confirm_password:
            flash("Passwords do not match.", "error")
            return redirect(url_for("reset_password", token=token))

        hashed = generate_password_hash(password)
        conn.execute(
            "UPDATE users SET password_hash = ?, reset_token = NULL, reset_token_expiry = 0, "
            "failed_attempts = 0, lock_until = 0, is_permanently_locked = 0, "
            "has_password = 1 WHERE id = ?",
            (hashed, user["id"])
        )
        conn.commit()

        flash("Your password has been reset. You can now log in.", "info")
        return redirect(url_for("login"))

    return render_template("reset_password.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))
