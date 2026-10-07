"""Loads KEY=value lines from the project's .env into the environment, without
overriding variables that are already set. Keeps keys out of the code and out
of git (.env is ignored), with no extra dependency."""
import os
from pathlib import Path


def load(path: Path | None = None) -> None:
    path = path or Path(__file__).resolve().parent.parent / ".env"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip().removeprefix("export ").strip(), value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value
