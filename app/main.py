import asyncio
import logging
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from threading import Lock
from typing import Literal
from urllib.parse import urlencode
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from . import providers, services, worker
from .storage import (
    IMAGE_DIR, ROOT, db, find_post, get_setting, initialize, instance_lock, now, parse_time,
    public_post, recover_interrupted, set_setting, timestamp, POST_QUERY,
)

initialize()
REDIRECT_URI = "http://localhost:8000/auth/linkedin/callback"
log = logging.getLogger(__name__)
ai_test_locks = {"text": Lock(), "image": Lock()}


@asynccontextmanager
async def lifespan(app):
    with instance_lock():
        recover_interrupted()
        task = asyncio.create_task(worker.scheduler())
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


app = FastAPI(title="Post Studio", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=get_setting("session_secret"), same_site="lax", https_only=False)


@app.middleware("http")
async def local_only(request: Request, call_next):
    host = request.headers.get("host", "").lower()
    if host not in {"localhost:8000", "127.0.0.1:8000"}:
        return JSONResponse({"detail": "Open the app at http://localhost:8000."}, status_code=400)
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        if request.headers.get("origin") != f"http://{host}" or request.headers.get("x-post-studio") != "1":
            return JSONResponse({"detail": "Request must come from this app."}, status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    )
    if request.url.path.startswith(("/api/", "/static/")) or request.url.path == "/":
        response.headers["Cache-Control"] = "no-store"
    return response


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


class SettingsInput(BaseModel):
    openai_api_key: str | None = Field(default=None, max_length=500)
    linkedin_client_id: str | None = Field(default=None, max_length=500)
    linkedin_client_secret: str | None = Field(default=None, max_length=1000)


class AISettingsInput(BaseModel):
    kind: Literal["text", "image"]
    provider: str = Field(min_length=1, max_length=40)
    model: str = Field(default="", max_length=200)
    api_key: str | None = Field(default=None, max_length=2000)
    base_url: str = Field(default="", max_length=1000)


class PostInput(BaseModel):
    topic: str = Field(min_length=1, max_length=300)
    context: str = Field(default="", max_length=10000)
    scheduled_at: datetime
    timezone: str = Field(min_length=1, max_length=100)
    repeat_unit: Literal["once", "hours", "days", "weeks"] = "once"
    repeat_every: int = Field(default=1, ge=1, le=365)


class PostEdit(BaseModel):
    topic: str | None = Field(default=None, max_length=300)
    content: str | None = Field(default=None, min_length=1, max_length=3000)
    scheduled_at: datetime | None = None
    timezone: str | None = Field(default=None, min_length=1, max_length=100)
    repeat_unit: Literal["once", "hours", "days", "weeks"] | None = None
    repeat_every: int | None = Field(default=None, ge=1, le=365)
    revision: int = Field(ge=0)


class RevisionInput(BaseModel):
    revision: int = Field(ge=0)


class ApprovalInput(RevisionInput):
    publish_without_image: bool = False


class ImageInput(RevisionInput):
    instruction: str = Field(default="", max_length=2000)


class ChatInput(RevisionInput):
    message: str = Field(min_length=1, max_length=4000)


def valid_time(value, zone):
    if value.tzinfo is None or value.utcoffset() is None:
        raise HTTPException(400, "The scheduled time must include a time zone.")
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(400, "Choose a valid time zone.")
    return timestamp(value)


def require_post(connection, post_id):
    row = find_post(connection, post_id)
    if not row:
        raise HTTPException(404, "Post not found.")
    return row


def editable(row, revision):
    if row["status"] != "needs_review":
        raise HTTPException(409, "Move this post back to review before editing it.")
    if row["revision"] != revision:
        raise HTTPException(409, "This draft changed in another window. Refresh and try again.")


def setting_values(connection):
    return {r["key"]: r["value"] for r in connection.execute("SELECT key, value FROM settings")}


def connected(settings):
    try:
        return bool(settings.get("linkedin_token") and settings.get("linkedin_person_id") and
                    parse_time(settings.get("linkedin_expires_at", "")) > now())
    except ValueError:
        return False


