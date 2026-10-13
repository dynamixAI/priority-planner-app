"""Signing up, logging in, Google sign-in and deleting accounts."""
import auth
from conftest import login, register


def test_signing_up_creates_the_account_and_four_priorities(client, db):
    r = register(client)
    assert r.status_code == 302
    assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM priorities").fetchone()[0] == 4


def test_passwords_are_never_stored_as_typed(client, db):
    register(client, password="pass1234")
    stored = db.execute("SELECT password_hash FROM users").fetchone()[0]
    assert "pass1234" not in stored


def test_logging_in(client):
    register(client)
    client.get("/logout")
    assert login(client).headers["Location"].endswith("/dashboard")
    client.get("/logout")
    assert login(client, password="wrong").headers["Location"].endswith("/login")


def test_repeated_wrong_passwords_lock_the_account_for_a_while(client, db):
    register(client)
    client.get("/logout")
    for _ in range(5):
        login(client, password="wrong")
    # Even the right password is refused while the lock lasts.
    assert login(client).headers["Location"].endswith("/login")
    assert db.execute("SELECT lock_until FROM users").fetchone()[0] > 0


def test_deleting_an_account_needs_the_password(client, db):
    register(client)
    bad = client.post("/api/account/delete", json={"password": "wrong"})
    assert bad.status_code == 400
    ok = client.post("/api/account/delete", json={"password": "pass1234"})
    assert ok.status_code == 200
    assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0


def google_signin(client, monkeypatch, email, verified=True):
    def fake_google(url, form=None, token=None):
        if "token" in url:
            return {"access_token": "t"}
        return {"email": email, "email_verified": verified}
    monkeypatch.setattr(auth, "google_fetch_json", fake_google)
    start = client.get("/auth/google")
    state = start.headers["Location"].split("state=")[1].split("&")[0]
    return client.get(f"/auth/google/callback?code=c&state={state}")


def test_google_sign_in_creates_a_new_account(client, db, monkeypatch):
    r = google_signin(client, monkeypatch, "New.Person@gmail.com")
    assert r.headers["Location"].endswith("/priority-setup")
    row = db.execute("SELECT email, has_password FROM users").fetchone()
    assert row == ("new.person@gmail.com", 0)


def test_google_sign_in_reuses_an_existing_account(client, db, monkeypatch):
    register(client, "sam", email="sam@example.com")
    client.get("/logout")
    r = google_signin(client, monkeypatch, "SAM@example.com")
    assert r.headers["Location"].endswith("/dashboard")
    assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


def test_google_sign_in_refuses_unverified_emails(client, db, monkeypatch):
    r = google_signin(client, monkeypatch, "x@example.com", verified=False)
    assert r.headers["Location"].endswith("/login")
    assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0


def test_a_google_only_account_deletes_with_a_confirmation(client, db, monkeypatch):
    google_signin(client, monkeypatch, "g@example.com")
    assert client.post("/api/account/delete", json={}).status_code == 400
    ok = client.post("/api/account/delete", json={"confirm": True})
    assert ok.status_code == 200
    assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
