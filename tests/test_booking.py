"""Booking links."""
import pytest

import app as app_module
import booking
from conftest import FROZEN_NOW, add_task

# Now is Tue 13 Oct 2026 10:00, notice is 2 hours
TUE, WED, SAT = "2026-10-13", "2026-10-14", "2026-10-17"


@pytest.fixture
def told(monkeypatch):
    sent = {"push": [], "email": []}
    monkeypatch.setattr(booking, "_notify_owner_later",
                        lambda uid, title, body: sent["push"].append(body))
    monkeypatch.setattr(booking, "_email_later",
                        lambda to, subject, body, reply_to=None:
                        sent["email"].append((to, body)))
    return sent


def set_hours(owner, **extra):
    # Mon-Fri 09:00-17:00
    form = {"action": "save", "len30": "1", "len60": "1",
            "days_ahead": "14", "notice_hours": "2", "booking_name": "Sam"}
    for i in range(5):
        form.update({"day%d_on" % i: "1", "day%d_start" % i: "09:00",
                     "day%d_end" % i: "17:00"})
    form.update(extra)
    return owner.post("/settings/booking", data=form)


def make_link(owner, db, kind="public"):
    owner.post("/settings/booking", data={"action": "new_" + kind})
    return db.execute("SELECT token FROM booking_links WHERE kind = ? AND active = 1",
                      (kind,)).fetchone()[0]


@pytest.fixture
def guest(db):
    return app_module.app.test_client()


def book(guest, token, **fields):
    form = {"length": "30", "date": WED, "time": "10:00", "kind": "in_person",
            "name": "Ada Lovelace", "email": "ada@example.com", "topic": "Intro"}
    form.update(fields)
    return guest.post("/book/" + token, data=form)


def booked(db):
    return db.execute("SELECT title, date, time, duration_minutes, status, "
                      "booked_by, detail FROM tasks WHERE booked_by IS NOT NULL "
                      "OR booked_via IS NOT NULL").fetchall()


def test_free_slots_skip_busy_times_and_the_notice_period():
    settings = {"hours": {1: ("09:00", "12:00")}, "days_ahead": 0,
                "notice_hours": 1}
    busy = {TUE: [(11 * 60, 11 * 60 + 30)]}  # 11:00-11:30
    slots = booking.free_slots(settings, busy, 30, FROZEN_NOW)
    assert slots == {TUE: ["11:30"]}


def test_booking_settings_need_you_to_be_logged_in(client):
    assert client.get("/settings/booking").headers["Location"].endswith("/login")


def test_the_page_shows_free_times_but_never_your_task_names(user, db, guest):
    set_hours(user)
    add_task(user, title="Secret dentist visit", date=WED, time="10:00")
    token = make_link(user, db)
    page = guest.get("/book/" + token).get_data(as_text=True)
    assert "Book an appointment" in page and "with Sam" in page
    assert "Secret dentist visit" not in page
    slots = booking.free_slots(booking.booking_settings(db, 1),
                               booking.busy_by_date(db, 1, TUE, SAT), 60,
                               booking.user_now("Europe/London"))
    assert "10:00" not in slots[WED] and "09:30" not in slots[WED]
    assert "11:00" in slots[WED]
    assert SAT not in slots


def test_a_booking_lands_in_your_planner_and_you_are_told(user, db, guest, told):
    set_hours(user)
    token = make_link(user, db)
    r = book(guest, token, kind="phone", phone="07700 900123")
    assert r.status_code == 200 and "booked" in r.get_data(as_text=True)
    (title, date, time, minutes, status, by, detail), = booked(db)
    assert (title, date, time, minutes, status, by) == (
        "Phone call with Ada Lovelace: Intro", WED, "10:00", 30, "pending",
        "Ada Lovelace")
    assert "ada@example.com" in detail and "07700 900123" in detail
    assert told["push"] == ["Ada Lovelace - Phone call, Wed 14 Oct at 10:00"]
    to, email = told["email"][0]
    assert to == "ada@example.com" and "/booking/" in email


def test_the_same_slot_cannot_be_booked_twice(user, db, guest, told):
    set_hours(user)
    token = make_link(user, db)
    assert book(guest, token).status_code == 200
    second = book(guest, token, name="Someone Else", email="s@example.com")
    assert second.status_code == 400
    assert len(booked(db)) == 1


def test_times_outside_your_hours_are_refused(user, db, guest, told):
    set_hours(user)
    token = make_link(user, db)
    assert book(guest, token, time="18:00").status_code == 400      # evening
    assert book(guest, token, date=SAT).status_code == 400          # weekend
    assert book(guest, token, date=TUE, time="10:30").status_code == 400  # too soon
    assert booked(db) == []


def test_guests_must_give_name_email_and_a_phone_for_calls(user, db, guest, told):
    set_hours(user)
    token = make_link(user, db)
    assert book(guest, token, name="").status_code == 400
    assert book(guest, token, email="").status_code == 400
    assert book(guest, token, email="not-an-email").status_code == 400
    assert book(guest, token, kind="phone", phone="").status_code == 400
    assert booked(db) == []


def test_your_assistant_can_book_outside_your_hours(user, db, guest, told):
    set_hours(user)
    token = make_link(user, db, "assistant")
    r = book(guest, token, date=SAT, time="18:00", email="")
    assert r.status_code == 200
    assert booked(db)[0][1:3] == (SAT, "18:00")
    assert told["email"] == []


def test_your_assistant_can_add_a_plain_task(user, db, guest, told):
    token = make_link(user, db, "assistant")  # no hours set
    r = book(guest, token, kind="task", name="", email="", topic="Renew passport",
             date=WED, time="15:00", length="60")
    assert r.status_code == 200
    assert booked(db)[0][0] == "Renew passport"


