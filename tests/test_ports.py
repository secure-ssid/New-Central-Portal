"""Unit tests: switch-port normalization and ops input validation helpers."""
import pytest

from routes.devices import (
    MAX_SHOW_COMMANDS,
    _normalize_ports,
    _parse_show_commands,
    _validate_ping_destination,
)


# ── _normalize_ports ─────────────────────────────────────────────────────────

REQUIRED_KEYS = {"name", "index", "alignment", "connected", "uplink", "poe",
                 "neighbour", "speed_mbps"}


class TestNormalizePorts:
    def test_non_list_input_returns_empty(self):
        assert _normalize_ports(None) == []
        assert _normalize_ports({"interfaces": []}) == []
        assert _normalize_ports("nope") == []

    def test_non_dict_entries_skipped(self):
        out = _normalize_ports(["junk", 42, None, {"name": "1/1/1", "index": 1}])
        assert len(out) == 1
        assert out[0]["name"] == "1/1/1"

    def test_contract_keys_always_present(self):
        out = _normalize_ports([{}])
        assert set(out[0]) == REQUIRED_KEYS

    def test_index_falls_back_to_list_position(self):
        out = _normalize_ports([{"name": "a"}, {"name": "b", "index": "x"}])
        assert [p["index"] for p in out] == [1, 2]

    def test_sorted_by_index(self):
        out = _normalize_ports([{"index": 3}, {"index": 1}, {"index": 2}])
        assert [p["index"] for p in out] == [1, 2, 3]

    def test_no_alignment_data_renders_single_row(self):
        out = _normalize_ports([{"index": 1}, {"index": 2}, {"index": 3}])
        assert {p["alignment"] for p in out} == {"top"}

    def test_partial_alignment_fills_by_parity(self):
        out = _normalize_ports([
            {"index": 1, "portAlignment": "Top"},
            {"index": 2},          # missing -> even index -> bottom
            {"index": 3},          # missing -> odd index -> top
            {"index": 4, "portAlignment": "BOTTOM"},
        ])
        assert [p["alignment"] for p in out] == ["top", "bottom", "top", "bottom"]

    def test_snake_case_alignment_accepted(self):
        out = _normalize_ports([{"index": 1, "port_alignment": "bottom"}])
        assert out[0]["alignment"] == "bottom"

    def test_garbage_alignment_treated_as_missing(self):
        out = _normalize_ports([{"index": 1, "portAlignment": "sideways"}])
        assert out[0]["alignment"] == "top"  # single-row fallback

    @pytest.mark.parametrize("status,expected", [
        ("Connected", True), ("up", True), ("UP", True),
        ("Down", False), ("disabled", False), (None, False), ("", False),
    ])
    def test_connected_from_status(self, status, expected):
        assert _normalize_ports([{"status": status}])[0]["connected"] is expected

    @pytest.mark.parametrize("speed,expected", [
        (1_000_000_000, 1000),
        (10_000_000_000, 10000),
        ("2500000000", 2500),
        (0, None),            # zero -> unknown
        (-100, None),
        (None, None),
        ("fast", None),
    ])
    def test_speed_bps_to_mbps(self, speed, expected):
        assert _normalize_ports([{"speed": speed}])[0]["speed_mbps"] == expected

    @pytest.mark.parametrize("poe,expected", [
        ("Delivering", True), ("Searching", True),
        ("Not Used", False), ("not used", False), ("", False), (None, False),
    ])
    def test_poe_coercion(self, poe, expected):
        assert _normalize_ports([{"poeStatus": poe}])[0]["poe"] is expected

    def test_poe_snake_case_key(self):
        assert _normalize_ports([{"poe_status": "Delivering"}])[0]["poe"] is True

    @pytest.mark.parametrize("raw,expected", [
        ({"uplink": True}, True), ({"uplink": 1}, True),
        ({"uplink": False}, False), ({}, False), ({"uplink": None}, False),
    ])
    def test_uplink_coercion(self, raw, expected):
        assert _normalize_ports([raw])[0]["uplink"] is expected

    def test_neighbour_spellings_and_whitespace(self):
        out = _normalize_ports([
            {"neighbour": " ap-1 "},
            {"neighbor": "ap-2"},
            {"neighbour": "   "},
            {},
        ])
        assert [p["neighbour"] for p in out] == ["ap-1", "ap-2", None, None]

    def test_name_fallbacks(self):
        out = _normalize_ports([
            {"index": 7},                      # -> "Port 7"
            {"index": 8, "id": "eth8"},        # -> id
            {"index": 9, "name": "1/1/9"},     # -> name
        ])
        assert [p["name"] for p in out] == ["Port 7", "eth8", "1/1/9"]


