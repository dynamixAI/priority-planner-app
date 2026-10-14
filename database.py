"""The database tables (created or updated on start-up) and the four
priorities every new account starts with."""
from core import get_db


DEFAULT_PRIORITIES = [
    {"label": "Urgent & Important", "color": "#ef4444"},
    {"label": "Not Urgent, but Important", "color": "#3b82f6"},
    {"label": "Urgent, Not Important", "color": "#f59e0b"},
    {"label": "Not Urgent & Not Important", "color": "#10b981"}
]


def seed_default_priorities(conn, user_id):
    for i, p in enumerate(DEFAULT_PRIORITIES):
        conn.execute(
            "INSERT INTO priorities (user_id, label, color, sort_order, is_active) VALUES (?, ?, ?, ?, 1)",
            (user_id, p["label"], p["color"], i)
        )
    conn.commit()


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            theme TEXT DEFAULT 'light',
            week_start TEXT DEFAULT 'sunday',
            active_days TEXT DEFAULT '["0","1","2","3","4","5","6"]',
            failed_attempts INTEGER DEFAULT 0,
            lock_until REAL DEFAULT 0,
            is_permanently_locked INTEGER DEFAULT 0,
            overdue_threshold_days INTEGER DEFAULT 14,
            reset_token TEXT,
            reset_token_expiry REAL DEFAULT 0,
            grid_orientation TEXT DEFAULT 'time_rows',
            time_format TEXT DEFAULT '24h',
            timezone TEXT DEFAULT 'Europe/London'
        );
    """)
    # Guarded migrations for databases created before these columns existed —
    # ALTER TABLE ADD COLUMN has no "IF NOT EXISTS" in SQLite/libSQL, so we
    # just try each and ignore the error if the column is already there.
    for stmt in [
        "ALTER TABLE users ADD COLUMN overdue_threshold_days INTEGER DEFAULT 14;",
        "ALTER TABLE users ADD COLUMN reset_token TEXT;",
        "ALTER TABLE users ADD COLUMN reset_token_expiry REAL DEFAULT 0;",
        "ALTER TABLE users ADD COLUMN grid_orientation TEXT DEFAULT 'time_rows';",
        "ALTER TABLE users ADD COLUMN terms_accepted_at REAL DEFAULT 0;",
        "ALTER TABLE users ADD COLUMN marketing_opt_in INTEGER DEFAULT 0;",
        "ALTER TABLE users ADD COLUMN time_format TEXT DEFAULT '24h';",
        "ALTER TABLE users ADD COLUMN timezone TEXT DEFAULT 'Europe/London';",
        # 0 for accounts made with Google that haven't set a password yet.
        "ALTER TABLE users ADD COLUMN has_password INTEGER DEFAULT 1;",
        # 1 once the user has seen (or skipped) the first-run tour.
        "ALTER TABLE users ADD COLUMN tour_seen INTEGER DEFAULT 0;",
        "ALTER TABLE users ADD COLUMN settings_tour_seen INTEGER DEFAULT 0;",
        # When the user last opened their dashboard (for the admin stats).
        "ALTER TABLE users ADD COLUMN last_seen_at REAL DEFAULT 0;",
        # Booking settings
        "ALTER TABLE users ADD COLUMN booking_name TEXT DEFAULT '';",
        "ALTER TABLE users ADD COLUMN booking_lengths TEXT DEFAULT '30,60';",
        "ALTER TABLE users ADD COLUMN booking_days_ahead INTEGER DEFAULT 14;",
        "ALTER TABLE users ADD COLUMN booking_notice_hours INTEGER DEFAULT 2;",
    ]:
        try:
            conn.execute(stmt)
            conn.commit()
        except Exception:
            pass
    conn.execute("""
        CREATE TABLE IF NOT EXISTS priorities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            label TEXT NOT NULL,
            color TEXT NOT NULL,
            sort_order INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS push_subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            endpoint TEXT NOT NULL UNIQUE,
            p256dh TEXT NOT NULL,
            auth TEXT NOT NULL,
            created_at REAL DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
    """)
    conn.commit()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            detail TEXT,
            location TEXT,
            date TEXT NOT NULL,
            time TEXT NOT NULL,
            duration TEXT NOT NULL,
            duration_minutes INTEGER DEFAULT 60,
            priority_id INTEGER,
            status TEXT DEFAULT 'pending',
            origin_id INTEGER,
            reminder_sent INTEGER DEFAULT 0,
            followup_dismissed INTEGER DEFAULT 0,
            completed_late INTEGER DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (priority_id) REFERENCES priorities(id),
            FOREIGN KEY (origin_id) REFERENCES tasks(id)
        );
    """)
    # weekday: 0 = Monday ... 6 = Sunday
    conn.execute("""
        CREATE TABLE IF NOT EXISTS availability (
            user_id INTEGER NOT NULL,
            weekday INTEGER NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            PRIMARY KEY (user_id, weekday)
        );
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS booking_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            token TEXT NOT NULL UNIQUE,
            active INTEGER DEFAULT 1,
            created_at REAL DEFAULT 0
        );
    """)
    # Guarded task-column migrations for databases created before these
    # columns existed. They must run AFTER the CREATE TABLE above — on a
    # brand-new database the tasks table doesn't exist until then.
    for stmt in [
        "ALTER TABLE tasks ADD COLUMN reminder_sent INTEGER DEFAULT 0;",
        "ALTER TABLE tasks ADD COLUMN followup_dismissed INTEGER DEFAULT 0;",
        "ALTER TABLE tasks ADD COLUMN completed_late INTEGER DEFAULT 0;",
        # When the task was added (for the admin stats). 0 for older tasks.
        "ALTER TABLE tasks ADD COLUMN created_at REAL DEFAULT 0;",
        # Bookings
        "ALTER TABLE tasks ADD COLUMN booked_by TEXT;",
        "ALTER TABLE tasks ADD COLUMN booked_via TEXT;",
        "ALTER TABLE tasks ADD COLUMN booking_ref TEXT;",
        "ALTER TABLE tasks ADD COLUMN booked_email TEXT;",
        # 0 until the owner opens the Meetings page
        "ALTER TABLE tasks ADD COLUMN booking_seen INTEGER DEFAULT 1;",
        # Meetings: in_person / phone / online, outcome held / missed / no_show
        "ALTER TABLE tasks ADD COLUMN meeting_type TEXT;",
        "ALTER TABLE tasks ADD COLUMN meeting_outcome TEXT;",
    ]:
        try:
            conn.execute(stmt)
            conn.commit()
        except Exception:
            pass
    # One-time migration for databases created before duration_minutes existed:
    # add the column (no default, so existing rows land as NULL), then backfill
    # from the old text durations. The UPDATE only touches NULL rows, so it's
    # safe — and necessary — to run on every startup.
    try:
        conn.execute("ALTER TABLE tasks ADD COLUMN duration_minutes INTEGER;")
        conn.commit()
    except Exception:
        pass
    try:
        conn.execute("""
            UPDATE tasks SET duration_minutes = CASE duration
                WHEN '30 mins' THEN 30
                WHEN '45 mins' THEN 45
                WHEN '1 hour' THEN 60
                WHEN '1.5 hours' THEN 90
                WHEN '2 hours' THEN 120
                ELSE 60
            END
            WHERE duration_minutes IS NULL;
        """)
        conn.commit()
    except Exception:
        pass
    conn.commit()
