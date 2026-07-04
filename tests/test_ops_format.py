"""Tests for ops response formatting."""

from ops_format import format_ops_response


def test_format_ops_response_text_output():
    r = format_ops_response({"output": "line one\nline two"})
    assert r.status_code == 200
    assert "line one" in r.body.decode()


def test_format_ops_response_table_from_list():
    r = format_ops_response([
        {"mac": "aa:bb:cc:dd:ee:ff", "vlan": 10, "port": "1/1/1"},
    ])
    body = r.body.decode()
    assert "aa:bb:cc:dd:ee:ff" in body
    assert "<table" in body


def test_format_ops_response_avoids_raw_dict_repr():
    r = format_ops_response({"secretKey": "value", "count": 3})
    body = r.body.decode()
    assert "secretKey" in body
    assert "{" not in body or "<table" in body
