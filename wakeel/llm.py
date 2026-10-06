"""One chat interface over several model providers.

Messages use a small provider-neutral shape:
    {"role": "system" | "user" | "assistant" | "tool", "content": str,
     "tool_calls": [{"id", "name", "args"}],   # assistant only
     "tool_call_id": str, "name": str}           # tool only
Each adapter translates to its provider's wire format with plain HTTP, so
switching providers (or adding one) is a small, testable change, and the
graph never depends on a vendor SDK.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

import httpx


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]


@dataclass
class Reply:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    ms: int = 0


class LLMError(Exception):
    """A provider failed. retryable: worth trying another provider."""

    def __init__(self, msg: str, retryable: bool = True):
        super().__init__(msg)
        self.retryable = retryable


class LLM(Protocol):
    name: str

    def chat(self, messages: list[dict], tools: list[dict] | None = None, json_mode: bool = False) -> Reply: ...


def _post(url: str, headers: dict, body: dict, timeout: float = 45) -> dict:
    try:
        r = httpx.post(url, headers=headers, json=body, timeout=timeout)
    except httpx.HTTPError as e:
        raise LLMError(f"network: {e}") from e
    if r.status_code == 429 or r.status_code >= 500:
        raise LLMError(f"HTTP {r.status_code}: {r.text[:200]}")
    if r.status_code >= 400:
        raise LLMError(f"HTTP {r.status_code}: {r.text[:300]}", retryable=False)
    return r.json()


# ---------------------------------------------------------------- Gemini

class Gemini:
    def __init__(self, key: str, model: str = "gemini-2.5-flash"):
        self.key, self.model, self.name = key, model, "gemini"

    def chat(self, messages, tools=None, json_mode=False) -> Reply:
        system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        contents = []
        for m in messages:
            if m["role"] == "user":
                contents.append({"role": "user", "parts": [{"text": m["content"]}]})
            elif m["role"] == "assistant":
                parts = [{"text": m["content"]}] if m.get("content") else []
                parts += [{"functionCall": {"name": c["name"], "args": c["args"]}} for c in m.get("tool_calls", [])]
                contents.append({"role": "model", "parts": parts})
            elif m["role"] == "tool":
                contents.append({"role": "user", "parts": [{"functionResponse": {"name": m["name"], "response": {"result": json.loads(m["content"])}}}]})
        body: dict[str, Any] = {"contents": contents, "generationConfig": {"temperature": 0.1}}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if tools:
            body["tools"] = [{"functionDeclarations": [{"name": t["name"], "description": t["description"], "parameters": t["parameters"]} for t in tools]}]
        if json_mode:
            body["generationConfig"]["responseMimeType"] = "application/json"
        t0 = time.monotonic()
        d = _post(f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent",
                  {"x-goog-api-key": self.key}, body)
        parts = (d.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
        rep = Reply(provider=self.name, model=self.model, ms=int((time.monotonic() - t0) * 1000))
        for i, p in enumerate(parts):
            if "text" in p:
                rep.text += p["text"]
            if "functionCall" in p:
                rep.tool_calls.append(ToolCall(f"call_{i}", p["functionCall"]["name"], p["functionCall"].get("args", {})))
        u = d.get("usageMetadata", {})
        rep.tokens_in, rep.tokens_out = u.get("promptTokenCount", 0), u.get("candidatesTokenCount", 0)
        return rep


# ------------------------------------------------- OpenAI-compatible (Groq, OpenAI)

class OpenAICompatible:
    def __init__(self, key: str, model: str, base: str, name: str):
        self.key, self.model, self.base, self.name = key, model, base.rstrip("/"), name

    def chat(self, messages, tools=None, json_mode=False) -> Reply:
        wire = []
        for m in messages:
            if m["role"] == "assistant" and m.get("tool_calls"):
                wire.append({"role": "assistant", "content": m.get("content") or None, "tool_calls": [
                    {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": json.dumps(c["args"])}} for c in m["tool_calls"]]})
            elif m["role"] == "tool":
                wire.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
            else:
                wire.append({"role": m["role"], "content": m["content"]})
        body: dict[str, Any] = {"model": self.model, "messages": wire, "temperature": 0.1}
        if tools:
            body["tools"] = [{"type": "function", "function": t} for t in tools]
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        t0 = time.monotonic()
        d = _post(f"{self.base}/chat/completions", {"Authorization": f"Bearer {self.key}"}, body)
        msg = d["choices"][0]["message"]
        rep = Reply(text=msg.get("content") or "", provider=self.name, model=self.model, ms=int((time.monotonic() - t0) * 1000))
        for c in msg.get("tool_calls") or []:
            try:
                args = json.loads(c["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            rep.tool_calls.append(ToolCall(c["id"], c["function"]["name"], args))
        u = d.get("usage", {})
        rep.tokens_in, rep.tokens_out = u.get("prompt_tokens", 0), u.get("completion_tokens", 0)
        return rep


# ---------------------------------------------------------------- Claude

class Claude:
    def __init__(self, key: str, model: str = "claude-haiku-4-5-20251001"):
        self.key, self.model, self.name = key, model, "claude"

    def chat(self, messages, tools=None, json_mode=False) -> Reply:
        system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        if json_mode:
            system += "\nRespond with one JSON object only."
        wire: list[dict] = []
        for m in messages:
            if m["role"] == "user":
                wire.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                blocks = [{"type": "text", "text": m["content"]}] if m.get("content") else []
                blocks += [{"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["args"]} for c in m.get("tool_calls", [])]
                wire.append({"role": "assistant", "content": blocks})
            elif m["role"] == "tool":
                block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
                if wire and wire[-1]["role"] == "user" and isinstance(wire[-1]["content"], list):
                    wire[-1]["content"].append(block)
                else:
                    wire.append({"role": "user", "content": [block]})
        body: dict[str, Any] = {"model": self.model, "max_tokens": 1500, "system": system, "messages": wire, "temperature": 0.1}
        if tools:
            body["tools"] = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools]
        t0 = time.monotonic()
        d = _post("https://api.anthropic.com/v1/messages", {"x-api-key": self.key, "anthropic-version": "2023-06-01"}, body)
        rep = Reply(provider=self.name, model=self.model, ms=int((time.monotonic() - t0) * 1000))
        for b in d.get("content", []):
            if b["type"] == "text":
                rep.text += b["text"]
            elif b["type"] == "tool_use":
                rep.tool_calls.append(ToolCall(b["id"], b["name"], b.get("input", {})))
        u = d.get("usage", {})
        rep.tokens_in, rep.tokens_out = u.get("input_tokens", 0), u.get("output_tokens", 0)
        return rep


# ---------------------------------------------------------------- fallback

class Fallback:
    """Tries providers in order. A rate limit, outage or timeout moves on to
    the next one; a malformed request (4xx) does not, since it would fail
    everywhere. on_switch is called so the trace shows every fallback."""

    def __init__(self, providers: list, on_switch: Callable[[str, str], None] | None = None):
        if not providers:
            raise ValueError("no LLM provider configured")
        self.providers, self.on_switch = providers, on_switch
        self.name = "+".join(p.name for p in providers)

    def chat(self, messages, tools=None, json_mode=False) -> Reply:
        last: LLMError | None = None
        for p in self.providers:
            try:
                return p.chat(messages, tools, json_mode)
            except LLMError as e:
                last = e
                if self.on_switch:
                    self.on_switch(p.name, str(e))
                if not e.retryable:
                    break
        raise last or LLMError("no provider answered")


class Scripted:
    """A deterministic stand-in for tests and offline evaluation: a function
    from the conversation to a Reply."""

    def __init__(self, fn: Callable[[list[dict], list[dict] | None, bool], Reply], name: str = "scripted"):
        self.fn, self.name = fn, name

    def chat(self, messages, tools=None, json_mode=False) -> Reply:
        r = self.fn(messages, tools, json_mode)
        r.provider, r.model = r.provider or self.name, r.model or self.name
        return r


def from_env(on_switch=None) -> Fallback:
    """Providers from the environment, in order of preference: Gemini, then
    Groq (both have free tiers), then Claude and OpenAI if keys are present."""
    ps: list = []
    if k := os.getenv("GEMINI_API_KEY"):
        ps.append(Gemini(k, os.getenv("GEMINI_MODEL", "gemini-2.5-flash")))
    if k := os.getenv("GROQ_API_KEY"):
        ps.append(OpenAICompatible(k, os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"), "https://api.groq.com/openai/v1", "groq"))
    if k := os.getenv("ANTHROPIC_API_KEY"):
        ps.append(Claude(k, os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")))
    if k := os.getenv("OPENAI_API_KEY"):
        ps.append(OpenAICompatible(k, os.getenv("OPENAI_MODEL", "gpt-4.1-mini"), "https://api.openai.com/v1", "openai"))
    return Fallback(ps, on_switch)
