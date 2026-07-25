"""The application-risk vocabulary and the ranking helpers built on it.

Pure module, so these run against payload-shaped fixtures rather than against a
stub of the layer a bug would live in.
"""
import app_risk
import pytest

from tests.conftest import RAW_APPLICATIONS


# ── Vocabulary ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("SUSPICIOUS", "suspicious"),
    ("MODERATE", "moderate"),
    ("LOW", "low"),
    ("TRUSTWORTHY", "trustworthy"),
    ("NOT_EVALUATED", "unknown"),
    ("not evaluated", "unknown"),
    ("not-evaluated", "unknown"),
    ("", "unknown"),
    (None, "unknown"),
    ("something Central invented last week", "unknown"),
])
def test_every_live_risk_value_maps_to_a_bucket(raw, expected):
    assert app_risk.normalize_risk(raw) == expected


def test_normalize_is_idempotent():
    """Counters accept raw payloads and already-normalised dicts alike, which
    only works if a second pass is a no-op."""
    for value in ("SUSPICIOUS", "NOT_EVALUATED", "", "nonsense"):
        once = app_risk.normalize_risk(value)
        assert app_risk.normalize_risk(once) == once


def test_unknown_is_ranked_last_not_second():
    """NOT_EVALUATED is absence of an opinion, not evidence of risk. Ranking it
    next to SUSPICIOUS would swamp the posture strip on a tenant where a tenth
    of all rows are unevaluated."""
    assert app_risk.RISK_BUCKETS[0] == "suspicious"
    assert app_risk.RISK_BUCKETS[-1] == "unknown"


def test_the_severity_vocabularies_stay_disjoint():
    """If these ever overlap, someone has started folding one into the other —
    and Central publishes no mapping between them."""
    import alert_severity
    assert not set(app_risk.RISK_BUCKETS) & set(alert_severity.SEVERITY_BUCKETS)


# ── Normalisation ────────────────────────────────────────────────────────────

def test_epoch_milliseconds_arrive_as_a_string():
    """lastUsedTime is "1785009176957" — not ISO, not a number."""
    app = app_risk.normalize_app(RAW_APPLICATIONS[0])
    assert app["last_used"] == 1785009176
    assert isinstance(app["last_used"], int)


def test_bytes_are_summed_and_kept_separately():
    app = app_risk.normalize_app(RAW_APPLICATIONS[0])
    assert app["rx"] == 4_250_000_000
    assert app["tx"] == 31_000_000
    assert app["total"] == 4_281_000_000


def test_a_record_without_a_name_is_dropped_not_half_built():
    assert app_risk.normalize_app({"risk": "SUSPICIOUS", "rxBytes": 5}) is None
    assert app_risk.normalize_app("not a dict") is None
    assert app_risk.normalize_app(None) is None


def test_junk_bytes_do_not_raise_or_become_none():
    app = app_risk.normalize_app({"name": "x", "rxBytes": "1500", "txBytes": None})
    assert app["rx"] == 1500 and app["tx"] == 0


def test_destinations_are_deduplicated_in_order():
    app = app_risk.normalize_app({"name": "x", "destLocation": [
        {"countryCode": "US", "countryName": "United States"},
        {"countryCode": "CA", "countryName": "Canada"},
        {"countryCode": "US", "countryName": "United States"},
    ]})
    assert app["countries"] == ["United States", "Canada"]


def test_a_missing_country_name_falls_back_to_the_code():
    app = app_risk.normalize_app({"name": "x", "destLocation": [{"countryCode": "SG"}]})
    assert app["countries"] == ["SG"]


@pytest.mark.parametrize("categories", [
    [], ["Not Available"], ["not available"], ["Unknown"], [""], None,
])
def test_unclassified_detection(categories):
    app = app_risk.normalize_app({"name": "x", "categories": categories})
    assert app["unclassified"] is True


def test_a_real_category_is_not_unclassified():
    app = app_risk.normalize_app({"name": "x", "categories": ["Not Available", "Web"]})
    assert app["unclassified"] is False


def test_apps_come_back_largest_first():
    apps = app_risk.normalize_apps(RAW_APPLICATIONS)
    assert [a["total"] for a in apps] == sorted((a["total"] for a in apps), reverse=True)


