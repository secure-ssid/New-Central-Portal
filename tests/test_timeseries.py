"""Normalisation of Central's two incompatible trend shapes.

Fixtures below are trimmed from payloads captured off the live tenant, keeping
the exact keys, casing and value types Central sends — including the
non-empty ``errors[]`` that accompanies a *successful* response.

The AP and switch fixtures deliberately describe the SAME three instants
(2026-07-25T12:30:00Z == 1784982600), which is what makes the shape tripwire
below unable to pass by accident.
"""
from datetime import datetime, timezone

import pytest

from timeseries import (
    Point,
    Series,
    canonical_key,
    deltas,
    downsample,
    envelope_error,
    error_counter_series,
    looks_like_counter,
    merge,
    normalize_device_trends,
    normalize_interface_trends,
    switch_snapshot,
    to_bits_per_second,
    to_epoch_seconds,
    to_float,
    window,
)

AP_CPU = {
    "serial_number": "PHQHKZ21HK", "metric": "cpu",
    "endpoint_used": "/network-monitoring/v1/aps/PHQHKZ21HK/cpu-utilization-trends",
    # Present on success — the earlier endpoint candidate 404'd.
    "errors": ["404 at /network-monitoring/v1alpha1/aps/PHQHKZ21HK/cpu-utilization-trends"],
    "trends": {"id": "PHQHKZ21HK", "graph": {"keys": ["cpu_utilization"], "samples": [
        {"timestamp": "2026-07-25T12:30:00Z", "data": [7]},
        {"timestamp": "2026-07-25T12:35:00Z", "data": [9]},
        {"timestamp": "2026-07-25T12:40:00Z", "data": [8]}]}},
}

SWITCH_HW = {
    "serial_number": "SG30LMR164", "metric": "cpu",
    "endpoint_used": "/network-monitoring/v1/switches/SG30LMR164/hardware-trends",
    "errors": ["404 at /network-monitoring/v1alpha1/switch/SG30LMR164/hardware-trends"],
    "trends": {"response": {"metric": "SwitchDeviceTrends",
        "keys": ["cpuUtilization", "memoryUtilization", "systemTemperature",
                 "poeAvailable", "poeConsumption", "powerConsumption",
                 "totalPowerConsumption"],
        "switchMetrics": [{"serialNumber": "SG30LMR164", "samples": [
            {"timestamp": 1784982600000, "data": ["21", "17", "25.5", "740", "84.4", "97.37", "181.77"]},
            {"timestamp": 1784982900000, "data": ["23", "17", "25.5", "740", "84.9", "97.99", "182.10"]},
            {"timestamp": 1784983200000, "data": ["19", "17", "26.0", "740", "84.4", "97.37", "181.77"]}]}]}},
}

IFACE = {
    "serial_number": "SG30LMR164",
    "errors": [],
    "trends": {"response": {"id": "SG30LMR164", "interfaceId": "1/1/3",
        "metric": "SwitchNetworkInterfaceTrends",
        "keys": ["rxBytes", "txBytes", "inErrors", "outErrors", "inGiants"],
        "samples": [
            {"timestamp": 1784982600000, "data": ["5458873", "5679464", "3", "0", "3"]},
            {"timestamp": 1784982900000, "data": ["5461001", "5680900", "3", "0", "4"]},
            {"timestamp": 1784983200000, "data": ["5470112", "5688001", "5", "0", "4"]}]}},
}

NOT_FOUND = {
    "serial_number": "X", "metric": "cpu", "trends": None, "endpoint_used": None,
    "errors": ["404 at .../v1/aps/X/cpu-utilization-trends",
               "404 at .../v1alpha1/aps/X/cpu-utilization-trends"],
}


# ── The tripwire ─────────────────────────────────────────────────────────────

