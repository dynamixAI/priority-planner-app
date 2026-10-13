"""The Behaviour Index score and the catch-up banner's task list."""
from datetime import timedelta

from core import fetch_all, fetch_one


# BHI weight for a task marked "Done Late" (done on time = 1.0, pushed = 0.4).
LATE_DONE_WEIGHT = 0.7


def compute_metrics(conn, user_id, now_dt, overdue_threshold_days):
    """Every dashboard number and the BHI score, worked out by the
    database (counts) rather than by loading every task into Python.
    Used by both the dashboard page and the live /api/metrics refresh,
    so the scoring rules live in exactly one place."""
    today_str = now_dt.strftime("%Y-%m-%d")
    now_hhmm = now_dt.strftime("%H:%M")
    row = fetch_one(conn, """
        SELECT
          COUNT(*) AS total,
          SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS done,
          SUM(CASE WHEN status = 'completed'
                    AND COALESCE(completed_late, 0) = 1
                   THEN 1 ELSE 0 END) AS done_late,
          SUM(CASE WHEN status = 'rescheduled' THEN 1 ELSE 0 END) AS pushed,
          SUM(CASE WHEN status = 'missed' THEN 1 ELSE 0 END) AS missed,
          SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS undone,
          SUM(CASE WHEN status = 'replanned' THEN 1 ELSE 0 END) AS replanned,
          SUM(CASE WHEN status = 'pending'
                    AND (date > ? OR (date = ? AND time > ?))
                   THEN 1 ELSE 0 END) AS not_yet_due
        FROM tasks WHERE user_id = ?
    """, (today_str, today_str, now_hhmm, user_id)) or {}
    n = {k: (v or 0) for k, v in row.items()}

    # Overdue-threshold penalty: a still-open task, chronically outstanding
    # since its FIRST scheduled date (even across pushes/replans), costs a
    # fixed 0.3 points once it crosses the user's own configured day count.
    cutoff = (now_dt.date() - timedelta(days=overdue_threshold_days)).strftime("%Y-%m-%d")
    overdue = fetch_one(conn, """
        SELECT COUNT(*) AS n FROM tasks t
        LEFT JOIN tasks o ON o.id = t.origin_id AND o.user_id = t.user_id
        WHERE t.user_id = ? AND t.status = 'pending'
          AND COALESCE(o.date, t.date) <= ?
    """, (user_id, cutoff))
    overdue_flagged = (overdue or {}).get("n") or 0

    # Free replans and not-yet-due tasks aren't judged at all. They still
    # count toward "Total Logged", which tracks every task ever entered.
    bhi_total = n["total"] - n["replanned"] - n["not_yet_due"]
    if bhi_total > 0:
        score_raw = (
            ((n["done"] - n["done_late"]) * 1.0)
            + (n["done_late"] * LATE_DONE_WEIGHT) + (n["pushed"] * 0.4)
            - (n["missed"] * 0.6) - (overdue_flagged * 0.3)
        ) / bhi_total * 100
        bhi_score = max(0, min(100, round(score_raw, 1)))
    else:
        bhi_score = 100.0

    if bhi_score >= 80:
        bhi_tier, bhi_color = "Disciplined", "#16a34a"
    elif bhi_score >= 50:
        bhi_tier, bhi_color = "Drifting", "#eab308"
    else:
        bhi_tier, bhi_color = "Avoidance", "#ef4444"

    return {
        "total_logged": n["total"],
        "total": n["total"],
        "done": n["done"],
        "done_late": n["done_late"],
        "undone": n["undone"],
        "pushed": n["pushed"],
        "missed": n["missed"],
        "bhi_score": bhi_score,
        "bhi_tier": bhi_tier,
        "bhi_color": bhi_color,
    }


def fetch_attention_tasks(conn, user_id, today_str):
    """Just the tasks the catch-up banner needs: today's (or earlier)
    tasks still open, plus missed tasks that haven't been rescheduled
    or dismissed. The browser no longer needs every task ever made."""
    open_tasks = fetch_all(conn, """
        SELECT * FROM tasks
        WHERE user_id = ? AND status = 'pending' AND date <= ?
        ORDER BY date ASC, time ASC
    """, (user_id, today_str))
    missed = fetch_all(conn, """
        SELECT t.* FROM tasks t
        WHERE t.user_id = ? AND t.status = 'missed'
          AND COALESCE(t.followup_dismissed, 0) = 0
          AND NOT EXISTS (
            SELECT 1 FROM tasks x
            WHERE x.user_id = t.user_id
              AND x.origin_id = COALESCE(t.origin_id, t.id)
              AND x.id > t.id
          )
        ORDER BY t.date ASC, t.time ASC
        LIMIT 50
    """, (user_id,))
    return open_tasks + missed
