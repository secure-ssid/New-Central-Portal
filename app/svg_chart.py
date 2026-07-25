"""Chart geometry for server-rendered SVG.

Emits **numbers, not markup** — a Jinja macro turns a Chart into elements. That
split keeps escaping in Jinja, makes the geometry unit-testable without parsing
HTML, and lets stroke widths be a CSS change rather than a Python one.

Why server-rendered SVG rather than a chart library (the honest reasons — it is
NOT a CSP argument; base.html already carries inline scripts and the Tailwind
Play CDN, and `<polyline points>` is not script anyway):

  * the geometry is unit-testable in Python;
  * no data blob is duplicated into the DOM alongside the rendered marks;
  * nothing to vendor and audit (cf. docs/VENDORED_JS.md);
  * it works in NOC wallboard mode and with JS off;
  * one render path, so a page and a later HTMX fragment cannot drift apart.

Colour is assigned by SLOT, in fixed order, never cycled, and emitted as a class
rather than an inline fill so the palette lives in CSS.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Sequence

from timeseries import Point, Series

# Validated with the dataviz palette validator against the portal's dark card
# surface (#131824, the midpoint of .card's gradient), --pairs all:
#   lightness band  PASS (all inside L 0.48-0.67)
#   chroma floor    PASS
#   CVD separation  PASS  worst pair ΔE 13.6 deutan (target >= 8)
#   normal vision   PASS  worst pair ΔE 26.4      (floor 15)
#   contrast        PASS  all >= 3:1
# The eyeballed first guess (#fb923c/#22d3ee/#c084fc) FAILED the lightness band,
# and orange+blue+purple — the portal's existing tints — fails CVD at ΔE 1.3.
# Do not "improve" these without re-running the validator.
SERIES_COLORS = ("#ea580c", "#0891b2", "#9333ea")

# Hard cap. A 4th series folds into small multiples rather than a new hue.
MAX_SERIES = 3


@dataclass(frozen=True)
class ChartGeom:
    width: int = 720
    height: int = 200
    pad_left: int = 48
    pad_right: int = 12
    pad_top: int = 12
    pad_bottom: int = 26

    @property
    def x0(self) -> float:
        return float(self.pad_left)

    @property
    def x1(self) -> float:
        return float(self.width - self.pad_right)

    @property
    def y0(self) -> float:
        return float(self.pad_top)

    @property
    def y1(self) -> float:
        return float(self.height - self.pad_bottom)

    @property
    def plot_w(self) -> float:
        return self.x1 - self.x0

    @property
    def plot_h(self) -> float:
        return self.y1 - self.y0


DEFAULT_GEOM = ChartGeom()
SMALL_GEOM = ChartGeom(width=360, height=140, pad_left=42, pad_bottom=22)


@dataclass(frozen=True)
class Line:
    key: str
    label: str
    slot: int                                   # 1..3 -> .chart-line--N
    unit: str
    segments: tuple[str, ...] = ()              # one "x,y x,y ..." per contiguous run
    dots: tuple[tuple[float, float], ...] = ()  # isolated points, incl. single-sample
    vmin_s: str = "—"
    vmax_s: str = "—"
    avg_s: str = "—"
    last_s: str = "—"


@dataclass(frozen=True)
class Chart:
    geom: ChartGeom = DEFAULT_GEOM
    lines: tuple[Line, ...] = ()
    y_ticks: tuple[tuple[float, str], ...] = ()
    x_ticks: tuple[tuple[float, str], ...] = ()
    y_lo: float = 0.0
    y_hi: float = 1.0
    empty: bool = True
    note: str = ""
    title: str = ""
    subtitle: str = ""
    aria_label: str = ""
    headers: tuple[str, ...] = ()
    rows: tuple[tuple[str, ...], ...] = ()


# ── Formatting ───────────────────────────────────────────────────────────────

def format_value(value: float | None, unit: str = "") -> str:
    if value is None:
        return "—"
    if unit == "%":
        return f"{value:.0f}%"
    if unit == "C":
        return f"{value:.1f}°C"
    if unit == "W":
        return f"{value:.1f} W"
    if unit == "bit/s":
        return _si(value, "bit/s")
    if unit == "bytes":
        return _si(value, "B")
    if unit == "count":
        return f"{value:.0f}"
    if abs(value) >= 100:
        return f"{value:.0f}"
    return f"{value:.1f}".rstrip("0").rstrip(".")


def _si(value: float, suffix: str) -> str:
    magnitude = abs(value)
    for scale, prefix in ((1e9, "G"), (1e6, "M"), (1e3, "k")):
        if magnitude >= scale:
            return f"{value / scale:.1f} {prefix}{suffix}"
    return f"{value:.0f} {suffix}"


def _nice_ceiling(value: float) -> float:
    """Round up to 1/2/2.5/5 x 10^n so ticks read 0/25/50/75/100."""
    if value <= 0:
        return 1.0
    exponent = math.floor(math.log10(value))
    base = 10 ** exponent
    for step in (1.0, 2.0, 2.5, 5.0, 10.0):
        if value <= step * base * 1.0000001:
            return step * base
    return 10.0 * base


# ── The builder ──────────────────────────────────────────────────────────────

def build_chart(
    series: Sequence[Series],
    *,
    title: str = "",
    subtitle: str = "",
    geom: ChartGeom = DEFAULT_GEOM,
    y_min: float | None = None,
    y_max: float | None = None,
    baseline_zero: bool = True,
    y_ticks: int = 4,
    x_ticks: int = 4,
    value_format: Callable[[float | None, str], str] = format_value,
    tz_label: str = "UTC",
    empty_note: str = "No data in this window",
) -> Chart:
    """Geometry for a multi-series line chart. Never raises on odd input."""
    series = [s for s in series if s is not None][:MAX_SERIES]
    aria = title or "chart"

    stamps = [p.t for s in series for p in s.points]
    values = [p.v for s in series for p in s.points if p.v is not None]
    if not series or not values:
        return Chart(geom=geom, empty=True, note=empty_note, title=title,
                     subtitle=subtitle, aria_label=aria)

    t0, t1 = min(stamps), max(stamps)
    span = t1 - t0

    lo = y_min if y_min is not None else min(values)
    hi = y_max if y_max is not None else max(values)
    if baseline_zero and y_min is None and lo > 0:
        lo = 0.0
    padded_flat = hi == lo
    if padded_flat:
        # NOT `range = max - min || 1` (home.html:589). That pins a constant
        # 740W series to the bottom of the plot, which reads as zero. Pad
        # symmetrically so a flat series draws through the middle — and skip
        # the nice-ceiling step below, which would push it back off centre.
        pad = max(abs(hi) * 0.1, 1.0)
        lo, hi = hi - pad, hi + pad
    elif y_max is None:
        hi = max(_nice_ceiling(hi), lo + 1e-9)
    if hi <= lo:                                    # belt and braces
        hi = lo + 1.0

    def to_x(t: int) -> float:
        if span <= 0:                               # single instant
            return geom.x0 + geom.plot_w / 2.0
        return geom.x0 + (t - t0) / span * geom.plot_w

    def to_y(v: float) -> float:
        return geom.y1 - (v - lo) / (hi - lo) * geom.plot_h

    lines: list[Line] = []
    for index, one in enumerate(series):
        segments, dots = _runs(one.points, to_x, to_y)
        lines.append(Line(
            key=one.key, label=one.label, slot=index + 1, unit=one.unit,
            segments=tuple(segments), dots=tuple(dots),
            vmin_s=value_format(one.vmin, one.unit),
            vmax_s=value_format(one.vmax, one.unit),
            avg_s=value_format(one.avg, one.unit),
            last_s=value_format(one.last, one.unit),
        ))

    ticks_y = []
    for i in range(y_ticks + 1):
        value = lo + (hi - lo) * i / y_ticks
        ticks_y.append((round(to_y(value), 1), value_format(value, series[0].unit)))

    ticks_x = []
    long_window = span > 86400
    for i in range(x_ticks + 1):
        stamp = t0 + int(span * i / x_ticks) if span > 0 else t0
        when = datetime.fromtimestamp(stamp, timezone.utc)
        ticks_x.append((round(to_x(stamp), 1),
                        when.strftime("%m-%d %H:%M" if long_window else "%H:%M")))

    headers, rows = _table(series, value_format)
    unit_note = f" ({series[0].unit})" if series[0].unit else ""
    return Chart(
        geom=geom, lines=tuple(lines),
        y_ticks=tuple(ticks_y), x_ticks=tuple(ticks_x),
        y_lo=lo, y_hi=hi, empty=False, note="",
        title=title,
        # The server cannot know the viewer's timezone; say which one this is
        # rather than render an unlabelled local time.
        subtitle=subtitle or f"times in {tz_label}",
        aria_label=(f"{title}{unit_note}: "
                    + ", ".join(f"{ln.label} now {ln.last_s}, min {ln.vmin_s}, "
                                f"max {ln.vmax_s}" for ln in lines)),
        headers=headers, rows=rows,
    )


def _runs(points: Sequence[Point], to_x, to_y) -> tuple[list[str], list[tuple[float, float]]]:
    """Split into contiguous runs so a gap is a visible break.

    A run of exactly one point becomes a dot: a <polyline> with a single point
    renders nothing at all, which is also the single-sample case.
    """
    segments: list[str] = []
    dots: list[tuple[float, float]] = []
    run: list[tuple[float, float]] = []

    def flush() -> None:
        if len(run) >= 2:
            segments.append(" ".join(f"{x:.1f},{y:.1f}" for x, y in run))
        elif len(run) == 1:
            dots.append((round(run[0][0], 1), round(run[0][1], 1)))
        run.clear()

    for point in points:
        if point.v is None:
            flush()
            continue
        run.append((to_x(point.t), to_y(point.v)))
    flush()
    return segments, dots


def _table(series: Sequence[Series], fmt) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    """The table twin. A value must never be reachable only by hovering — and
    these charts have no hover layer, so the legend and this table are the
    readout."""
    stamps = sorted({p.t for s in series for p in s.points})
    headers = ("Time",) + tuple(s.label for s in series)
    lookup = [{p.t: p.v for p in s.points} for s in series]
    rows = tuple(
        (datetime.fromtimestamp(t, timezone.utc).strftime("%m-%d %H:%M"),)
        + tuple(fmt(table.get(t), s.unit) for table, s in zip(lookup, series))
        for t in stamps
    )
    return headers, rows


def build_bars(series: Series, *, geom: ChartGeom = SMALL_GEOM, title: str = "",
               value_format: Callable[[float | None, str], str] = format_value,
               empty_note: str = "No data in this window") -> Chart:
    """Bars for per-bucket counts.

    An interface error counter is a count *per bucket*; a bar is the honest
    mark for it, and a line would imply interpolation between buckets.
    """
    points = [p for p in series.points if p.v is not None]
    if not points:
        return Chart(geom=geom, empty=True, note=empty_note, title=title,
                     aria_label=title or "chart")

    hi = _nice_ceiling(max(p.v for p in points) or 1.0)
    count = max(len(series.points), 1)
    slot_w = geom.plot_w / count
    bar_w = max(slot_w - 2.0, 1.0)      # 2px surface gap between adjacent bars

    bars: list[tuple[float, float, float, float]] = []
    for index, point in enumerate(series.points):
        if point.v is None:
            continue
        height = (point.v / hi) * geom.plot_h if hi else 0.0
        bars.append((round(geom.x0 + index * slot_w, 1),
                     round(geom.y1 - height, 1),
                     round(bar_w, 1), round(max(height, 0.0), 1)))

    line = Line(key=series.key, label=series.label, slot=1, unit=series.unit,
                segments=tuple(f"{x},{y},{w},{h}" for x, y, w, h in bars),
                vmin_s=value_format(series.vmin, series.unit),
                vmax_s=value_format(series.vmax, series.unit),
                avg_s=value_format(series.avg, series.unit),
                last_s=value_format(series.last, series.unit))
    headers, rows = _table([series], value_format)
    return Chart(
        geom=geom, lines=(line,), empty=False, y_lo=0.0, y_hi=hi,
        y_ticks=((round(geom.y0, 1), value_format(hi, series.unit)),
                 (round(geom.y1, 1), "0")),
        title=title or series.label,
        aria_label=f"{series.label}: peak {value_format(series.vmax, series.unit)}",
        headers=headers, rows=rows,
    )


def build_meter(value: float | None, total: float | None, *, unit: str = "W",
                warn_at: float = 0.75, crit_at: float = 0.9) -> dict:
    """A budget bar — the right read for PoE headroom.

    Returns geometry in PERCENT so the template can size it responsively
    without knowing a pixel width.
    """
    if value is None or not total:
        return {"pct": 0.0, "fill_pct": 0.0, "tone": "unknown",
                "label": "Not reported", "value_s": "—", "total_s": "—"}
    ratio = value / total if total else 0.0
    tone = "crit" if ratio >= crit_at else "warn" if ratio >= warn_at else "ok"
    return {
        "pct": round(ratio * 100, 1),
        "fill_pct": round(min(max(ratio, 0.0), 1.0) * 100, 1),
        "tone": tone,
        "value_s": format_value(value, unit),
        "total_s": format_value(total, unit),
        "label": f"{format_value(value, unit)} of {format_value(total, unit)} "
                 f"({ratio * 100:.0f}%)",
    }
