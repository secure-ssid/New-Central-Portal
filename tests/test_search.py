"""Global search: relevance, caps, and partial-source-failure degradation."""
import pytest

import search_inventory_cache as search_cache
from routes.search import (
    OVERALL_CAP,
    PER_TYPE_CAP,
    _client_status,
    build_results,
    search_alerts,
    search_devices,
    search_wlans,
)


def raw_device(i, name=None, **over):
    d = {"serialNumber": f"SER{i:03d}", "deviceName": name or f"sw-{i}",
         "deviceType": "SWITCH", "status": "Up", "ipv4": f"10.0.0.{i}",
         "macAddress": f"aa:bb:cc:00:00:{i:02x}", "model": "6300M",
         "siteName": "HQ"}
    d.update(over)
    return d


def raw_client(i, host=None, **over):
    c = {"macAddress": f"00:11:22:33:44:{i:02x}", "ipv4": f"10.0.1.{i}",
         "hostName": host or f"laptop-{i}", "status": "CONNECTED",
         "clientConnectionType": "WIRELESS", "siteName": "HQ"}
    c.update(over)
    return c


@pytest.fixture(autouse=True)
def _clear_search_cache():
    search_cache.clear_search_inventory_cache()
    yield
    search_cache.clear_search_inventory_cache()


class TestRelevance:
    def test_matches_device_name_serial_ip(self):
        devices = [raw_device(1, name="core-sw"), raw_device(2, name="edge-sw")]
        assert len(search_devices("core", devices)) == 1
        assert len(search_devices("ser002", devices)) == 1   # serial, case-folded
        assert len(search_devices("10.0.0.1", devices)) == 1
        assert search_devices("zzz-no-match", devices) == []

    def test_device_result_row_shape(self):
        rows = search_devices("core", [raw_device(1, name="core-sw")])
        assert rows == [{
            "type": "device", "label": "core-sw", "sublabel": "6300M · 10.0.0.1",
            "url": "/devices/SER001", "status": "online",
        }]

    def test_offline_device_status(self):
        rows = search_devices("sw", [raw_device(1, status="Down")])
        assert rows[0]["status"] == "offline"

    def test_non_dict_rows_ignored(self):
        assert search_devices("x", ["junk", None, 7]) == []

    @pytest.mark.parametrize("raw,expected", [
        ("CONNECTED", "online"), ("online", "online"), ("Up", "online"),
        ("disconnected", "offline"), ("FAILED", "offline"), ("", ""),
        ("weird", ""),
    ])
    def test_client_status_mapping(self, raw, expected):
        assert _client_status({"status": raw}) == expected


class TestCaps:
    def test_per_type_cap(self):
        devices = [raw_device(i, name=f"match-sw-{i}") for i in range(20)]
        assert len(search_devices("match", devices)) == PER_TYPE_CAP

    def test_overall_cap(self):
        devices = [raw_device(i, name=f"match-sw-{i}") for i in range(20)]
        clients = [raw_client(i, host=f"match-host-{i}") for i in range(20)]
        sites = [{"site_name": f"match-site-{i}"} for i in range(20)]
        results = build_results("match", devices, clients, sites)
        assert len(results) >= OVERALL_CAP
        assert len(results) <= OVERALL_CAP + 3
        assert sum(1 for r in results if r["type"] == "device") == PER_TYPE_CAP

    def test_one_source_crashing_keeps_other_results(self, monkeypatch):
        import routes.search as search_mod

        def boom(query, raw):
            raise RuntimeError("normalizer exploded")

        monkeypatch.setattr(search_mod, "search_clients", boom)
        results = search_mod.build_results(
            "match", [raw_device(1, name="match-sw")], [raw_client(1)],
            [{"site_name": "match-site"}])
        types = {r["type"] for r in results}
        assert types == {"device", "site"}


