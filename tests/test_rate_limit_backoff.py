"""The portal-side guard on centralmcp's 429 backoff floor.

centralmcp is mounted read-only and upstream force-pushes main, so the patch in
pipeline/clients/central_client.py can disappear on any update. main.py rebinds
the constant at startup; these tests pin that behaviour.
"""
import sys
import types

import pytest

import main


@pytest.fixture
def fake_central_client(monkeypatch):
    """Install a stub pipeline.clients.central_client importable by main.py."""
    mod = types.ModuleType("pipeline.clients.central_client")
    pkg = types.ModuleType("pipeline")
    clients = types.ModuleType("pipeline.clients")
    clients.central_client = mod
    pkg.clients = clients
    monkeypatch.setitem(sys.modules, "pipeline", pkg)
    monkeypatch.setitem(sys.modules, "pipeline.clients", clients)
    monkeypatch.setitem(sys.modules, "pipeline.clients.central_client", mod)
    return mod


def test_lowers_the_upstream_60s_floor(fake_central_client, monkeypatch):
    monkeypatch.delenv("CENTRAL_RATE_LIMIT_INITIAL_DELAY", raising=False)
    fake_central_client._INITIAL_RETRY_DELAY = 60

    main._tame_centralmcp_rate_limit_backoff()

    assert fake_central_client._INITIAL_RETRY_DELAY == 5


def test_respects_the_env_override(fake_central_client, monkeypatch):
    monkeypatch.setenv("CENTRAL_RATE_LIMIT_INITIAL_DELAY", "2")
    fake_central_client._INITIAL_RETRY_DELAY = 60

    main._tame_centralmcp_rate_limit_backoff()

    assert fake_central_client._INITIAL_RETRY_DELAY == 2


def test_does_not_raise_an_already_low_value(fake_central_client, monkeypatch):
    """A patched checkout already sits at 5 — leave it alone."""
    monkeypatch.delenv("CENTRAL_RATE_LIMIT_INITIAL_DELAY", raising=False)
    fake_central_client._INITIAL_RETRY_DELAY = 1

    main._tame_centralmcp_rate_limit_backoff()

    assert fake_central_client._INITIAL_RETRY_DELAY == 1


def test_warns_but_survives_if_upstream_drops_the_symbol(
    fake_central_client, monkeypatch, caplog
):
    monkeypatch.delenv("CENTRAL_RATE_LIMIT_INITIAL_DELAY", raising=False)
    # No _INITIAL_RETRY_DELAY attribute at all.
    main._tame_centralmcp_rate_limit_backoff()

    assert "no longer applies" in caplog.text


def test_survives_centralmcp_being_absent(monkeypatch):
    """The portal must still boot without the /centralmcp mount."""
    for name in ("pipeline", "pipeline.clients", "pipeline.clients.central_client"):
        monkeypatch.setitem(sys.modules, name, None)

    main._tame_centralmcp_rate_limit_backoff()  # must not raise
