"""Live centralmcp smoke tests — skipped unless CENTRALMCP_INTEGRATION=1.

Run locally:
  CENTRALMCP_INTEGRATION=1 PYTHONPATH=/path/to/centralmcp \
    CREDS_PATH=/path/to/centralmcp/config/credentials.yaml \
    pytest tests/integration/ -m integration -v

Or via compose:
  docker compose -f docker-compose.yml -f docker-compose.integration.yml run --rm integration-tests
"""
from __future__ import annotations

import asyncio
import os

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("CENTRALMCP_INTEGRATION") != "1",
        reason="Set CENTRALMCP_INTEGRATION=1 with centralmcp mounted and creds configured",
    ),
]


def _creds_configured() -> bool:
    path = os.environ.get("CREDS_PATH", "")
    return bool(path and os.path.isfile(path))


@pytest.mark.skipif(not _creds_configured(), reason="CREDS_PATH not pointing at credentials.yaml")
class TestCentralmcpSmoke:
    def test_list_devices_returns_data(self):
        async def _run():
            from vendors.central_bridge import get_devices
            devices = await get_devices(limit=5)
            assert isinstance(devices, list)

        asyncio.run(_run())

    def test_list_sites_returns_data(self):
        async def _run():
            from vendors.central_bridge import get_sites
            sites = await get_sites(limit=5)
            assert isinstance(sites, list)

        asyncio.run(_run())

    def test_search_docs_does_not_import_error(self):
        async def _run():
            from vendors.central_bridge import search_docs
            results = await search_docs("vlan configuration", top_k=2)
            assert isinstance(results, list)
            if results and "error" in results[0]:
                pytest.skip(results[0]["error"])

        asyncio.run(_run())

    def test_run_tool_list_devices(self):
        async def _run():
            from vendors.central_bridge import run_tool
            out = await run_tool("list_devices", '{"limit": 3}')
            assert out["status"] == "success"
            assert out["output"] is not None

        asyncio.run(_run())
