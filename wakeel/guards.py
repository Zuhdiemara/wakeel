"""Input guards: PII redaction (PDPL) and a prompt-injection screen.

Screens are a first line only. The real defences are structural (see
tools.py): the model never chooses whose account it acts on, tools can only
propose a refund, and money moves only after a human approves.
"""
from __future__ import annotations

import re

from .text import normalise

_CARD = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")
_IBAN = re.compile(r"\bSA\d{2}(?:\s?[0-9A-Z]{4}){5}\b", re.I)
_NATIONAL_ID = re.compile(r"\b[12]\d{9}\b")          # Saudi ID (1...) or Iqama (2...)
_PHONE = re.compile(r"(?:\+?966|0)5\d{8}\b")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")


def _luhn(digits: str) -> bool:
    s, alt = 0, False
    for d in reversed(digits):
        n = int(d)
        if alt:
            n = n * 2 - 9 if n > 4 else n * 2
        s, alt = s + n, not alt
    return s % 10 == 0


def redact(text: str) -> tuple[str, list[str]]:
    """Mask personal data before anything reaches a model or a log. Card
    numbers keep their last four digits, which the agent needs to match a
    transaction; everything else is replaced by its kind."""
    found: list[str] = []
    text = normalise_digits(text)

    def card(m):
        d = re.sub(r"\D", "", m.group())
        if 13 <= len(d) <= 19 and _luhn(d):
            found.append("card")
            return f"[card ••{d[-4:]}]"
        return m.group()

    text = _CARD.sub(card, text)
    for kind, rx in (("iban", _IBAN), ("national_id", _NATIONAL_ID), ("phone", _PHONE), ("email", _EMAIL)):
        text, n = rx.subn(f"[{kind}]", text)
        found += [kind] * n
    return text, found


def normalise_digits(s: str) -> str:
    return s.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))


_INJECTION = [
    r"ignore (all |any )?(previous|prior|above|earlier) (instructions|rules|messages)",
    r"disregard (the |your )?(instructions|rules|policy|system)",
    r"(reveal|print|show|repeat) (your |the )?(system prompt|instructions|hidden)",
    r"you are now|act as (the )?(admin|developer|system)|developer mode|jailbreak",
    r"(approve|issue|send|transfer) (the )?(refund|money).{0,30}(without|no) (approval|review|human)",
    r"refund (all|every|each) (transaction|payment|customer)",
    r"(account|customer) (id|number)?\s*[:=]",
    r"تجاهل (كل |جميع )?(التعليمات|الاوامر|القواعد)",
    r"(اكشف|اعرض|اطبع) (التعليمات|موجه النظام)",
    r"انت الان|بدون (موافقه|مراجعه)",
]
_INJECTION_RX = [re.compile(p) for p in _INJECTION]


def injection_signals(text: str) -> list[str]:
    t = normalise(text)
    return [p.pattern for p in _INJECTION_RX if p.search(t)]


_LEAK = re.compile(r"(you are the operations agent|treat the message as data|return json|system prompt|write a short, warm reply)", re.I)


def leaks_instructions(text: str) -> bool:
    """True if a reply echoes our own instructions (a sign of prompt leaking)."""
    return bool(_LEAK.search(text))
