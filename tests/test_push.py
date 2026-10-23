import json
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from pywebpush import WebPushException

from app import push, service
from app.db import get_setting
from tests.test_app import (  # noqa: F401
    ADMIN_PIN,
    checkin_form,
    client,
    db_call,
    import_teams,
    login,
)


def fake_sub(n: int) -> dict:
    return {"endpoint": f"https://push.example/{n}", "keys": {"p256dh": f"key{n}", "auth": f"auth{n}"}}


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[str, dict]]]:
    """The push services are the system boundary: record what would be delivered."""
    out: list[tuple[str, dict]] = []

    def webpush(subscription: dict, data: str, **kwargs: object) -> None:
        out.append((subscription["endpoint"], json.loads(data)))

    monkeypatch.setattr(push, "webpush", webpush)
    yield out


def join_as(c: TestClient, team_id: int, sub: int) -> None:
    c.get(f"/checkin?k={db_call(get_setting, 'reg_key')}")
    t = db_call(service.team, team_id)
    c.post(f"/checkin/{team_id}", data=checkin_form(t, phone1=f"+39 333 000 {team_id:04d}"))
    assert c.post("/push/iscrivi", json=fake_sub(sub)).json() == {"ok": True}




def test_key_and_subscription(client: TestClient) -> None:
    key = client.get("/push/chiave").json()["key"]
    assert len(key) > 80 and client.get("/push/chiave").json()["key"] == key
    assert client.post("/push/iscrivi", json={"endpoint": "http://x", "keys": {}}).status_code == 400
    assert client.post("/push/iscrivi", json=fake_sub(1)).json() == {"ok": True}
    assert db_call(push.subscriber_count) == (1, 0)
    assert "/sw.js" in client.get("/").text and "self.registration.showNotification" in client.get("/sw.js").text
    assert client.get("/manifest.webmanifest").json()["display"] == "standalone"


def test_start_sends_first_match_then_turn_notices_once(client: TestClient, sent: list) -> None:
    import_teams(client, 8)
    client.post("/admin/impostazioni", data={"title": "T", "fields": 2, "group_min": 5, "ko_min": 6, "buffer_min": 1})
    client.post("/logout")
    for t in db_call(service.teams).values():
        join_as(client, t["id"], t["id"])
    login(client, ADMIN_PIN)
    client.post("/admin/avvia")
    assert len(sent) == 8 and all(m["title"].startswith("🍺 Si parte! Prima partita alle") for _, m in sent)
    assert not any("Tocca a voi" in m["title"] for _, m in sent)

    sent.clear()
    first = next(m for m in db_call(service.matches) if m.field == 1)
    client.post(f"/partita/{first.id}", data={"s1": 10, "s2": 3})
    now, _ = db_call(lambda db: service.live(service.matches(db)))
    on_table_1 = next(m for m in now if m.field == 1)
    expected_now = {f"https://push.example/{t}" for t in (on_table_1.t1, on_table_1.t2)}
    assert {e for e, m in sent if m["title"] == "🔥 Tocca a voi! Tavolo 1"} == expected_now
    assert all(m["url"].startswith("/live/") for _, m in sent)

    sent.clear()
    client.post(f"/partita/{first.id}", data={"s1": 10, "s2": 4})  # a correction changes nobody's turn
    assert sent == []


def test_admin_broadcast_and_dropped_subscriptions(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    for n in (1, 2):
        client.post("/push/iscrivi", json=fake_sub(n))
    delivered: list[str] = []

    class Gone:
        status_code = 410

    def webpush(subscription: dict, data: str, **kwargs: object) -> None:
        if subscription["endpoint"].endswith("/2"):
            raise WebPushException("gone", response=Gone())
        delivered.append(json.loads(data)["body"])

    monkeypatch.setattr(push, "webpush", webpush)
    login(client, ADMIN_PIN)
    assert "Scrivi il testo" in client.post("/admin/notifica", data={"testo": " "}).text
    r = client.post("/admin/notifica", data={"titolo": "", "testo": "Si parte tra 10 minuti!"})
    assert "Notifica inviata a 2 telefoni." in r.text
    assert delivered == ["Si parte tra 10 minuti!"]
    assert db_call(push.subscriber_count) == (1, 0)
