from __future__ import annotations

import os
from typing import Any

from omni.config import provider_model
from omni.providers.base import Provider, ProviderError
from omni.providers.fallback import FallbackProvider
from omni.providers.gemini import GeminiProvider
from omni.providers.ollama import OllamaProvider
from omni.providers.openai_compat import OpenAICompatProvider

__all__ = [
    "Provider",
    "ProviderError",
    "OllamaProvider",
    "OpenAICompatProvider",
    "GeminiProvider",
    "build_provider",
    "provider_status",
]

FREE_PROVIDERS = ("gemini", "groq", "openrouter", "openai")
# Free tiers that need no API key at all. Only used as fallbacks: prompts go to
# a third party, so local models stay first whenever they are available.
KEYLESS_PROVIDERS = ("pollinations",)
FALLBACK_ORDER = ("gemini", "groq", "openrouter", "pollinations")
# Roughly ordered by tool-calling reliability in Ollama.
PREFERRED_OLLAMA = (
    "qwen2.5",
    "qwen3",
    "llama3.3",
    "llama3.2",
    "llama3.1",
    "mistral",
    "command-r",
)


def pick_ollama_model(installed: list[str], configured: str) -> str:
    if configured and configured in installed:
        return configured
    for family in PREFERRED_OLLAMA:
        for model in installed:
            if model.split(":")[0] == family or model.startswith(family):
                return model
    return installed[0]



def _key(config: dict[str, Any], provider: str) -> str:
    settings = config.get("providers", {}).get(provider, {})
    env_name = settings.get("api_key_env") or f"{provider.upper()}_API_KEY"
    return os.environ.get(env_name, "").strip()


def _ollama_models(config: dict[str, Any]) -> list[str]:
    settings = config.get("providers", {}).get("ollama", {})
    provider = OllamaProvider(settings.get("base_url", "http://127.0.0.1:11434"), "")
    return provider.models()


def detect_provider(config: dict[str, Any]) -> str:
    ollama_error = None
    try:
        models = _ollama_models(config)
    except ProviderError as exc:
        ollama_error = exc
    else:
        if models:
            return "ollama"

    for candidate in FREE_PROVIDERS:
        if _key(config, candidate):
            return candidate

    # Nothing local and no keys: the keyless free endpoint still works.
    if KEYLESS_PROVIDERS:
        return KEYLESS_PROVIDERS[0]

    if ollama_error is not None:
        raise ProviderError(
            "no AI provider available: Ollama is not reachable and no free API key is set.\n"
            "  start Ollama:   systemctl --user enable --now ollama\n"
            f"  pull a model:   ollama pull {provider_model(config, 'ollama') or 'qwen2.5:3b'}\n"
            "  or set a key:   export GEMINI_API_KEY=...  (free tier)"
        )
    raise ProviderError(
        "Ollama is running but has no models. Pull one:\n"
        f"  ollama pull {provider_model(config, 'ollama') or 'qwen2.5:3b'}"
    )


def build_provider(config: dict[str, Any], override: str | None = None) -> Provider:
    name = (override or config.get("agent", {}).get("provider") or "auto").lower()
    if name == "auto":
        name = detect_provider(config)

    primary = _build_one(config, name)
    # An explicit --provider choice means "use exactly this one".
    if override:
        return primary

    agent = config.get("agent", {})
    wanted = agent.get("fallback")
    if wanted is False:
        return primary
    if isinstance(wanted, list):
        names = [str(item).lower() for item in wanted]
    else:
        names = list(FALLBACK_ORDER)

    chain: list[Provider] = [primary]
    seen = {primary.name}
    for candidate in names:
        if candidate in seen:
            continue
        try:
            provider = _build_one(config, candidate)
        except ProviderError:
            continue
        seen.add(provider.name)
        chain.append(provider)
    return FallbackProvider(chain) if len(chain) > 1 else primary


def _build_one(config: dict[str, Any], name: str) -> Provider:
    name = name.lower()
    settings = config.get("providers", {}).get(name, {})
    model = provider_model(config, name)

    if name == "ollama":
        base_url = settings.get("base_url", "http://127.0.0.1:11434")
        explicit = str(config.get("agent", {}).get("model") or "").strip()
        installed = OllamaProvider(base_url, "").models()
        if not installed:
            raise ProviderError(
                "Ollama is running but has no models installed. Pull one:\n"
                f"  ollama pull {model or 'qwen2.5:3b'}"
            )
        if model not in installed:
            if explicit:
                raise ProviderError(
                    f"Ollama model {model!r} is not installed. Available: {', '.join(installed)}"
                )
            model = pick_ollama_model(installed, model)
        return OllamaProvider(
            base_url,
            model,
            timeout=float(settings.get("timeout", 180)),
            num_ctx=int(settings.get("num_ctx", 4096)),
            keep_alive=str(settings.get("keep_alive", "30m")),
            num_predict=int(settings.get("num_predict", 512)),
            num_thread=int(settings.get("num_thread", 0)),
        )

    if name == "gemini":
        key = _key(config, "gemini")
        if not key:
            raise ProviderError(
                "GEMINI_API_KEY is not set. Get a free key at https://aistudio.google.com/apikey"
            )
        return GeminiProvider(
            settings.get("base_url", "https://generativelanguage.googleapis.com/v1beta"),
            model,
            key,
        )

    if name == "pollinations":
        # Keyless free endpoint (OpenAI-compatible). Optional key raises limits.
        return OpenAICompatProvider(
            settings.get("base_url", "https://text.pollinations.ai/openai"),
            model or "openai-fast",
            _key(config, "pollinations") or None,
            timeout=float(settings.get("timeout", 120)),
            name="pollinations",
        )

    if name in {"groq", "openrouter", "openai"}:
        key = _key(config, name)
        if not key:
            raise ProviderError(f"{name}: API key environment variable is not set")
        extra_headers = {}
        if name == "openrouter":
            extra_headers = {
                "HTTP-Referer": "http://localhost",
                "X-Title": "Omni",
            }
        return OpenAICompatProvider(
            settings.get("base_url", ""),
            model,
            key,
            extra_headers=extra_headers,
            name=name,
        )

    raise ProviderError(f"unknown provider: {name}")


def provider_status(config: dict[str, Any]) -> dict[str, Any]:
    status: dict[str, Any] = {"configured": config.get("agent", {}).get("provider", "auto")}
    try:
        models = _ollama_models(config)
        status["ollama"] = {"reachable": True, "models": models}
    except ProviderError as exc:
        status["ollama"] = {"reachable": False, "error": str(exc)}
    for name in FREE_PROVIDERS:
        status[name] = {"key_set": bool(_key(config, name))}
    for name in KEYLESS_PROVIDERS:
        status[name] = {"key_set": True, "keyless": True}
    try:
        status["resolved"] = detect_provider(config)
    except ProviderError as exc:
        status["resolved"] = None
        status["error"] = str(exc)
    try:
        provider = build_provider(config)
        status["chain"] = provider.chain if hasattr(provider, "chain") else [provider.name]
    except ProviderError:
        status["chain"] = []
    if status.get("resolved") == "ollama":
        models = status.get("ollama", {}).get("models") or []
        if models:
            status["model"] = pick_ollama_model(models, provider_model(config, "ollama"))
    return status
