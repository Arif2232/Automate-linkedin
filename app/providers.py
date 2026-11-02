import os
import ipaddress
import re
import hashlib
import hmac
import json
from urllib.parse import urlsplit

# Model IDs can be changed in Settings as providers add or retire models.
TEXT_MODEL = os.environ.get("OPENAI_TEXT_MODEL", "gpt-4.1-mini")
IMAGE_MODEL = os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-1-mini")

PROVIDERS = {
    "openai": {"name": "OpenAI", "base_url": "https://api.openai.com/v1", "text": TEXT_MODEL, "image": IMAGE_MODEL},
    "gemini": {"name": "Google Gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "text": "gemini-3.8-flash", "image": "gemini-3.1-flash-image"},
    "anthropic": {"name": "Anthropic (Claude)", "base_url": "https://api.anthropic.com/v1", "text": "claude-haiku-5-5"},
    "xai": {"name": "xAI (Grok)", "base_url": "https://api.x.ai/v1", "text": "grok-4.7", "image": "grok-imagine-image-2.0"},
    "groq": {"name": "Groq", "base_url": "https://api.groq.com/openai/v1", "text": "llama-3.3-70b-versatile"},
    "deepseek": {"name": "DeepSeek", "base_url": "https://api.deepseek.com", "text": "deepseek-flash"},
    "mistral": {"name": "Mistral", "base_url": "https://api.mistral.ai/v1", "text": "mistral-small-latest"},
    "perplexity": {"name": "Perplexity", "base_url": "https://api.perplexity.ai/v1", "text": "openai/gpt-5.6-luna"},
    "openrouter": {"name": "OpenRouter", "base_url": "https://openrouter.ai/api/v1", "text": "openai/gpt-4.1-mini", "image": "black-forest-labs/flux.2-klein-4b"},
    "together": {"name": "Together AI", "base_url": "https://api.together.ai/v1", "text": "MiniMaxAI/MiniMax-M3"},
    "huggingface": {"name": "Hugging Face", "base_url": "https://router.huggingface.co/v1", "text": "openai/gpt-oss-120b:fastest", "image": "black-forest-labs/FLUX.1-schnell"},
    "ollama": {"name": "Ollama (local)", "base_url": "http://127.0.0.1:11434/v1", "text": "gpt-oss:20b", "key_optional": True},
    "azure": {"name": "Azure OpenAI", "text": "", "image": "", "needs_url": True},
    "qwen": {"name": "Alibaba Cloud / Qwen", "text": "qwen3.7-plus", "needs_url": True},
    "custom": {"name": "Other OpenAI-compatible", "text": "", "image": "", "needs_url": True},
    "custom_anthropic": {"name": "Other Anthropic-compatible", "text": "", "needs_url": True},
    "same": {"name": "Same as writing provider", "image": "", "key_optional": True},
    "none": {"name": "No image — text only", "image": "", "key_optional": True},
}


def key_name(provider, kind):
    if provider == "openai":
        return "openai_api_key"
    suffix = f"_{kind}" if PROVIDERS[provider].get("needs_url") else ""
    return f"ai_{provider}{suffix}_api_key"


def model_name(provider, kind):
    return f"ai_{kind}_model_{provider}"


def url_name(provider, kind):
    return f"ai_{kind}_url_{provider}"


def test_key_name(provider, kind):
    return f"ai_{kind}_tested_{provider}"


def test_signature(settings, kind, config):
    secret = settings.get("session_secret", "")
    if not secret:
        return ""
    values = [kind, config["provider"], config["model"], config["base_url"], config["api_key"]]
    data = json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode()
    return hmac.new(secret.encode(), data, hashlib.sha256).hexdigest()


def loopback(host):
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def huggingface_model_id(value):
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}/[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", value))


def valid_base_url(value):
    try:
        parts = urlsplit(value.strip())
        port = parts.port
        local = loopback(parts.hostname)
        if (parts.scheme not in ({"http", "https"} if local else {"https"}) or
                not parts.hostname or parts.username or parts.password or parts.query or parts.fragment or
                port == 0):
            raise ValueError
    except ValueError:
        raise ValueError("Use an HTTPS base URL, or HTTP for a local server, without credentials or query parameters.") from None
    return value.strip().rstrip("/")


def selected(settings, kind):
    choice = settings.get(f"ai_{kind}_provider", "same" if kind == "image" else "openai")
    provider = choice
    if kind == "image" and choice == "same":
        text_provider = settings.get("ai_text_provider", "openai")
        text_spec = PROVIDERS.get(text_provider, {})
        provider = text_provider if "image" in text_spec and not text_spec.get("needs_url") else "none"
    spec = PROVIDERS.get(provider)
    if not spec or kind not in spec:
        raise ValueError("Choose a valid AI provider in Settings.")
    model = settings.get(model_name(provider, kind), spec[kind])
    url = settings.get(url_name(provider, kind), "") if spec.get("needs_url") else spec.get("base_url", "")
    key = settings.get(key_name(provider, kind), "")
    local = bool(url) and loopback(urlsplit(url).hostname)
    ready = provider == "none" or bool(model and url and (key or spec.get("key_optional") or (provider == "custom" and local)))
    config = {"provider": provider, "selection": choice, "name": spec["name"], "model": model,
              "base_url": url, "api_key": key, "ready": ready}
    signature = test_signature(settings, kind, config)
    saved = settings.get(test_key_name(provider, kind), "")
    config["tested"] = provider == "none" or bool(ready and signature and hmac.compare_digest(saved, signature))
    return config


def public_config(settings):
    public = {"providers": [], "models": {"text": {}, "image": {}}, "urls": {"text": {}, "image": {}}, "keys": {"text": {}, "image": {}}}
    for provider, spec in PROVIDERS.items():
        public["providers"].append({"id": provider, "name": spec["name"], "text": "text" in spec,
                                    "image": "image" in spec, "needs_url": bool(spec.get("needs_url")),
                                    "key_optional": bool(spec.get("key_optional")),
                                    "default_text": spec.get("text", ""), "default_image": spec.get("image", "")})
        for kind in ("text", "image"):
            if kind in spec:
                public["models"][kind][provider] = settings.get(model_name(provider, kind), spec[kind])
                public["urls"][kind][provider] = settings.get(url_name(provider, kind), "") if spec.get("needs_url") else spec.get("base_url", "")
                public["keys"][kind][provider] = bool(settings.get(key_name(provider, kind)))
    for kind in ("text", "image"):
        config = selected(settings, kind)
        public[kind] = {key: value for key, value in config.items() if key != "api_key"}
    return public