def test_normalize_apps_tolerates_junk_in_the_list():
    assert app_risk.normalize_apps([None, "x", {}, {"name": "ok"}]) == \
        [a for a in app_risk.normalize_apps([{"name": "ok"}])]


# ── Ranking ──────────────────────────────────────────────────────────────────

def test_the_watchlist_puts_the_small_unclassified_row_above_the_big_known_one():
    """The whole point of the split. needost.shop moves 0.6 MB and
    githubcopilot.com moves 6.1 GB; both are flagged SUSPICIOUS. A single
    bytes-sorted list would bury the one worth looking at."""
    apps = app_risk.normalize_apps(RAW_APPLICATIONS)
    unknown, known = app_risk.watchlist(apps)
    assert [a["name"] for a in unknown] == ["needost.shop"]
    assert [a["name"] for a in known] == [
        "Apple Push Notification Service", "githubcopilot.com"]


def test_the_watchlist_excludes_low_and_trustworthy_and_unevaluated():
    apps = app_risk.normalize_apps(RAW_APPLICATIONS)
    listed = {a["name"] for group in app_risk.watchlist(apps) for a in group}
    assert "Disney Plus" not in listed     # trustworthy
    assert "DNS" not in listed             # not evaluated


def test_the_risk_strip_keeps_empty_buckets():
    """A bucket falling to zero must read as a zero, not vanish and silently
    change the strip's width."""
    strip = app_risk.risk_strip(app_risk.normalize_apps(RAW_APPLICATIONS))
    assert [r["bucket"] for r in strip] == list(app_risk.RISK_BUCKETS)
    assert dict((r["bucket"], r["count"]) for r in strip) == {
        "suspicious": 2, "moderate": 1, "low": 0, "trustworthy": 1, "unknown": 1}


def test_counts_add_up_to_the_total():
    counts = app_risk.count_risks(app_risk.normalize_apps(RAW_APPLICATIONS))
    assert counts["total"] == len(RAW_APPLICATIONS)
    assert sum(counts[b] for b in app_risk.RISK_BUCKETS) == counts["total"]


def test_counters_accept_raw_payloads_too():
    assert app_risk.count_risks(RAW_APPLICATIONS)["suspicious"] == 2


def test_category_rollup_double_counts_and_says_so_in_the_numbers():
    """APNS carries two categories, so it contributes its full total to both.
    The parts summing to more than the whole is expected — the template labels
    it — but the per-category totals must still be right."""
    apps = app_risk.normalize_apps(RAW_APPLICATIONS)
    rollup = {r["name"]: r for r in app_risk.category_rollup(apps)}
    apns = next(a for a in apps if a["name"].startswith("Apple"))
    assert rollup["Web"]["total"] == apns["total"]
    assert rollup["Computer and Internet Info"]["total"] == \
        apns["total"] + next(a for a in apps if a["name"] == "githubcopilot.com")["total"]
    assert rollup["Computer and Internet Info"]["apps"] == 2
    assert sum(r["total"] for r in rollup.values()) > sum(a["total"] for a in apps)


def test_top_talkers_is_capped_and_ordered():
    apps = app_risk.normalize_apps(RAW_APPLICATIONS)
    assert app_risk.top_talkers(apps, limit=2) == apps[:2]


def test_the_helpers_survive_an_empty_window():
    assert app_risk.normalize_apps([]) == []
    assert app_risk.watchlist([]) == ([], [])
    assert app_risk.top_talkers([]) == []
    assert app_risk.category_rollup([]) == []
    assert app_risk.count_risks([])["total"] == 0
    assert len(app_risk.risk_strip([])) == len(app_risk.RISK_BUCKETS)


def test_every_bucket_has_a_label_and_a_defined_css_tone():
    """A tone that is not in the stylesheet renders an unstyled chip — the
    exact failure that shipped on the compliance board."""
    import pathlib
    css = (pathlib.Path(__file__).resolve().parent.parent
           / "app" / "static" / "app.css").read_text()
    for bucket in app_risk.RISK_BUCKETS:
        assert app_risk.risk_label(bucket)
        tone = app_risk.risk_tone(bucket)
        assert tone == "" or f".{tone} {{" in css, f"{bucket} -> .{tone} is not styled"
