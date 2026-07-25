"""Chart geometry — pure maths, asserted directly.

Two of these pin bugs that are easy to reintroduce because the obvious
implementation has them: a flat series divided by a `|| 1` fallback, and a
single sample emitted as a one-point <polyline> (which draws nothing).
"""
import re

import pytest

from svg_chart import (
    MAX_SERIES,
    SERIES_COLORS,
    build_bars,
    build_chart,
    build_meter,
    format_value,
)
from timeseries import Point, Series


def mk(key="cpu", unit="%", values=(1.0, 2.0, 3.0), start=1784982600, step=300,
       label="CPU"):
    points = [Point(start + i * step, v) for i, v in enumerate(values)]
    return Series.build(key, key, label, unit, points)


# ── The two bugs worth pinning ───────────────────────────────────────────────

def test_flat_series_is_not_pinned_to_the_floor():
    """`range = max - min || 1` (home.html:589) renders a constant 740 W flat
    against the bottom of the plot, which reads as zero."""
    chart = build_chart([mk("poe_available", "W", (740.0, 740.0, 740.0))],
                        baseline_zero=False)
    assert not chart.empty
    assert chart.y_hi > chart.y_lo

    ys = [float(pair.split(",")[1])
          for seg in chart.lines[0].segments for pair in seg.split()]
    middle = chart.geom.y0 + chart.geom.plot_h / 2.0
    assert all(abs(y - middle) < 2.0 for y in ys), \
        "a flat series must draw through the middle, not along the floor"


def test_flat_zero_series_does_not_divide_by_zero():
    chart = build_chart([mk("in_errors", "count", (0.0, 0.0, 0.0))])
    assert not chart.empty and chart.y_hi > chart.y_lo


def test_single_sample_renders_a_dot_not_an_empty_polyline():
    """A <polyline> with one point draws nothing at all."""
    chart = build_chart([mk(values=(42.0,))])
    line = chart.lines[0]
    assert line.segments == ()
    assert len(line.dots) == 1
    assert line.dots[0][0] == pytest.approx(chart.geom.x0 + chart.geom.plot_w / 2, abs=0.6)


# ── Gaps ─────────────────────────────────────────────────────────────────────

def test_a_gap_splits_the_polyline():
    series = Series.build("cpu", "cpu", "CPU", "%", [
        Point(0, 1.0), Point(300, 2.0), Point(600, None),
        Point(900, 3.0), Point(1200, 4.0)])
    assert len(build_chart([series]).lines[0].segments) == 2


def test_an_isolated_point_between_gaps_becomes_a_dot():
    series = Series.build("cpu", "cpu", "CPU", "%", [
        Point(0, 1.0), Point(300, None), Point(600, 5.0),
        Point(900, None), Point(1200, 2.0)])
    line = build_chart([series]).lines[0]
    assert len(line.dots) == 3, "three runs of one point each"
    assert line.segments == ()


def test_all_none_is_empty_not_a_crash():
    series = Series.build("cpu", "cpu", "CPU", "%", [Point(0, None), Point(300, None)])
    chart = build_chart([series])
    assert chart.empty is True and chart.note


def test_no_series_is_empty():
    assert build_chart([]).empty is True


# ── Scales ───────────────────────────────────────────────────────────────────

def test_multi_series_share_one_x_domain():
    """Otherwise two series drawn together would not line up in time."""
    early = Series.build("a", "a", "A", "%", [Point(0, 1.0), Point(600, 2.0)])
    late = Series.build("b", "b", "B", "%", [Point(300, 1.0), Point(600, 2.0)])
    chart = build_chart([early, late])
    xs_a = [float(p.split(",")[0]) for p in chart.lines[0].segments[0].split()]
    xs_b = [float(p.split(",")[0]) for p in chart.lines[1].segments[0].split()]
    assert xs_a[0] == pytest.approx(chart.geom.x0)      # earliest overall
    assert xs_b[-1] == pytest.approx(xs_a[-1])          # same final instant
    assert xs_b[0] > xs_a[0]


def test_percent_chart_can_be_clamped():
    chart = build_chart([mk(values=(3.0, 7.0))], y_min=0, y_max=100)
    assert (chart.y_lo, chart.y_hi) == (0, 100)


def test_baseline_starts_at_zero_by_default():
    assert build_chart([mk(values=(40.0, 50.0))]).y_lo == 0.0


def test_y_axis_snaps_to_a_round_tick_interval():
    chart = build_chart([mk(values=(0.0, 23.7))])
    assert chart.y_hi == 30.0                       # step 10 -> 0/10/20/30
    labels = [label for _, label in chart.y_ticks]
    assert labels == ["0%", "10%", "20%", "30%"], labels


def test_a_narrow_high_range_does_not_waste_the_plot():
    """A 25.5-28C temperature series must not be drawn on a 0-50C axis, which
    is what deriving the ceiling from the absolute value does."""
    chart = build_chart([mk("temperature", "C", (25.5, 26.0, 28.0))],
                        baseline_zero=False)
    assert chart.y_lo >= 24.0 and chart.y_hi <= 30.0, (chart.y_lo, chart.y_hi)


