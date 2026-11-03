import asyncio
import logging
from uuid import uuid4

from . import providers, services
from .storage import (
    IMAGE_DIR, MAX_REPEATS, db, find_post, get_setting, next_occurrence, now, parse_time, timestamp,
)

log = logging.getLogger(__name__)


def claim(status, target, due=False):
    with db(write=True) as connection:
        sql = "SELECT id FROM posts WHERE status = ?"
        values = [status]
        if due:
            sql += " AND scheduled_at <= ?"
            values.append(timestamp(now()))
        row = connection.execute(sql + " ORDER BY scheduled_at LIMIT 1", values).fetchone()
        if not row:
            return None
        connection.execute(
            "UPDATE posts SET status = ?, updated_at = ?, revision = revision + 1 WHERE id = ?",
            (target, timestamp(now()), row["id"]),
        )
        return row["id"]


async def make_draft(post_id, image_only=False):
    expected = "image_generating" if image_only else "generating"
    with db() as connection:
        row = find_post(connection, post_id)
    if not row or row["status"] != expected:
        return
    content = row["content"]
    old_image = row["image_file"]
    active_provider = "AI provider"
    try:
        with db() as connection:
            settings = {item["key"]: item["value"] for item in connection.execute("SELECT key, value FROM settings")}
            previous = [item["content"] for item in connection.execute(
                "SELECT content FROM posts WHERE series_id = ? AND status = 'published' AND id != ? "
                "ORDER BY created_at DESC LIMIT ?", (row["series_id"], post_id, MAX_REPEATS),
            )] if not content else []
        text_config = providers.selected(settings, "text")
        image_config = providers.selected(settings, "image")
        if not content:
            active_provider = text_config["name"]
            if not text_config["ready"]:
                raise LookupError("Configure a writing provider and model in Settings.")
            if not text_config["tested"]:
                raise LookupError("Test the writing connection in Settings before continuing.")
            content = await services.generate_text(text_config, row["topic"], row["context"], previous)
            with db(write=True) as connection:
                connection.execute(
                    "UPDATE posts SET content = ?, updated_at = ?, revision = revision + 1 WHERE id = ? AND status = ?",
                    (content, timestamp(now()), post_id, expected),
                )
        active_provider = image_config["name"]
        if image_config["provider"] == "none" and not image_only:
            with db(write=True) as connection:
                connection.execute(
                    "UPDATE posts SET status = 'needs_review', image_error = NULL, error = NULL, "
                    "updated_at = ?, revision = revision + 1 WHERE id = ? AND status = ?",
                    (timestamp(now()), post_id, expected),
                )
            return
        if image_config["provider"] == "none" or not image_config["ready"]:
            raise LookupError("Choose and configure an image provider in Settings.")
        if not image_config["tested"]:
            raise LookupError("Test the image connection in Settings before continuing.")
        existing = services.read_image(old_image) if image_only and old_image and (IMAGE_DIR / old_image).is_file() else None
        filename = await services.generate_image(
            image_config, row["topic"], row["context"], content, row["image_instruction"], existing,
        )
        with db(write=True) as connection:
            result = connection.execute(
                "UPDATE posts SET status = 'needs_review', image_file = ?, image_error = NULL, error = NULL, "
                "updated_at = ?, revision = revision + 1 WHERE id = ? AND status = ?",
                (filename, timestamp(now()), post_id, expected),
            )
        if result.rowcount:
            if old_image and old_image != filename:
                (IMAGE_DIR / old_image).unlink(missing_ok=True)
        else:
            (IMAGE_DIR / filename).unlink(missing_ok=True)
    except Exception as exc:
        message = str(exc) if isinstance(exc, LookupError) else services.error_message(exc, active_provider)
        log.warning("Draft generation failed: %s", type(exc).__name__)
        with db(write=True) as connection:
            current = find_post(connection, post_id)
            if current and current["status"] == expected:
                if current["content"]:
                    connection.execute(
                        "UPDATE posts SET status = 'needs_review', image_error = ?, updated_at = ?, "
                        "revision = revision + 1 WHERE id = ?",
                        (message, timestamp(now()), post_id),
                    )
                else:
                    connection.execute(
                        "UPDATE posts SET status = 'generation_failed', error = ?, updated_at = ?, "
                        "revision = revision + 1 WHERE id = ?",
                        (message, timestamp(now()), post_id),
                    )


