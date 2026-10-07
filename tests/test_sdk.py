"""The SDK adapters and Vertex speak the right wire format (mocked transport)."""
import json

import httpx

from wakeel import llm
from wakeel.sdk import ClaudeSDK, OpenAISDK

TOOLS = [{"name": "list_transactions", "description": "d", "parameters": {"type": "object", "properties": {}}}]
MSGS = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]


def transport(answer, seen, lib=httpx):
    """A mock HTTP client from the library the SDK uses (the Anthropic SDK has moved to httpx2)."""
    def handler(req):
        seen.update(url=str(req.url), body=json.loads(req.content), headers=dict(req.headers))
        return lib.Response(200, json=answer)
    return lib.Client(transport=lib.MockTransport(handler))


def test_openai_sdk_tool_call_through_the_official_client():
    seen = {}
    ans = {"id": "c", "object": "chat.completion", "created": 1, "model": "m",
           "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {"role": "assistant", "content": None,
                        "tool_calls": [{"id": "t1", "type": "function", "function": {"name": "list_transactions", "arguments": "{\"days\": 7}"}}]}}],
           "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}}
    p = OpenAISDK("k", "openai/gpt-oss-20b", "https://api.groq.com/openai/v1", http_client=transport(ans, seen))
    r = p.chat(MSGS, TOOLS)
    assert r.tool_calls[0].args == {"days": 7} and r.tokens_in == 5
    assert seen["url"] == "https://api.groq.com/openai/v1/chat/completions" and seen["body"]["tools"][0]["function"]["name"] == "list_transactions"


def test_claude_sdk_tool_call_through_the_official_client():
    seen = {}
    ans = {"id": "m", "type": "message", "role": "assistant", "model": "claude-haiku-4-5-20251001", "stop_reason": "tool_use",
           "content": [{"type": "tool_use", "id": "t", "name": "list_transactions", "input": {}}], "usage": {"input_tokens": 9, "output_tokens": 4}}
    import httpx2
    p = ClaudeSDK("k", http_client=transport(ans, seen, httpx2))
    r = p.chat(MSGS, TOOLS)
    assert r.tool_calls[0].name == "list_transactions" and r.tokens_in == 9
    assert seen["url"].endswith("/v1/messages") and seen["body"]["system"] == "sys"


def test_vertex_uses_the_regional_endpoint_and_a_bearer_token(monkeypatch):
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen.update(url=url, headers=headers)
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}, request=httpx.Request("POST", url))

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    r = llm.VertexGemini("my-project", "me-central2", token="ya29.test").chat(MSGS)
    assert r.text == "ok"
    assert seen["url"].startswith("https://me-central2-aiplatform.googleapis.com/v1/projects/my-project/locations/me-central2/")
    assert seen["headers"] == {"Authorization": "Bearer ya29.test"}
