"""The topology page's JS/DOM contract.

topology.html is ~1100 lines of inline JavaScript driving a DOM defined 200
lines above it. Nothing connects the two but string ids, so restyling the page
can silently detach a control: the button still renders, it just stops doing
anything. That is invisible to a page-renders smoke test.

This parses the template and asserts every id and class the script reaches for
actually exists in the markup, and vice versa for the interactive controls.
"""
import pathlib
import re

import pytest

TEMPLATE = (
    pathlib.Path(__file__).resolve().parent.parent
    / "app" / "templates" / "topology.html"
)


@pytest.fixture(scope="module")
def parts():
    html = TEMPLATE.read_text()
    marker = "{% block scripts %}"
    assert marker in html, "topology.html should keep its scripts in a block"
    return html[: html.index(marker)], html[html.index(marker):]


def _ids_in(markup: str) -> set[str]:
    return set(re.findall(r'id="([^"]+)"', markup))


def test_every_id_the_script_looks_up_exists_in_the_markup(parts):
    markup, js = parts
    referenced = set(re.findall(r"getElementById\(['\"]([^'\"]+)['\"]\)", js))
    # 'stat-' is a prefix the script completes at runtime ('stat-' + kind).
    referenced = {r for r in referenced if r != "stat-"}
    missing = sorted(referenced - _ids_in(markup))
    assert not missing, f"script reaches for ids that no longer exist: {missing}"


def test_runtime_built_stat_ids_exist(parts):
    """The script writes counts into 'stat-' + kind for each device kind."""
    markup, _ = parts
    ids = _ids_in(markup)
    for kind in ("switch", "ap", "gateway", "online", "offline"):
        assert f"stat-{kind}" in ids, f"missing #stat-{kind} counter"


def test_query_selectors_still_match_something(parts):
    markup, js = parts
    selectors = set(re.findall(r"querySelectorAll?\(['\"]([^'\"]+)['\"]\)", js))
    for sel in selectors:
        if sel.startswith("."):
            token = sel[1:].split()[0]
            assert token in markup, f"no element carries the class {sel}"
        elif sel.startswith("#") and " " in sel:
            root = sel.split()[0][1:]
            assert f'id="{root}"' in markup, f"selector root {sel} is missing"


def test_interactive_controls_are_buttons_with_accessible_names(parts):
    """Every toolbar control the script binds a click handler to."""
    markup, js = parts
    clicked = set(re.findall(r"(\w+)\.addEventListener\('click'", js))
    assert clicked, "expected click handlers in the topology script"

    for button_id in re.findall(r'<button id="(btn-[^"]+)"', markup):
        block = markup[markup.index(f'id="{button_id}"'):]
        block = block[: block.index(">")]
        assert 'type="button"' in block, f"#{button_id} needs type=button"
        has_name = "aria-label=" in block or "title=" in block
        # A button with visible text does not need an aria-label.
        tail = markup[markup.index(f'id="{button_id}"'):]
        text = tail[tail.index(">") + 1: tail.index("</button>")]
        visible = re.sub(r"<[^>]+>", "", text).strip()
        assert has_name or visible, f"#{button_id} has no accessible name"


def test_collapsible_panels_use_the_hidden_attribute(parts):
    """The script toggles .hidden; markup that only set style.display would
    reopen on every render, and a class setting `display` outranks the UA
    [hidden] rule — hence the explicit override in the stylesheet."""
    markup, js = parts
    for panel in ("topo-filters", "topo-legend", "topo-list-panel", "topo-fallback"):
        block = markup[markup.index(f'id="{panel}"'):]
        block = block[: block.index(">")]
        assert "hidden" in block, f"#{panel} should start hidden"
    assert "[hidden] { display:none !important; }" in markup, \
        "the [hidden] override is what makes the attribute authoritative"
    assert ".style.display" not in js.replace("tierAxis.style.display", ""), \
        "panels should toggle .hidden, not style.display"


def test_legend_is_collapsible_and_remembers_the_choice(parts):
    markup, js = parts
    assert 'id="btn-legend"' in markup
    assert "setLegendOpen" in js
    assert "topoLegendOpen" in js, "the legend choice should persist"


def test_toolbar_labels_are_not_restated_by_the_script(parts):
    """State lives in the pressed style now. The script used to write
    'Layout: Hierarchy' / 'Orbit: on' / 'View: 3D' into the labels, which made
    the toolbar a wall of same-weight text."""
    _, js = parts
    for stale in ("Layout: ", "Orbit: on", "Orbit: off", "View: 3D", "View: List"):
        assert stale not in js, f"stale verbose label still written: {stale!r}"


def test_header_badges_use_the_shared_class(parts):
    """Four hand-styled inline chips previously; one shape with modifiers now.

    The badge moved to app/static/app.css after the compliance board borrowed
    it and rendered unstyled text — a page-scoped <style> block is invisible to
    every other page.
    """
    markup, _ = parts
    assert markup.count('class="badge') >= 4
    shared = (TEMPLATE.parent.parent / "static" / "app.css").read_text()
    assert ".badge {" in shared, "the badge must live in the shared stylesheet"
    assert ".topo-badge" not in markup, "the page-scoped copy should be gone"


def test_no_orphaned_style_classes_in_the_canvas_chrome(parts):
    """Catches the failure that shipped mid-redesign: markup referencing a
    class name that was never added to the stylesheet, so the element rendered
    completely unstyled."""
    markup, _ = parts
    style_block = markup[markup.index("<style>"): markup.index("</style>")]
    defined = set(re.findall(r"\.(topo-[a-z0-9-]+)", style_block))
    used = set(re.findall(r'class="([^"]*)"', markup))
    used_topo = {c for group in used for c in group.split() if c.startswith("topo-")}
    missing = sorted(used_topo - defined)
    assert not missing, f"classes used in markup but never styled: {missing}"
