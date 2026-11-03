import asyncio
import base64
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main, providers, services, storage, worker

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+X2ZkAAAAASUVORK5CYII="
)


@pytest.fixture
def client(tmp_path, monkeypatch):
    data = tmp_path / "data"
    images = data / "images"
    monkeypatch.setattr(storage, "DATA_DIR", data)
    monkeypatch.setattr(storage, "DB_PATH", data / "studio.sqlite3")
    monkeypatch.setattr(storage, "IMAGE_DIR", images)
    monkeypatch.setattr(services, "IMAGE_DIR", images)
    monkeypatch.setattr(main, "IMAGE_DIR", images)
    monkeypatch.setattr(worker, "IMAGE_DIR", images)
    storage.initialize()
    client = TestClient(main.app, base_url="http://localhost:8000", headers={
        "Origin": "http://localhost:8000", "X-Post-Studio": "1",
    })
    yield client
    client.close()


def mark_tested(kind):
    with storage.db(write=True) as connection:
        settings = {row["key"]: row["value"] for row in connection.execute("SELECT key, value FROM settings")}
        config = providers.selected(settings, kind)
        if config["provider"] != "none":
            storage.set_setting(connection, providers.test_key_name(config["provider"], kind),
                                providers.test_signature(settings, kind, config))


def setup(client, linkedin=True):
    response = client.post("/api/config", json={
        "openai_api_key": "private-openai-key", "linkedin_client_id": "client-id",
        "linkedin_client_secret": "private-client-secret",
    })
    assert response.status_code == 200
    mark_tested("text")
    mark_tested("image")
    if linkedin:
        with storage.db(write=True) as connection:
            for key, value in {
                "linkedin_token": "private-linkedin-token", "linkedin_person_id": "person-1",
                "linkedin_name": "Test Member",
                "linkedin_expires_at": storage.timestamp(storage.now() + timedelta(days=30)),
            }.items():
                storage.set_setting(connection, key, value)


def create(client, unit="once", scheduled=None):
    scheduled = scheduled or storage.now() + timedelta(hours=1)
    result = client.post("/api/posts", json={
        "topic": "A meaningful topic", "context": "A few details", "scheduled_at": storage.timestamp(scheduled),
        "timezone": "Asia/Kolkata", "repeat_unit": unit, "repeat_every": 1,
    })
    assert result.status_code == 201, result.text
    return result.json()


def generate(post_id, monkeypatch):
    async def text(*args):
        return "A draft worth reviewing."

    async def image(*args):
        return services.save_image(base64.b64encode(PNG).decode())

    monkeypatch.setattr(services, "generate_text", text)
    monkeypatch.setattr(services, "generate_image", image)
    assert worker.claim("queued", "generating") == post_id
    asyncio.run(worker.make_draft(post_id))


def detail(client, post_id):
    response = client.get(f"/api/posts/{post_id}")
    assert response.status_code == 200
    return response.json()


def approve(client, post):
    return client.post(f"/api/posts/{post['id']}/approve", json={"revision": post["revision"]})


def test_review_is_required_and_each_recurring_post_is_a_new_draft(client, monkeypatch):
    setup(client)
    scheduled = storage.now() + timedelta(hours=1)
    first = create(client, "days", scheduled)
    assert approve(client, first).status_code == 409
    generate(first["id"], monkeypatch)
    first = detail(client, first["id"])
    assert first["status"] == "needs_review"
    assert client.get(first["image_url"]).content == PNG
    assert worker.claim("approved", "publishing", due=True) is None
    assert approve(client, first).status_code == 200

    edit = client.patch(f"/api/posts/{first['id']}", json={
        "content": "An unapproved change", "scheduled_at": first["scheduled_at"],
        "timezone": "Asia/Kolkata", "revision": first["revision"],
    })
    assert edit.status_code == 409

    sent = []
    async def publish(token, person, content, image):
        sent.append((person, content, image))
        return "urn:li:share:123"
    monkeypatch.setattr(services, "publish_to_linkedin", publish)
    monkeypatch.setattr(worker, "now", lambda: scheduled + timedelta(seconds=1))
    assert worker.claim("approved", "publishing", due=True) == first["id"]
    asyncio.run(worker.publish(first["id"]))
    assert worker.claim("approved", "publishing", due=True) is None
    assert sent == [("person-1", "A draft worth reviewing.", PNG)]
    posts = client.get("/api/posts").json()
    assert len(posts) == 2
    next_post = next(post for post in posts if post["id"] != first["id"])
    assert next_post["status"] == "queued"
    assert storage.parse_time(next_post["scheduled_at"]) == scheduled + timedelta(days=1)
    assert detail(client, first["id"])["status"] == "published"


def test_unapproved_overdue_draft_and_missing_image_are_never_published(client, monkeypatch):
    setup(client)
    post = create(client)
    async def text(*args): return "The text survived."
    async def image(*args): raise RuntimeError("do not leak this error")
    monkeypatch.setattr(services, "generate_text", text)
    monkeypatch.setattr(services, "generate_image", image)
    assert worker.claim("queued", "generating") == post["id"]
    asyncio.run(worker.make_draft(post["id"]))
    post = detail(client, post["id"])
    assert post["status"] == "needs_review" and post["content"] == "The text survived."
    assert "do not leak" not in post["image_error"]
    assert approve(client, post).status_code == 400
    with storage.db(write=True) as connection:
        connection.execute("UPDATE posts SET scheduled_at = ? WHERE id = ?", (storage.timestamp(storage.now() - timedelta(days=1)), post["id"]))
    assert worker.claim("approved", "publishing", due=True) is None


def test_chat_suggests_changes_without_applying_them(client, monkeypatch):
    setup(client)
    post = create(client)
    generate(post["id"], monkeypatch)
    post = detail(client, post["id"])
    async def answer(key, topic, context, content, history, question):
        assert content == "A draft worth reviewing."
        assert question == "Make it shorter"
        return "A shorter draft."
    monkeypatch.setattr(services, "ask_ai", answer)
    response = client.post(f"/api/posts/{post['id']}/chat", json={"revision": post["revision"], "message": "Make it shorter"})
    assert response.json()["answer"] == "A shorter draft."
    after = detail(client, post["id"])
    assert after["content"] == post["content"]
    assert [message["role"] for message in after["messages"]] == ["user", "assistant"]
    stale = client.patch(f"/api/posts/{post['id']}", json={"content": "stale", "scheduled_at": post["scheduled_at"], "timezone": "Asia/Kolkata", "revision": post["revision"]})
    assert stale.status_code == 409
    saved = client.patch(f"/api/posts/{post['id']}", json={"content": "A shorter draft.", "scheduled_at": post["scheduled_at"], "timezone": "Asia/Kolkata", "revision": after["revision"]})
    assert saved.json()["content"] == "A shorter draft."


