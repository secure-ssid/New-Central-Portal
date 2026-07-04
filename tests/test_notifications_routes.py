"""Smoke tests for notification settings and alert-rule CRUD routes."""


def test_notifications_page_renders(client, mock_central, stub_db):
    r = client.get("/notifications/")
    assert r.status_code == 200
    assert "Notification" in r.text or "notification" in r.text.lower()


def test_save_settings(client, mock_central, stub_db, monkeypatch):
    import db as db_module

    saved = {}

    def set_setting(key, value):
        saved[key] = value

    monkeypatch.setattr(db_module, "set_setting", set_setting)
    r = client.post("/notifications/settings", json={"thresholds": "60,30,15"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert saved["thresholds"] == "60,30,15"


def test_save_settings_rejects_invalid_json(client, mock_central, stub_db):
    r = client.post("/notifications/settings", content=b"not-json", headers={"Content-Type": "application/json"})
    assert r.status_code == 400


def test_recipients_add_and_remove(client, mock_central, stub_db, monkeypatch):
    import db as db_module

    calls = []

    monkeypatch.setattr(db_module, "add_recipient", lambda email: calls.append(("add", email)))
    monkeypatch.setattr(db_module, "remove_recipient", lambda email: calls.append(("remove", email)))

    r = client.post("/notifications/recipients", json={"action": "add", "email": "Ops@Example.com"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert calls == [("add", "ops@example.com")]

    r = client.post("/notifications/recipients", json={"action": "remove", "email": "ops@example.com"})
    assert r.status_code == 200
    assert calls[-1] == ("remove", "ops@example.com")


def test_recipients_rejects_invalid_email(client, mock_central, stub_db):
    r = client.post("/notifications/recipients", json={"action": "add", "email": "not-an-email"})
    assert r.status_code == 400


def test_create_alert_rule(client, mock_central, stub_db, monkeypatch):
    import db as db_module

    monkeypatch.setattr(
        db_module, "add_alert_rule",
        lambda site, dtype, offline, cooldown: {
            "id": 1, "site_filter": site, "device_type_filter": dtype,
            "offline_minutes": offline, "cooldown_minutes": cooldown, "enabled": True,
        },
    )
    r = client.post("/notifications/rules", json={
        "site_filter": "HQ",
        "device_type_filter": "switch",
        "offline_minutes": 10,
        "cooldown_minutes": 30,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["rule"]["offline_minutes"] == 10


def test_update_alert_rule(client, mock_central, stub_db, monkeypatch):
    import db as db_module

    monkeypatch.setattr(db_module, "update_alert_rule", lambda rid, fields: rid == 1)
    r = client.patch("/notifications/rules/1", json={"enabled": False})
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_update_alert_rule_not_found(client, mock_central, stub_db, monkeypatch):
    import db as db_module

    monkeypatch.setattr(db_module, "update_alert_rule", lambda rid, fields: False)
    r = client.patch("/notifications/rules/99", json={"enabled": False})
    assert r.status_code == 404


def test_delete_alert_rule(client, mock_central, stub_db, monkeypatch):
    import db as db_module

    monkeypatch.setattr(db_module, "delete_alert_rule", lambda rid: rid == 2)
    r = client.delete("/notifications/rules/2")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_save_report_settings(client, mock_central, stub_db, monkeypatch):
    import db as db_module

    monkeypatch.setattr(db_module, "save_report_settings", lambda **kw: None)
    monkeypatch.setattr(db_module, "get_report_settings", lambda: {
        "id": 1, "enabled": True, "frequency": "weekly", "hour": 9, "last_sent": None,
    })
    r = client.post("/notifications/reports/settings", json={
        "enabled": True, "frequency": "weekly", "hour": 9,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["settings"]["frequency"] == "weekly"
