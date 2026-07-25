"""Scheduler leader election.

The portal now runs multiple uvicorn workers. The background jobs send alert
email and hit the Central API, so exactly one worker may run them — otherwise
alerts duplicate and upstream load multiplies, which is what the response cache
exists to prevent.
"""
import pytest

import db


class _FakeCursor:
    def __init__(self, result):
        self._result = result
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return (self._result,)


class _FakeConn:
    def __init__(self, result):
        self._result = result
        self.autocommit = False
        self.closed_count = 0
        self.cursor_obj = _FakeCursor(result)

    def cursor(self):
        return self.cursor_obj

    def close(self):
        self.closed_count += 1


@pytest.fixture(autouse=True)
def _reset_lock_state():
    db._scheduler_lock_conn = None
    yield
    db._scheduler_lock_conn = None


def test_leader_acquires_and_holds_the_connection(monkeypatch):
    conn = _FakeConn(True)
    monkeypatch.setattr(db.psycopg2, "connect", lambda **kw: conn)

    assert db.try_acquire_scheduler_lock() == "acquired"
    # The lock is session-scoped, so the connection must stay open to hold it.
    assert conn.closed_count == 0
    assert conn.autocommit is True
    sql, params = conn.cursor_obj.executed[0]
    assert "pg_try_advisory_lock" in sql
    assert params == (db._SCHEDULER_LOCK_KEY,)


def test_follower_is_denied_and_releases_its_connection(monkeypatch):
    conn = _FakeConn(False)
    monkeypatch.setattr(db.psycopg2, "connect", lambda **kw: conn)

    assert db.try_acquire_scheduler_lock() == "held_by_peer"
    assert conn.closed_count == 1, "a losing worker must not leak a connection"
    assert db._scheduler_lock_conn is None


def test_only_one_of_several_workers_wins(monkeypatch):
    """Simulate Postgres: the first caller gets the lock, the rest do not."""
    granted = {"taken": False}

    def connect(**kw):
        if granted["taken"]:
            return _FakeConn(False)
        granted["taken"] = True
        return _FakeConn(True)

    monkeypatch.setattr(db.psycopg2, "connect", connect)

    results = []
    for _ in range(3):
        db._scheduler_lock_conn = None  # each worker is a fresh process
        results.append(db.try_acquire_scheduler_lock())
    assert results.count("acquired") == 1
    assert results.count("held_by_peer") == 2


def test_repeat_calls_in_the_leader_are_idempotent(monkeypatch):
    conn = _FakeConn(True)
    calls = {"n": 0}

    def connect(**kw):
        calls["n"] += 1
        return conn

    monkeypatch.setattr(db.psycopg2, "connect", connect)

    assert db.try_acquire_scheduler_lock() == "acquired"
    assert db.try_acquire_scheduler_lock() == "acquired"
    assert calls["n"] == 1, "must not open a second connection"


def test_database_down_means_no_scheduler_rather_than_a_crash(monkeypatch):
    def boom(**kw):
        raise OSError("db unreachable")

    monkeypatch.setattr(db.psycopg2, "connect", boom)

    # Must be distinguishable from "a peer has it": here NOBODY is running the
    # jobs, so main.py has to keep retrying instead of giving up for good.
    assert db.try_acquire_scheduler_lock() == "unavailable"


def test_release_closes_the_session(monkeypatch):
    conn = _FakeConn(True)
    monkeypatch.setattr(db.psycopg2, "connect", lambda **kw: conn)

    db.try_acquire_scheduler_lock()
    db.release_scheduler_lock()

    assert conn.closed_count == 1
    assert db._scheduler_lock_conn is None
    # Releasing twice must not raise.
    db.release_scheduler_lock()
