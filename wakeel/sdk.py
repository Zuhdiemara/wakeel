"""Providers through the official SDKs (openai, anthropic), as most client
teams use them. They share request building and response parsing with the
plain-HTTP adapters, so the graph cannot tell them apart; the SDKs add their
own retries, typed errors and connection pooling.

The OpenAI SDK also speaks to any OpenAI-compatible endpoint: with
OPENAI_SDK_BASE_URL=https://api.groq.com/openai/v1 it runs on Groq's free tier.
"""
from __future__ import annotations

import time

import anthropic
import openai

from .llm import Claude, LLMError, OpenAICompatible, Reply


class OpenAISDK(OpenAICompatible):
    def __init__(self, key: str, model: str, base_url: str | None = None, http_client=None):
        super().__init__(key, model, base_url or "https://api.openai.com/v1", "openai-sdk")
        self.client = openai.OpenAI(api_key=key, base_url=self.base, max_retries=2, timeout=45, http_client=http_client)

    def chat(self, messages, tools=None, json_mode=False) -> Reply:
        t0 = time.monotonic()
        try:
            r = self.client.chat.completions.create(**self.body(messages, tools, json_mode))
        except (openai.RateLimitError, openai.APIConnectionError, openai.APITimeoutError, openai.InternalServerError) as e:
            raise LLMError(f"{type(e).__name__}: {e}") from e
        except openai.APIStatusError as e:
            raise LLMError(f"HTTP {e.status_code}: {e}", retryable=False) from e
        return self.parse(r.model_dump(), t0)


class ClaudeSDK(Claude):
    def __init__(self, key: str, model: str = "claude-haiku-4-5-20251001", http_client=None):
        super().__init__(key, model)
        self.name = "claude-sdk"
        self.client = anthropic.Anthropic(api_key=key, max_retries=2, timeout=45, http_client=http_client)

    def chat(self, messages, tools=None, json_mode=False) -> Reply:
        t0 = time.monotonic()
        try:
            r = self.client.messages.create(**self.body(messages, tools, json_mode))
        except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.APITimeoutError) as e:
            raise LLMError(f"{type(e).__name__}: {e}") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"HTTP {e.status_code}: {e}", retryable=e.status_code >= 500) from e
        return self.parse(r.model_dump(), t0)
