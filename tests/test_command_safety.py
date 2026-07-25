"""Command validation and request-body handling on the write/exec routes.

/lab/config ran whatever was typed, split on ';', straight at the switch —
while /devices/{serial}/show, the same capability reached from the Devices
page, validated it. Both now use the one validator.

The JSON handlers parsed the body with no guard, so a malformed request came
back as an unhandled 500 with a stack trace in the log, rather than a 400.
"""
import pytest

from routes.devices import _parse_show_commands


# ── The shared show-command validator ────────────────────────────────────────

@pytest.mark.parametrize("raw", [
    "show version",
    "show running-config",
    "show interface 1/1/1 status",
    "show vlan; show version",
    "  show   version  ",
])
def test_accepts_ordinary_show_commands(raw):
    cmds, err = _parse_show_commands(raw)
    assert err is None
    assert cmds and all(c.lower().startswith("show ") for c in cmds)


@pytest.mark.parametrize("raw", [
    "reload",
    "configure terminal",
    "write erase",
    "show version | include foo",       # pipe
    "show version && reload",           # chained
    "show version `id`",                # backtick
    "show version > /tmp/out",          # redirect
    'show "version"',                   # quotes
    "show version; reload",             # second command not a show
    "",
    "   ",
    ";;;",
])
def test_rejects_anything_that_is_not_a_plain_show(raw):
    cmds, err = _parse_show_commands(raw)
    assert cmds is None
    assert err


def test_a_newline_cannot_smuggle_in_a_second_command():
    """Not rejected — neutralised. `" ".join(c.split())` collapses the newline,
    so the payload becomes the single nonsense command "show version reload"
    rather than two commands, and the device rejects it as an unknown show."""
    cmds, err = _parse_show_commands("show version\nreload")
    assert err is None
    assert cmds == ["show version reload"], "must stay one command, not split"


def test_rejects_an_over_long_command():
    cmds, err = _parse_show_commands("show " + "a" * 200)
    assert cmds is None and "too long" in err.lower()


def test_rejects_too_many_commands():
    cmds, err = _parse_show_commands("; ".join(["show version"] * 50))
    assert cmds is None and "too many" in err.lower()


def test_lab_config_uses_the_same_validator_as_the_devices_page():
    """Not a second copy: a divergent duplicate is how this hole opened."""
    import routes.lab as lab

    assert lab._parse_show_commands is _parse_show_commands


# ── JSON body handling ───────────────────────────────────────────────────────

_JSON_ROUTES = [
    "/devices/assign-group",
    "/devices/assign-site",
    "/lab/greenlake/assign-subscription",
    "/lab/greenlake/unassign-subscription",
    "/lab/greenlake/add-device",
    "/lab/greenlake/assign-to-central",
]


@pytest.mark.parametrize("path", _JSON_ROUTES)
def test_malformed_json_is_a_client_error_not_a_crash(client, mock_central, stub_db, path):
    r = client.post(path, content=b"{not json", headers={"content-type": "application/json"})
    assert r.status_code == 400, f"{path} returned {r.status_code} for malformed JSON"


@pytest.mark.parametrize("path", _JSON_ROUTES)
def test_non_object_json_is_a_client_error(client, mock_central, stub_db, path):
    """`[1,2,3]` parses fine, then .get() blows up on a list."""
    r = client.post(path, json=[1, 2, 3])
    assert r.status_code == 400, f"{path} returned {r.status_code} for a JSON array"


@pytest.mark.parametrize("path", _JSON_ROUTES)
def test_empty_body_is_a_client_error(client, mock_central, stub_db, path):
    r = client.post(path, content=b"", headers={"content-type": "application/json"})
    assert r.status_code == 400, f"{path} returned {r.status_code} for an empty body"