async def publish(post_id):
    with db() as connection:
        row = find_post(connection, post_id)
    if not row or row["status"] != "publishing":
        return
    try:
        token = get_setting("linkedin_token")
        person_id = get_setting("linkedin_person_id")
        expiry = get_setting("linkedin_expires_at")
        if not token or not person_id or not expiry or parse_time(expiry) <= now():
            raise LookupError("Your LinkedIn connection has expired. Reconnect in Settings.")
        if row["approved_author"] != person_id:
            raise LookupError("The connected LinkedIn account changed. Review and approve the post again.")
        image = services.read_image(row["image_file"]) if row["image_file"] else None
        linkedin_id = await services.publish_to_linkedin(token, person_id, row["content"], image)
    except Exception as exc:
        message = str(exc) if isinstance(exc, LookupError) else services.error_message(exc, "LinkedIn")
        log.warning("Publishing failed: %s", type(exc).__name__)
        with db(write=True) as connection:
            connection.execute(
                "UPDATE posts SET status = 'publish_failed', error = ?, updated_at = ?, "
                "revision = revision + 1 WHERE id = ? AND status = 'publishing'",
                (message + " Check LinkedIn before trying again; it may have been posted.", timestamp(now()), post_id),
            )
        return

    try:
        with db(write=True) as connection:
            result = connection.execute(
                "UPDATE posts SET status = 'published', linkedin_id = ?, error = NULL, updated_at = ?, "
                "revision = revision + 1 WHERE id = ? AND status = 'publishing'",
                (linkedin_id, timestamp(now()), post_id),
            )
            if not result.rowcount:
                return
            current = find_post(connection, post_id)
            if current["series_active"] and current["repeat_unit"] != "once":
                try:
                    if current["series_post_count"] - 1 >= current["repeat_limit"]:
                        connection.execute("UPDATE series SET active = 0 WHERE id = ?", (current["series_id"],))
                    else:
                        scheduled = next_occurrence(
                            current["anchor_at"], current["repeat_unit"], current["repeat_every"],
                            current["timezone"], now(),
                        )
                        created = timestamp(now())
                        connection.execute(
                            "INSERT INTO posts(id, series_id, scheduled_at, status, created_at, updated_at) "
                            "VALUES(?, ?, ?, 'queued', ?, ?)",
                            (uuid4().hex, current["series_id"], timestamp(scheduled), created, created),
                        )
                except Exception:
                    connection.execute("UPDATE series SET active = 0 WHERE id = ?", (current["series_id"],))
                    connection.execute(
                        "UPDATE posts SET error = ? WHERE id = ?",
                        ("Published, but the next recurring draft could not be scheduled. Create a new schedule.", post_id),
                    )
                    log.exception("Could not create the next recurring draft")
    except Exception:
        log.exception("Could not save the publishing result")
        try:
            with db(write=True) as connection:
                connection.execute(
                    "UPDATE posts SET status = 'publish_uncertain', error = ?, updated_at = ?, "
                    "revision = revision + 1 WHERE id = ? AND status = 'publishing'",
                    ("LinkedIn accepted the post, but its result could not be saved. Check LinkedIn before trying again.",
                     timestamp(now()), post_id),
                )
        except Exception:
            log.error("Could not record the uncertain publishing state; restart to recover it")


async def scheduler():
    # shortcut: scheduling stops with this process; use an always-on host for unattended publishing.
    publishing = set()
    generating = set()

    def start(coro, tasks):
        task = asyncio.create_task(coro)
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    try:
        while True:
            while len(publishing) < 2:
                post_id = claim("approved", "publishing", due=True)
                if not post_id:
                    break
                start(publish(post_id), publishing)
            while len(generating) < 2:
                post_id = claim("queued", "generating")
                image_only = False
                if not post_id:
                    post_id = claim("image_queued", "image_generating")
                    image_only = True
                if not post_id:
                    break
                start(make_draft(post_id, image_only), generating)
            await asyncio.sleep(2)
    finally:
        for task in publishing | generating:
            task.cancel()
        await asyncio.gather(*publishing, *generating, return_exceptions=True)