def test_a_zero_based_axis_still_starts_at_zero():
    chart = build_chart([mk("poe", "W", (84.9, 88.0, 740.0))], y_min=0)
    assert chart.y_lo == 0
    assert 740.0 <= chart.y_hi <= 800.0


def test_series_beyond_the_cap_are_dropped_not_recoloured():
    """A 4th hue is never generated; extra series belong in small multiples."""
    chart = build_chart([mk(f"k{i}", "%", (1.0, 2.0)) for i in range(6)])
    assert len(chart.lines) == MAX_SERIES
    assert [ln.slot for ln in chart.lines] == [1, 2, 3]


def test_slots_are_assigned_in_fixed_order():
    chart = build_chart([mk("cpu"), mk("memory", label="Memory")])
    assert chart.lines[0].slot == 1 and chart.lines[1].slot == 2
    assert len(SERIES_COLORS) == MAX_SERIES


# ── Output hygiene ───────────────────────────────────────────────────────────

def test_coordinates_are_one_decimal():
    """Guards HTML size: 288 points x 3 series adds up quickly."""
    chart = build_chart([mk(values=tuple(float(i) for i in range(50)))])
    for segment in chart.lines[0].segments:
        for pair in segment.split():
            assert re.fullmatch(r"-?\d+\.\d,-?\d+\.\d", pair), pair


def test_table_view_covers_every_timestamp():
    chart = build_chart([mk(values=(1.0, 2.0, 3.0))])
    assert chart.headers[0] == "Time"
    assert len(chart.rows) == 3


def test_aria_label_carries_the_numbers():
    """The chart has no hover layer, so the accessible name must say something."""
    label = build_chart([mk(values=(1.0, 9.0))], title="CPU").aria_label
    assert "CPU" in label and "max" in label


def test_x_axis_is_labelled_utc_not_silently_local():
    assert "UTC" in build_chart([mk()], title="CPU").subtitle


def test_ticks_are_generated():
    chart = build_chart([mk(values=(1.0, 50.0))], y_ticks=4, x_ticks=3)
    assert len(chart.y_ticks) >= 3 and len(chart.x_ticks) == 4


def test_tick_labels_are_round_numbers():
    """Snapping the bounds but dividing the range equally puts labels at 7.5."""
    for values in [(0.0, 23.7), (25.5, 28.0), (0.0, 740.0), (3.0, 97.0)]:
        chart = build_chart([mk("x", "", values)], baseline_zero=False)
        for _, label in chart.y_ticks:
            assert "." not in label or label.endswith(".5"), (values, label)


# ── Bars ─────────────────────────────────────────────────────────────────────

def test_bars_emit_rect_geometry_with_a_gap():
    chart = build_bars(mk("in_errors", "count", (1.0, 3.0, 2.0)))
    assert not chart.empty
    rects = [tuple(float(n) for n in s.split(",")) for s in chart.lines[0].segments]
    assert len(rects) == 3
    widths = {r[2] for r in rects}
    assert len(widths) == 1 and next(iter(widths)) > 0


def test_bars_skip_gaps_without_shifting_later_bars():
    series = Series.build("in_errors", "inErrors", "In errors", "count",
                          [Point(0, 1.0), Point(300, None), Point(600, 2.0)])
    rects = [tuple(float(n) for n in s.split(","))
             for s in build_bars(series).lines[0].segments]
    assert len(rects) == 2
    assert rects[1][0] > rects[0][0], "the third bucket keeps its slot"


def test_bars_with_no_data_are_empty():
    series = Series.build("in_errors", "e", "In errors", "count", [Point(0, None)])
    assert build_bars(series).empty is True


# ── Meter ────────────────────────────────────────────────────────────────────

def test_poe_meter_reports_headroom():
    meter = build_meter(84.9, 740.0)
    assert meter["tone"] == "ok"
    assert meter["fill_pct"] == pytest.approx(11.5, abs=0.2)
    assert "740" in meter["label"]


@pytest.mark.parametrize("drawn,tone", [(100.0, "ok"), (600.0, "warn"), (700.0, "crit")])
def test_meter_tones(drawn, tone):
    assert build_meter(drawn, 740.0)["tone"] == tone


@pytest.mark.parametrize("value,total", [(None, 740.0), (10.0, None), (10.0, 0)])
def test_meter_degrades_when_not_reported(value, total):
    meter = build_meter(value, total)
    assert meter["tone"] == "unknown" and meter["fill_pct"] == 0.0


def test_meter_clamps_over_budget():
    assert build_meter(900.0, 740.0)["fill_pct"] == 100.0


# ── Formatting ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value,unit,expected", [
    (21.0, "%", "21%"), (25.5, "C", "25.5°C"), (84.9, "W", "84.9 W"),
    (3.0, "count", "3"), (None, "%", "—"),
    (1500.0, "bytes", "1.5 kB"), (2_000_000.0, "bit/s", "2.0 Mbit/s"),
])
def test_format_value(value, unit, expected):
    assert format_value(value, unit) == expected
