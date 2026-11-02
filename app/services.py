import base64
import io
import os
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx
from huggingface_hub import AsyncInferenceClient
from openai import AsyncOpenAI
from PIL import Image, UnidentifiedImageError

from .providers import IMAGE_MODEL, TEXT_MODEL, huggingface_model_id
from .storage import IMAGE_DIR


class ProviderError(ValueError):
    pass


def error_message(exc, provider="AI provider"):
    if isinstance(exc, ProviderError):
        return str(exc)
    status = getattr(exc, "status_code", None)
    if status is None and getattr(exc, "response", None) is not None:
        status = getattr(exc.response, "status_code", None)
    if provider == "LinkedIn":
        if status == 401:
            return "LinkedIn rejected the connection. Reconnect your account in Settings."
        if status == 403:
            return "LinkedIn denied the request. Check that Share on LinkedIn is enabled for your app."
        if status == 429:
            return "LinkedIn's rate limit was reached. Try again later."
        return "LinkedIn could not complete the request" + (f" (HTTP {status})." if status else ". Check your connection.")
    if status == 401:
        return f"{provider} rejected the API key. Update it in Settings."
    if status == 403:
        return f"{provider} denied the request. Check your account and model access."
    if status == 404:
        return f"{provider} could not find the model or endpoint. Check both in Settings."
    if status == 429:
        return f"{provider}'s quota or rate limit was reached. Check billing and try again."
    return f"{provider} could not complete the request" + (f" (HTTP {status}). Check the API key, model, and provider settings." if status else ". Check your connection, model access, and provider settings.")


def read_image(filename):
    return (IMAGE_DIR / filename).read_bytes()


