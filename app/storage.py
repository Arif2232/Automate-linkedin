import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("POST_STUDIO_DATA_DIR", ROOT / "data")).expanduser().resolve()
DB_PATH = DATA_DIR / "studio.sqlite3"
IMAGE_DIR = DATA_DIR / "images"
UTC = timezone.utc


def now():
    return datetime.now(UTC)


def timestamp(value):
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def next_occurrence(anchor, unit, every, zone_name, after):
    """Skip missed slots; daily and weekly schedules retain their local clock time."""
    anchor = parse_time(anchor)
    after = after.astimezone(UTC)
    if unit == "hours":
        interval = timedelta(hours=every)
        steps = max(1, (after - anchor) // interval + 1)
        return anchor + steps * interval

    zone = ZoneInfo(zone_name)
    local_anchor = anchor.astimezone(zone)
    days = every * (7 if unit == "weeks" else 1)
    steps = max(1, (after.astimezone(zone).date() - local_anchor.date()).days // days)
    candidate = (local_anchor + timedelta(days=steps * days)).astimezone(UTC)
    while candidate <= after:
        steps += 1
        candidate = (local_anchor + timedelta(days=steps * days)).astimezone(UTC)
    return candidate


@contextmanager
def db(write=False):
    connection = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    if write:
        connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
        if write:
            connection.commit()
    except BaseException:
        if write:
            connection.rollback()
        raise
    finally:
        connection.close()


def initialize():
    DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix" and DATA_DIR.stat().st_mode & 0o077:
        raise RuntimeError("The data directory must be private (permissions 700).")
    IMAGE_DIR.mkdir(exist_ok=True, mode=0o700)
    if os.name == "posix" and IMAGE_DIR.stat().st_mode & 0o077:
        raise RuntimeError("The images directory must be private (permissions 700).")
    if DB_PATH.is_symlink():
        raise RuntimeError("The database cannot be a symlink.")
    fd = os.open(DB_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    if os.name == "posix" and DB_PATH.stat().st_mode & 0o077:
        raise RuntimeError("The database must be private (permissions 600).")
    with db() as connection:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS series (
                id TEXT PRIMARY KEY,
                topic TEXT NOT NULL,
                context TEXT NOT NULL,
                repeat_unit TEXT NOT NULL,
                repeat_every INTEGER NOT NULL,
                timezone TEXT NOT NULL,
                anchor_at TEXT NOT NULL,
                active INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS posts (
                id TEXT PRIMARY KEY,
                series_id TEXT NOT NULL REFERENCES series(id),
                scheduled_at TEXT NOT NULL,
                status TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                image_file TEXT,
                image_instruction TEXT NOT NULL DEFAULT '',
                image_error TEXT,
                error TEXT,
                approved_author TEXT,
                linkedin_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id TEXT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS posts_due ON posts(status, scheduled_at);
        """)
    with db(write=True) as connection:
        connection.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES('session_secret', ?)",
            (secrets.token_urlsafe(48),),
        )


@contextmanager
def instance_lock():
    lock = open(DATA_DIR / "server.lock", "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            lock.seek(0)
            if not lock.read(1):
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("Post Studio is already running with this data directory.") from exc
        else:
            import fcntl
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("Post Studio is already running with this data directory.") from exc
        yield
    finally:
        lock.close()


def recover_interrupted():
    with db(write=True) as connection:
        connection.execute(
            "UPDATE posts SET status = 'queued', updated_at = ?, revision = revision + 1 WHERE status = 'generating'",
            (timestamp(now()),),
        )
        connection.execute(
            "UPDATE posts SET status = 'image_queued', updated_at = ?, revision = revision + 1 WHERE status = 'image_generating'",
            (timestamp(now()),),
        )
        connection.execute(
            "UPDATE posts SET status = 'publish_uncertain', error = ?, updated_at = ?, revision = revision + 1 "
            "WHERE status = 'publishing'",
            ("Publishing was interrupted. Check LinkedIn before trying again; the post may have been sent.", timestamp(now())),
        )


def get_setting(key, default=""):
    with db() as connection:
        row = connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_setting(connection, key, value):
    connection.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


POST_QUERY = """
    SELECT posts.*, series.topic, series.context, series.repeat_unit, series.repeat_every,
           series.timezone, series.anchor_at, series.active AS series_active
    FROM posts JOIN series ON series.id = posts.series_id
"""


def find_post(connection, post_id):
    return connection.execute(POST_QUERY + " WHERE posts.id = ?", (post_id,)).fetchone()


def public_post(row):
    if row is None:
        return None
    post = {key: row[key] for key in (
        "id", "series_id", "topic", "context", "scheduled_at", "status", "content",
        "image_error", "error", "linkedin_id", "created_at", "updated_at", "revision",
        "repeat_unit", "repeat_every", "timezone", "series_active",
    )}
    post["image_url"] = f"/api/posts/{row['id']}/image?v={row['revision']}" if row["image_file"] else None
    return post