def config_payload(connection):
    settings = setting_values(connection)
    return {
        "ai": providers.public_config(settings),
        "openai_configured": bool(settings.get("openai_api_key")),
        "linkedin_client_id": settings.get("linkedin_client_id", ""),
        "linkedin_secret_configured": bool(settings.get("linkedin_client_secret")),
        "linkedin_connected": connected(settings),
        "linkedin_name": settings.get("linkedin_name", ""),
        "linkedin_expires_at": settings.get("linkedin_expires_at", ""),
        "text_model": services.TEXT_MODEL,
        "image_model": services.IMAGE_MODEL,
    }


def require_tested(config, kind):
    label = "writing" if kind == "text" else "image"
    if not config["ready"]:
        raise HTTPException(400, f"Configure your {label} provider and model in Settings first.")
    if not config["tested"]:
        raise HTTPException(400, f"Test the {label} connection in Settings before continuing.")


def disconnect(connection):
    connection.execute("DELETE FROM settings WHERE key IN ('linkedin_token', 'linkedin_person_id', 'linkedin_name', 'linkedin_expires_at')")
    connection.execute(
        "UPDATE posts SET status = 'needs_review', approved_author = NULL, updated_at = ?, revision = revision + 1 "
        "WHERE status = 'approved'", (timestamp(now()),),
    )


@app.get("/api/config")
def get_config():
    with db() as connection:
        return config_payload(connection)


@app.post("/api/config")
def update_config(payload: SettingsInput):
    with db(write=True) as connection:
        settings = setting_values(connection)
        values = payload.model_dump(exclude_none=True)
        for key, value in values.items():
            value = value.strip()
            if not value:
                continue
            if key.startswith("linkedin_client_") and settings.get(key) != value:
                disconnect(connection)
            set_setting(connection, key, value)
        return config_payload(connection)


@app.post("/api/ai-config")
def update_ai_config(payload: AISettingsInput):
    spec = providers.PROVIDERS.get(payload.provider)
    if not spec or payload.kind not in spec:
        raise HTTPException(400, "Choose a provider that supports this task.")
    model = payload.model.strip() or spec[payload.kind]
    if payload.provider not in {"none", "same"} and (not model or any(ord(char) < 32 for char in model)):
        raise HTTPException(400, "Enter a valid model ID.")
    if payload.provider == "huggingface" and payload.kind == "image" and not providers.huggingface_model_id(model):
        raise HTTPException(400, "Enter a Hugging Face image model ID in the form owner/model.")
    key = (payload.api_key or "").strip()
    if any(char in key for char in "\r\n"):
        raise HTTPException(400, "The API key cannot contain line breaks.")
    try:
        base_url = providers.valid_base_url(payload.base_url) if spec.get("needs_url") else ""
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    with db(write=True) as connection:
        settings = setting_values(connection)
        key_name = providers.key_name(payload.provider, payload.kind)
        url_name = providers.url_name(payload.provider, payload.kind)
        if (spec.get("needs_url") and settings.get(url_name) and settings[url_name] != base_url
                and settings.get(key_name) and not key):
            raise HTTPException(400, "Re-enter the API key when changing its endpoint.")
        if key:
            set_setting(connection, key_name, key)
        if spec.get("needs_url"):
            set_setting(connection, url_name, base_url)
        if payload.provider not in {"none", "same"}:
            set_setting(connection, providers.model_name(payload.provider, payload.kind), model)
        set_setting(connection, f"ai_{payload.kind}_provider", payload.provider)
        return config_payload(connection)