def test_ap_and_switch_shapes_normalise_to_one_contract():
    """graph{}/ISO/int and response.switchMetrics[]/epoch-ms/str must land on
    the same contract. Both fixtures describe the same three instants."""
    ap = normalize_device_trends(AP_CPU, serial="PHQHKZ21HK")
    sw = normalize_device_trends(SWITCH_HW, serial="SG30LMR164")

    # Two different raw spellings reach the same canonical key.
    assert "cpu" in ap.series and "cpu" in sw.series
    assert ap.series["cpu"].raw_key == "cpu_utilization"
    assert sw.series["cpu"].raw_key == "cpuUtilization"

    # int vs str: every value is a float on both paths.
    assert [p.v for p in ap.series["cpu"].points] == [7.0, 9.0, 8.0]
    assert [p.v for p in sw.series["cpu"].points] == [21.0, 23.0, 19.0]
    assert sw.series["temperature"].points[0].v == 25.5

    # ISO vs epoch-MILLISECONDS. If this regresses, the year is 58000.
    assert ap.series["cpu"].points[0].t == 1784982600
    assert sw.series["cpu"].points[0].t == 1784982600
    assert datetime.fromtimestamp(sw.series["cpu"].points[0].t, timezone.utc).year == 2026

    # One switch call is seven series; losing the fan-out is a regression.
    assert len(sw.series) == 7
    assert sw.series["poe_available"].unit == "W"
    assert sw.series["cpu"].unit == "%"

    # Buckets detected identically on both paths.
    assert ap.bucket_seconds == sw.bucket_seconds == 300


def test_undivided_epoch_ms_would_be_an_absurd_year():
    """Documents why the ms/seconds branch is safe to rely on."""
    with pytest.raises((OverflowError, OSError, ValueError)):
        datetime.fromtimestamp(1784982600000, timezone.utc)


# ── The errors[] gotcha ──────────────────────────────────────────────────────

def test_non_empty_errors_on_success_is_not_a_failure():
    """Every one of these envelopes carries a 404 from an earlier candidate."""
    assert AP_CPU["errors"]
    trends = normalize_device_trends(AP_CPU)
    assert trends.ok is True
    assert trends.error is None


def test_none_payload_is_an_error_not_an_empty_chart():
    trends = normalize_device_trends(NOT_FOUND)
    assert trends.ok is False
    assert trends.error
    assert trends.series == {}


def test_envelope_error_prefers_a_real_reason_over_404_noise():
    assert envelope_error({"trends": None, "errors": ["404 at /a", "quota exceeded"]},
                          "trends") == "quota exceeded"
    assert "not available" in envelope_error(
        {"trends": None, "errors": ["404 at /a"]}, "trends")
    assert envelope_error({"trends": {"x": 1}}, "trends") is None


# ── The two switch shapes share one parser ───────────────────────────────────

def test_interface_trends_use_the_same_parser_as_hardware_trends():
    iface = normalize_interface_trends(IFACE, serial="SG30LMR164", interface_id="1/1/3")
    assert iface.ok
    assert iface.kind == "interface"
    assert iface.series["in_errors"].points[-1].v == 5.0
    assert iface.series["in_giants"].vmax == 4.0
    assert iface.bucket_seconds == 300


def test_switch_metrics_are_filtered_by_serial():
    payload = {"trends": {"response": {"keys": ["cpuUtilization"], "switchMetrics": [
        {"serialNumber": "OTHER", "samples": [{"timestamp": 1784982600000, "data": ["99"]}]},
        {"serialNumber": "MINE", "samples": [{"timestamp": 1784982600000, "data": ["11"]}]}]}}}
    assert normalize_device_trends(payload, serial="MINE").series["cpu"].last == 11.0


# ── Edge cases, all renderable ───────────────────────────────────────────────

def test_single_sample_survives():
    payload = {"trends": {"graph": {"keys": ["cpu_utilization"],
        "samples": [{"timestamp": "2026-07-25T12:30:00Z", "data": [42]}]}}}
    series = normalize_device_trends(payload).series["cpu"]
    assert len(series.points) == 1
    assert series.vmin == series.vmax == series.last == 42.0


def test_empty_samples_is_not_an_error():
    payload = {"trends": {"graph": {"keys": ["cpu_utilization"], "samples": []}}}
    trends = normalize_device_trends(payload)
    assert trends.error is None
    assert trends.series["cpu"].n == 0


def test_missing_bucket_becomes_a_gap_not_a_straight_line():
    payload = {"trends": {"graph": {"keys": ["cpu_utilization"], "samples": [
        {"timestamp": "2026-07-25T12:30:00Z", "data": [5]},
        {"timestamp": "2026-07-25T12:35:00Z", "data": [6]},
        {"timestamp": "2026-07-25T13:05:00Z", "data": [7]}]}}}
    points = normalize_device_trends(payload).series["cpu"].points
    assert any(p.v is None for p in points), "an outage must be a visible break"


