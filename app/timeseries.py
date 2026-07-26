"""Normalise Central's trend payloads into one contract.

Central returns time series in two mutually incompatible shapes, and any UI over
them has to absorb both:

                     access point                 switch
    envelope         trends.graph                 trends.response.switchMetrics[0]
    timestamps       ISO-8601 ("...Z")            epoch MILLISECONDS
    values           ints                         strings ("25.5")
    series per call  1 (2 for throughput)         7, incl. temperature and power

Both use positional ``data[]`` arrays that must be zipped against ``keys[]``.

Two shapes look like three but are two: ``SwitchDeviceTrends`` (hardware) and
``SwitchNetworkInterfaceTrends`` (per-port) differ only by a wrapper list, so
one parser handles both. Do not write a second one.

Pure by design — no db, no routes, no network, no vendors. That is what lets it
be tested against real captured payloads instead of against a stub of the layer
the bug would live in.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any, Iterable, NamedTuple, Sequence

# Central buckets these at 5 minutes.
DEFAULT_BUCKET_SECONDS = 300

# A timestamp at or above this is milliseconds, below it is seconds. 1e11
# seconds is the year 5138 and 1e11 milliseconds is 1973, so no real date is
# ambiguous. Getting this wrong is loud rather than silent: passing an
# undivided epoch-ms to datetime.fromtimestamp raises OverflowError.
_MS_THRESHOLD = 1e11


class Point(NamedTuple):
    """One sample. ``v is None`` marks a gap — a missing bucket or a value that
    could not be parsed. It is never rendered as zero."""

    t: int  # epoch seconds, UTC
    v: float | None


@dataclass(frozen=True)
class Series:
    key: str        # canonical: "cpu", "memory", "temperature", "in_errors"
    raw_key: str    # exactly what Central sent: "cpuUtilization" / "cpu_utilization"
    label: str
    unit: str       # "%" | "C" | "W" | "bytes" | "count" | ""
    points: tuple[Point, ...]
    vmin: float | None = None
    vmax: float | None = None
    last: float | None = None
    avg: float | None = None
    n: int = 0

    @classmethod
    def build(cls, key: str, raw_key: str, label: str, unit: str,
              points: Sequence[Point]) -> "Series":
        """Compute the stats once, here, so no caller can recompute them differently."""
        vals = [p.v for p in points if p.v is not None]
        return cls(
            key=key, raw_key=raw_key, label=label, unit=unit,
            points=tuple(points),
            vmin=min(vals) if vals else None,
            vmax=max(vals) if vals else None,
            last=vals[-1] if vals else None,
            avg=(sum(vals) / len(vals)) if vals else None,
            n=len(vals),
        )


@dataclass(frozen=True)
class TrendSet:
    serial: str = ""
    kind: str = "unknown"           # "ap" | "switch" | "interface" | "unknown"
    source: str = "none"            # "graph" | "switchMetrics" | "samples" | "none"
    series: dict[str, Series] = field(default_factory=dict)
    start: int | None = None
    end: int | None = None
    bucket_seconds: int | None = None
    warnings: tuple[str, ...] = ()
    # Deliberately separate from `warnings`: a route needs exactly one boolean to
    # choose between rendering a chart and rendering the red load-error card.
    error: str | None = None

    @property
    def ok(self) -> bool:
        # A series with zero real data points (n == 0) is not "present" — the
        # AP trend endpoints answer HTTP 200 on a gateway with a well-formed
        # payload whose every sample is [None]. Without this, ok is True, has()
        # is True, and the page builds empty chart cards instead of rendering
        # the "no trend data" state.
        return self.error is None and any(s.n > 0 for s in self.series.values())

    def has(self, *keys: str) -> bool:
        return all(k in self.series and self.series[k].n > 0 for k in keys)

    def pick(self, *keys: str) -> list[Series]:
        """Series in the order asked for, silently skipping absent ones."""
        return [self.series[k] for k in keys if k in self.series]


# ── Envelope handling — the errors[] gotcha, in exactly one place ────────────

def payload_of(envelope: Any, key: str) -> Any | None:
    """Return ``envelope[key]``, or None.

    A non-empty ``errors[]`` is NOT a failure. Several centralmcp monitoring
    tools return {"serial_number", "<payload>", "endpoint_used", "errors"} and
    fill errors[] with the 404s from earlier endpoint candidates even when a
    later candidate succeeded. The payload key being None is the only signal.
    """
    if not isinstance(envelope, dict):
        return None
    return envelope.get(key)


def envelope_error(envelope: Any, key: str) -> str | None:
    """A short human reason, only when the payload really is missing."""
    if not isinstance(envelope, dict):
        return "unexpected response shape"
    if envelope.get(key) is not None:
        return None
    errors = envelope.get("errors") or []
    for err in errors:
        text = str(err)
        # Skip the "tried this candidate, got 404" noise; surface a real reason.
        if not text.lower().startswith("404 at"):
            return text
    if errors:
        return "this endpoint is not available on this tenant"
    return "no data returned"


# ── Scalar coercion ──────────────────────────────────────────────────────────

def to_epoch_seconds(value: Any) -> int | None:
    """Epoch seconds from epoch-ms, epoch-s, or an ISO-8601 string."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value / 1000) if abs(value) >= _MS_THRESHOLD else int(value)
    text = str(value).strip()
    if not text:
        return None
    try:                                    # Central stringifies some epochs
        number = float(text)
    except ValueError:
        pass
    else:
        return int(number / 1000) if abs(number) >= _MS_THRESHOLD else int(number)
    try:
        # Python 3.11+ parses a trailing "Z" natively — no dependency needed.
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def to_float(value: Any) -> float | None:
    """A number, or None for a gap. Never coerces junk to zero."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text or text.lower() in ("n/a", "na", "null", "-", "--"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


# ── Canonical keys ───────────────────────────────────────────────────────────

_CANON = {
    "cpu_utilization": "cpu", "cpuutilization": "cpu", "cpu": "cpu",
    "memory_utilization": "memory", "memoryutilization": "memory", "memory": "memory",
    "systemtemperature": "temperature", "temperature": "temperature",
    "poeavailable": "poe_available",
    "poeconsumption": "poe_consumption",
    "powerconsumption": "power",
    "totalpowerconsumption": "power_total",
    "tx": "tx", "txbytes": "tx",
    "rx": "rx", "rxbytes": "rx",
    "inerrors": "in_errors", "outerrors": "out_errors",
    "indiscards": "in_discards", "outdiscards": "out_discards",
    "infcs": "in_fcs", "incrcerrors": "in_crc_errors",
    "infragmented": "in_fragmented", "outcollision": "out_collision",
    "inrunts": "in_runts", "ingiants": "in_giants",
}

_META: dict[str, tuple[str, str]] = {
    "cpu": ("CPU", "%"),
    "memory": ("Memory", "%"),
    "temperature": ("Temperature", "C"),
    "poe_available": ("PoE budget", "W"),
    "poe_consumption": ("PoE draw", "W"),
    "power": ("Power", "W"),
    "power_total": ("Power (total)", "W"),
    "tx": ("Tx", "bytes"),
    "rx": ("Rx", "bytes"),
    "in_errors": ("In errors", "count"),
    "out_errors": ("Out errors", "count"),
    "in_discards": ("In discards", "count"),
    "out_discards": ("Out discards", "count"),
    "in_fcs": ("In FCS", "count"),
    "in_crc_errors": ("In CRC errors", "count"),
    "in_fragmented": ("In fragmented", "count"),
    "out_collision": ("Out collisions", "count"),
    "in_runts": ("In runts", "count"),
    "in_giants": ("In giants", "count"),
}

_ERROR_COUNTER_KEYS = (
    "in_errors", "out_errors", "in_discards", "out_discards", "in_fcs",
    "in_crc_errors", "in_fragmented", "out_collision", "in_runts", "in_giants",
)


def _snake(raw: str) -> str:
    text = re.sub(r"(?<!^)(?=[A-Z])", "_", str(raw)).replace("-", "_").replace(" ", "_")
    return re.sub(r"_+", "_", text).strip("_").lower() or "value"


def canonical_key(raw: str) -> str:
    """Map a Central key onto our vocabulary.

    Unknown keys are kept under a snake_case name rather than dropped — a
    silently discarded key is how a chart ends up empty after an upstream
    rename, with nothing in the logs to say so.
    """
    return _CANON.get(str(raw).strip().lower(), _snake(raw))


def meta_for(key: str) -> tuple[str, str]:
    if key in _META:
        return _META[key]
    return key.replace("_", " ").capitalize(), ""


# ── The two entry points ─────────────────────────────────────────────────────

def normalize_device_trends(payload: Any, *, serial: str = "",
                            kind: str = "") -> TrendSet:
    """Absorb an AP or switch device-trends payload (envelope or bare)."""
    trends, err = _unwrap_trends(payload)
    if trends is None:
        return TrendSet(serial=serial, kind=kind or "unknown", error=err)

    if isinstance(trends, dict) and isinstance(trends.get("graph"), dict):
        graph = trends["graph"]
        return _from_keyed_samples(
            graph.get("keys"), graph.get("samples"),
            serial=serial or str(trends.get("id") or ""),
            kind=kind or "ap", source="graph",
        )
    if isinstance(trends, dict) and isinstance(trends.get("response"), dict):
        return _from_response(trends["response"], serial=serial, kind=kind or "switch")
    if isinstance(trends, dict) and "samples" in trends:
        return _from_keyed_samples(
            trends.get("keys"), trends.get("samples"),
            serial=serial, kind=kind or "unknown", source="samples",
        )
    return TrendSet(serial=serial, kind=kind or "unknown",
                    error="unrecognised trends payload shape")


def normalize_interface_trends(payload: Any, *, serial: str = "",
                               interface_id: str = "") -> TrendSet:
    """Per-interface trends. Same parser as the switch hardware shape."""
    trends, err = _unwrap_trends(payload)
    if trends is None:
        return TrendSet(serial=serial, kind="interface", error=err)
    if isinstance(trends, dict) and isinstance(trends.get("response"), dict):
        out = _from_response(trends["response"], serial=serial, kind="interface")
    elif isinstance(trends, dict) and "samples" in trends:
        out = _from_keyed_samples(trends.get("keys"), trends.get("samples"),
                                  serial=serial, kind="interface", source="samples")
    else:
        return TrendSet(serial=serial, kind="interface",
                        error="unrecognised interface trends payload shape")
    return out


def _unwrap_trends(payload: Any) -> tuple[Any, str | None]:
    """Accept the full envelope, the bare `trends` value, or None."""
    if payload is None:
        return None, "no data returned"
    if isinstance(payload, dict) and "trends" in payload:
        inner = payload.get("trends")
        if inner is None:
            return None, envelope_error(payload, "trends")
        return inner, None
    if isinstance(payload, dict):
        return payload, None
    return None, "unrecognised trends payload shape"


def _from_response(response: dict, *, serial: str, kind: str) -> TrendSet:
    """SwitchDeviceTrends and SwitchNetworkInterfaceTrends in one parser.

    They differ only by a wrapper list: hardware trends nest their samples in
    switchMetrics[], per-interface trends carry `samples` directly.
    """
    keys = response.get("keys")
    metrics = response.get("switchMetrics")
    if isinstance(metrics, list) and metrics:
        chosen = None
        if serial:
            chosen = next(
                (m for m in metrics
                 if isinstance(m, dict) and str(m.get("serialNumber")) == serial),
                None,
            )
        if chosen is None:
            chosen = next((m for m in metrics if isinstance(m, dict)), {})
        samples = chosen.get("samples")
        source = "switchMetrics"
    else:
        samples = response.get("samples")
        source = "samples"
    return _from_keyed_samples(keys, samples, serial=serial, kind=kind, source=source)


def _from_keyed_samples(keys: Any, samples: Any, *, serial: str, kind: str,
                        source: str) -> TrendSet:
    warnings: list[str] = []
    rows = [s for s in (samples or []) if isinstance(s, dict)]

    key_list = [str(k) for k in keys] if isinstance(keys, (list, tuple)) and keys else []
    if not key_list:
        widths = {len(r.get("data") or []) for r in rows}
        key_list = ["value"] if widths == {1} else []
        if rows and not key_list:
            return TrendSet(serial=serial, kind=kind, source=source,
                            error="trends payload has samples but no keys")

    parsed: list[tuple[int, list[Any]]] = []
    dropped = 0
    short = 0
    for row in rows:
        stamp = to_epoch_seconds(row.get("timestamp"))
        if stamp is None:
            dropped += 1
            continue
        data = row.get("data")
        data = list(data) if isinstance(data, (list, tuple)) else []
        if len(data) != len(key_list):
            short += 1
        parsed.append((stamp, data))

    if dropped:
        warnings.append(f"dropped {dropped} sample(s) with unparseable timestamps")
    if short:
        warnings.append(f"{short} sample(s) had a row width different from keys")

    parsed.sort(key=lambda pair: pair[0])
    # Central can repeat a bucket; keep the last write for an instant.
    deduped: dict[int, list[Any]] = {}
    for stamp, data in parsed:
        deduped[stamp] = data
    stamps = sorted(deduped)

    bucket = _median_delta(stamps)
    series: dict[str, Series] = {}
    for index, raw_key in enumerate(key_list):
        key = canonical_key(raw_key)
        label, unit = meta_for(key)
        points = [
            Point(stamp, to_float(deduped[stamp][index])
                  if index < len(deduped[stamp]) else None)
            for stamp in stamps
        ]
        series[key] = Series.build(key, str(raw_key), label, unit,
                                   _insert_gaps(points, bucket))

    return TrendSet(
        serial=serial, kind=kind, source=source, series=series,
        start=stamps[0] if stamps else None,
        end=stamps[-1] if stamps else None,
        bucket_seconds=bucket,
        warnings=tuple(warnings),
    )


def _median_delta(stamps: Sequence[int]) -> int | None:
    if len(stamps) < 2:
        return None
    deltas = [b - a for a, b in zip(stamps, stamps[1:]) if b > a]
    return int(median(deltas)) if deltas else None


def _insert_gaps(points: Sequence[Point], bucket: int | None) -> list[Point]:
    """Mark missing buckets explicitly.

    Without this a device that was offline for half an hour draws a straight,
    confident line across the outage — the chart's most misleading failure mode.
    """
    if not bucket or len(points) < 2:
        return list(points)
    out: list[Point] = []
    for prev, nxt in zip(points, points[1:]):
        out.append(prev)
        if nxt.t - prev.t > bucket * 1.5:
            out.append(Point(prev.t + bucket, None))
    out.append(points[-1])
    return out


# ── Composition and shaping ──────────────────────────────────────────────────

def merge(*sets: TrendSet) -> TrendSet:
    """Combine several TrendSets into one.

    This is the point of the module: an AP needs three calls each returning one
    series, a switch needs one call returning seven. After merge the template
    code is identical for both.
    """
    live = [s for s in sets if s is not None]
    if not live:
        return TrendSet(error="no data returned")
    combined: dict[str, Series] = {}
    warnings: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    buckets: list[int] = []
    for one in live:
        combined.update(one.series)
        warnings.extend(one.warnings)
        if one.start is not None:
            starts.append(one.start)
        if one.end is not None:
            ends.append(one.end)
        if one.bucket_seconds:
            buckets.append(one.bucket_seconds)
    first = live[0]
    errors = [s.error for s in live if s.error]
    return TrendSet(
        serial=first.serial, kind=first.kind, source=first.source,
        series=combined,
        start=min(starts) if starts else None,
        end=max(ends) if ends else None,
        bucket_seconds=int(median(buckets)) if buckets else None,
        warnings=tuple(warnings),
        # Only fatal if nothing at all came back.
        error=None if combined else (errors[0] if errors else "no data returned"),
    )


def downsample(series: Series, max_points: int = 180, how: str = "mean") -> Series:
    """Reduce point count for HTML size.

    ``how="max"`` for error counters — averaging a spike away is exactly the
    wrong thing on the chart someone opened to find a spike.
    """
    points = series.points
    if len(points) <= max_points or max_points < 1:
        return series
    stride = (len(points) + max_points - 1) // max_points
    out: list[Point] = []
    for start in range(0, len(points), stride):
        chunk = points[start:start + stride]
        vals = [p.v for p in chunk if p.v is not None]
        if not vals:
            out.append(Point(chunk[0].t, None))
        elif how == "max":
            out.append(Point(chunk[0].t, max(vals)))
        else:
            out.append(Point(chunk[0].t, sum(vals) / len(vals)))
    return Series.build(series.key, series.raw_key, series.label, series.unit, out)


def looks_like_counter(series: Series) -> bool:
    """True if the series only ever rises — i.e. it is a lifetime counter."""
    vals = [p.v for p in series.points if p.v is not None]
    if len(vals) < 3:
        return False
    return all(b >= a for a, b in zip(vals, vals[1:])) and vals[-1] > vals[0]


def deltas(series: Series) -> Series:
    """Per-bucket change. Resets (counter wrap) become gaps, not negatives."""
    out: list[Point] = []
    previous: float | None = None
    for point in series.points:
        if point.v is None:
            out.append(Point(point.t, None))
            previous = None
            continue
        out.append(Point(point.t, None if previous is None or point.v < previous
                         else point.v - previous))
        previous = point.v
    return Series.build(series.key, series.raw_key, series.label, series.unit, out)


def to_bits_per_second(series: Series, bucket_seconds: int | None) -> Series:
    """Bytes-per-bucket to bits/s. A y-axis reading "1.2 GB" means nothing
    without the bucket width."""
    if not bucket_seconds:
        return series
    out = [Point(p.t, None if p.v is None else p.v * 8.0 / bucket_seconds)
           for p in series.points]
    return Series.build(series.key, series.raw_key, series.label, "bit/s", out)


def error_counter_series(trends: TrendSet, *, only_nonzero: bool = True) -> list[Series]:
    """The interface error counters, worst first. Suppresses all-zero counters
    so twelve empty charts collapse into one honest summary line."""
    out = []
    for key in _ERROR_COUNTER_KEYS:
        series = trends.series.get(key)
        if series is None:
            continue
        if only_nonzero and not (series.vmax or 0):
            continue
        out.append(series)
    out.sort(key=lambda s: s.vmax or 0, reverse=True)
    return out


def switch_snapshot(details: Any) -> dict[str, float | None]:
    """Instantaneous values from get_switch_details — no time series needed."""
    empty: dict[str, float | None] = {k: None for k in (
        "cpu", "memory", "temperature", "poe_available", "poe_consumption",
        "power", "power_total")}
    if not isinstance(details, dict):
        return empty
    trends = details.get("switchTrends")
    row = trends[0] if isinstance(trends, list) and trends and isinstance(trends[0], dict) else {}
    for raw, value in row.items():
        key = canonical_key(raw)
        if key in empty:
            empty[key] = to_float(value)
    return empty


# ── Cache-key safety ─────────────────────────────────────────────────────────

def window(hours: int = 6, *, now: datetime | None = None,
           quantum_seconds: int = DEFAULT_BUCKET_SECONDS) -> tuple[str, str]:
    """An ISO start/end pair, quantised to the bucket.

    ``start_iso``/``end_iso`` are part of the response-cache key
    (central_bridge._cached builds it from the arguments). An unquantised
    ``now`` therefore makes every single request a unique key: the decorator is
    present, the hit rate is zero, and every page view goes upstream cold.
    Flooring to the bucket makes consecutive requests share a key.
    """
    end = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    floored = end.replace(second=0, microsecond=0)
    if quantum_seconds >= 60:
        minutes = (floored.minute // (quantum_seconds // 60)) * (quantum_seconds // 60)
        floored = floored.replace(minute=minutes)
    start = floored - timedelta(hours=max(1, hours))
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return start.strftime(fmt), floored.strftime(fmt)


def iter_series(sets: Iterable[TrendSet]) -> list[Series]:
    out: list[Series] = []
    for one in sets:
        out.extend(one.series.values())
    return out
