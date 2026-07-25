"""Connection-pool construction (no real Postgres).

The pool is reached from the uvicorn event loop, the starlette threadpool and
three APScheduler threads, so it must be the thread-safe variant and must be
built exactly once.
"""
import threading
import time

import db as db_module


class _SentinelPool:
    instances = 0

    def __init__(self, minconn, maxconn, **dsn):
        type(self).instances += 1
        self.minconn, self.maxconn, self.dsn = minconn, maxconn, dsn

    def closeall(self):
        pass


def test_pool_is_the_thread_safe_variant(monkeypatch):
    monkeypatch.setattr(db_module.pool, "ThreadedConnectionPool", _SentinelPool)
    monkeypatch.setattr(db_module, "_pool", None)
    created = db_module.get_pool()
    try:
        assert isinstance(created, _SentinelPool)
        assert created.maxconn >= 10, "too few connections for loop + threadpool + 3 jobs"
    finally:
        monkeypatch.setattr(db_module, "_pool", None)


def test_get_pool_creates_exactly_one_pool_under_concurrency(monkeypatch):
    class _SlowPool(_SentinelPool):
        def __init__(self, *a, **k):
            time.sleep(0.01)  # widen the check-then-set race window
            super().__init__(*a, **k)

    _SlowPool.instances = 0
    monkeypatch.setattr(db_module.pool, "ThreadedConnectionPool", _SlowPool)
    monkeypatch.setattr(db_module, "_pool", None)
    try:
        threads = [threading.Thread(target=db_module.get_pool) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert _SlowPool.instances == 1, (
            f"{_SlowPool.instances} pools built — every extra one is a leaked pool"
        )
    finally:
        monkeypatch.setattr(db_module, "_pool", None)
