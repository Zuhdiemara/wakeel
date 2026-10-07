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


def _post(url: str, headers: dict, body: dict, timeout: float = 45, retries: int = 2) -> dict:
    for attempt in range(retries + 1):
        try:
            r = httpx.post(url, headers=headers, json=body, timeout=timeout)
        except httpx.HTTPError as e:
            raise LLMError(f"network: {e}") from e
        wait = r.headers.get("retry-after") if r.status_code == 429 else None
        # A short per-minute limit is worth waiting out; a long one (a daily
        # quota) is not: fall through to the next provider instead.
        if wait is None or attempt == retries or not wait.replace(".", "", 1).isdigit() or float(wait) > 10:
            break
        time.sleep(float(wait))
    if r.status_code == 429 or r.status_code >= 500:
        raise LLMError(f"HTTP {r.status_code}: {r.text[:200]}")
    if r.status_code >= 400:
        raise LLMError(f"HTTP {r.status_code}: {r.text[:300]}", retryable=False)
    return r.json()


# ---------------------------------------------------------------- Gemini

class Gemini:
    supports_tools = True

    def __init__(self, key: str, model: str = "gemini-2.5-flash"):
        self.key, self.model, self.name = key, model, "gemini"

    def url(self) -> str:
        return f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"

    def headers(self) -> dict:
        return {"x-goog-api-key": self.key}

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
        d = _post(self.url(), self.headers(), body)
        parts = (d.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
        rep = Reply(provider=self.name, model=self.model, ms=int((time.monotonic() - t0) * 1000))
        for i, p in enumerate(parts):
            if p.get("thought"):           # a thinking model's reasoning, not its answer
                continue
            if "text" in p:
                rep.text += p["text"]
            if "functionCall" in p:
                rep.tool_calls.append(ToolCall(f"call_{i}", p["functionCall"]["name"], p["functionCall"].get("args", {})))
        u = d.get("usageMetadata", {})
        rep.tokens_in, rep.tokens_out = u.get("promptTokenCount", 0), u.get("candidatesTokenCount", 0)
        return rep


# ------------------------------------------------- OpenAI-compatible (Groq, OpenAI)

class OpenAICompatible:
    def __init__(self, key: str, model: str, base: str, name: str, supports_tools: bool = True):
        self.key, self.model, self.base, self.name, self.supports_tools = key, model, base.rstrip("/"), name, supports_tools

    def chat(self, messages, tools=None, json_mode=False) -> Reply:
        t0 = time.monotonic()
        d = _post(f"{self.base}/chat/completions", {"Authorization": f"Bearer {self.key}"}, self.body(messages, tools, json_mode))
        return self.parse(d, t0)

    def body(self, messages, tools=None, json_mode=False) -> dict:
        wire = []
        for m in messages:
            if m["role"] == "assistant" and m.get("tool_calls"):
                wire.append({"role": "assistant", "content": m.get("content") or None, "tool_calls": [
                    {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": json.dumps(c["args"])}} for c in m["tool_calls"]]})
            elif m["role"] == "tool":
                wire.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
            else:
                wire.append({"role": m["role"], "content": m["content"]})
        # An explicit output cap: some free tiers count the requested maximum
        # against the per-minute token budget and reject an uncapped request.
        body: dict[str, Any] = {"model": self.model, "messages": wire, "temperature": 0.1, "max_tokens": 1500}
        if tools:
            body["tools"] = [{"type": "function", "function": t} for t in tools]
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        return body

    def parse(self, d: dict, t0: float) -> Reply:
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
    supports_tools = True

    def __init__(self, key: str, model: str = "claude-haiku-4-5-20251001"):
        self.key, self.model, self.name = key, model, "claude"

    def body(self, messages, tools=None, json_mode=False) -> dict:
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
        # No temperature: the current Messages API and SDK no longer accept it.
        body: dict[str, Any] = {"model": self.model, "max_tokens": 1500, "system": system, "messages": wire}
        if tools:
            body["tools"] = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools]
        return body

    def chat(self, messages, tools=None, json_mode=False) -> Reply:
        t0 = time.monotonic()
        d = _post("https://api.anthropic.com/v1/messages", {"x-api-key": self.key, "anthropic-version": "2023-06-01"}, self.body(messages, tools, json_mode))
        return self.parse(d, t0)

    def parse(self, d: dict, t0: float) -> Reply:
        rep = Reply(provider=self.name, model=self.model, ms=int((time.monotonic() - t0) * 1000))
        for b in d.get("content", []):
            if b["type"] == "text":
                rep.text += b["text"]
            elif b["type"] == "tool_use":
                rep.tool_calls.append(ToolCall(b["id"], b["name"], b.get("input", {})))
        u = d.get("usage", {})
        rep.tokens_in, rep.tokens_out = u.get("input_tokens", 0), u.get("output_tokens", 0)
        return rep


# ---------------------------------------------------------------- Vertex AI

class VertexGemini(Gemini):
    """The same Gemini request through Vertex AI: a Google Cloud project and
    region (data residency, enterprise identity and quotas) and an OAuth
    token instead of an API key. The token comes from VERTEX_ACCESS_TOKEN
    (e.g. `gcloud auth print-access-token`) or Application Default
    Credentials when google-auth is installed."""

    def __init__(self, project: str, region: str, model: str = "gemini-2.5-flash", token: str | None = None):
        super().__init__("", model)
        self.project, self.region, self.token, self.name = project, region, token, "vertex"

    def url(self) -> str:
        return (f"https://{self.region}-aiplatform.googleapis.com/v1/projects/{self.project}/locations/{self.region}"
                f"/publishers/google/models/{self.model}:generateContent")

    def headers(self) -> dict:
        token = self.token or os.getenv("VERTEX_ACCESS_TOKEN")
        if not token:
            try:
                import google.auth
                import google.auth.transport.requests
                creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
                creds.refresh(google.auth.transport.requests.Request())
                token = creds.token
            except Exception as e:
                raise LLMError(f"no Vertex credentials: {e}", retryable=False) from e
        return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------- fallback

class Fallback:
    """Tries providers in order; any failure (rate limit, outage, timeout, or an
    option this model does not support) moves on to the next. on_switch is
    called so the trace shows every fallback."""

    def __init__(self, providers: list, on_switch: Callable[[str, str], None] | None = None):
        if not providers:
            raise ValueError("no LLM provider configured")
        self.providers, self.on_switch = providers, on_switch
        self.name = "+".join(p.name for p in providers)

    def chat(self, messages, tools=None, json_mode=False) -> Reply:
        last: LLMError | None = None
        for p in self.providers:
            if tools and not getattr(p, "supports_tools", True):
                continue                   # a chat-only model is skipped for tool steps
            try:
                return p.chat(messages, tools, json_mode)
            except LLMError as e:
                last = e
                if self.on_switch:
                    self.on_switch(p.name, str(e))
                # Even a 4xx moves on: with mixed models it usually means "this
                # model does not support that option" (JSON mode, tools), and
                # the next model may accept the same request.
        raise last or LLMError("no provider answered")


class Scripted:
    supports_tools = True
    """A deterministic stand-in for tests and offline evaluation: a function
    from the conversation to a Reply."""

    def __init__(self, fn: Callable[[list[dict], list[dict] | None, bool], Reply], name: str = "scripted"):
        self.fn, self.name = fn, name

    def chat(self, messages, tools=None, json_mode=False) -> Reply:
        r = self.fn(messages, tools, json_mode)
        r.provider, r.model = r.provider or self.name, r.model or self.name
        return r


# Free models verified on 7 October 2026 (chat, tool calling, Arabic), in the
# default order of preference. Each has its own free quota, so a longer chain
# also keeps the agent on a model for longer before falling back to rules.
DEFAULT_CHAIN = [
    "gemini:gemini-2.5-flash",
    "groq:openai/gpt-oss-120b",
    "gemini:gemini-3.5-flash-lite",
    "groq:openai/gpt-oss-20b",
    "gemini:gemini-3.1-flash-lite",
    "gemini:gemma-4-26b-a4b-it",
    "groq:allam-2-7b",              # SDAIA's Arabic model: chat only, no tool calling
]
CHAT_ONLY = {"allam-2-7b"}


def build(spec: str):
    """One provider from "vendor:model", using that vendor's key from the environment."""
    vendor, model = spec.split(":", 1)
    vendor = vendor.strip().lower()
    key = {"gemini": "GEMINI_API_KEY", "groq": "GROQ_API_KEY", "claude": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY",
           "cerebras": "CEREBRAS_API_KEY", "openrouter": "OPENROUTER_API_KEY", "mistral": "MISTRAL_API_KEY", "github": "GITHUB_MODELS_TOKEN",
           "openai-sdk": "OPENAI_SDK_KEY", "claude-sdk": "ANTHROPIC_API_KEY"}.get(vendor)
    if vendor == "vertex":
        return VertexGemini(os.environ["VERTEX_PROJECT"], os.getenv("VERTEX_REGION", "us-central1"), model) if os.getenv("VERTEX_PROJECT") else None
    if key is None or not os.getenv(key):
        return None
    k = os.environ[key]
    if vendor == "gemini":
        return Gemini(k, model)
    if vendor == "openai-sdk":
        from .sdk import OpenAISDK
        return OpenAISDK(k, model, os.getenv("OPENAI_SDK_BASE_URL"))
    if vendor == "claude-sdk":
        from .sdk import ClaudeSDK
        return ClaudeSDK(k, model)
    if vendor == "claude":
        return Claude(k, model)
    base = {"groq": "https://api.groq.com/openai/v1", "openai": "https://api.openai.com/v1", "cerebras": "https://api.cerebras.ai/v1",
            "openrouter": "https://openrouter.ai/api/v1", "mistral": "https://api.mistral.ai/v1", "github": "https://models.github.ai/inference"}[vendor]
    return OpenAICompatible(k, model, base, vendor, supports_tools=model not in CHAT_ONLY)


def from_env(on_switch=None) -> Fallback:
    """The provider chain: WAKEEL_MODELS ("vendor:model,vendor:model,...") or the
    default chain, keeping the models whose vendor has a key configured.
    Vendors: gemini, groq, claude, openai, cerebras, openrouter, mistral, github."""
    specs = [x.strip() for x in os.getenv("WAKEEL_MODELS", "").split(",") if x.strip()] or DEFAULT_CHAIN
    ps = [p for p in (build(x) for x in specs) if p is not None]
    if not os.getenv("WAKEEL_MODELS"):           # paid vendors only when their keys are set
        ps += [p for p in (build("claude:claude-haiku-4-5-20251001"), build("openai:gpt-4.1-mini")) if p is not None]
    return Fallback(ps, on_switch)