def test_junk_values_become_gaps_not_zeros():
    payload = {"trends": {"response": {"keys": ["cpuUtilization"], "samples": [
        {"timestamp": 1784982600000, "data": ["21"]},
        {"timestamp": 1784982900000, "data": [""]},
        {"timestamp": 1784983200000, "data": ["N/A"]},
        {"timestamp": 1784983500000, "data": [None]}]}}}
    vals = [p.v for p in normalize_device_trends(payload).series["cpu"].points]
    assert vals == [21.0, None, None, None]


def test_unknown_key_is_kept_with_a_prettified_label():
    """A silently dropped key is how a chart empties out after a rename."""
    payload = {"trends": {"response": {"keys": ["fanSpeedRpm"], "samples": [
        {"timestamp": 1784982600000, "data": ["3200"]}]}}}
    trends = normalize_device_trends(payload)
    assert "fan_speed_rpm" in trends.series
    assert trends.series["fan_speed_rpm"].label == "Fan speed rpm"


def test_short_data_row_warns_and_does_not_raise():
    payload = {"trends": {"response": {"keys": ["cpuUtilization", "memoryUtilization"],
        "samples": [{"timestamp": 1784982600000, "data": ["21"]}]}}}
    trends = normalize_device_trends(payload)
    assert trends.series["cpu"].last == 21.0
    assert trends.series["memory"].n == 0
    assert trends.warnings


def test_unparseable_timestamps_are_dropped_with_a_warning():
    payload = {"trends": {"graph": {"keys": ["cpu_utilization"], "samples": [
        {"timestamp": "not-a-date", "data": [1]},
        {"timestamp": "2026-07-25T12:30:00Z", "data": [2]}]}}}
    trends = normalize_device_trends(payload)
    assert trends.series["cpu"].n == 1
    assert any("unparseable" in w for w in trends.warnings)


@pytest.mark.parametrize("payload", [[], "nope", 42, {"trends": []}])
def test_garbage_payloads_produce_an_error_not_an_exception(payload):
    assert normalize_device_trends(payload).ok is False


def test_samples_are_sorted_and_deduped():
    payload = {"trends": {"response": {"keys": ["cpuUtilization"], "samples": [
        {"timestamp": 1784983200000, "data": ["3"]},
        {"timestamp": 1784982600000, "data": ["1"]},
        {"timestamp": 1784982600000, "data": ["2"]}]}}}
    points = normalize_device_trends(payload).series["cpu"].points
    assert [p.t for p in points] == sorted(p.t for p in points)
    assert [p.v for p in points] == [2.0, 3.0]


# ── merge: the reason the template is written once ───────────────────────────

def test_merge_of_three_ap_calls_matches_the_switch_key_set():
    cpu = normalize_device_trends(AP_CPU, serial="AP1")
    mem = normalize_device_trends({"trends": {"graph": {"keys": ["memory_utilization"],
        "samples": [{"timestamp": "2026-07-25T12:30:00Z", "data": [40]}]}}}, serial="AP1")
    thr = normalize_device_trends({"trends": {"graph": {"keys": ["tx", "rx"],
        "samples": [{"timestamp": "2026-07-25T12:30:00Z", "data": [100, 200]}]}}}, serial="AP1")
    merged = merge(cpu, mem, thr)
    assert {"cpu", "memory", "tx", "rx"} <= set(merged.series)
    assert merged.ok


def test_merge_of_all_failures_is_a_single_error():
    assert merge(normalize_device_trends(NOT_FOUND),
                 normalize_device_trends(NOT_FOUND)).ok is False


def test_merge_keeps_partial_success():
    merged = merge(normalize_device_trends(AP_CPU), normalize_device_trends(NOT_FOUND))
    assert merged.ok is True and "cpu" in merged.series


# ── Shaping helpers ──────────────────────────────────────────────────────────