class TestApi:
    def test_basic_search(self, client, mock_central):
        results = client.get("/search/api?q=core").json()["results"]
        assert any(r["type"] == "device" and r["label"] == "core-sw-1"
                   and r["url"] == "/devices/SW1SERIAL" for r in results)

    def test_case_insensitive(self, client, mock_central):
        results = client.get("/search/api?q=CORE").json()["results"]
        assert any(r["label"] == "core-sw-1" for r in results)

    def test_client_and_site_hits(self, client, mock_central):
        results = client.get("/search/api?q=laptop").json()["results"]
        assert any(r["type"] == "client" and r["label"] == "laptop-1"
                   for r in results)
        results = client.get("/search/api?q=memphis").json()["results"]
        assert any(r["type"] == "site" and r["label"] == "HQ" and r["url"] == "/sites/101"
                   for r in results)

    def test_empty_query_returns_empty(self, client, mock_central):
        assert client.get("/search/api?q=").json() == {
            "results": [], "total_matched": 0, "has_more": False,
        }
        assert client.get("/search/api?q=%20%20").json() == {
            "results": [], "total_matched": 0, "has_more": False,
        }

    def test_short_query_returns_empty(self, client, mock_central):
        assert client.get("/search/api?q=a").json() == {
            "results": [], "total_matched": 0, "has_more": False,
        }

    def test_search_uses_inventory_cache(self, client, mock_central, monkeypatch):
        import search_inventory_cache as cache_mod

        calls = {"n": 0}
        real_fetch = cache_mod._fetch_inventory

        async def counting_fetch():
            calls["n"] += 1
            return await real_fetch()

        monkeypatch.setattr(cache_mod, "_fetch_inventory", counting_fetch)
        cache_mod.clear_search_inventory_cache()
        client.get("/search/api?q=core")
        client.get("/search/api?q=sw")
        assert calls["n"] == 1

    def test_partial_source_failure_degrades(self, client, mock_central,
                                             monkeypatch):
        from vendors import central_bridge as cb

        async def boom(*a, **k):
            raise RuntimeError("clients backend down")

        monkeypatch.setattr(cb, "get_all_clients", boom)
        r = client.get("/search/api?q=hq")
        assert r.status_code == 200
        results = r.json()["results"]
        assert any(r_["type"] == "site" for r_ in results)
        assert all(r_["type"] != "client" for r_ in results)

    def test_all_sources_failing_returns_empty_200(self, client, monkeypatch):
        from vendors import central_bridge as cb

        async def boom(*a, **k):
            raise RuntimeError("everything down")

        for fn in (
            "get_all_devices", "get_all_clients", "get_central_sites",
            "list_active_alerts", "list_wlans",
        ):
            monkeypatch.setattr(cb, fn, boom)
        r = client.get("/search/api?q=anything")
        assert r.status_code == 200
        assert r.json() == {"results": [], "total_matched": 0, "has_more": False}

    def test_search_returns_total_matched(self, client, mock_central):
        data = client.get("/search/api?q=hq").json()
        assert data["total_matched"] >= 1
        assert "has_more" in data

    def test_search_type_filter(self, client, mock_central):
        data = client.get("/search/api?q=hq&type=site").json()
        assert all(r["type"] == "site" for r in data["results"])

    def test_overlong_query_rejected_by_validation(self, client, mock_central):
        assert client.get("/search/api?q=" + "a" * 201).status_code == 422


class TestAlertsAndWlans:
    def test_search_alerts_by_title(self):
        alerts = [{"alertName": "AP Down", "severity": "critical", "deviceName": "lobby-ap"}]
        rows = search_alerts("down", alerts)
        assert len(rows) == 1
        assert rows[0]["type"] == "alert"

    def test_search_wlans_by_ssid(self):
        wlans = [{"ssid": "corp-wifi", "security": "WPA3"}]
        rows = search_wlans("corp", wlans)
        assert len(rows) == 1
        assert rows[0]["type"] == "wlan"
        assert rows[0]["url"] == "/wlans/?q=corp"
