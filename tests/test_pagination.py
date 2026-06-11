"""_paginate clamping/slicing/param-preservation (devices & clients copies)."""
import pytest
from starlette.requests import Request

from routes import clients as clients_mod
from routes import devices as devices_mod


def make_request(query_string=""):
    scope = {
        "type": "http", "method": "GET", "path": "/", "headers": [],
        "query_string": query_string.encode(),
    }
    return Request(scope)


ITEMS = list(range(1, 121))  # 120 items


@pytest.fixture(params=[devices_mod, clients_mod],
                ids=["devices._paginate", "clients._paginate"])
def paginate(request):
    return request.param._paginate


class TestDefaults:
    def test_first_page_default_size(self, paginate):
        pg = paginate(make_request(""), ITEMS)
        assert pg["page"] == 1
        assert pg["per_page"] == 50
        assert pg["items"] == ITEMS[:50]
        assert pg["total"] == 120
        assert pg["total_pages"] == 3
        assert pg["has_prev"] is False
        assert pg["has_next"] is True
        assert pg["base_qs"] == ""

    def test_empty_list(self, paginate):
        pg = paginate(make_request(""), [])
        assert pg["items"] == []
        assert pg["total"] == 0
        assert pg["total_pages"] == 1  # never zero pages
        assert pg["page"] == 1
        assert pg["has_prev"] is False and pg["has_next"] is False


class TestSlicing:
    def test_middle_page(self, paginate):
        pg = paginate(make_request("page=2&per_page=10"), ITEMS)
        assert pg["items"] == ITEMS[10:20]
        assert pg["has_prev"] is True and pg["has_next"] is True

    def test_last_page_is_partial(self, paginate):
        pg = paginate(make_request("page=3&per_page=50"), ITEMS)
        assert pg["items"] == ITEMS[100:120]
        assert pg["has_next"] is False

    def test_exact_multiple(self, paginate):
        pg = paginate(make_request("per_page=60&page=2"), ITEMS)
        assert pg["items"] == ITEMS[60:]
        assert pg["total_pages"] == 2


class TestClamping:
    def test_page_beyond_end_clamps_to_last(self, paginate):
        pg = paginate(make_request("page=999"), ITEMS)
        assert pg["page"] == 3
        assert pg["items"] == ITEMS[100:]

    @pytest.mark.parametrize("raw", ["0", "-5"])
    def test_page_below_one_clamps_to_first(self, paginate, raw):
        pg = paginate(make_request(f"page={raw}"), ITEMS)
        assert pg["page"] == 1

    def test_per_page_capped_at_max(self, paginate):
        pg = paginate(make_request("per_page=9999"), ITEMS)
        assert pg["per_page"] == 200
        assert pg["items"] == ITEMS  # all 120 fit on one page

    def test_per_page_floor_is_one(self, paginate):
        pg = paginate(make_request("per_page=0"), ITEMS)
        assert pg["per_page"] == 1
        assert pg["total_pages"] == 120

    @pytest.mark.parametrize("qs", ["page=abc", "per_page=abc",
                                    "page=abc&per_page=xyz"])
    def test_non_numeric_falls_back_to_defaults(self, paginate, qs):
        pg = paginate(make_request(qs), ITEMS)
        assert pg["page"] == 1
        assert pg["per_page"] == 50


class TestParamPreservation:
    def test_base_qs_drops_page_keeps_rest(self, paginate):
        pg = paginate(make_request("page=2&per_page=10&site=hq&q=ap"), ITEMS)
        assert "page=" not in pg["base_qs"].replace("per_page=", "")
        assert "per_page=10" in pg["base_qs"]
        assert "site=hq" in pg["base_qs"]
        assert "q=ap" in pg["base_qs"]

    def test_base_qs_preserves_multi_valued_params(self, paginate):
        pg = paginate(make_request("tag=a&tag=b&page=2"), ITEMS)
        assert pg["base_qs"] == "tag=a&tag=b"

    def test_base_qs_empty_when_only_page(self, paginate):
        pg = paginate(make_request("page=3"), ITEMS)
        assert pg["base_qs"] == ""


def test_both_implementations_agree():
    """devices.py and clients.py carry copies — they must behave identically."""
    for qs in ("", "page=2&per_page=7", "page=-1", "per_page=100000",
               "page=zzz&site=hq"):
        a = devices_mod._paginate(make_request(qs), ITEMS)
        b = clients_mod._paginate(make_request(qs), ITEMS)
        assert a == b, f"divergence for {qs!r}"


def test_constants_match():
    assert devices_mod.DEFAULT_PER_PAGE == clients_mod.DEFAULT_PER_PAGE == 50
    assert devices_mod.MAX_PER_PAGE == clients_mod.MAX_PER_PAGE == 200
