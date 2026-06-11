"""Dashboard precompute: donut math, event-time parsing, ages, fragment render."""
from datetime import datetime, timedelta, timezone

import pytest

from routes.home import _donut_segments, _parse_event_time, _pct, _relative_age

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


class TestDonutSegments:
    def test_zero_total_returns_no_segments(self):
        assert _donut_segments([(3, "#fff")], 0) == []
        assert _donut_segments([], 0) == []

    def test_segments_math(self):
        segs = _donut_segments(
            [(5, "green"), (3, "red"), (2, "grey")], 10)
        assert [s["color"] for s in segs] == ["green", "red", "grey"]
        # r=15.9155 trick: dash lengths are percentages.
        assert segs[0]["dash"] == "50.00 50.00"
        assert segs[1]["dash"] == "30.00 70.00"
        assert segs[2]["dash"] == "20.00 80.00"
        # First segment starts at 12 o'clock (offset 25), then shifts by the
        # cumulative percentage already drawn.
        assert segs[0]["offset"] == "25.00"
        assert segs[1]["offset"] == "-25.00"
        assert segs[2]["offset"] == "-55.00"

    def test_zero_count_segments_skipped(self):
        segs = _donut_segments([(10, "green"), (0, "red")], 10)
        assert len(segs) == 1
        assert segs[0]["dash"] == "100.00 0.00"

    def test_dash_pairs_sum_to_100(self):
        for seg in _donut_segments([(1, "a"), (2, "b"), (4, "c")], 7):
            a, b = (float(x) for x in seg["dash"].split())
            assert a + b == pytest.approx(100.0)


class TestParseEventTime:
    def test_iso_with_z(self):
        dt = _parse_event_time("2026-06-01T11:55:00Z")
        assert dt == datetime(2026, 6, 1, 11, 55, tzinfo=timezone.utc)

    def test_iso_with_offset_normalised_to_utc(self):
        dt = _parse_event_time("2026-06-01T06:55:00-05:00")
        assert dt == datetime(2026, 6, 1, 11, 55, tzinfo=timezone.utc)

    def test_naive_iso_assumed_utc(self):
        dt = _parse_event_time("2026-06-01 11:55:00")
        assert dt.tzinfo is not None
        assert dt == datetime(2026, 6, 1, 11, 55, tzinfo=timezone.utc)

    def test_epoch_seconds(self):
        ts = int(NOW.timestamp())
        assert _parse_event_time(ts) == NOW

    def test_epoch_milliseconds(self):
        ts_ms = int(NOW.timestamp() * 1000)
        assert _parse_event_time(ts_ms) == NOW

    def test_epoch_string(self):
        assert _parse_event_time(str(int(NOW.timestamp()))) == NOW

    @pytest.mark.parametrize("garbage", [
        None, "", "not-a-date", "2026-13-45T99:99:99Z", float("inf"),
    ])
    def test_garbage_returns_none(self, garbage):
        assert _parse_event_time(garbage) is None


class TestRelativeAge:
    @pytest.mark.parametrize("delta,expected", [
        (timedelta(seconds=10), "just now"),
        (timedelta(minutes=4), "4m ago"),
        (timedelta(minutes=59), "59m ago"),
        (timedelta(hours=3), "3h ago"),
        (timedelta(hours=23), "23h ago"),
        (timedelta(days=2), "2d ago"),
    ])
    def test_buckets(self, delta, expected):
        assert _relative_age(NOW - delta, NOW) == expected

    def test_none_timestamp_is_blank(self):
        assert _relative_age(None, NOW) == ""

    def test_future_timestamp_clamped_to_just_now(self):
        assert _relative_age(NOW + timedelta(minutes=5), NOW) == "just now"


class TestPct:
    @pytest.mark.parametrize("part,whole,expected", [
        (1, 4, 25), (1, 3, 33), (2, 3, 67), (0, 10, 0), (5, 0, 0), (10, 10, 100),
    ])
    def test_rounding(self, part, whole, expected):
        assert _pct(part, whole) == expected


class TestDashboardRender:
    def test_full_page_stats(self, client, mock_central, stub_db):
        r = client.get("/")
        assert r.status_code == 200
        # 5 mocked devices, 3 online; both donut legend and counts render.
        assert 'id="dashboard-live"' in r.text
        assert "Network Dashboard" in r.text

    def test_fragment_is_isolated(self, client, mock_central, stub_db):
        r = client.get("/?partial=1")
        assert r.status_code == 200
        body = r.text
        # The live block only — no document chrome, no nav, no polling wrapper.
        assert "<html" not in body
        assert "<title>" not in body
        assert 'id="dashboard-live"' not in body
        assert "Total Devices" in body

    def test_fragment_and_full_page_share_content(self, client, mock_central,
                                                  stub_db):
        fragment = client.get("/?partial=1").text
        full = client.get("/").text
        # The same stat-card labels appear in both (single-template fragments).
        for marker in ("Total Devices", "Online", "Clients"):
            assert marker in fragment and marker in full

    def test_dashboard_survives_total_backend_failure(self, client, monkeypatch,
                                                      stub_db):
        from vendors.aruba_central import aruba
        from vendors import central_bridge as cb

        async def no_devices():
            return []

        async def boom(*a, **k):
            raise RuntimeError("down")

        monkeypatch.setattr(aruba, "get_devices", no_devices)
        monkeypatch.setattr(aruba, "get_clients", no_devices)
        monkeypatch.setattr(cb, "get_sites", boom)
        r = client.get("/")
        assert r.status_code == 200  # zero-division guards hold