# ── show-command allowlist ───────────────────────────────────────────────────

class TestShowCommands:
    def test_single_command_ok(self):
        cmds, err = _parse_show_commands("show version")
        assert err is None
        assert cmds == ["show version"]

    def test_multiple_commands_split_and_squeezed(self):
        cmds, err = _parse_show_commands("  show   version ; show ip route ;")
        assert err is None
        assert cmds == ["show version", "show ip route"]

    def test_case_insensitive_show_prefix(self):
        cmds, err = _parse_show_commands("SHOW Version")
        assert err is None

    def test_empty_input_rejected(self):
        cmds, err = _parse_show_commands("")
        assert cmds is None and err

    def test_too_many_commands_rejected(self):
        raw = ";".join(["show version"] * (MAX_SHOW_COMMANDS + 1))
        cmds, err = _parse_show_commands(raw)
        assert cmds is None and "Too many" in err

    def test_overlong_command_rejected(self):
        cmds, err = _parse_show_commands("show " + "a" * 150)
        assert cmds is None and "too long" in err.lower()

    def test_non_show_command_rejected(self):
        cmds, err = _parse_show_commands("reload")
        assert cmds is None and "show" in err.lower()

    @pytest.mark.parametrize("evil", [
        "show version | include foo",
        "show version && reboot",
        "show `id`",
        "show version > /tmp/x",
        "show 'quoted'",
        'show "quoted"',
        "show $(reboot)",
    ])
    def test_shell_metacharacters_rejected(self, evil):
        cmds, err = _parse_show_commands(evil)
        assert cmds is None
        assert err

    def test_newline_collapsed_into_single_command(self):
        # All whitespace (incl. newlines) is squeezed: this stays ONE command,
        # so a newline can never smuggle in a second, non-show command.
        cmds, err = _parse_show_commands("show version\nreboot")
        assert err is None
        assert cmds == ["show version reboot"]

    def test_interface_punctuation_allowed(self):
        cmds, err = _parse_show_commands("show interface 1/1/1, 1/1/2")
        assert err is None


# ── ping destination validation ──────────────────────────────────────────────

class TestPingDestination:
    @pytest.mark.parametrize("good", [
        "8.8.8.8", "10.0.0.1", "2001:db8::1", "::1",
        "example.com", "host-1.internal.example.com", "localhost", "a1",
    ])
    def test_valid_destinations(self, good):
        assert _validate_ping_destination(good) == good

    def test_whitespace_trimmed(self):
        assert _validate_ping_destination("  8.8.8.8  ") == "8.8.8.8"

    @pytest.mark.parametrize("evil", [
        "", "   ", None,
        "8.8.8.8; rm -rf /",
        "evil.com && reboot",
        "$(whoami).example.com",
        "fe80::1%eth0",          # scoped IPv6 rejected by design
        "host name with spaces",
        "-leadinghyphen.example.com",
        "trailing-.example.com",
        "a" * 254,               # > 253 chars
        "exa_mple.com",          # underscore not in hostname grammar
    ])
    def test_invalid_destinations(self, evil):
        assert _validate_ping_destination(evil) is None