def test_downsample_max_keeps_the_spike():
    points = [Point(i * 300, 1.0) for i in range(20)]
    points[7] = Point(7 * 300, 99.0)
    series = Series.build("in_errors", "inErrors", "In errors", "count", points)
    assert downsample(series, max_points=5, how="max").vmax == 99.0
    assert downsample(series, max_points=5, how="mean").vmax < 99.0


def test_counter_detection_and_deltas():
    rising = Series.build("rx", "rxBytes", "Rx", "bytes",
                          [Point(i * 300, float(i * 100)) for i in range(5)])
    assert looks_like_counter(rising) is True
    assert [p.v for p in deltas(rising).points] == [None, 100.0, 100.0, 100.0, 100.0]

    bouncy = Series.build("cpu", "cpu", "CPU", "%",
                          [Point(0, 5.0), Point(300, 3.0), Point(600, 9.0)])
    assert looks_like_counter(bouncy) is False


def test_counter_reset_becomes_a_gap_not_a_negative():
    wrapped = Series.build("rx", "rxBytes", "Rx", "bytes",
                           [Point(0, 100.0), Point(300, 150.0), Point(600, 10.0)])
    assert [p.v for p in deltas(wrapped).points] == [None, 50.0, None]


def test_bits_per_second_uses_the_bucket_width():
    series = Series.build("tx", "tx", "Tx", "bytes", [Point(0, 300.0)])
    assert to_bits_per_second(series, 300).last == 8.0
    assert to_bits_per_second(series, 300).unit == "bit/s"


def test_error_counters_suppress_all_zero_series():
    iface = normalize_interface_trends(IFACE, serial="SG30LMR164")
    keys = [s.key for s in error_counter_series(iface)]
    assert "out_errors" not in keys, "an all-zero counter should not get a chart"
    assert keys[0] == "in_errors", "worst counter first"


def test_switch_snapshot_reads_the_instantaneous_row():
    snap = switch_snapshot({"switchTrends": [{"cpuUtilization": 22, "memoryUtilization": 17,
        "systemTemperature": 28, "poeAvailable": 740, "poeConsumption": 84.9,
        "powerConsumption": 97.99, "totalPowerConsumption": 182.89}]})
    assert snap["temperature"] == 28.0
    assert snap["poe_consumption"] == 84.9
    assert snap["poe_available"] == 740.0


@pytest.mark.parametrize("bad", [None, {}, {"switchTrends": None}, {"switchTrends": []}, "x"])
def test_switch_snapshot_degrades(bad):
    assert switch_snapshot(bad)["cpu"] is None


# ── Cache-key quantisation ───────────────────────────────────────────────────

def test_window_is_quantised_so_the_cache_key_is_stable():
    """start_iso/end_iso are part of the response-cache key. An unquantised now
    makes every request unique: decorator present, hit rate zero."""
    a = window(6, now=datetime(2026, 7, 25, 12, 34, 7, tzinfo=timezone.utc))
    b = window(6, now=datetime(2026, 7, 25, 12, 34, 37, tzinfo=timezone.utc))
    assert a == b, "two calls 30s apart must produce the same cache key"
    assert a[1] == "2026-07-25T12:30:00Z"
    assert a[0] == "2026-07-25T06:30:00Z"


def test_window_hours_are_respected():
    start, end = window(24, now=datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc))
    assert start == "2026-07-24T12:00:00Z" and end == "2026-07-25T12:00:00Z"


# ── Scalars ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    (1784982600000, 1784982600), (1784982600, 1784982600),
    ("1784982600000", 1784982600), ("2026-07-25T12:30:00Z", 1784982600),
    ("2026-07-25T12:30:00+00:00", 1784982600), (None, None), ("", None),
    ("nonsense", None), (True, None),
])
def test_to_epoch_seconds(raw, expected):
    assert to_epoch_seconds(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("25.5", 25.5), (21, 21.0), (21.5, 21.5), ("", None), (None, None),
    ("N/A", None), ("-", None), ("abc", None), (True, None),
])
def test_to_float(raw, expected):
    assert to_float(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("cpuUtilization", "cpu"), ("cpu_utilization", "cpu"),
    ("systemTemperature", "temperature"), ("rxBytes", "rx"),
    ("inGiants", "in_giants"), ("somethingNew", "something_new"),
])
def test_canonical_key(raw, expected):
    assert canonical_key(raw) == expected
