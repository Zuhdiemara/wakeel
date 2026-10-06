"""Each provider adapter speaks its provider's wire format, including tool calls."""
import json

import httpx
import pytest

from wakeel import llm

TOOLS = [{"name": "list_transactions", "description": "d", "parameters": {"type": "object", "properties": {}}}]
CONVO = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"},
         {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": "list_transactions", "args": {"days": 7}}]},
         {"role": "tool", "tool_call_id": "c1", "name": "list_transactions", "content": json.dumps({"transactions": []})}]


@pytest.fixture
def wire(monkeypatch):
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen.update(url=url, headers=headers, body=json)
        return httpx.Response(200, json=seen["answer"], request=httpx.Request("POST", url))

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    return seen


def test_gemini(wire):
    wire["answer"] = {"candidates": [{"content": {"parts": [{"functionCall": {"name": "list_transactions", "args": {"days": 30}}}]}}],
                      "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 3}}
    r = llm.Gemini("k").chat(CONVO, TOOLS)
    assert r.tool_calls[0].name == "list_transactions" and r.tool_calls[0].args == {"days": 30} and r.tokens_in == 10
    b = wire["body"]
    assert b["systemInstruction"]["parts"][0]["text"] == "sys" and "functionResponse" in b["contents"][-1]["parts"][0]
    assert wire["headers"]["x-goog-api-key"] == "k"


def test_openai_compatible(wire):
    wire["answer"] = {"choices": [{"message": {"content": None, "tool_calls": [{"id": "x", "type": "function", "function": {"name": "list_transactions", "arguments": "{\"days\": 7}"}}]}}],
                      "usage": {"prompt_tokens": 5, "completion_tokens": 2}}
    r = llm.OpenAICompatible("k", "m", "https://api.groq.com/openai/v1", "groq").chat(CONVO, TOOLS)
    assert r.tool_calls[0].args == {"days": 7}
    msgs = wire["body"]["messages"]
    assert msgs[2]["tool_calls"][0]["function"]["arguments"] == "{\"days\": 7}" and msgs[3]["role"] == "tool"


def test_claude(wire):
    wire["answer"] = {"content": [{"type": "text", "text": "ok"}, {"type": "tool_use", "id": "t", "name": "list_transactions", "input": {}}],
                      "usage": {"input_tokens": 9, "output_tokens": 4}}
    r = llm.Claude("k").chat(CONVO, TOOLS)
    assert r.text == "ok" and r.tool_calls[0].name == "list_transactions"
    b = wire["body"]
    assert b["system"] == "sys" and b["messages"][-1]["content"][0]["type"] == "tool_result"


def test_rate_limits_are_retryable_and_bad_requests_are_not(monkeypatch):
    for code, retryable in ((429, True), (503, True), (400, False)):
        monkeypatch.setattr(llm.httpx, "post", lambda *a, code=code, **k: httpx.Response(code, text="x", request=httpx.Request("POST", "u")))
        with pytest.raises(llm.LLMError) as e:
            llm.Gemini("k").chat([{"role": "user", "content": "hi"}])
        assert e.value.retryable is retryable
