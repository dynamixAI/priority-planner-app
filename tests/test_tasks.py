"""Adding, finishing and moving tasks: the honesty rules."""
from conftest import TODAY, add_task, status_of, task_id


def test_you_must_be_logged_in_to_add_a_task(client):
    assert add_task(client).status_code == 401


def test_adding_a_task_saves_it(user, db):
    r = add_task(user, title="Gym", time="18:00", minutes=45)
    assert r.status_code == 200
    row = db.execute("SELECT date, time, duration_minutes, status, created_at "
                     "FROM tasks WHERE title = 'Gym'").fetchone()
    assert row[:4] == (TODAY, "18:00", 45, "pending")
    assert row[4] > 0      # when it was added, for the admin stats


def test_bad_input_is_refused(user):
    assert add_task(user, title="").status_code == 400
    assert add_task(user, date="13/10/2026").status_code == 400
    assert add_task(user, time="25:99").status_code == 400


def test_done_and_done_late(user, db):
    add_task(user, title="Run", time="07:00")
    add_task(user, title="Read", time="08:00")
    run, read = task_id(db, "Run"), task_id(db, "Read")
    user.post("/api/task/status", json={"task_id": run, "status": "completed"})
    user.post("/api/task/status", json={"task_id": read, "status": "completed",
                                        "late": True})
    late = dict(db.execute("SELECT title, completed_late FROM tasks").fetchall())
    assert late == {"Run": 0, "Read": 1}


def test_a_missed_task_cannot_be_turned_into_done(user, db):
    add_task(user, title="Run", time="07:00")
    tid = task_id(db, "Run")
    assert user.post("/api/task/status",
                     json={"task_id": tid, "status": "missed"}).status_code == 200
    r = user.post("/api/task/status", json={"task_id": tid, "status": "completed"})
    assert r.status_code == 400
    assert status_of(db, tid) == "missed"


def test_moving_a_task_before_it_is_due_is_a_free_replan(user, db):
    add_task(user, title="Gym", time="18:00")           # later today (now is 10:00)
    tid = task_id(db, "Gym")
    r = user.post("/api/task/reschedule",
                  json={"task_id": tid, "new_date": "2026-10-14",
                        "new_time": "18:00"})
    assert r.get_json()["result"] == "replanned"
    assert status_of(db, tid) == "replanned"
    new = db.execute("SELECT date, status, origin_id FROM tasks WHERE id > ?",
                     (tid,)).fetchone()
    assert new == ("2026-10-14", "pending", tid)


def test_moving_a_task_after_it_was_due_counts_as_a_push(user, db):
    add_task(user, title="Run", time="07:00")           # already due (now is 10:00)
    tid = task_id(db, "Run")
    r = user.post("/api/task/reschedule",
                  json={"task_id": tid, "new_date": "2026-10-14",
                        "new_time": "07:00"})
    assert r.get_json()["result"] == "pushed"
    assert status_of(db, tid) == "rescheduled"


def test_a_missed_task_gets_one_follow_up_and_stays_missed(user, db):
    add_task(user, title="Run", date="2026-10-12", time="07:00")
    tid = task_id(db, "Run")
    user.post("/api/task/status", json={"task_id": tid, "status": "missed"})
    first = user.post("/api/task/reschedule", json={
        "task_id": tid, "new_date": "2026-10-14", "new_time": "07:00"})
    assert first.get_json()["result"] == "missed_kept"
    assert status_of(db, tid) == "missed"               # the miss isn't erased
    second = user.post("/api/task/reschedule", json={
        "task_id": tid, "new_date": "2026-10-15", "new_time": "07:00"})
    assert second.status_code == 400                    # only one follow-up


def test_you_cannot_touch_someone_elses_task(client, db):
    from conftest import register
    register(client, "alex")
    add_task(client, title="Alex's task")
    tid = task_id(db, "Alex's task")
    client.get("/logout")
    register(client, "sam")
    r = client.post("/api/task/status", json={"task_id": tid, "status": "completed"})
    assert r.status_code == 404
    assert status_of(db, tid) == "pending"