def save_image(encoded):
    if not encoded:
        raise ProviderError("The image provider returned no image. Check that the selected model can generate images.")
    try:
        data = base64.b64decode(encoded, validate=True)
        if len(data) > 16 * 1024 * 1024:
            raise ProviderError("The generated image is larger than 16 MB. Choose a smaller image model or size.")
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in {"PNG", "JPEG", "WEBP"} or image.width * image.height >= 36_152_320:
                raise ProviderError("Use a PNG, JPEG, or WebP image under LinkedIn's pixel limit.")
            image.load()
            if image.format != "PNG":
                output = io.BytesIO()
                image.convert("RGBA" if "A" in image.getbands() else "RGB").save(output, format="PNG")
                data = output.getvalue()
        if len(data) > 16 * 1024 * 1024:
            raise ProviderError("The converted image is larger than 16 MB. Choose a smaller image model or size.")
    except (ValueError, OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        if isinstance(exc, ProviderError):
            raise
        raise ProviderError("The provider returned an invalid or unsupported image.") from None
    filename = f"{uuid4().hex}.png"
    fd = os.open(IMAGE_DIR / filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as image:
        image.write(data)
    return filename


async def text_response(config, instructions, messages):
    provider, model, key, base_url = (config[name] for name in ("provider", "model", "api_key", "base_url"))
    if provider in {"anthropic", "custom_anthropic"}:
        async with httpx.AsyncClient(timeout=120, follow_redirects=False) as client:
            response = await client.post(
                f"{base_url}/messages",
                headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                json={"model": model, "max_tokens": 1500, "system": instructions, "messages": messages},
            )
            response.raise_for_status()
            result = "\n".join(part.get("text", "") for part in response.json().get("content", []) if part.get("type") == "text")
    else:
        async with AsyncOpenAI(api_key=key or "local", base_url=base_url, timeout=120, max_retries=1,
                               http_client=httpx.AsyncClient(follow_redirects=False)) as client:
            if provider in {"openai", "perplexity"}:
                response = await client.responses.create(model=model, instructions=instructions, input=messages)
                result = response.output_text
            else:
                response = await client.chat.completions.create(
                    model=model, messages=[{"role": "system", "content": instructions}, *messages],
                )
                result = response.choices[0].message.content if response.choices else ""
    if not isinstance(result, str) or not result.strip():
        raise ProviderError(f"{config['name']} returned no text. Check that the selected model supports text generation.")
    return result.strip()


async def generate_text(config, topic, context):
    return await text_response(
        config,
        "Write a compelling LinkedIn post for a person's professional network. "
        "Use the supplied context, avoid inventing facts or personal experiences, "
        "and keep it under 3000 characters. Return only the post, without quotation marks.",
        [{"role": "user", "content": f"Topic: {topic}\n\nContext and preferences:\n{context or '(none provided)'}"}],
    )


async def generate_image(config, topic, context, content, instruction="", existing=None):
    prompt = (
        "Create a polished, engaging image for a professional LinkedIn post. "
        "Do not include logos, watermarks, or blocks of text. "
        f"Topic: {topic}. Context: {context[:2000]}. Post: {content[:2000]}. "
        f"Visual direction: {instruction or 'Choose a clear visual concept that supports the post.'}"
    )
    provider, model, key, base_url = (config[name] for name in ("provider", "model", "api_key", "base_url"))
    image_data = base64.b64encode(existing).decode() if existing else None
    if provider == "gemini":
        parts = [{"text": prompt}]
        if image_data:
            parts.append({"inlineData": {"mimeType": "image/png", "data": image_data}})
        async with httpx.AsyncClient(timeout=180, follow_redirects=False) as client:
            response = await client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{quote(model, safe='')}:generateContent",
                headers={"x-goog-api-key": key},
                json={"contents": [{"parts": parts}], "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]}},
            )
            response.raise_for_status()
            candidates = response.json().get("candidates", [])
            parts = candidates[0].get("content", {}).get("parts", []) if candidates else []
            encoded = next((part.get("inlineData", {}).get("data") for part in parts if part.get("inlineData", {}).get("data")), None)
    elif provider == "huggingface":
        if not huggingface_model_id(model):
            raise ProviderError("Enter a Hugging Face image model ID in the form owner/model.")
        async with AsyncInferenceClient(provider="auto", api_key=key, timeout=180) as client:
            image = (await client.image_to_image(existing, prompt=prompt, model=model) if existing
                     else await client.text_to_image(prompt, model=model))
            if image.width * image.height >= 36_152_320:
                raise ProviderError("The generated image exceeds LinkedIn's pixel limit.")
            output = io.BytesIO()
            image.save(output, format="PNG")
            encoded = base64.b64encode(output.getvalue()).decode()
    elif provider in {"xai", "openrouter"}:
        body = {"model": model, "prompt": prompt}
        if provider == "xai":
            body["response_format"] = "b64_json"
            if image_data:
                body["image"] = {"url": f"data:image/png;base64,{image_data}", "type": "image_url"}
            url = f"{base_url}/images/{'edits' if image_data else 'generations'}"
        else:
            url = f"{base_url}/images"
            if image_data:
                body["input_references"] = [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_data}"}}]
        async with httpx.AsyncClient(timeout=180, follow_redirects=False) as client:
            response = await client.post(url, headers={"Authorization": f"Bearer {key}"}, json=body)
            response.raise_for_status()
            data = response.json().get("data", [])
            encoded = data[0].get("b64_json") if data else None
    else:
        async with AsyncOpenAI(api_key=key or "local", base_url=base_url, timeout=180, max_retries=0,
                               http_client=httpx.AsyncClient(follow_redirects=False)) as client:
            if existing:
                kwargs = {"model": model, "image": ("current.png", existing, "image/png"), "prompt": prompt}
                if provider == "openai":
                    kwargs.update(size="1024x1024", quality="medium")
                result = await client.images.edit(**kwargs)
            else:
                kwargs = {"model": model, "prompt": prompt}
                if provider == "openai":
                    kwargs.update(size="1024x1024", quality="medium")
                elif provider != "azure":
                    kwargs["response_format"] = "b64_json"
                result = await client.images.generate(**kwargs)
            encoded = result.data[0].b64_json if result.data else None
    return save_image(encoded)


async def ask_ai(config, topic, context, content, history, question):
    messages = [{"role": item["role"], "content": item["content"]} for item in history[-12:]]
    messages.append({"role": "user", "content": question})
    return await text_response(
        config,
        "Help the user review a LinkedIn post. Answer questions naturally. "
        "If asked to rewrite it, return the complete revised post, without preamble. "
        "Do not invent facts or imply you have published anything. "
        f"\nTopic: {topic}\nContext: {context}\nCurrent post:\n{content}",
        messages,
    )


async def publish_to_linkedin(token, person_id, content, image):
    author = f"urn:li:person:{person_id}"
    headers = {"Authorization": f"Bearer {token}", "X-Restli-Protocol-Version": "2.0.0"}
    async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
        share = {"shareCommentary": {"text": content}, "shareMediaCategory": "NONE"}
        if image is not None:
            registration = await client.post(
                "https://api.linkedin.com/v2/assets?action=registerUpload",
                headers=headers,
                json={"registerUploadRequest": {
                    "recipes": ["urn:li:digitalmediaRecipe:feedshare-image"],
                    "owner": author,
                    "serviceRelationships": [{"relationshipType": "OWNER", "identifier": "urn:li:userGeneratedContent"}],
                    "supportedUploadMechanism": ["SYNCHRONOUS_UPLOAD"],
                }},
            )
            registration.raise_for_status()
            value = registration.json()["value"]
            mechanism = value["uploadMechanism"]["com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest"]
            upload_url = mechanism["uploadUrl"]
            parsed = urlsplit(upload_url)
            if parsed.scheme != "https" or not parsed.hostname or not (
                parsed.hostname == "linkedin.com" or parsed.hostname.endswith(".linkedin.com")
            ) or parsed.username or parsed.password or parsed.port not in (None, 443):
                raise ValueError("LinkedIn returned an unexpected upload URL")
            upload_headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/octet-stream"}
            if mechanism.get("headers", {}).get("media-type-family"):
                upload_headers["media-type-family"] = mechanism["headers"]["media-type-family"]
            uploaded = await client.put(upload_url, headers=upload_headers, content=image)
            uploaded.raise_for_status()
            share.update(shareMediaCategory="IMAGE", media=[{"status": "READY", "media": value["asset"]}])
        response = await client.post(
            "https://api.linkedin.com/v2/ugcPosts",
            headers=headers,
            json={
                "author": author,
                "lifecycleState": "PUBLISHED",
                "specificContent": {"com.linkedin.ugc.ShareContent": share},
                "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
            },
        )
        response.raise_for_status()
        return response.headers.get("x-restli-id", "")
