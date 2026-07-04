"""Unit tests for central_bridge helpers (no live centralmcp required)."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from vendors import central_bridge as cb


def test_run_awaits_coroutine_functions():
    async def _exercise():
        async def async_add(a, b):
            return a + b

        return await cb._run(async_add, 2, 3)

    assert asyncio.run(_exercise()) == 5


def test_run_executes_sync_functions():
    async def _exercise():
        def sync_mul(a, b):
            return a * b

        return await cb._run(sync_mul, 3, 4)

    assert asyncio.run(_exercise()) == 12


def test_unwrap_list_and_bounded_dict():
    assert cb._unwrap([{"a": 1}]) == [{"a": 1}]
    assert cb._unwrap({"items": [{"b": 2}], "_pagination": {}}) == [{"b": 2}]
    assert cb._unwrap(None) == []


def test_ops_error_shape():
    err = cb._ops_error("nope")
    assert err == {"status": None, "errors": ["nope"]}


def test_run_show_rejects_access_points(monkeypatch):
    monkeypatch.setattr(cb, "_resolve_troubleshoot_type", lambda serial, dtype: "aps")

    async def _exercise():
        return await cb.run_show("AP1", "access_point", ["show version"])

    result = asyncio.run(_exercise())
    assert result["errors"] == [
        "Show commands are not supported on Access Points via the Central troubleshooting API."
    ]


def test_run_ping_rejects_access_points(monkeypatch):
    monkeypatch.setattr(cb, "_resolve_troubleshoot_type", lambda serial, dtype: "aps")

    async def _exercise():
        return await cb.run_ping("AP1", "access_point", "8.8.8.8")

    result = asyncio.run(_exercise())
    assert "not supported on Access Points" in result["errors"][0]