@app.post("/api/ai-config/{kind}/test")
async def test_ai_config(kind: Literal["text", "image"]):
    lock = ai_test_locks[kind]
    if not lock.acquire(blocking=False):
        raise HTTPException(409, "A test for this connection is already running.")
    try:
        with db() as connection:
            settings = setting_values(connection)
            config = providers.selected(settings, kind)
            if not config["ready"]:
                raise HTTPException(400, "Save a provider, model, and any required API key first.")
            if config["provider"] == "none":
                return config_payload(connection)
            signature = providers.test_signature(settings, kind, config)
        try:
            if kind == "text":
                await services.text_response(
                    config, "Reply with the single word OK.",
                    [{"role": "user", "content": "Reply OK."}],
                )
            else:
                filename = await services.generate_image(
                    config, "A simple blue circle on a plain background", "", "A blue circle.",
                )
                try:
                    (IMAGE_DIR / filename).unlink(missing_ok=True)
                except OSError:
                    log.warning("Could not remove the temporary AI test image")
                    raise services.ProviderError("The test image could not be deleted. Check permissions on data/images.") from None
        except Exception as exc:
            with db(write=True) as connection:
                connection.execute(
                    "DELETE FROM settings WHERE key = ? AND value = ?",
                    (providers.test_key_name(config["provider"], kind), signature),
                )
            raise HTTPException(502, services.error_message(exc, config["name"])) from None
        with db(write=True) as connection:
            settings = setting_values(connection)
            current = providers.selected(settings, kind)
            if not current["ready"] or providers.test_signature(settings, kind, current) != signature:
                raise HTTPException(409, "Settings changed during the test. Test the current settings again.")
            set_setting(connection, providers.test_key_name(config["provider"], kind), signature)
            return config_payload(connection)
    finally:
        lock.release()


@app.delete("/api/ai-config/{kind}/{provider}/key")
def delete_provider_key(kind: Literal["text", "image"], provider: str):
    spec = providers.PROVIDERS.get(provider)
    if not spec or kind not in spec or provider in {"none", "same"}:
        raise HTTPException(404, "Provider not found.")
    with db(write=True) as connection:
        connection.execute("DELETE FROM settings WHERE key = ?", (providers.key_name(provider, kind),))
        return config_payload(connection)


@app.delete("/api/config/openai")
def delete_openai_key():
    with db(write=True) as connection:
        connection.execute("DELETE FROM settings WHERE key = 'openai_api_key'")
    return {"ok": True}


@app.post("/api/linkedin/disconnect")
def disconnect_linkedin():
    with db(write=True) as connection:
        disconnect(connection)
    return {"ok": True}


@app.get("/api/linkedin/connect")
def linkedin_connect(request: Request):
    if request.url.hostname != "localhost":
        return RedirectResponse("http://localhost:8000/api/linkedin/connect", status_code=303)
    client_id = get_setting("linkedin_client_id")
    if not client_id or not get_setting("linkedin_client_secret"):
        raise HTTPException(400, "Save your LinkedIn app credentials in Settings first.")
    state = secrets.token_urlsafe(32)
    request.session["linkedin_state"] = state
    query = urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT_URI,
        "state": state, "scope": "openid profile w_member_social",
    })
    return RedirectResponse("https://www.linkedin.com/oauth/v2/authorization?" + query, status_code=303)


