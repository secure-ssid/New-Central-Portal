"""Shell chrome: keyboard shortcuts overlay, sidebar wallboard, keyboard access.

The last two tests guard a rule the shell already follows by hand: markup that
runs an action on ``@click`` must also be reachable and runnable from the
keyboard. Tables in clients/devices/greenlake sort on a ``<th @click>`` and the
portal's own detail pages hang off a ``<tr @click>``, so a mouse-only handler
locks a keyboard user out of sorting and of opening a device at all.
"""
import pathlib
import re

TEMPLATES = pathlib.Path(__file__).resolve().parent.parent / "app" / "templates"
_CLICK_RE = re.compile(r'@click(?:\.[a-z]+)*="([^"]*)"')
_TAG_RE = re.compile(r"<([a-zA-Z][a-zA-Z0-9]*)\b([^>]*)>")


def _click_handler(attrs: str) -> str | None:
    """The action a tag's ``@click`` runs, or None when there is nothing to run.

    ``@click.stop`` with no value only stops propagation, and ``@click.self`` on
    a modal backdrop is a mouse convenience for dismissing it — neither is an
    action a keyboard user is expected to run, and the overlay's own Escape
    binding (see base.html) covers dismissal.
    """
    if "@click.self" in attrs:
        return None
    m = _CLICK_RE.search(attrs)
    if not m or not m.group(1).strip():
        return None
    return m.group(1)


# The actions a keyboard user must be able to run from a table or a disclosure
# header. Deliberately not every @click: the shell's dismissals (`close()`,
# `onOutside($event)`, `sidebarOpen = false`) are mouse conveniences with an
# Escape binding of their own, and a click-outside backdrop is not an action.
_ACTION_HANDLERS = ("sortBy(", "= !", "openModal(")   # sort a column, expand a result, open a row


def test_sort_and_disclosure_handlers_have_a_keyboard_path():
    offenders = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        for m in _TAG_RE.finditer(text):
            tag, attrs = m.group(1).lower(), m.group(2)
            handler = _click_handler(attrs)
            if not handler or tag in ("button", "a"):
                continue
            if not any(a in handler for a in _ACTION_HANDLERS):
                continue
            focusable = 'tabindex="0"' in attrs
            runnable = "@keydown.enter" in attrs or "@keydown.space" in attrs
            if not (focusable and runnable):
                offenders.append(f"{path.name}: <{tag} @click=\"{handler[:40]}\">")
    assert not offenders, offenders


def test_clickable_navigating_rows_contain_a_real_link():
    """A row that navigates on click must also hold a link to the same page.

    The click keeps the big target, but a link is what a keyboard user can Tab
    to (and what open-in-new-tab works on).
    """
    offenders = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"<tr\b([^>]*)>(.*?)</tr>", text, re.S):
            handler = _click_handler(m.group(1)) or ""
            if "window.location" in handler and "<a " not in m.group(2):
                offenders.append(f"{path.name}: <tr @click=\"{handler[:40]}\">")
    assert not offenders, offenders


def test_shortcuts_overlay_lists_sites_and_wlans(client, mock_central, stub_db):
    r = client.get("/devices/")
    assert r.status_code == 200
    assert "Go Sites" in r.text
    assert "Go WLANs" in r.text
    assert "Keyboard shortcuts" in r.text


def test_sidebar_wallboard_button_on_desktop(client, mock_central, stub_db):
    r = client.get("/devices/")
    assert r.status_code == 200
    assert r.text.count('aria-label="Toggle NOC wallboard mode"') >= 2
    assert "hidden md:inline-flex" in r.text
