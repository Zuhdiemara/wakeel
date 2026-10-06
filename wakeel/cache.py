"""A semantic cache for policy answers.

Policy answers depend only on the question and the policy documents, not on
the customer, so a question that means the same as a recent one (cosine
similarity above a threshold, same language) reuses the cited answer without a
model call. Anything account-specific (transactions, refunds) is never cached.
The cache is keyed to a hash of the corpus, so editing a policy empties it.
"""
from __future__ import annotations

import threading
import time

import numpy as np


class SemanticCache:
    def __init__(self, embedder, corpus_version: str, threshold: float = 0.92, ttl: float = 24 * 3600, size: int = 2000):
        self.embedder, self.version, self.threshold, self.ttl, self.size = embedder, corpus_version, threshold, ttl, size
        self.items: list[tuple[str, np.ndarray, dict, float]] = []   # (lang, vector, answer, time)
        self.lock = threading.Lock()
        self.hits = self.misses = 0

    def get(self, text: str, lang: str) -> tuple[dict, float] | None:
        q = self.embedder.embed([text])[0]
        now = time.time()
        with self.lock:
            self.items = [x for x in self.items if now - x[3] < self.ttl]
            best, score = None, -1.0
            for l, v, ans, _ in self.items:
                if l == lang and (s := float(v @ q)) > score:
                    best, score = ans, s
            if best is not None and score >= self.threshold:
                self.hits += 1
                return best, score
            self.misses += 1
            return None

    def put(self, text: str, lang: str, answer: dict) -> None:
        v = self.embedder.embed([text])[0]
        with self.lock:
            self.items.append((lang, v, answer, time.time()))
            del self.items[:-self.size]