@app.get("/auth/linkedin/callback")
async def linkedin_callback(request: Request, state: str = "", code: str = "", error: str = ""):
    expected = request.session.pop("linkedin_state", "")
    if not expected or not state.isascii() or not secrets.compare_digest(expected, state):
        return RedirectResponse("/?linkedin_error=state", status_code=303)
    if error or not code:
        return RedirectResponse("/?linkedin_error=cancelled", status_code=303)
    client_id = get_setting("linkedin_client_id")
    client_secret = get_setting("linkedin_client_secret")
    if not client_id or not client_secret:
        return RedirectResponse("/?linkedin_error=credentials", status_code=303)
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            token_response = await client.post("https://www.linkedin.com/oauth/v2/accessToken", data={
                "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI,
                "client_id": client_id, "client_secret": client_secret,
            })
            token_response.raise_for_status()
            token_data = token_response.json()
            token = token_data["access_token"]
            expires_in = int(token_data["expires_in"])
            if not isinstance(token, str) or not token or len(token) > 10000 or expires_in <= 0:
                raise ValueError("Invalid token response")
            profile_response = await client.get(
                "https://api.linkedin.com/v2/userinfo", headers={"Authorization": f"Bearer {token}"},
            )
            profile_response.raise_for_status()
            profile = profile_response.json()
            person_id = profile["sub"]
            if not isinstance(person_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", person_id):
                raise ValueError("Invalid member ID")
        with db(write=True) as connection:
            # Credentials may have been changed while the browser was on LinkedIn.
            settings = setting_values(connection)
            if settings.get("linkedin_client_id") != client_id or settings.get("linkedin_client_secret") != client_secret:
                return RedirectResponse("/?linkedin_error=credentials", status_code=303)
            connection.execute(
                "UPDATE posts SET status = 'needs_review', approved_author = NULL, updated_at = ?, "
                "revision = revision + 1 WHERE status = 'approved' AND approved_author != ?",
                (timestamp(now()), person_id),
            )
            set_setting(connection, "linkedin_token", token)
            set_setting(connection, "linkedin_person_id", person_id)
            set_setting(connection, "linkedin_name", str(profile.get("name") or "LinkedIn member")[:200])
            set_setting(connection, "linkedin_expires_at", timestamp(now() + timedelta(seconds=expires_in)))
    except (httpx.HTTPError, ValueError, KeyError, TypeError, OverflowError):
        return RedirectResponse("/?linkedin_error=connect", status_code=303)
    return RedirectResponse("/?linkedin_connected=1", status_code=303)


@app.get("/api/posts")
def list_posts():
    with db() as connection:
        rows = connection.execute(
            POST_QUERY + " ORDER BY CASE WHEN posts.status = 'published' THEN 1 ELSE 0 END, "
            "CASE WHEN posts.status = 'published' THEN posts.scheduled_at END DESC, posts.scheduled_at ASC"
        ).fetchall()
        return [public_post(row) for row in rows]


@app.post("/api/posts", status_code=201)
def create_post(payload: PostInput):
    topic = payload.topic.strip()
    if not topic:
        raise HTTPException(400, "Add a topic for the post.")
    scheduled = valid_time(payload.scheduled_at, payload.timezone)
    if parse_time(scheduled) <= now():
        raise HTTPException(400, "Choose a future time for the first post.")
    created = timestamp(now())
    post_id, series_id = uuid4().hex, uuid4().hex
    with db(write=True) as connection:
        settings = setting_values(connection)
        require_tested(providers.selected(settings, "text"), "text")
        require_tested(providers.selected(settings, "image"), "image")
        connection.execute(
            "INSERT INTO series(id, topic, context, repeat_unit, repeat_every, timezone, anchor_at, active) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (series_id, topic, payload.context.strip(), payload.repeat_unit, payload.repeat_every,
             payload.timezone, scheduled, int(payload.repeat_unit != "once")),
        )
        connection.execute(
            "INSERT INTO posts(id, series_id, scheduled_at, status, created_at, updated_at) "
            "VALUES(?, ?, ?, 'queued', ?, ?)", (post_id, series_id, scheduled, created, created),
        )
        return public_post(find_post(connection, post_id))


@app.get("/api/posts/{post_id}")
def post_detail(post_id: str):
    with db() as connection:
        row = require_post(connection, post_id)
        post = public_post(row)
        post["messages"] = [dict(message) for message in connection.execute(
            "SELECT id, role, content, created_at FROM messages WHERE post_id = ? ORDER BY id", (post_id,),
        )]
        return post


@app.get("/api/posts/{post_id}/image")
def post_image(post_id: str):
    with db() as connection:
        row = require_post(connection, post_id)
        filename = row["image_file"]
    if not filename or not (IMAGE_DIR / filename).is_file():
        raise HTTPException(404, "Image not found.")
    return FileResponse(IMAGE_DIR / filename, media_type="image/png")


