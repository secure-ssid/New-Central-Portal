"""Schema migration runner against a fake connection (no Postgres).

Covers: fresh database, stamp-existing (pre-migration deployments),
incremental upgrade, failed-migration-not-stamped, and MIGRATIONS validation.
"""
from contextlib import contextmanager

import pytest

import db as db_module


class FakeCursor:
    def __init__(self, sink, fake):
        self._sink = sink
        self._fake = fake

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        if self._fake.fail_marker and self._fake.fail_marker in sql:
            raise RuntimeError("migration blew up (test)")
        self._sink.append((sql, params))


class FakeConn:
    def __init__(self, sink, fake):
        self._sink = sink
        self._fake = fake

    def cursor(self, **kwargs):
        return FakeCursor(self._sink, self._fake)


class FakePG:
    """Mimics the db.py helpers run_migrations relies on, with real
    transaction semantics: statements only take effect on commit."""

    def __init__(self, applied_versions=(), baseline_exists=False):
        self.versions = set(applied_versions)
        self.baseline_exists = baseline_exists
        self.committed: list[tuple] = []   # (sql, params) actually committed
        self.fail_marker: str | None = None

    # -- db.execute (autocommit single statement) ------------------------
    def execute(self, sql, params=None):
        self._apply([(sql, params)])

    # -- db.fetchone -------------------------------------------------------
    def fetchone(self, sql, params=None):
        if "MAX(version)" in sql:
            return {"version": max(self.versions) if self.versions else None}
        if "to_regclass" in sql:
            return {"reg": "alert_settings" if self.baseline_exists else None}
        raise AssertionError(f"unexpected fetchone: {sql}")

    # -- db.get_conn (transactional) ----------------------------------------
    @contextmanager
    def get_conn(self):
        txn: list[tuple] = []
        yield FakeConn(txn, self)
        # Only reached on success — mirror get_conn()'s commit-on-success.
        self._apply(txn)

    # ---------------------------------------------------------------------
    def _apply(self, statements):
        for sql, params in statements:
            self.committed.append((sql, params))
            if sql == db_module._STAMP_SQL:
                self.versions.add(params[0])

    @property
    def committed_sql(self):
        return [sql for sql, _ in self.committed]


@pytest.fixture
def fake_pg_factory(monkeypatch):
    def make(applied_versions=(), baseline_exists=False):
        fake = FakePG(applied_versions, baseline_exists)
        monkeypatch.setattr(db_module, "execute", fake.execute)
        monkeypatch.setattr(db_module, "fetchone", fake.fetchone)
        monkeypatch.setattr(db_module, "get_conn", fake.get_conn)
        return fake
    return make


MIG2_SQL = "CREATE TABLE IF NOT EXISTS widgets (id SERIAL PRIMARY KEY)"


class TestFreshDatabase:
    def test_baseline_applied_and_stamped(self, fake_pg_factory):
        fake = fake_pg_factory()
        db_module.run_migrations()
        assert db_module.SCHEMA_SQL in fake.committed_sql
        assert fake.versions == {1}
        # schema_version table is always ensured first.
        assert fake.committed_sql[0] == db_module.SCHEMA_VERSION_SQL

    def test_rerun_is_a_noop(self, fake_pg_factory):
        fake = fake_pg_factory()
        db_module.run_migrations()
        before = list(fake.committed_sql)
        db_module.run_migrations()
        # Second run only re-ensures schema_version; baseline not re-applied.
        assert fake.committed_sql.count(db_module.SCHEMA_SQL) == 1
        assert len(fake.committed_sql) == len(before) + 1


class TestStampExisting:
    def test_existing_schema_stamped_without_rerunning(self, fake_pg_factory):
        fake = fake_pg_factory(baseline_exists=True)
        db_module.run_migrations()
        assert fake.versions == {1}
        assert db_module.SCHEMA_SQL not in fake.committed_sql

    def test_stamp_existing_covers_every_version(self, fake_pg_factory,
                                                 monkeypatch):
        monkeypatch.setattr(db_module, "MIGRATIONS",
                            [(1, db_module.SCHEMA_SQL), (2, MIG2_SQL)])
        fake = fake_pg_factory(baseline_exists=True)
        db_module.run_migrations()
        assert fake.versions == {1, 2}
        assert MIG2_SQL not in fake.committed_sql


