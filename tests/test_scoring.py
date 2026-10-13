"""The Behaviour Index: the heart of Agenndar.

Scoring rules being checked (each judged task adds its weight, then the
total is turned into a percentage of the judged tasks):
  done on time = 1.0, done late = 0.7, pushed after due = 0.4,
  missed = -0.6, open long past its first date = an extra -0.3.
Moving a task before it's due ("replanned") and tasks not due yet are not
judged at all.
"""
from conftest import FROZEN_NOW


def make_tasks(db, user_id, rows):
    """rows: (date, time, status, completed_late)"""
    for date, time, status, late in rows:
        db.execute(
            "INSERT INTO tasks (user_id, title, date, time, duration, status, "
            "completed_late) VALUES (?, 'x', ?, ?, '60 mins', ?, ?)",
            (user_id, date, time, status, late))
    db.commit()


def score(app, db, user_id, threshold=14):
    return app.compute_metrics(db, user_id, FROZEN_NOW, threshold)


def new_user(db):
    db.execute("INSERT INTO users (username, email, password_hash) "
               "VALUES ('u', 'u@example.com', 'x')")
    db.commit()
    return db.execute("SELECT id FROM users WHERE username = 'u'").fetchone()[0]


def test_a_new_user_with_no_tasks_starts_at_100(app, db):
    m = score(app, db, new_user(db))
    assert m["bhi_score"] == 100.0
    assert m["bhi_tier"] == "Disciplined"


def test_everything_done_on_time_scores_100(app, db):
    uid = new_user(db)
    make_tasks(db, uid, [("2026-10-12", "09:00", "completed", 0)] * 3)
    assert score(app, db, uid)["bhi_score"] == 100.0


def test_done_late_counts_less_than_done_on_time(app, db):
    uid = new_user(db)
    make_tasks(db, uid, [("2026-10-12", "09:00", "completed", 1)])
    m = score(app, db, uid)
    assert m["bhi_score"] == 70.0       # 0.7 out of 1
    assert m["done"] == 1 and m["done_late"] == 1


def test_one_of_each_outcome(app, db):
    uid = new_user(db)
    make_tasks(db, uid, [
        ("2026-10-12", "09:00", "completed", 0),    # +1.0
        ("2026-10-12", "10:00", "completed", 1),    # +0.7
        ("2026-10-12", "11:00", "rescheduled", 0),  # +0.4 (pushed after due)
        ("2026-10-12", "12:00", "missed", 0),       # -0.6
    ])
    m = score(app, db, uid)
    assert m["bhi_score"] == 37.5       # (1 + 0.7 + 0.4 - 0.6) / 4 * 100
    assert m["bhi_tier"] == "Avoidance"
    assert (m["pushed"], m["missed"]) == (1, 1)


def test_moving_a_task_before_it_is_due_is_not_judged(app, db):
    uid = new_user(db)
    make_tasks(db, uid, [
        ("2026-10-12", "09:00", "completed", 0),
        ("2026-10-12", "10:00", "replanned", 0),    # free move: ignored
    ])
    assert score(app, db, uid)["bhi_score"] == 100.0


def test_tasks_not_due_yet_are_not_judged(app, db):
    uid = new_user(db)
    make_tasks(db, uid, [
        ("2026-10-12", "09:00", "completed", 0),
        ("2026-10-13", "18:00", "pending", 0),      # later today
        ("2026-10-20", "09:00", "pending", 0),      # next week
    ])
    m = score(app, db, uid)
    assert m["bhi_score"] == 100.0
    assert m["total_logged"] == 3       # still counted as logged


def test_a_task_left_open_past_the_threshold_costs_extra(app, db):
    uid = new_user(db)
    make_tasks(db, uid, [
        ("2026-10-12", "09:00", "completed", 0),
        ("2026-09-01", "09:00", "pending", 0),      # 6 weeks old, still open
    ])
    # The old open task is judged (0) and flagged overdue (-0.3): (1 - 0.3) / 2
    assert score(app, db, uid, threshold=14)["bhi_score"] == 35.0
    # With a 60-day threshold it isn't overdue yet: 1 / 2
    assert score(app, db, uid, threshold=60)["bhi_score"] == 50.0


def test_the_score_never_goes_below_zero(app, db):
    uid = new_user(db)
    make_tasks(db, uid, [("2026-10-12", "09:00", "missed", 0)] * 5)
    assert score(app, db, uid)["bhi_score"] == 0


def test_tiers(app, db):
    uid = new_user(db)
    make_tasks(db, uid, [
        ("2026-10-12", "09:00", "completed", 0),
        ("2026-10-12", "10:00", "rescheduled", 0),
    ])
    m = score(app, db, uid)            # (1 + 0.4) / 2 = 70
    assert (m["bhi_score"], m["bhi_tier"]) == (70.0, "Drifting")