@app.patch("/api/posts/{post_id}")
def edit_post(post_id: str, payload: PostEdit):
    if payload.content is not None and not payload.content.strip():
        raise HTTPException(400, "The post cannot be empty.")
    if (payload.scheduled_at is None) != (payload.timezone is None):
        raise HTTPException(400, "Include both the scheduled time and time zone.")
    if (payload.repeat_unit is None) != (payload.repeat_every is None):
        raise HTTPException(400, "Include both the repeat interval and unit.")
    if payload.topic is None and payload.content is None and payload.scheduled_at is None and payload.repeat_unit is None:
        raise HTTPException(400, "No changes provided.")
    scheduled = valid_time(payload.scheduled_at, payload.timezone) if payload.scheduled_at is not None else None
    with db(write=True) as connection:
        row = require_post(connection, post_id)
        editable(row, payload.revision)
        topic = payload.topic.strip() if payload.topic is not None else row["topic_override"]
        if payload.topic is not None and not topic:
            raise HTTPException(400, "The topic cannot be empty.")
        content = payload.content if payload.content is not None else row["content"]
        scheduled = scheduled if scheduled is not None else row["scheduled_at"]
        updated_at = timestamp(now())
        connection.execute(
            "UPDATE posts SET topic_override = ?, content = ?, scheduled_at = ?, updated_at = ?, revision = revision + 1 WHERE id = ?",
            (topic, content, scheduled, updated_at, post_id),
        )
        series_changed = False
        if payload.timezone is not None and (scheduled != row["scheduled_at"] or payload.timezone != row["timezone"]):
            connection.execute("UPDATE series SET anchor_at = ?, timezone = ? WHERE id = ?", (scheduled, payload.timezone, row["series_id"]))
            series_changed = True
        if payload.repeat_unit is not None:
            every = 1 if payload.repeat_unit == "once" else payload.repeat_every
            active = int(payload.repeat_unit != "once")
            if (payload.repeat_unit, every, active) != (row["repeat_unit"], row["repeat_every"], row["series_active"]):
                connection.execute(
                    "UPDATE series SET repeat_unit = ?, repeat_every = ?, active = ?, anchor_at = ? WHERE id = ?",
                    (payload.repeat_unit, every, active, scheduled, row["series_id"]),
                )
                series_changed = True
        if series_changed:
            connection.execute(
                "UPDATE posts SET updated_at = ?, revision = revision + 1 WHERE series_id = ? AND id != ? AND status != 'published'",
                (updated_at, row["series_id"], post_id),
            )
        return public_post(find_post(connection, post_id))


@app.post("/api/posts/{post_id}/image")
def edit_image(post_id: str, payload: ImageInput):
    with db(write=True) as connection:
        row = require_post(connection, post_id)
        editable(row, payload.revision)
        if row["image_file"] and not payload.instruction.strip():
            raise HTTPException(400, "Describe the image change you want.")
        config = providers.selected(setting_values(connection), "image")
        if config["provider"] == "none":
            raise HTTPException(400, "Choose and configure an image provider in Settings first.")
        require_tested(config, "image")
        connection.execute(
            "UPDATE posts SET status = 'image_queued', image_instruction = ?, image_error = NULL, "
            "updated_at = ?, revision = revision + 1 WHERE id = ?",
            (payload.instruction.strip(), timestamp(now()), post_id),
        )
        return public_post(find_post(connection, post_id))


@app.post("/api/posts/{post_id}/chat")
async def chat(post_id: str, payload: ChatInput):
    message = payload.message.strip()
    if not message:
        raise HTTPException(400, "Write a question or an editing request.")
    with db() as connection:
        row = require_post(connection, post_id)
        editable(row, payload.revision)
        history = [dict(item) for item in connection.execute(
            "SELECT role, content FROM messages WHERE post_id = ? ORDER BY id", (post_id,),
        )]
        config = providers.selected(setting_values(connection), "text")
    require_tested(config, "text")
    try:
        answer = await services.ask_ai(config, row["topic"], row["context"], row["content"], history, message)
    except Exception as exc:
        raise HTTPException(502, services.error_message(exc, config["name"])) from None
    with db(write=True) as connection:
        current = require_post(connection, post_id)
        editable(current, payload.revision)
        created = timestamp(now())
        connection.executemany(
            "INSERT INTO messages(post_id, role, content, created_at) VALUES(?, ?, ?, ?)",
            [(post_id, "user", message, created), (post_id, "assistant", answer, created)],
        )
        connection.execute("UPDATE posts SET updated_at = ?, revision = revision + 1 WHERE id = ?", (created, post_id))
    return {"answer": answer}


