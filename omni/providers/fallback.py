from __future__ import annotations

from typing import Any

from omni.providers.base import ChatReply, Provider, ProviderError


class FallbackProvider(Provider):
    """Try providers in order and return the first reply that works.

    Used so a local model that is missing, out of memory, or too slow can fall
    back to a free cloud provider instead of failing the whole turn. Each
    provider sees the same messages, so a failure before any tool ran is safe
    to retry elsewhere.
    """

    name = "fallback"

    def __init__(self, providers: list[Provider]) -> None:
        if not providers:
            raise ProviderError("no providers in fallback chain")
        self.providers = providers
        # Name of the provider that answered most recently, for the UI and logs.
        self.last_used: str | None = None

    @property
    def chain(self) -> list[str]:
        return [provider.name for provider in self.providers]

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ChatReply:
        errors: list[str] = []
        for provider in self.providers:
            try:
                reply = provider.chat(messages, tools)
                self.last_used = provider.name
                return reply
            except ProviderError as exc:
                errors.append(f"{provider.name}: {exc}")
            except OSError as exc:  # network hiccups from urllib
                errors.append(f"{provider.name}: {exc}")
        raise ProviderError("every provider failed — " + " | ".join(errors))