def test_account_change_and_interrupted_publish_do_not_retry_automatically(client, monkeypatch):
    setup(client)
    post = create(client)
    generate(post["id"], monkeypatch)
    post = detail(client, post["id"])
    assert approve(client, post).status_code == 200
    with storage.db(write=True) as connection:
        storage.set_setting(connection, "linkedin_person_id", "another-person")
    monkeypatch.setattr(worker, "now", lambda: storage.now() + timedelta(hours=2))
    assert worker.claim("approved", "publishing", due=True) == post["id"]
    async def should_not_publish(*args): raise AssertionError("wrong account")
    monkeypatch.setattr(services, "publish_to_linkedin", should_not_publish)
    asyncio.run(worker.publish(post["id"]))
    failed = detail(client, post["id"])
    assert failed["status"] == "publish_failed"
    assert "account changed" in failed["error"]
    assert worker.claim("approved", "publishing", due=True) is None

    with storage.db(write=True) as connection:
        connection.execute("UPDATE posts SET status = 'publishing' WHERE id = ?", (post["id"],))
    storage.recover_interrupted()
    assert detail(client, post["id"])["status"] == "publish_uncertain"
    assert worker.claim("approved", "publishing", due=True) is None


def test_secrets_stay_local_and_cross_origin_requests_are_rejected(client):
    setup(client)
    response = client.get("/api/config")
    for secret in ("private-openai-key", "private-client-secret", "private-linkedin-token"):
        assert secret not in response.text
    assert client.post("/api/config", headers={"Origin": "https://evil.example"}, json={"openai_api_key": "bad"}).status_code == 403
    assert client.get("/api/config", headers={"Host": "evil.example"}).status_code == 400
    assert storage.DB_PATH.stat().st_mode & 0o777 == 0o600
    assert storage.DATA_DIR.stat().st_mode & 0o777 == 0o700


def test_only_one_scheduler_can_use_a_data_directory(client):
    with storage.instance_lock():
        with pytest.raises(RuntimeError, match="already running"):
            with storage.instance_lock():
                pass


def test_oauth_state_and_connection_invalidates_another_account_approval(client, monkeypatch):
    setup(client)
    post = create(client)
    generate(post["id"], monkeypatch)
    post = detail(client, post["id"])
    assert approve(client, post).status_code == 200
    start = client.get("/api/linkedin/connect", follow_redirects=False)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    bad = client.get("/auth/linkedin/callback", params={"state": "wrong", "code": "bad"}, follow_redirects=False)
    assert "linkedin_error=state" in bad.headers["location"]
    start = client.get("/api/linkedin/connect", follow_redirects=False)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    original_client = httpx.AsyncClient
    def handler(request):
        if request.url.path.endswith("accessToken"):
            return httpx.Response(200, json={"access_token": "new-token", "expires_in": 3600})
        assert request.url.path == "/v2/userinfo"
        return httpx.Response(200, json={"sub": "person-2", "name": "Another Member"})
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs))
    response = client.get("/auth/linkedin/callback", params={"state": state, "code": "the-code"}, follow_redirects=False)
    assert "linkedin_connected=1" in response.headers["location"]
    assert detail(client, post["id"])["status"] == "needs_review"
    assert client.get("/api/config").json()["linkedin_name"] == "Another Member"


def test_linkedin_image_upload_and_post_request_are_correct(monkeypatch):
    requests = []
    original_client = httpx.AsyncClient
    def handler(request):
        requests.append(request)
        if "registerUpload" in str(request.url):
            body = json.loads(request.content)
            assert body["registerUploadRequest"]["supportedUploadMechanism"] == ["SYNCHRONOUS_UPLOAD"]
            return httpx.Response(200, json={"value": {"asset": "urn:li:digitalmediaAsset:img", "uploadMechanism": {
                "com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest": {"uploadUrl": "https://api.linkedin.com/mediaUpload/abc"}
            }}})
        if request.url.path.startswith("/mediaUpload"):
            assert request.method == "PUT" and request.content == PNG
            return httpx.Response(201)
        assert request.url.path == "/v2/ugcPosts"
        body = json.loads(request.content)
        assert body["author"] == "urn:li:person:person-1"
        assert body["specificContent"]["com.linkedin.ugc.ShareContent"]["media"][0]["media"] == "urn:li:digitalmediaAsset:img"
        assert body["specificContent"]["com.linkedin.ugc.ShareContent"]["shareCommentary"]["text"] == "Approved text"
        return httpx.Response(201, headers={"x-restli-id": "urn:li:share:123"})
    monkeypatch.setattr(services.httpx, "AsyncClient", lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs))
    assert asyncio.run(services.publish_to_linkedin("token", "person-1", "Approved text", PNG)) == "urn:li:share:123"
    assert len(requests) == 3


def test_linkedin_will_not_send_token_to_unexpected_upload_host(monkeypatch):
    original_client = httpx.AsyncClient
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"value": {"asset": "img", "uploadMechanism": {
            "com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest": {"uploadUrl": "https://evil.example/upload"}
        }}})
    monkeypatch.setattr(services.httpx, "AsyncClient", lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs))
    with pytest.raises(ValueError, match="unexpected upload URL"):
        asyncio.run(services.publish_to_linkedin("token", "person-1", "Approved text", PNG))
    assert len(calls) == 1


def test_recurring_schedule_keeps_local_time_across_dst_and_skips_missed_slots():
    utc = timezone.utc
    anchor = "2026-03-07T14:00:00.000000Z"  # 9am in New York before daylight saving
    after = datetime(2026, 3, 7, 14, 1, tzinfo=utc)
    assert storage.next_occurrence(anchor, "days", 1, "America/New_York", after) == datetime(2026, 3, 8, 13, 0, tzinfo=utc)
    after = datetime(2026, 3, 10, 15, 0, tzinfo=utc)
    assert storage.next_occurrence(anchor, "days", 1, "America/New_York", after) == datetime(2026, 3, 11, 13, 0, tzinfo=utc)
    assert storage.next_occurrence(anchor, "hours", 3, "America/New_York", after) > after


