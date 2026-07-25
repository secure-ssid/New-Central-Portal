"""Site name/id resolution against the shapes Central actually returns.

Every site picker in the portal read siteName/site_name/name. Central's
/network-config/v1/sites collection returns none of those — it returns
scopeName — so the dashboard's Site Health card rendered "Unnamed site" for a
site the device list happily showed as SecureSSID, and the topology filter,
alert-rule dropdown and command-palette site search were all blank.

These fixtures are the real payload keys observed on the live tenant.
"""
import pytest

from vendors.aruba_central import site_display_name, site_id_of

# Trimmed from the live /network-config/v1/sites response.
NEW_CENTRAL_SITE = {
    "type": "network-config/sites",
    "id": "79244870000394240",
    "scopeId": "79244870000394240",
    "scopeName": "SecureSSID",
    "collectionId": None,
    "collectionName": None,
    "address": "4347 Charleswood Ave",
    "city": "Memphis",
    "deviceCount": 9,
}

# The Classic Central gateway shape the code used to assume.
CLASSIC_SITE = {
    "site_id": 42,
    "site_name": "Memphis HQ",
    "city": "Memphis",
}


def test_resolves_the_name_new_central_actually_returns():
    assert site_display_name(NEW_CENTRAL_SITE) == "SecureSSID"


def test_resolves_the_id_new_central_actually_returns():
    assert site_id_of(NEW_CENTRAL_SITE) == "79244870000394240"


def test_still_resolves_the_classic_gateway_shape():
    assert site_display_name(CLASSIC_SITE) == "Memphis HQ"
    assert site_id_of(CLASSIC_SITE) == "42"


def test_collection_name_is_used_when_scope_name_is_absent():
    assert site_display_name({"collectionName": "West Region"}) == "West Region"


@pytest.mark.parametrize("raw", [{}, {"scopeName": ""}, {"scopeName": "   "}, None, "nope"])
def test_unnamed_inputs_return_empty_rather_than_a_placeholder(raw):
    """Callers decide how to render "no name"; the resolver must not invent one."""
    assert site_display_name(raw) == ""


@pytest.mark.parametrize("raw", [{}, {"id": None}, None])
def test_missing_id_returns_empty(raw):
    assert site_id_of(raw) == ""


def test_explicit_site_name_wins_over_scope_name():
    """If a payload ever carries both, the site-specific field is preferred."""
    both = {"siteName": "Preferred", "scopeName": "Fallback"}
    assert site_display_name(both) == "Preferred"


def test_sites_page_no_longer_invents_a_site_when_central_is_down():
    """It used to return a fabricated "Memphis HQ" with 9 devices and 32 clients
    — indistinguishable from real data, on the page whose job is to say what you
    have, exactly when Central was unreachable."""
    import asyncio
    import routes.sites as sites_route

    async def boom(*a, **k):
        raise RuntimeError("central down")

    original = sites_route.__dict__.get("_load_sites")
    assert original is not None

    import vendors.central_bridge as bridge
    real = bridge.get_sites
    bridge.get_sites = boom
    try:
        assert asyncio.run(sites_route._load_sites()) == []
    finally:
        bridge.get_sites = real
