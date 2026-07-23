"""
llm_client.py
=============
Provider-agnostic narrative client. The rest of the system depends only on the
`LLMClient` interface (a `.complete(system, user) -> str` method), so the LLM is
a swappable component and never a trusted source of numbers.

Implementations
---------------
AnthropicClient : real calls to the Anthropic API. Requires ANTHROPIC_API_KEY.
                  This is the production path (locked stack: "one LLM API").
ReplayClient    : returns pre-recorded responses keyed by scenario id. Makes the
                  eval fully deterministic and runnable offline / in CI.
ScriptedClient  : returns whatever text you hand it; used to feed adversarial
                  (deliberately fabricated) outputs through the real guardrails.
"""
from __future__ import annotations
from typing import Protocol
import os


class LLMClient(Protocol):
    def complete(self, system: str, user: str) -> str: ...


class AnthropicClient:
    """Real Anthropic API client. Narrative-only; never trusted for numbers."""

    def __init__(self, model: str = "claude-sonnet-4-5", max_tokens: int = 900,
                 temperature: float = 0.2):
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature

    def complete(self, system: str, user: str) -> str:
        import anthropic  # imported lazily so offline use needs no key/SDK
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        resp = client.messages.create(
            model=self.model, max_tokens=self.max_tokens, temperature=self.temperature,
            system=system, messages=[{"role": "user", "content": user}],
        )
        return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")


class OpenAIClient:
    """OpenAI narration client. Same interface as AnthropicClient; never trusted for numbers."""

    def __init__(self, model: str = "gpt-4.1", max_tokens: int = 900,
                 temperature: float = 0.2):
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature

    def complete(self, system: str, user: str) -> str:
        from openai import OpenAI  # lazy import so offline use needs no SDK/key
        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        resp = client.chat.completions.create(
            model=self.model, max_tokens=self.max_tokens, temperature=self.temperature,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
        )
        return resp.choices[0].message.content or ""


class ReplayClient:
    """Deterministic client: yields recorded responses in sequence per key."""

    def __init__(self, responses: dict, key: str = "default"):
        # responses: {key: [attempt1_text, attempt2_text, ...]}
        self._responses = responses
        self._key = key
        self._i = 0

    def for_key(self, key: str) -> "ReplayClient":
        return ReplayClient(self._responses, key)

    def complete(self, system: str, user: str) -> str:
        seq = self._responses.get(self._key, self._responses.get("default", [""]))
        text = seq[min(self._i, len(seq) - 1)]
        self._i += 1
        return text


class ScriptedClient:
    """Returns a fixed list of texts in order (for adversarial injection)."""

    def __init__(self, texts: list):
        self._texts = texts
        self._i = 0

    def complete(self, system: str, user: str) -> str:
        text = self._texts[min(self._i, len(self._texts) - 1)]
        self._i += 1
        return text