@app.post("/api/posts/{post_id}/approve")
def approve(post_id: str, payload: ApprovalInput):
    with db(write=True) as connection:
        row = require_post(connection, post_id)
        editable(row, payload.revision)
        settings = setting_values(connection)
        if not connected(settings):
            raise HTTPException(400, "Connect your LinkedIn account in Settings before approving.")
        if not row["content"].strip() or len(row["content"]) > 3000:
            raise HTTPException(400, "The post must contain 1–3000 characters.")
        has_image = bool(row["image_file"] and (IMAGE_DIR / row["image_file"]).is_file())
        if not has_image and not payload.publish_without_image:
            raise HTTPException(400, "Generate an image or explicitly approve a text-only post.")
        if parse_time(row["scheduled_at"]) <= now():
            raise HTTPException(400, "The scheduled time has passed. Choose a future time.")
        connection.execute(
            "UPDATE posts SET status = 'approved', approved_author = ?, image_file = ?, error = NULL, updated_at = ?, "
            "revision = revision + 1 WHERE id = ?",
            (settings["linkedin_person_id"], row["image_file"] if has_image else None, timestamp(now()), post_id),
        )
        return public_post(find_post(connection, post_id))


@app.post("/api/posts/{post_id}/unapprove")
def unapprove(post_id: str):
    with db(write=True) as connection:
        row = require_post(connection, post_id)
        if row["status"] != "approved":
            raise HTTPException(409, "This post is no longer awaiting its scheduled time.")
        connection.execute(
            "UPDATE posts SET status = 'needs_review', approved_author = NULL, updated_at = ?, "
            "revision = revision + 1 WHERE id = ?", (timestamp(now()), post_id),
        )
        return public_post(find_post(connection, post_id))


@app.post("/api/posts/{post_id}/retry")
def retry(post_id: str):
    with db(write=True) as connection:
        row = require_post(connection, post_id)
        if row["status"] == "generation_failed":
            require_tested(providers.selected(setting_values(connection), "text"), "text")
            status = "queued"
        elif row["status"] in {"publish_failed", "publish_uncertain"}:
            status = "needs_review"
        else:
            raise HTTPException(409, "This post cannot be retried right now.")
        connection.execute(
            "UPDATE posts SET status = ?, error = NULL, approved_author = NULL, updated_at = ?, "
            "revision = revision + 1 WHERE id = ?", (status, timestamp(now()), post_id),
        )
        return public_post(find_post(connection, post_id))


@app.post("/api/posts/{post_id}/stop-repeat")
def stop_repeat(post_id: str):
    with db(write=True) as connection:
        row = require_post(connection, post_id)
        if row["series_active"]:
            connection.execute("UPDATE series SET active = 0 WHERE id = ?", (row["series_id"],))
            connection.execute(
                "UPDATE posts SET updated_at = ?, revision = revision + 1 WHERE series_id = ? AND (id = ? OR status != 'published')",
                (timestamp(now()), row["series_id"], post_id),
            )
        return public_post(find_post(connection, post_id))


@app.delete("/api/posts/{post_id}")
def delete_post(post_id: str):
    with db(write=True) as connection:
        row = require_post(connection, post_id)
        if row["status"] in {"generating", "image_generating", "publishing", "published"}:
            raise HTTPException(409, "This post cannot be deleted while processing or after publishing.")
        filename = row["image_file"]
        connection.execute("UPDATE series SET active = 0 WHERE id = ?", (row["series_id"],))
        connection.execute("DELETE FROM posts WHERE id = ?", (post_id,))
    if filename:
        (IMAGE_DIR / filename).unlink(missing_ok=True)
    return {"ok": True}
