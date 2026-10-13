"""Reminders: sent once, only to tasks starting in the next 15 minutes, and
only marked as sent when a device actually received them."""
import pytest

from conftest import add_task

CRON = "/api/cron/send-reminders?secret=test-cron-secret"


@pytest.fixture
def pushes(app, monkeypatch):
    """Records reminders instead of really sending them."""
    sent = []
    monkeypatch.setattr(app, "webpush", lambda **kw: sent.append(kw["data"]))
    return sent


def add_device(db):
    db.execute("INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth) "
               "VALUES (1, 'https://push.example/1', 'k', 'a')")
    db.commit()


def reminded(db, title):
    return db.execute("SELECT reminder_sent FROM tasks WHERE title = ?",
                      (title,)).fetchone()[0]


def test_the_reminder_address_needs_the_secret(client):
    assert client.get("/api/cron/send-reminders").status_code == 403
    assert client.get("/api/cron/send-reminders?secret=nope").status_code == 403


def test_a_task_starting_soon_gets_one_reminder(user, db, pushes):
    add_device(db)
    add_task(user, title="Call the bank", time="10:10")   # now is 10:00
    assert user.get(CRON).get_json()["sent"] == 1
    assert "Call the bank" in pushes[0]
    assert reminded(db, "Call the bank") == 1
    assert user.get(CRON).get_json()["sent"] == 0          # not sent twice


def test_tasks_further_away_wait(user, db, pushes):
    add_device(db)
    add_task(user, title="Gym", time="18:00")
    assert user.get(CRON).get_json()["sent"] == 0
    assert reminded(db, "Gym") == 0


def test_no_device_means_the_reminder_waits_for_one(user, db, pushes):
    add_task(user, title="Call the bank", time="10:10")
    assert user.get(CRON).get_json()["sent"] == 0
    assert reminded(db, "Call the bank") == 0               # can still be sent later


def test_a_failed_send_is_tried_again_next_time(user, db, app, monkeypatch):
    add_device(db)
    add_task(user, title="Call the bank", time="10:10")

    def broken(**kw):
        raise TimeoutError("push service down")
    monkeypatch.setattr(app, "webpush", broken)
    assert user.get(CRON).get_json()["sent"] == 0
    assert reminded(db, "Call the bank") == 0
