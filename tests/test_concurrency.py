import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from quantdeck.api.jobs import JobManager, QueueFull
from quantdeck.live.runlock import AlreadyRunning, RunLock
from quantdeck.ratelimit import Throttled, TokenBucket, retry


def test_token_bucket_caps_rate_across_threads():
    bucket = TokenBucket(rate=50, capacity=5)
    calls = []

    def work():
        for _ in range(10):
            bucket.acquire()
            calls.append(time.monotonic())

    t0 = time.monotonic()
    threads = [threading.Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.monotonic() - t0
    # 40 calls, 5 free from the burst, the rest at 50/s -> at least ~0.7s
    assert len(calls) == 40
    assert elapsed >= 0.6


def test_throttled_proxy_passes_through():
    class Client:
        value = 3

        def ping(self, x):
            return x * 2

    c = Throttled(Client(), TokenBucket(1000))
    assert c.ping(2) == 4 and c.value == 3


def test_retry_only_on_transient():
    n = {"calls": 0}

    class NetworkError(Exception):
        pass

    def flaky():
        n["calls"] += 1
        if n["calls"] < 3:
            raise NetworkError("timeout")
        return "ok"

    assert retry(flaky, lambda e: isinstance(e, NetworkError), base_delay=0.001) == "ok"
    with pytest.raises(ValueError):
        retry(lambda: (_ for _ in ()).throw(ValueError("bad")), lambda e: False)


def test_run_lock(tmp_path):
    lock = RunLock(tmp_path / "run.lock")
    lock.acquire()
    other = RunLock(tmp_path / "run.lock")
    # same pid holds it, so simulate another process by writing a live foreign pid
    (tmp_path / "run.lock").write_text("1")
    with pytest.raises(AlreadyRunning):
        other.acquire()
    lock.release()


def test_run_lock_replaces_stale(tmp_path):
    (tmp_path / "run.lock").write_text("999999999")
    with RunLock(tmp_path / "run.lock"):
        assert (tmp_path / "run.lock").exists()
    assert not (tmp_path / "run.lock").exists()


def slow_job(cfg):
    time.sleep(cfg.get("sleep", 0.05))
    return {"echo": cfg["n"]}


def test_jobs_dedupe_and_queue_limit():
    jm = JobManager(executor=ThreadPoolExecutor(2), max_pending=3, func=slow_job)
    a = jm.submit({"n": 1, "sleep": 0.3})
    b = jm.submit({"n": 1, "sleep": 0.3})
    assert a["id"] == b["id"]  # same request shares a job
    jm.submit({"n": 2, "sleep": 0.3})
    jm.submit({"n": 3, "sleep": 0.3})
    with pytest.raises(QueueFull):
        jm.submit({"n": 4, "sleep": 0.3})
    deadline = time.time() + 5
    while jm.get(a["id"])["status"] != "done" and time.time() < deadline:
        time.sleep(0.02)
    assert jm.get(a["id"])["result"] == {"echo": 1}
    jm.shutdown()


def test_jobs_many_concurrent_submitters():
    jm = JobManager(executor=ThreadPoolExecutor(4), max_pending=100, func=slow_job)
    with ThreadPoolExecutor(16) as pool:
        ids = list(pool.map(lambda i: jm.submit({"n": i % 5, "sleep": 0.01})["id"], range(200)))
    assert len(set(ids)) == 5
    jm.shutdown()