class TestIncremental:
    def test_only_pending_migrations_run(self, fake_pg_factory, monkeypatch):
        monkeypatch.setattr(db_module, "MIGRATIONS",
                            [(1, db_module.SCHEMA_SQL), (2, MIG2_SQL)])
        fake = fake_pg_factory(applied_versions=(1,))
        db_module.run_migrations()
        assert MIG2_SQL in fake.committed_sql
        assert db_module.SCHEMA_SQL not in fake.committed_sql  # 1 not re-run
        assert fake.versions == {1, 2}

    def test_callable_migration_receives_connection(self, fake_pg_factory,
                                                    monkeypatch):
        ran: list = []

        def mig(conn):
            ran.append(conn)
            with conn.cursor() as cur:
                cur.execute("SELECT 'callable migration ran'")

        monkeypatch.setattr(db_module, "MIGRATIONS",
                            [(1, db_module.SCHEMA_SQL), (2, mig)])
        fake = fake_pg_factory(applied_versions=(1,))
        db_module.run_migrations()
        assert len(ran) == 1
        assert "SELECT 'callable migration ran'" in fake.committed_sql
        assert fake.versions == {1, 2}


class TestFailureHandling:
    def test_failed_migration_is_not_stamped(self, fake_pg_factory, monkeypatch):
        monkeypatch.setattr(db_module, "MIGRATIONS",
                            [(1, db_module.SCHEMA_SQL), (2, MIG2_SQL)])
        fake = fake_pg_factory(applied_versions=(1,))
        fake.fail_marker = "widgets"
        with pytest.raises(RuntimeError):
            db_module.run_migrations()
        # Neither the migration nor its stamp was committed.
        assert MIG2_SQL not in fake.committed_sql
        assert fake.versions == {1}

    def test_failure_then_retry_succeeds(self, fake_pg_factory, monkeypatch):
        monkeypatch.setattr(db_module, "MIGRATIONS",
                            [(1, db_module.SCHEMA_SQL), (2, MIG2_SQL)])
        fake = fake_pg_factory(applied_versions=(1,))
        fake.fail_marker = "widgets"
        with pytest.raises(RuntimeError):
            db_module.run_migrations()
        fake.fail_marker = None
        db_module.run_migrations()
        assert fake.versions == {1, 2}

    def test_earlier_failure_blocks_later_migrations(self, fake_pg_factory,
                                                     monkeypatch):
        mig3 = "CREATE TABLE IF NOT EXISTS gadgets (id SERIAL PRIMARY KEY)"
        monkeypatch.setattr(db_module, "MIGRATIONS",
                            [(1, db_module.SCHEMA_SQL), (2, MIG2_SQL), (3, mig3)])
        fake = fake_pg_factory(applied_versions=(1,))
        fake.fail_marker = "widgets"
        with pytest.raises(RuntimeError):
            db_module.run_migrations()
        assert mig3 not in fake.committed_sql
        assert fake.versions == {1}


class TestMigrationsValidation:
    @pytest.mark.parametrize("bad", [
        [(2, "SQL"), (1, "SQL")],   # not increasing
        [(1, "SQL"), (1, "SQL")],   # duplicate
        [(0, "SQL")],               # < 1
        [(-3, "SQL")],
    ])
    def test_bad_version_lists_rejected(self, fake_pg_factory, monkeypatch, bad):
        fake_pg_factory()
        monkeypatch.setattr(db_module, "MIGRATIONS", bad)
        with pytest.raises(ValueError):
            db_module.run_migrations()

    def test_shipped_migrations_list_is_valid(self):
        versions = [v for v, _ in db_module.MIGRATIONS]
        assert versions == sorted(set(versions))
        assert all(v >= 1 for v in versions)
