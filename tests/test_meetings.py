"""Meetings."""
from conftest import TODAY, add_task, status_of, task_id
from scoring import compute_metrics
from conftest import FROZEN_NOW

YESTERDAY, TOMORROW = "2026-10-12", "2026-10-14"


def add_meeting(client, title="Dentist", date=TODAY, time="09:00", kind="in_person"):
    return add_task(client, title=title, date=date, time=time, meeting_type=kind)


def outcome(client, tid, value):
    return client.post("/api/meeting/outcome",
                       json={"task_id": tid, "outcome": value})


def score(db):
    return compute_metrics(db, 1, FROZEN_NOW, 14)["bhi_score"]


def test_you_can_add_your_own_meeting(user, db):
    assert add_meeting(user, kind="phone").status_code == 200
    row = db.execute("SELECT meeting_type, status FROM tasks").fetchone()
    assert row == ("phone", "pending")
    assert add_meeting(user, kind="carrier pigeon").status_code == 400


def test_a_missed_meeting_does_not_touch_your_score(user, db):
    add_task(user, title="Gym", time="07:00")
    user.post("/api/task/status", json={"task_id": task_id(db, "Gym"),
                                        "status": "completed"})
    add_meeting(user)  # 09:00
    assert outcome(user, task_id(db, "Dentist"), "missed").status_code == 200
    assert score(db) == 100.0
    assert status_of(db, task_id(db, "Dentist")) == "missed"


def test_meetings_use_their_own_buttons_not_done_or_missed(user, db):
    add_meeting(user)
    r = user.post("/api/task/status", json={"task_id": task_id(db, "Dentist"),
                                            "status": "completed"})
    assert r.status_code == 400


def test_you_answer_how_a_meeting_went_and_can_undo_it(user, db):
    add_meeting(user)
    tid = task_id(db, "Dentist")
    for value, status in (("held", "completed"), ("no_show", "missed"),
                          ("missed", "missed")):
        assert outcome(user, tid, value).status_code == 200
        assert status_of(db, tid) == status
    assert outcome(user, tid, None).status_code == 200
    assert db.execute("SELECT status, meeting_outcome FROM tasks").fetchone() == (
        "pending", None)
    assert outcome(user, tid, "fell asleep").status_code == 400


def test_you_cannot_answer_before_the_meeting_starts(user, db):
    add_meeting(user, time="15:00")
    assert outcome(user, task_id(db, "Dentist"), "held").status_code == 400


def test_meetings_are_not_marked_missed_overnight(user, db):
    add_task(user, title="Old task", date=YESTERDAY, time="09:00")
    add_meeting(user, title="Old meeting", date=YESTERDAY)
    user.get("/dashboard")
    assert status_of(db, task_id(db, "Old task")) == "missed"
    assert status_of(db, task_id(db, "Old meeting")) == "pending"


def test_the_meetings_page_shows_waiting_upcoming_and_your_record(user, db):
    add_meeting(user, title="Coffee with Ada")
    add_meeting(user, title="Call the bank", date=TOMORROW, kind="phone")
    add_meeting(user, title="Old review", date=YESTERDAY)
    outcome(user, task_id(db, "Old review"), "no_show")
    page = user.get("/meetings").get_data(as_text=True)
    waiting, rest = page.split("How did these go?")[1].split("Coming up")
    assert "Coffee with Ada" in waiting
    upcoming, earlier = rest.split("Earlier")
    assert "Call the bank" in upcoming
    assert "Old review" in earlier
    assert "<b>1</b>they" in page.replace("\n", "").replace("  ", "")


def test_the_dashboard_lists_leave_meetings_out(user, db):
    add_task(user, title="Gym", time="18:00")
    add_meeting(user, time="18:30")
    listed = user.get("/api/tasks/list?status=pending").get_json()["tasks"]
    assert [t["title"] for t in listed] == ["Gym"]
    # overlap check
    clash = user.get("/api/tasks/list?status=pending&date=" + TODAY).get_json()
    assert len(clash["tasks"]) == 2


def test_moving_a_meeting_keeps_it_a_meeting(user, db):
    add_meeting(user, time="15:00")
    tid = task_id(db, "Dentist")
    db.execute("UPDATE tasks SET booking_ref = 'ref123' WHERE id = ?", (tid,))
    db.commit()
    user.post("/api/task/reschedule", json={"task_id": tid, "new_date": TOMORROW,
                                            "new_time": "15:00"})
    new = db.execute("SELECT meeting_type, booking_ref FROM tasks WHERE id > ?",
                     (tid,)).fetchone()
    assert new == ("in_person", "ref123")
    assert db.execute("SELECT booking_ref FROM tasks WHERE id = ?",
                      (tid,)).fetchone()[0] is None
