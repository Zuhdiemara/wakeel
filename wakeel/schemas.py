"""Output schemas for the model's structured answers, and a small validator.

Providers that support it decode against the schema (strict structured
output); the others are asked for JSON. Either way, code validates the result
before using it: an answer that doesn't match is treated like no answer, and
the step falls back to rules. Strict mode needs every property required and
no extra properties, so the schemas are written that way.
"""
from __future__ import annotations

from typing import Any

INTENTS = ["duplicate_charge", "not_received", "wrong_amount", "cancelled_still_charged",
           "unrecognised", "fee_question", "other"]


def _obj(props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


TRIAGE = {"name": "triage", "schema": _obj({"intent": {"type": "string", "enum": INTENTS}})}

POLICY_ANSWER = {"name": "policy_answer", "schema": _obj({
    "answer": {"type": "string"},
    "citations": {"type": "array", "items": {"type": "string"}},
})}

VERDICT = {"name": "verdict", "schema": _obj({
    "verdict": {"type": "string", "enum": ["supports", "doubt"]},
    "confidence": {"type": "number"},
    "reasons": {"type": "array", "items": {"type": "string"}},
})}


def valid(value: Any, schema: dict) -> bool:
    """Checks the subset of JSON Schema used above: types, enums, required
    properties, no extra properties, array items."""
    t = schema.get("type")
    if t == "object":
        if not isinstance(value, dict):
            return False
        props = schema.get("properties", {})
        if any(k not in value for k in schema.get("required", [])):
            return False
        if schema.get("additionalProperties") is False and any(k not in props for k in value):
            return False
        return all(valid(value[k], s) for k, s in props.items() if k in value)
    if t == "array":
        return isinstance(value, list) and all(valid(v, schema.get("items", {})) for v in value)
    if t == "string":
        ok = isinstance(value, str)
    elif t == "number":
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    else:
        ok = True
    return ok and ("enum" not in schema or value in schema["enum"])
