"""The links that connect the main portal pages to the deep tools.

Every one of these tools was built against real data and then left reachable
only from the Lab menu, which is not where anyone with the question is
standing. These tests pin the connections so a template edit cannot quietly
strand a tool again — the same class of failure as the badge class that
shipped styled on one page and unstyled on another.

They deliberately assert the href AND that the target answers, because a link
to a 404 passes any test that only greps the markup.
"""
import re

import pytest


CROSS_LINKS = [
    # (page that should carry the link, href it should carry, why)
    ("/devices/SW1SERIAL", "/lab/device-scope?serial=SW1SERIAL",
     "trends, PoE budget, VLANs and interface errors live only on the deep-dive"),
    ("/clients/AA:11:22:33:44:55", "/lab/app-visibility?client=AA:11:22:33:44:55",
     "per-client application attribution exists nowhere else"),
    ("/alerts/", "/lab/activity",
     "the alerts hub shows Active only; resolved history is on the activity page"),
]


@pytest.mark.parametrize("page,href,why", CROSS_LINKS)
def test_the_link_is_present(client, mock_central, stub_db, page, href, why):
    r = client.get(page)
    assert r.status_code == 200, page
    assert f'href="{href}"' in r.text, f"{page} should link to {href} — {why}"


@pytest.mark.parametrize("page,href,why", CROSS_LINKS)
def test_the_link_target_answers(client, mock_central, stub_db, page, href, why):
    """A link to a 404 satisfies any test that only greps for the href."""
    assert client.get(href).status_code == 200, href


def test_compliance_is_promoted_to_the_main_nav(client, mock_central, stub_db):
    """It answers a standing question ("what is out of date, what is out of
    sync") that nothing else in the main nav covers."""
    r = client.get("/").text
    assert 'href="/lab/compliance"' in r, "Compliance should be in the sidebar nav"


def test_the_promoted_page_highlights_itself_not_lab(client, mock_central, stub_db):
    """Same arrangement GreenLake already uses: the route lives under /lab but
    the nav entry that is current must be the promoted one."""
    r = client.get("/lab/compliance").text
    nav = r[r.index('<div class="nav-section">Operations</div>'):
            r.index('<div class="nav-section">Inventory</div>')]
    assert 'href="/lab/compliance" class="nav-link active"' in nav
    assert 'aria-current="page"' in nav
    # ...and exactly one entry is current across the whole sidebar.
    assert r.count('aria-current="page"') == 1


def test_the_promoted_page_no_longer_claims_you_came_from_lab(
        client, mock_central, stub_db):
    r = client.get("/lab/compliance").text
    header = r[r.index("Compliance Board") - 600:r.index("Compliance Board")]
    assert 'href="/lab/"' not in header, \
        "a Lab breadcrumb is a lie once the page is reached from the main nav"


def test_compliance_stays_listed_on_the_lab_menu(client, mock_central, stub_db):
    """Promotion adds a way in; it does not remove one. GreenLake is in both."""
    assert "Compliance Board" in client.get("/lab/").text


def test_a_preselected_client_lands_on_a_loaded_drilldown(
        client, mock_central, stub_db):
    """The whole point of ?client= — arriving from a client page should show
    the answer, not a dropdown waiting to be operated."""
    r = client.get("/lab/app-visibility?client=AA:11:22:33:44:55")
    assert r.status_code == 200
    assert "No client selected" not in r.text
    assert "Disney Plus" in r.text
    # The client-scoped list, not the site-wide one.
    assert "githubcopilot.com" not in r.text.split('id="client-apps"')[1]


def test_the_preselected_client_is_selected_in_the_picker(
        client, mock_central, stub_db):
    r = client.get("/lab/app-visibility?client=AA:11:22:33:44:55").text
    option = re.search(r'<option value="AA:11:22:33:44:55"[^>]*>', r)
    assert option and "selected" in option.group(0)


def test_no_client_param_costs_no_extra_upstream_call(
        client, mock_central, stub_db, monkeypatch):
    """The drilldown fetch must be conditional — otherwise every page view pays
    for a panel nobody asked for."""
    from vendors import central_bridge as cb

    calls = []
    original = cb.list_applications

    async def counted(site_id, start, end, client_id=None, **kw):
        calls.append(client_id)
        return await original(site_id, start, end, client_id=client_id, **kw)

    monkeypatch.setattr(cb, "list_applications", counted)
    client.get("/lab/app-visibility")
    assert calls == [None], f"expected one site-wide fetch, got {calls}"
