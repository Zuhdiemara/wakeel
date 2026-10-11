#!/usr/bin/env bash
# Starts the interview demo at http://localhost:8000 with demo sign-in, a fresh
# database and demo ledger, and Groq first in the model chain (the fastest one
# measured; 16/16 decisions in CI). Keys come from .env as usual.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p .data
rm -f .data/demo.db
export WAKEEL_DEMO=1 WAKEEL_DB=.data/demo.db
export WAKEEL_MODELS="${WAKEEL_MODELS:-groq:openai/gpt-oss-120b,gemini:gemini-2.5-flash,groq:openai/gpt-oss-20b,gemini:gemini-3.5-flash-lite,gemini:gemini-3.1-flash-lite,gemini:gemma-4-26b-a4b-it,groq:allam-2-7b}"
exec .venv/bin/uvicorn wakeel.api:app --port "${PORT:-8000}"
