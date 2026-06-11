"""Notification-bell API contract: GET /notifications/api/recent + mark-read."""
from datetime import datetime, timedelta, timezone

import db as db_module

ITEM_KEYS = {"id", "title", "body", "severity", "device_serial", "url",
             "created_at_iso", "age", "read"}


def seed(store, n=3, **kwargs):
    for i in range(n):
        store.add(f"Notification {i}", body=f"body {i}", **kwargs)


class TestRecent:
    def test_contract_shape(self, client, bell_store):
        now = datetime.now(timezone.utc)
        bell_store.add("Device offline: ap-1", body="ap-1 down 5 min",
                       severity="critical", device_serial="AP1SERIAL",
                       url="/devices/AP1SERIAL", created_at=now - timedelta(seconds=30))
        bell_store.add("Device recovered: ap-1", severity="info",
                       created_at=now - timedelta(minutes=5), read=True)

        body = client.get("/notifications/api/recent").json()
        assert set(body) == {"items", "unread"}
        assert body["unread"] == 1
        assert len(body["items"]) == 2
        for item in body["items"]:
            assert set(item) == ITEM_KEYS

        newest = body["items"][0]
        assert newest["title"] == "Device offline: ap-1"
        assert newest["severity"] == "critical"
        assert newest["device_serial"] == "AP1SERIAL"
        assert newest["url"] == "/devices/AP1SERIAL"
        assert newest["read"] is False
        # ISO timestamp parses back.
        datetime.fromisoformat(newest["created_at_iso"])

    def test_newest_first_and_capped_at_15(self, client, bell_store):
        seed(bell_store, n=20)
        body = client.get("/notifications/api/recent").json()
        assert len(body["items"]) == 15
        assert body["unread"] == 20  # unread count is global, not capped
        ids = [i["id"] for i in body["items"]]
        assert ids == sorted(ids, reverse=True)

    def test_age_strings(self, client, bell_store):
        now = datetime.now(timezone.utc)
        for delta, expected in [
            (timedelta(seconds=20), "just now"),
            (timedelta(minutes=5), "5m ago"),
            (timedelta(hours=3), "3h ago"),
            (timedelta(days=2), "2d ago"),
        ]:
            bell_store.add(f"age {expected}", created_at=now - delta)
        items = client.get("/notifications/api/recent").json()["items"]
        by_title = {i["title"]: i["age"] for i in items}
        assert by_title == {
            "age just now": "just now", "age 5m ago": "5m ago",
            "age 3h ago": "3h ago", "age 2d ago": "2d ago",
        }

    def test_naive_created_at_treated_as_utc(self, client, bell_store):
        naive = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=7)
        bell_store.add("naive", created_at=naive)
        item = client.get("/notifications/api/recent").json()["items"][0]
        assert item["age"] == "7m ago"

    def test_empty_store(self, client, bell_store):
        assert client.get("/notifications/api/recent").json() == {
            "items": [], "unread": 0}

    def test_degraded_db_returns_200_empty(self, client, dead_db):
        r = client.get("/notifications/api/recent")
        assert r.status_code == 200
        assert r.json() == {"items": [], "unread": 0}


class TestMarkRead:
    def test_mark_specific_ids(self, client, bell_store):
        seed(bell_store, n=3)
        r = client.post("/notifications/api/mark-read", json={"ids": [1, 2]})
        assert r.status_code == 200
        assert r.json() == {"ok": True, "unread": 1}
        assert [row["read"] for row in bell_store.rows] == [True, True, False]

    def test_ids_coerced_to_int(self, client, bell_store):
        seed(bell_store, n=2)
        r = client.post("/notifications/api/mark-read", json={"ids": ["1"]})
        assert r.json() == {"ok": True, "unread": 1}

    def test_mark_all(self, client, bell_store):
        seed(bell_store, n=4)
        r = client.post("/notifications/api/mark-read", json={"all": True})
        assert r.json() == {"ok": True, "unread": 0}
        assert all(row["read"] for row in bell_store.rows)

    def test_missing_ids_and_all_rejected(self, client, bell_store):
        assert client.post("/notifications/api/mark-read", json={}).status_code == 400

    def test_empty_ids_rejected(self, client, bell_store):
        r = client.post("/notifications/api/mark-read", json={"ids": []})
        assert r.status_code == 400

    def test_non_integer_ids_rejected(self, client, bell_store):
        r = client.post("/notifications/api/mark-read", json={"ids": ["x"]})
        assert r.status_code == 400
        assert r.json()["ok"] is False

    def test_malformed_json_rejected(self, client, bell_store):
        r = client.post("/notifications/api/mark-read",
                        content=b"not json",
                        headers={"content-type": "application/json"})
        assert r.status_code == 400

    def test_all_false_without_ids_rejected(self, client, bell_store):
        r = client.post("/notifications/api/mark-read", json={"all": False})
        assert r.status_code == 400

    def test_degraded_db_returns_503(self, client, dead_db):
        r = client.post("/notifications/api/mark-read", json={"all": True})
        assert r.status_code == 503
        assert r.json()["ok"] is False


def test_routes_use_db_helpers_with_expected_signatures(bell_store):
    """The fixture monkeypatches these names — fail loudly if db.py renames."""
    for name in ("get_in_app_notifications", "count_unread_notifications",
                 "mark_notifications_read"):
        assert callable(getattr(db_module, name))