def test_openai_sdk_writes_and_edits_images_using_the_configured_models(client, monkeypatch):
    requests = []
    real_openai = services.AsyncOpenAI

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/responses"):
            body = json.loads(request.content)
            assert body["model"] == services.TEXT_MODEL
            assert "A useful topic" in body["input"][0]["content"]
            return httpx.Response(200, json={
                "id": "resp_test", "object": "response", "created_at": 1, "model": services.TEXT_MODEL,
                "output": [{"id": "msg_test", "type": "message", "status": "completed", "role": "assistant",
                            "content": [{"type": "output_text", "text": "A useful draft.", "annotations": []}]}],
            })
        if request.url.path.endswith("/images/generations"):
            assert json.loads(request.content)["model"] == services.IMAGE_MODEL
        else:
            assert request.url.path.endswith("/images/edits")
            assert "multipart/form-data" in request.headers["content-type"]
            assert PNG in request.content
        return httpx.Response(200, json={"created": 1, "data": [{"b64_json": base64.b64encode(PNG).decode()}]})

    def fake_openai(**kwargs):
        kwargs["http_client"] = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return real_openai(**kwargs)

    monkeypatch.setattr(services, "AsyncOpenAI", fake_openai)
    text_config = providers.selected({"openai_api_key": "test"}, "text")
    image_config = providers.selected({"openai_api_key": "test"}, "image")
    assert asyncio.run(services.generate_text(text_config, "A useful topic", "Some context")) == "A useful draft."
    image = asyncio.run(services.generate_image(image_config, "A useful topic", "Some context", "A useful draft."))
    assert services.read_image(image) == PNG
    edited = asyncio.run(services.generate_image(image_config, "A useful topic", "Some context", "A useful draft.", "Use blue", PNG))
    assert services.read_image(edited) == PNG
    assert len(requests) == 3


def test_failed_image_edit_keeps_the_previous_reviewed_image(client, monkeypatch):
    setup(client)
    post = create(client)
    generate(post["id"], monkeypatch)
    post = detail(client, post["id"])
    old_image = client.get(post["image_url"]).content
    result = client.post(f"/api/posts/{post['id']}/image", json={"revision": post["revision"], "instruction": "Make it blue"})
    assert result.json()["status"] == "image_queued"
    async def fail(*args):
        raise RuntimeError("failed")
    monkeypatch.setattr(services, "generate_image", fail)
    assert worker.claim("image_queued", "image_generating") == post["id"]
    asyncio.run(worker.make_draft(post["id"], image_only=True))
    after = detail(client, post["id"])
    assert after["status"] == "needs_review"
    assert after["image_error"]
    assert client.get(after["image_url"]).content == old_image


def test_slow_image_generation_does_not_delay_a_due_post(client, monkeypatch):
    setup(client)
    scheduled = storage.now() + timedelta(hours=1)
    first = create(client, scheduled=scheduled)
    generate(first["id"], monkeypatch)
    assert approve(client, detail(client, first["id"])).status_code == 200
    second = create(client, scheduled=scheduled + timedelta(hours=1))
    monkeypatch.setattr(worker, "now", lambda: scheduled + timedelta(seconds=1))

    async def exercise():
        published = asyncio.Event()
        never_finished = asyncio.Event()

        async def slow_text(*args):
            await never_finished.wait()
            return "Late draft"

        async def fast_publish(*args):
            published.set()
            return "urn:li:share:456"

        monkeypatch.setattr(services, "generate_text", slow_text)
        monkeypatch.setattr(services, "publish_to_linkedin", fast_publish)
        task = asyncio.create_task(worker.scheduler())
        try:
            await asyncio.wait_for(published.wait(), timeout=1)
            await asyncio.sleep(0)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(exercise())
    assert detail(client, first["id"])["status"] == "published"
    assert detail(client, second["id"])["status"] == "generating"
    storage.recover_interrupted()
    assert detail(client, second["id"])["status"] == "queued"


def configure_provider(client, kind, provider, model="", key=None, base_url=""):
    return client.post("/api/ai-config", json={
        "kind": kind, "provider": provider, "model": model, "api_key": key, "base_url": base_url,
    })


def test_provider_choices_keep_keys_separate_and_reuse_the_same_image_provider(client):
    setup(client)
    response = configure_provider(client, "text", "gemini", "gemini-test", "private-gemini-key")
    assert response.status_code == 200
    ai = response.json()["ai"]
    assert ai["text"]["provider"] == "gemini" and ai["text"]["model"] == "gemini-test"
    assert ai["image"]["selection"] == "same" and ai["image"]["provider"] == "gemini"
    assert ai["text"]["ready"] and ai["image"]["ready"]
    assert ai["keys"]["text"]["openai"] and ai["keys"]["image"]["gemini"]
    for secret in ("private-openai-key", "private-gemini-key", "private-linkedin-token"):
        assert secret not in response.text
    response = configure_provider(client, "text", "anthropic", "claude-test", "private-claude-key")
    assert response.json()["ai"]["image"]["provider"] == "none"
    assert storage.get_setting("openai_api_key") == "private-openai-key"
    assert storage.get_setting("ai_gemini_api_key") == "private-gemini-key"
    assert storage.get_setting("ai_anthropic_api_key") == "private-claude-key"
    response = configure_provider(client, "text", "huggingface", "owner/text-model", "hf-test-key")
    assert response.json()["ai"]["image"]["provider"] == "huggingface"
    assert response.json()["ai"]["image"]["ready"]


def test_custom_endpoint_requires_secure_url_and_does_not_send_old_key_to_new_host(client):
    assert configure_provider(client, "text", "custom", "model", "secret", "http://remote.example/v1").status_code == 400
    assert configure_provider(client, "text", "custom", "model", "secret", "https://user:pass@remote.example/v1").status_code == 400
    assert configure_provider(client, "text", "custom", "model", "secret", "https://first.example/v1").status_code == 200
    changed = configure_provider(client, "text", "custom", "model", None, "https://second.example/v1")
    assert changed.status_code == 400 and "Re-enter" in changed.json()["detail"]
    ai = client.get("/api/config").json()["ai"]
    assert ai["text"]["base_url"] == "https://first.example/v1"
    assert configure_provider(client, "text", "custom", "model", "new-secret", "https://second.example/v1").status_code == 200
    assert configure_provider(client, "text", "custom", "model", "local", "http://127.0.0.1:1234/v1").status_code == 200
    assert configure_provider(client, "image", "huggingface", "https://evil.example/steal", "hf-secret").status_code == 400


