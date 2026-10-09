"""A worker process for queued cases (WAKEEL_QUEUE=1), separate from the API.

    WAKEEL_DATABASE_URL=postgresql://... python -m wakeel.worker

Run as many as the load needs: the Postgres queue gives each job to exactly
one worker (FOR UPDATE SKIP LOCKED) and retries failures with backoff.
"""
import os
import signal
import threading

os.environ.setdefault("WAKEEL_RETENTION", "0")

from .api import Worker, agent  # noqa: E402

if __name__ == "__main__":
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    threads = [threading.Thread(target=Worker(agent).run, args=(stop,)) for _ in range(int(os.getenv("WAKEEL_WORKER_THREADS", "4")))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
