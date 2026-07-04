"""Platform route tests — firmware compliance normalization and error UX."""

from routes.platform import _normalize_firmware_compliance


def test_normalize_firmware_compliance_from_items():
    raw = {
        "items": [
            {
                "serialNumber": "AP1SERIAL",
                "deviceName": "lobby-ap-1",
                "model": "AP-635",
                "firmwareVersion": "10.4.1.0",
                "targetVersion": "10.4.2.0",
                "complianceStatus": "Non-Compliant",
                "siteName": "HQ",
            },
            {
                "serialNumber": "SW1SERIAL",
                "deviceName": "core-sw-1",
                "firmwareVersion": "10.13.1110",
                "targetVersion": "10.13.1110",
                "status": "Compliant",
            },
        ]
    }
    out = _normalize_firmware_compliance(raw)
    assert out["summary"]["total"] == 2
    assert out["summary"]["compliant"] == 1
    assert out["summary"]["non_compliant"] == 1
    assert out["rows"][0]["serial"] == "AP1SERIAL"
    assert out["rows"][0]["current"] == "10.4.1.0"


def test_platform_config_renders(client, mock_central, stub_db):
    r = client.get("/platform/config")
    assert r.status_code == 200
    assert "Running Config" in r.text
    assert "No compliance records returned" in r.text


def test_platform_config_shows_compliance_table(client, mock_central, stub_db, monkeypatch):
    from vendors import central_bridge as cb

    async def compliance(**_kw):
        return {
            "items": [{
                "serialNumber": "SW1SERIAL",
                "deviceName": "core-sw-1",
                "firmwareVersion": "10.13.1110",
                "targetVersion": "10.13.1110",
                "complianceStatus": "Compliant",
            }]
        }

    monkeypatch.setattr(cb, "get_firmware_compliance", compliance)
    r = client.get("/platform/config")
    assert r.status_code == 200
    assert "Firmware Compliance" in r.text
    assert "core-sw-1" in r.text
    assert "1 compliant" in r.text


def test_platform_bridge_error_is_sanitized(client, mock_central, stub_db, monkeypatch):
    from vendors import central_bridge as cb

    async def boom(*a, **k):
        raise RuntimeError("secret token leak")

    monkeypatch.setattr(cb, "get_firmware_compliance", boom)
    r = client.get("/platform/config")
    assert r.status_code == 200
    assert "secret token leak" not in r.text
    assert "Central integration unavailable" in r.text


def test_nac_search_filter(client, mock_central, stub_db, monkeypatch):
    from vendors import central_bridge as cb

    async def regs(**_kw):
        return [
            {"macAddress": "aa:bb:cc:dd:ee:01", "description": "lab-printer", "role": "guest"},
            {"macAddress": "aa:bb:cc:dd:ee:02", "description": "corp-laptop", "role": "employee"},
        ]

    monkeypatch.setattr(cb, "list_mac_registrations", regs)
    r = client.get("/platform/nac?q=printer")
    assert r.status_code == 200
    assert "lab-printer" in r.text
    assert "corp-laptop" not in r.text