def test_text_only_requires_explicit_approval_and_skips_linkedin_image_upload(client, monkeypatch):
    setup(client)
    assert configure_provider(client, "text", "groq", "llama-model", "groq-key").status_code == 200
    mark_tested("text")
    assert client.get("/api/config").json()["ai"]["image"]["provider"] == "none"
    post = create(client)

    async def fake_text(config, *args):
        assert config["provider"] == "groq" and config["api_key"] == "groq-key"
        return "A text-only draft."

    async def no_image(*args):
        raise AssertionError("No image provider should be called")

    monkeypatch.setattr(services, "generate_text", fake_text)
    monkeypatch.setattr(services, "generate_image", no_image)
    assert worker.claim("queued", "generating") == post["id"]
    asyncio.run(worker.make_draft(post["id"]))
    post = detail(client, post["id"])
    assert post["status"] == "needs_review" and post["image_url"] is None
    assert approve(client, post).status_code == 400
    result = client.post(f"/api/posts/{post['id']}/approve", json={"revision": post["revision"], "publish_without_image": True})
    assert result.status_code == 200

    requests = []
    original_client = httpx.AsyncClient
    def handler(request):
        requests.append(request)
        assert request.url.path == "/v2/ugcPosts"
        share = json.loads(request.content)["specificContent"]["com.linkedin.ugc.ShareContent"]
        assert share["shareMediaCategory"] == "NONE" and "media" not in share
        return httpx.Response(201, headers={"x-restli-id": "urn:li:share:789"})
    monkeypatch.setattr(services.httpx, "AsyncClient", lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr(worker, "now", lambda: storage.now() + timedelta(hours=2))
    assert worker.claim("approved", "publishing", due=True) == post["id"]
    asyncio.run(worker.publish(post["id"]))
    assert detail(client, post["id"])["status"] == "published"
    assert len(requests) == 1


def test_anthropic_and_openai_compatible_text_use_the_chosen_key_and_model(monkeypatch):
    calls = []
    original_client = httpx.AsyncClient
    def handler(request):
        calls.append(request)
        payload = json.loads(request.content)
        if request.url.host == "api.anthropic.com":
            assert request.headers["x-api-key"] == "claude-key"
            assert request.headers["anthropic-version"] == "2023-06-01"
            assert payload["model"] == "claude-test" and payload["messages"][-1]["content"] == "Make it shorter"
            return httpx.Response(200, json={"content": [{"type": "text", "text": "Claude reply"}]})
        assert request.url.host == "api.groq.com"
        assert request.headers["authorization"] == "Bearer groq-key"
        assert payload["model"] == "groq-model" and payload["messages"][0]["role"] == "system"
        return httpx.Response(200, json={"id": "test", "object": "chat.completion", "created": 1, "model": "groq-model", "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "Groq draft"}}]})
    class MockClient(original_client):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(services.httpx, "AsyncClient", MockClient)
    groq = providers.selected({"ai_text_provider": "groq", "ai_groq_api_key": "groq-key", "ai_text_model_groq": "groq-model"}, "text")
    claude = providers.selected({"ai_text_provider": "anthropic", "ai_anthropic_api_key": "claude-key", "ai_text_model_anthropic": "claude-test"}, "text")
    assert asyncio.run(services.generate_text(groq, "topic", "context")) == "Groq draft"
    assert asyncio.run(services.ask_ai(claude, "topic", "context", "draft", [], "Make it shorter")) == "Claude reply"
    assert len(calls) == 2


@pytest.mark.parametrize("provider", ["gemini", "openrouter", "xai"])
def test_image_provider_generation_and_editing(provider, client, monkeypatch):
    import io
    from PIL import Image

    image_bytes = io.BytesIO()
    Image.new("RGB", (2, 2), "orange").save(image_bytes, format="WEBP" if provider == "openrouter" else "JPEG")
    encoded = base64.b64encode(image_bytes.getvalue()).decode()
    calls = []
    original_client = httpx.AsyncClient
    def handler(request):
        calls.append(request)
        body = json.loads(request.content)
        if provider == "gemini":
            assert request.url.host == "generativelanguage.googleapis.com"
            assert request.headers["x-goog-api-key"] == "image-key"
            if len(calls) == 2:
                assert body["contents"][0]["parts"][1]["inlineData"]["data"] == base64.b64encode(PNG).decode()
            return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": "image/jpeg", "data": encoded}}]}}]})
        assert request.headers["authorization"] == "Bearer image-key"
        if provider == "xai":
            assert request.url.host == "api.x.ai" and body["response_format"] == "b64_json"
            assert request.url.path.endswith("/edits" if len(calls) == 2 else "/generations")
            if len(calls) == 2:
                assert body["image"]["url"].startswith("data:image/png;base64,")
        else:
            assert request.url.host == "openrouter.ai" and request.url.path == "/api/v1/images"
            if len(calls) == 2:
                assert body["input_references"][0]["image_url"]["url"].startswith("data:image/png;base64,")
        return httpx.Response(200, json={"data": [{"b64_json": encoded}]})
    class MockClient(original_client):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(services.httpx, "AsyncClient", MockClient)
    settings = {"ai_image_provider": provider, providers.key_name(provider, "image"): "image-key"}
    config = providers.selected(settings, "image")
    first = asyncio.run(services.generate_image(config, "topic", "context", "post"))
    second = asyncio.run(services.generate_image(config, "topic", "context", "post", "make it blue", PNG))
    for filename in (first, second):
        with Image.open(io.BytesIO(services.read_image(filename))) as converted:
            assert converted.format == "PNG" and converted.size == (2, 2)
    assert len(calls) == 2


@pytest.mark.parametrize("provider", ["azure", "custom"])
def test_openai_compatible_image_providers_use_their_endpoint_and_format(provider, client, monkeypatch):
    calls = []
    original_client = httpx.AsyncClient
    def handler(request):
        calls.append(request)
        assert request.url.host == "images.example"
        assert request.url.path == "/v1/images/generations"
        assert request.headers["authorization"] == "Bearer provider-key"
        body = json.loads(request.content)
        assert body["model"] == "image-deployment"
        if provider == "azure":
            assert "response_format" not in body
        else:
            assert body["response_format"] == "b64_json"
        return httpx.Response(200, json={"created": 1, "data": [{"b64_json": base64.b64encode(PNG).decode()}]})
    class MockClient(original_client):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(services.httpx, "AsyncClient", MockClient)
    settings = {
        "ai_image_provider": provider,
        providers.key_name(provider, "image"): "provider-key",
        providers.model_name(provider, "image"): "image-deployment",
        providers.url_name(provider, "image"): "https://images.example/v1",
    }
    filename = asyncio.run(services.generate_image(providers.selected(settings, "image"), "topic", "", "post"))
    assert services.read_image(filename) == PNG and len(calls) == 1


@pytest.mark.parametrize("provider", [
    "gemini", "xai", "groq", "deepseek", "mistral", "openrouter", "together",
    "huggingface", "ollama", "azure", "qwen", "custom",
])
def test_writing_presets_use_their_own_endpoint_key_and_model(provider, monkeypatch):
    calls = []
    original_client = httpx.AsyncClient
    def handler(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer chosen-key"
        payload = json.loads(request.content)
        assert payload["model"] == "chosen-model"
        assert payload["messages"][0]["role"] == "system"
        return httpx.Response(200, json={"id": "test", "object": "chat.completion", "created": 1, "model": "chosen-model", "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "Selected provider draft"}}]})
    class MockClient(original_client):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(services.httpx, "AsyncClient", MockClient)
    settings = {
        "ai_text_provider": provider,
        providers.key_name(provider, "text"): "chosen-key",
        providers.model_name(provider, "text"): "chosen-model",
    }
    if providers.PROVIDERS[provider].get("needs_url"):
        settings[providers.url_name(provider, "text")] = "https://custom.example/v1"
    config = providers.selected(settings, "text")
    assert config["ready"]
    assert asyncio.run(services.generate_text(config, "topic", "context")) == "Selected provider draft"
    assert str(calls[0].url) == f"{config['base_url']}/chat/completions"


def test_huggingface_image_generation_and_edit_use_the_chosen_model(client, monkeypatch):
    import io
    from PIL import Image

    calls = []
    class FakeInferenceClient:
        def __init__(self, **kwargs):
            assert kwargs["api_key"] == "hf-test-key" and kwargs["provider"] == "auto"
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return None
        async def text_to_image(self, prompt, model):
            calls.append(("generate", model, prompt))
            return Image.new("RGB", (2, 2), "blue")
        async def image_to_image(self, image, prompt, model):
            assert image == PNG
            calls.append(("edit", model, prompt))
            return Image.new("RGB", (2, 2), "green")
    monkeypatch.setattr(services, "AsyncInferenceClient", FakeInferenceClient)
    settings = {"ai_image_provider": "huggingface", "ai_huggingface_api_key": "hf-test-key", "ai_image_model_huggingface": "owner/test-image-model"}
    config = providers.selected(settings, "image")
    generated = asyncio.run(services.generate_image(config, "topic", "context", "post"))
    edited = asyncio.run(services.generate_image(config, "topic", "context", "post", "change it", PNG))
    for filename in (generated, edited):
        with Image.open(io.BytesIO(services.read_image(filename))) as image:
            assert image.format == "PNG" and image.size == (2, 2)
    assert [call[:2] for call in calls] == [("generate", "owner/test-image-model"), ("edit", "owner/test-image-model")]


def test_perplexity_uses_responses_and_handles_non_message_results(monkeypatch):
    original_client = httpx.AsyncClient
    calls = []
    def handler(request):
        calls.append(request)
        assert request.url == "https://api.perplexity.ai/v1/responses"
        assert request.headers["authorization"] == "Bearer pplx-key"
        assert json.loads(request.content)["model"] == "openai/gpt-5.6-luna"
        return httpx.Response(200, json={
            "id": "resp_test", "object": "response", "created_at": 1, "model": "openai/gpt-5.6-luna",
            "output": [
                {"type": "search_results", "queries": ["topic"], "results": []},
                {"id": "msg_test", "type": "message", "status": "completed", "role": "assistant",
                 "content": [{"type": "output_text", "text": "Perplexity draft", "annotations": []}]},
            ],
        })
    class MockClient(original_client):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(services.httpx, "AsyncClient", MockClient)
    config = providers.selected({"ai_text_provider": "perplexity", "ai_perplexity_api_key": "pplx-key"}, "text")
    assert asyncio.run(services.generate_text(config, "topic", "context")) == "Perplexity draft"
    assert len(calls) == 1


def test_connections_must_pass_before_creating_a_draft_and_sample_is_deleted(client, monkeypatch):
    response = client.post("/api/config", json={"openai_api_key": "private-test-key"})
    assert response.status_code == 200
    schedule = storage.timestamp(storage.now() + timedelta(hours=1))
    payload = {"topic": "Test", "scheduled_at": schedule, "timezone": "UTC"}
    assert client.post("/api/posts", json=payload).status_code == 400

    async def text_probe(config, instructions, messages):
        assert config["api_key"] == "private-test-key" and messages[-1]["content"] == "Reply OK."
        return "OK"
    monkeypatch.setattr(services, "text_response", text_probe)
    text_result = client.post("/api/ai-config/text/test")
    assert text_result.status_code == 200 and text_result.json()["ai"]["text"]["tested"]
    assert "private-test-key" not in text_result.text
    assert client.post("/api/posts", json=payload).status_code == 400

    retained = services.save_image(base64.b64encode(PNG).decode())
    sample = []
    async def image_probe(config, *args):
        assert config["api_key"] == "private-test-key"
        sample.append(services.save_image(base64.b64encode(PNG).decode()))
        return sample[-1]
    monkeypatch.setattr(services, "generate_image", image_probe)
    image_result = client.post("/api/ai-config/image/test")
    assert image_result.status_code == 200 and image_result.json()["ai"]["image"]["tested"]
    assert not (storage.IMAGE_DIR / sample[0]).exists()
    assert (storage.IMAGE_DIR / retained).exists()
    assert client.post("/api/posts", json=payload).status_code == 201

    assert configure_provider(client, "text", "openai", "other-model").status_code == 200
    changed = client.get("/api/config").json()["ai"]
    assert not changed["text"]["tested"] and changed["image"]["tested"]
    assert client.post("/api/posts", json=payload).status_code == 400
    assert configure_provider(client, "text", "openai", "other-model", "new-private-key").status_code == 200
    changed = client.get("/api/config").json()["ai"]
    assert not changed["text"]["tested"] and not changed["image"]["tested"]


def test_failed_connection_test_revokes_previous_result_without_leaking_keys(client, monkeypatch):
    setup(client)
    async def failure(config, *args):
        request = httpx.Request("POST", "https://api.openai.com/v1/responses")
        response = httpx.Response(401, request=request, text="private-openai-key")
        raise httpx.HTTPStatusError("private-openai-key", request=request, response=response)
    monkeypatch.setattr(services, "text_response", failure)
    response = client.post("/api/ai-config/text/test")
    assert response.status_code == 502 and "rejected the API key" in response.json()["detail"]
    assert "private-openai-key" not in response.text
    assert not client.get("/api/config").json()["ai"]["text"]["tested"]
    assert client.get("/api/config").json()["ai"]["image"]["tested"]


def test_settings_changed_during_connection_test_do_not_get_marked_tested(client, monkeypatch):
    setup(client)
    async def changed_during_test(config, *args):
        with storage.db(write=True) as connection:
            storage.set_setting(connection, providers.model_name("openai", "text"), "new-model")
        return "OK"
    monkeypatch.setattr(services, "text_response", changed_during_test)
    response = client.post("/api/ai-config/text/test")
    assert response.status_code == 409
    assert not client.get("/api/config").json()["ai"]["text"]["tested"]


def test_approved_posts_still_publish_when_ai_settings_need_retesting(client, monkeypatch):
    setup(client)
    post = create(client)
    generate(post["id"], monkeypatch)
    assert approve(client, detail(client, post["id"])).status_code == 200
    assert configure_provider(client, "text", "openai", "changed-model").status_code == 200
    assert not client.get("/api/config").json()["ai"]["text"]["tested"]
    async def send(*args): return "urn:li:share:123"
    monkeypatch.setattr(services, "publish_to_linkedin", send)
    monkeypatch.setattr(worker, "now", lambda: storage.now() + timedelta(hours=2))
    assert worker.claim("approved", "publishing", due=True) == post["id"]
    asyncio.run(worker.publish(post["id"]))
    assert detail(client, post["id"])["status"] == "published"


def test_repeated_clicks_do_not_start_duplicate_paid_tests(client, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    setup(client)
    started, release = Event(), Event()
    calls = []
    async def slow_test(*args):
        calls.append(1)
        started.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        return "OK"
    monkeypatch.setattr(services, "text_response", slow_test)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(client.post, "/api/ai-config/text/test")
        try:
            assert started.wait(timeout=2)
            assert client.post("/api/ai-config/text/test").status_code == 409
        finally:
            release.set()
        assert first.result(timeout=2).status_code == 200
    assert calls == [1]


def test_editing_a_review_topic_preserves_history_and_future_drafts(client, monkeypatch):
    setup(client)
    scheduled = storage.now() + timedelta(hours=1)
    first = create(client, "days", scheduled)
    generate(first["id"], monkeypatch)
    assert approve(client, detail(client, first["id"])).status_code == 200

    async def publish(*args):
        return "urn:li:share:123"

    monkeypatch.setattr(services, "publish_to_linkedin", publish)
    monkeypatch.setattr(worker, "now", lambda: scheduled + timedelta(seconds=1))
    assert worker.claim("approved", "publishing", due=True) == first["id"]
    asyncio.run(worker.publish(first["id"]))
    second = next(post for post in client.get("/api/posts").json() if post["id"] != first["id"])
    generate(second["id"], monkeypatch)
    with storage.db(write=True) as connection:
        connection.execute("UPDATE posts SET content = ? WHERE id = ?", ("A draft worth reviewing.  \n", second["id"]))
    second = detail(client, second["id"])
    edited = client.patch(f"/api/posts/{second['id']}", json={
        "topic": "The next topic", "content": second["content"], "scheduled_at": second["scheduled_at"],
        "timezone": "Asia/Kolkata", "revision": second["revision"],
    })
    assert edited.status_code == 200
    assert edited.json()["topic"] == "The next topic"
    assert edited.json()["content"] == second["content"]
    assert detail(client, first["id"])["topic"] == "A meaningful topic"
    invalid = client.patch(f"/api/posts/{second['id']}", json={
        "topic": "   ", "content": second["content"], "scheduled_at": second["scheduled_at"],
        "timezone": "Asia/Kolkata", "revision": edited.json()["revision"],
    })
    assert invalid.status_code == 400
    assert detail(client, second["id"])["topic"] == "The next topic"
    assert approve(client, edited.json()).status_code == 200
    locked = client.patch(f"/api/posts/{second['id']}", json={
        "topic": "A hidden change", "content": second["content"], "scheduled_at": second["scheduled_at"],
        "timezone": "Asia/Kolkata", "revision": edited.json()["revision"],
    })
    assert locked.status_code == 409
    monkeypatch.setattr(worker, "now", lambda: storage.parse_time(second["scheduled_at"]) + timedelta(seconds=1))
    assert worker.claim("approved", "publishing", due=True) == second["id"]
    asyncio.run(worker.publish(second["id"]))
    newest = next(post for post in client.get("/api/posts").json() if post["status"] == "queued")
    assert newest["topic"] == "A meaningful topic"


def test_existing_database_keeps_drafts_and_credentials_during_topic_migration(tmp_path, monkeypatch):
    import sqlite3

    data = tmp_path / "legacy"
    data.mkdir(mode=0o700)
    database = data / "studio.sqlite3"
    database.touch(mode=0o600)
    with sqlite3.connect(database) as connection:
        connection.executescript("""
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE series (id TEXT PRIMARY KEY, topic TEXT NOT NULL, context TEXT NOT NULL,
                repeat_unit TEXT NOT NULL, repeat_every INTEGER NOT NULL, timezone TEXT NOT NULL,
                anchor_at TEXT NOT NULL, active INTEGER NOT NULL);
            CREATE TABLE posts (id TEXT PRIMARY KEY, series_id TEXT NOT NULL REFERENCES series(id),
                scheduled_at TEXT NOT NULL, status TEXT NOT NULL, content TEXT NOT NULL DEFAULT '',
                image_file TEXT, image_instruction TEXT NOT NULL DEFAULT '', image_error TEXT, error TEXT,
                approved_author TEXT, linkedin_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 0);
            INSERT INTO settings VALUES ('openai_api_key', 'legacy-test-key');
            INSERT INTO series VALUES ('series', 'Original topic', 'Context', 'once', 1, 'UTC',
                '2026-11-03T00:00:00.000000Z', 0);
            INSERT INTO posts (id, series_id, scheduled_at, status, content, created_at, updated_at)
                VALUES ('draft', 'series', '2026-11-03T00:00:00.000000Z', 'needs_review',
                'Existing draft text', '2026-11-02T00:00:00.000000Z', '2026-11-02T00:00:00.000000Z');
        """)
    monkeypatch.setattr(storage, "DATA_DIR", data)
    monkeypatch.setattr(storage, "DB_PATH", database)
    monkeypatch.setattr(storage, "IMAGE_DIR", data / "images")
    storage.initialize()
    storage.initialize()
    with storage.db() as connection:
        post = storage.find_post(connection, "draft")
        assert post["topic"] == "Original topic" and post["topic_override"] is None
        assert post["content"] == "Existing draft text"
        assert post["repeat_limit"] == 5 and post["series_post_count"] == 1
        assert connection.execute("SELECT value FROM settings WHERE key = 'openai_api_key'").fetchone()[0] == "legacy-test-key"


def test_topic_only_edit_does_not_change_text_or_schedule(client, monkeypatch):
    setup(client)
    post = create(client, "days")
    generate(post["id"], monkeypatch)
    exact_text = "  Keep this formatting.\r\n\r\n"
    exact_time = "2030-01-02T12:34:45.000000Z"
    with storage.db(write=True) as connection:
        connection.execute("UPDATE posts SET content = ?, scheduled_at = ? WHERE id = ?", (exact_text, exact_time, post["id"]))
        connection.execute("UPDATE series SET anchor_at = ? WHERE id = ?", (exact_time, post["series_id"]))
    before = detail(client, post["id"])
    response = client.patch(f"/api/posts/{post['id']}", json={"topic": "Updated topic", "revision": before["revision"]})
    assert response.status_code == 200
    updated = response.json()
    assert updated["topic"] == "Updated topic"
    assert updated["content"] == exact_text and updated["scheduled_at"] == exact_time
    assert updated["timezone"] == "Asia/Kolkata"
    with storage.db() as connection:
        assert connection.execute("SELECT anchor_at FROM series WHERE id = ?", (post["series_id"],)).fetchone()[0] == exact_time


def test_ui_assets_are_not_cached_after_updates(client):
    for path in ("/", "/static/app.js?v=5", "/static/styles.css?v=5"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"


def test_review_can_change_repeat_interval_without_moving_current_post(client, monkeypatch):
    setup(client)
    scheduled = storage.now() + timedelta(hours=1)
    first = create(client, "days", scheduled)
    generate(first["id"], monkeypatch)
    assert approve(client, detail(client, first["id"])).status_code == 200

    async def publish(*args):
        return "urn:li:share:123"

    monkeypatch.setattr(services, "publish_to_linkedin", publish)
    monkeypatch.setattr(worker, "now", lambda: scheduled + timedelta(seconds=1))
    assert worker.claim("approved", "publishing", due=True) == first["id"]
    asyncio.run(worker.publish(first["id"]))
    current = next(post for post in client.get("/api/posts").json() if post["id"] != first["id"])
    generate(current["id"], monkeypatch)
    current = detail(client, current["id"])
    updated = client.patch(f"/api/posts/{current['id']}", json={
        "revision": current["revision"], "repeat_unit": "weeks", "repeat_every": 2, "repeat_limit": 3,
    })
    assert updated.status_code == 200
    edited = updated.json()
    assert (edited["repeat_unit"], edited["repeat_every"], edited["series_active"]) == ("weeks", 2, 1)
    assert edited["repeat_limit"] == 3 and edited["repeats_created"] == 1
    assert edited["scheduled_at"] == current["scheduled_at"]
    assert edited["content"] == current["content"] and edited["image_url"]
    with storage.db() as connection:
        assert connection.execute("SELECT anchor_at FROM series WHERE id = ?", (current["series_id"],)).fetchone()[0] == current["scheduled_at"]
    assert approve(client, edited).status_code == 200
    current_time = storage.parse_time(current["scheduled_at"])
    monkeypatch.setattr(worker, "now", lambda: current_time + timedelta(seconds=1))
    assert worker.claim("approved", "publishing", due=True) == current["id"]
    asyncio.run(worker.publish(current["id"]))
    next_post = next(post for post in client.get("/api/posts").json() if post["status"] == "queued")
    assert storage.parse_time(next_post["scheduled_at"]) == current_time + timedelta(weeks=2)


@pytest.mark.parametrize("initial,changed,every,interval", [
    ("once", "hours", 2, timedelta(hours=2)),
    ("days", "once", 1, None),
])
def test_review_can_start_or_stop_repeating(client, monkeypatch, initial, changed, every, interval):
    setup(client)
    scheduled = storage.now() + timedelta(hours=1)
    post = create(client, initial, scheduled)
    generate(post["id"], monkeypatch)
    post = detail(client, post["id"])
    response = client.patch(f"/api/posts/{post['id']}", json={
        "revision": post["revision"], "repeat_unit": changed, "repeat_every": every,
    })
    assert response.status_code == 200
    assert response.json()["series_active"] == int(interval is not None)
    assert approve(client, response.json()).status_code == 200

    async def publish(*args):
        return "urn:li:share:123"

    monkeypatch.setattr(services, "publish_to_linkedin", publish)
    monkeypatch.setattr(worker, "now", lambda: scheduled + timedelta(seconds=1))
    assert worker.claim("approved", "publishing", due=True) == post["id"]
    asyncio.run(worker.publish(post["id"]))
    queued = [item for item in client.get("/api/posts").json() if item["status"] == "queued"]
    assert len(queued) == int(interval is not None)
    if interval is not None:
        assert storage.parse_time(queued[0]["scheduled_at"]) == scheduled + interval


def test_repeat_edit_rejects_invalid_or_stale_changes(client, monkeypatch):
    setup(client)
    post = create(client, "days")
    generate(post["id"], monkeypatch)
    post = detail(client, post["id"])
    url = f"/api/posts/{post['id']}"
    assert client.patch(url, json={"revision": post["revision"], "repeat_unit": "weeks"}).status_code == 400
    assert client.patch(url, json={"revision": post["revision"], "repeat_unit": "weeks", "repeat_every": 0}).status_code == 422
    assert client.patch(url, json={"revision": post["revision"], "repeat_unit": "months", "repeat_every": 1}).status_code == 422
    assert client.post(f"/api/posts/{post['id']}/stop-repeat").status_code == 200
    assert client.patch(url, json={"revision": post["revision"], "repeat_unit": "weeks", "repeat_every": 1}).status_code == 409
    current = detail(client, post["id"])
    assert current["series_active"] == 0
    resumed = client.patch(url, json={"revision": current["revision"], "repeat_unit": "weeks", "repeat_every": 1})
    assert resumed.status_code == 200 and resumed.json()["series_active"] == 1
    assert approve(client, resumed.json()).status_code == 200
    assert client.patch(url, json={"revision": resumed.json()["revision"], "repeat_unit": "days", "repeat_every": 1}).status_code == 409


def test_stopping_repeat_from_history_invalidates_open_draft(client, monkeypatch):
    setup(client)
    scheduled = storage.now() + timedelta(hours=1)
    first = create(client, "days", scheduled)
    generate(first["id"], monkeypatch)
    assert approve(client, detail(client, first["id"])).status_code == 200

    async def publish(*args):
        return "urn:li:share:123"

    monkeypatch.setattr(services, "publish_to_linkedin", publish)
    monkeypatch.setattr(worker, "now", lambda: scheduled + timedelta(seconds=1))
    assert worker.claim("approved", "publishing", due=True) == first["id"]
    asyncio.run(worker.publish(first["id"]))
    current = next(post for post in client.get("/api/posts").json() if post["status"] == "queued")
    generate(current["id"], monkeypatch)
    current = detail(client, current["id"])
    assert client.post(f"/api/posts/{first['id']}/stop-repeat").status_code == 200
    stale = client.patch(f"/api/posts/{current['id']}", json={
        "revision": current["revision"], "repeat_unit": "weeks", "repeat_every": 1,
    })
    assert stale.status_code == 409
    assert detail(client, current["id"])["series_active"] == 0
    assert detail(client, first["id"])["status"] == "published"


def test_changing_time_and_repeat_together_sets_the_new_local_anchor(client, monkeypatch):
    setup(client)
    post = create(client)
    generate(post["id"], monkeypatch)
    post = detail(client, post["id"])
    new_time = "2030-03-09T14:00:00.000000Z"  # 9am in New York before daylight saving
    response = client.patch(f"/api/posts/{post['id']}", json={
        "revision": post["revision"], "scheduled_at": new_time, "timezone": "America/New_York",
        "repeat_unit": "days", "repeat_every": 1,
    })
    assert response.status_code == 200
    with storage.db() as connection:
        series = connection.execute("SELECT anchor_at, timezone, repeat_unit, repeat_every FROM series WHERE id = ?", (post["series_id"],)).fetchone()
    assert series["anchor_at"] == new_time and series["timezone"] == "America/New_York"
    after = datetime(2030, 3, 9, 14, 1, tzinfo=timezone.utc)
    assert storage.next_occurrence(series["anchor_at"], series["repeat_unit"], series["repeat_every"], series["timezone"], after) == datetime(2030, 3, 10, 13, 0, tzinfo=timezone.utc)


def test_five_repeats_each_generate_fresh_content_and_image_for_review(client, monkeypatch):
    setup(client)
    first_time = storage.now() + timedelta(hours=1)
    response = client.post("/api/posts", json={
        "topic": "A topic", "context": "A perspective", "scheduled_at": storage.timestamp(first_time),
        "timezone": "UTC", "repeat_unit": "days", "repeat_every": 1, "repeat_limit": 5,
    })
    assert response.status_code == 201
    post = response.json()
    prompts, images, sent = [], [], []

    async def text(config, topic, context, previous):
        prompts.append(list(previous))
        return f"Fresh draft {len(prompts)}"

    async def image(config, topic, context, content, instruction, existing):
        assert existing is None
        filename = services.save_image(base64.b64encode(PNG).decode())
        images.append((content, filename))
        return filename

    async def publish(token, person, content, image_data):
        sent.append(content)
        assert image_data == PNG
        return f"urn:li:share:{len(sent)}"

    monkeypatch.setattr(services, "generate_text", text)
    monkeypatch.setattr(services, "generate_image", image)
    monkeypatch.setattr(services, "publish_to_linkedin", publish)
    clock = [first_time]
    monkeypatch.setattr(worker, "now", lambda: clock[0])

    for index in range(6):
        assert worker.claim("queued", "generating") == post["id"]
        asyncio.run(worker.make_draft(post["id"]))
        post = detail(client, post["id"])
        assert post["status"] == "needs_review" and post["repeats_created"] == index
        assert post["content"] == f"Fresh draft {index + 1}" and post["image_url"]
        assert set(prompts[index]) == {f"Fresh draft {number}" for number in range(1, index + 1)}
        if index == 2:
            too_few = client.patch(f"/api/posts/{post['id']}", json={"revision": post["revision"], "repeat_limit": 1})
            assert too_few.status_code == 400
        assert worker.claim("approved", "publishing", due=True) is None
        assert approve(client, post).status_code == 200
        clock[0] = storage.parse_time(post["scheduled_at"]) + timedelta(seconds=1)
        assert worker.claim("approved", "publishing", due=True) == post["id"]
        asyncio.run(worker.publish(post["id"]))
        posts = client.get("/api/posts").json()
        queued = [item for item in posts if item["status"] == "queued"]
        if index < 5:
            assert len(queued) == 1
            post = queued[0]
        else:
            assert not queued and len(posts) == 6
            assert all(item["series_active"] == 0 for item in posts)

    assert len({filename for _, filename in images}) == 6
    assert sent == [content for content, _ in images]


def test_repeat_count_can_be_changed_but_never_exceeds_five(client, monkeypatch):
    setup(client)
    first = storage.now() + timedelta(hours=1)
    payload = {"topic": "Repeat test", "scheduled_at": storage.timestamp(first), "timezone": "UTC", "repeat_unit": "days"}
    assert client.post("/api/posts", json={**payload, "repeat_limit": 6}).status_code == 422
    assert client.post("/api/posts", json={**payload, "repeat_limit": 0}).status_code == 422
    post = client.post("/api/posts", json={**payload, "repeat_limit": 2}).json()
    generate(post["id"], monkeypatch)
    post = detail(client, post["id"])
    changed = client.patch(f"/api/posts/{post['id']}", json={"revision": post["revision"], "repeat_limit": 1})
    assert changed.status_code == 200 and changed.json()["repeat_limit"] == 1
    assert approve(client, changed.json()).status_code == 200
    async def publish(*args): return "urn:li:share:1"
    monkeypatch.setattr(services, "publish_to_linkedin", publish)
    monkeypatch.setattr(worker, "now", lambda: first + timedelta(seconds=1))
    assert worker.claim("approved", "publishing", due=True) == post["id"]
    asyncio.run(worker.publish(post["id"]))
    repeat = next(item for item in client.get("/api/posts").json() if item["status"] == "queued")
    generate(repeat["id"], monkeypatch)
    repeat = detail(client, repeat["id"])
    assert repeat["repeats_created"] == 1
    assert client.patch(f"/api/posts/{repeat['id']}", json={"revision": repeat["revision"], "repeat_limit": 6}).status_code == 422
    assert approve(client, repeat).status_code == 200
    monkeypatch.setattr(worker, "now", lambda: storage.parse_time(repeat["scheduled_at"]) + timedelta(seconds=1))
    assert worker.claim("approved", "publishing", due=True) == repeat["id"]
    asyncio.run(worker.publish(repeat["id"]))
    posts = client.get("/api/posts").json()
    assert len(posts) == 2 and all(item["series_active"] == 0 for item in posts)


def test_generate_text_asks_for_a_new_angle_on_previous_posts(monkeypatch):
    calls = []
    async def response(config, instructions, messages):
        calls.append(messages[0]["content"])
        return "A new draft"
    monkeypatch.setattr(services, "text_response", response)
    assert asyncio.run(services.generate_text({}, "A topic", "Some context", ["An earlier post"])) == "A new draft"
    assert "An earlier post" in calls[0] and "different angle and wording" in calls[0]