def test_even_your_assistant_cannot_double_book_you(user, db, guest, told):
    add_task(user, title="Board meeting", date=WED, time="14:00", minutes=60)
    token = make_link(user, db, "assistant")
    assert book(guest, token, time="14:30").status_code == 400
    assert booked(db) == []


def test_a_turned_off_link_stops_working(user, db, guest):
    set_hours(user)
    token = make_link(user, db)
    user.post("/settings/booking", data={"action": "off_public"})
    assert guest.get("/book/" + token).status_code == 404
    new_token = make_link(user, db)
    assert new_token != token and guest.get("/book/" + new_token).status_code == 200


def test_the_guest_can_cancel_from_their_email(user, db, guest, told):
    set_hours(user)
    token = make_link(user, db)
    book(guest, token)
    ref = db.execute("SELECT booking_ref FROM tasks").fetchone()[0]
    assert "Cancel this booking?" in guest.get(
        "/booking/%s/cancel" % ref).get_data(as_text=True)
    guest.post("/booking/%s/cancel" % ref)
    assert [row[4] for row in booked(db)] == ["cancelled"]
    assert told["push"][-1].startswith("Ada Lovelace cancelled")
    # the time is free again
    assert book(guest, token).status_code == 200
    assert [row[4] for row in booked(db)] == ["cancelled", "pending"]


def test_spam_robots_are_quietly_ignored(user, db, guest, told):
    set_hours(user)
    token = make_link(user, db)
    r = book(guest, token, website="http://spam.example")
    assert r.status_code == 200
    assert booked(db) == [] and told["push"] == []


def test_a_booking_is_a_meeting_and_a_plain_task_is_not(user, db, guest, told):
    set_hours(user)
    book(guest, make_link(user, db), kind="online")
    assistant = make_link(user, db, "assistant")
    book(guest, assistant, kind="task", name="", email="", topic="Renew passport",
         date=WED, time="15:00")
    kinds = dict(db.execute("SELECT title, meeting_type FROM tasks").fetchall())
    assert kinds == {"Online meeting with Ada Lovelace: Intro": "online",
                     "Renew passport": None}


def test_deleting_your_account_removes_your_booking_links(user, db):
    set_hours(user)
    make_link(user, db)
    user.post("/api/account/delete", json={"password": "pass1234"})
    assert db.execute("SELECT COUNT(*) FROM booking_links").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM availability").fetchone()[0] == 0


def move(owner, db, notify, message=""):
    tid = db.execute("SELECT id FROM tasks WHERE status = 'pending'").fetchone()[0]
    return owner.post("/api/task/reschedule", json={
        "task_id": tid, "new_date": WED, "new_time": "11:00",
        "notify": notify, "message": message})


def test_moving_a_booking_can_email_the_guest(user, db, guest, told):
    set_hours(user)
    book(guest, make_link(user, db))
    r = move(user, db, True, "Something came up, sorry!")
    assert r.get_json()["emailed"] is True
    to, email = told["email"][-1]
    assert to == "ada@example.com"
    assert "Something came up, sorry!" in email
    assert "Wed 14 Oct 2026 at 11:00 (London time)" in email
    assert "/booking/" in email


def test_moving_a_booking_without_emailing(user, db, guest, told):
    set_hours(user)
    book(guest, make_link(user, db))
    before = len(told["email"])
    assert move(user, db, False).get_json()["emailed"] is False
    assert len(told["email"]) == before


def test_your_username_is_not_shown_without_a_booking_name(user, db, guest):
    set_hours(user, booking_name="")
    page = guest.get("/book/" + make_link(user, db)).get_data(as_text=True)
    assert "Book an appointment" in page and "sam" not in page.lower()


def test_new_bookings_show_until_you_open_meetings(user, db, guest, told):
    set_hours(user)
    book(guest, make_link(user, db))
    page = user.get("/dashboard").get_data(as_text=True)
    assert 'title="New bookings">1<' in page
    meetings = user.get("/meetings").get_data(as_text=True)
    assert 'class="new-tag"' in meetings
    page = user.get("/dashboard").get_data(as_text=True)
    assert "New bookings" not in page


def test_guest_cancel_keeps_it_on_your_calendar(user, db, guest, told):
    set_hours(user)
    book(guest, make_link(user, db))
    ref = db.execute("SELECT booking_ref FROM tasks").fetchone()[0]
    user.get("/meetings")
    guest.post("/booking/%s/cancel" % ref)
    status, seen = db.execute("SELECT status, booking_seen FROM tasks").fetchone()
    assert (status, seen) == ("cancelled", 0)
    assert any("Ada" in body and "cancelled" in body for _, body in told["email"])
    assert 'title="New bookings">1<' in user.get("/dashboard").get_data(as_text=True)
    page = user.get("/meetings").get_data(as_text=True)
    assert "Cancelled by them" in page
    again = guest.get("/booking/%s/cancel" % ref).get_data(as_text=True)
    assert "already been cancelled" in again


def test_deleting_a_booking_can_email_the_guest(user, db, guest, told):
    set_hours(user)
    book(guest, make_link(user, db))
    tid = db.execute("SELECT id FROM tasks").fetchone()[0]
    r = user.post("/api/task/delete", json={
        "task_id": tid, "notify": True, "message": "Sorry about this."})
    assert r.get_json()["emailed"] is True
    to, email = told["email"][-1]
    assert to == "ada@example.com" and "Sorry about this." in email
    assert "has been cancelled" in email and "/book/" in email
    assert db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
