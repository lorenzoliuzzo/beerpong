import base64
import json
from collections.abc import Iterator

import pytest
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from pywebpush import WebPushException

from app import push, service
from tests.test_app import (  # noqa: F401
    ADMIN_PIN,
    record,
    check_in,
    client,
    db_call,
    import_teams,
    login,
)


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def browser_keys() -> dict:
    point = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return {"p256dh": b64(point), "auth": b64(b"0123456789abcdef")}


def fake_sub(n: int) -> dict:
    return {"endpoint": f"https://fcm.googleapis.com/fcm/send/{n}", "keys": {"p256dh": b64(b"\x04" + bytes(64)), "auth": b64(bytes(16))}}


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[str, dict]]]:
    """The push services are the system boundary: record what would be delivered."""
    out: list[tuple[str, dict]] = []

    def webpush(subscription: dict, data: str, **kwargs: object) -> None:
        out.append((subscription["endpoint"], json.loads(data)))

    monkeypatch.setattr(push, "webpush", webpush)
    yield out


def join_as(c: TestClient, team_id: int, sub: int) -> None:
    check_in(c, team_id, phone1=f"+39 333 000 {team_id:04d}")
    assert c.post("/push/iscrivi", json=fake_sub(sub)).json() == {"ok": True}




def test_key_and_subscription(client: TestClient) -> None:
    key = client.get("/push/chiave").json()["key"]
    assert len(key) > 80 and client.get("/push/chiave").json()["key"] == key
    assert client.post("/push/iscrivi", json={"endpoint": "http://x", "keys": {}}).status_code == 422
    assert client.post("/push/iscrivi", json={"endpoint": 5}).status_code == 422
    evil = {**fake_sub(9), "endpoint": "https://10.0.0.1/steal"}
    assert client.post("/push/iscrivi", json=evil).status_code == 400
    short_keys = {**fake_sub(9), "keys": {"p256dh": "a" * 20, "auth": "b" * 10}}
    assert client.post("/push/iscrivi", json=short_keys).status_code == 400
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
    record(client, first.id, 6, 3)
    now, _ = db_call(lambda db: service.live(service.matches(db)))
    on_table_1 = next(m for m in now if m.field == 1)
    expected_now = {f"https://fcm.googleapis.com/fcm/send/{t}" for t in (on_table_1.t1, on_table_1.t2)}
    assert {e for e, m in sent if m["title"] == "🔥 Tocca a voi! Tavolo 1"} == expected_now
    assert all(m["url"] == "/io" for _, m in sent)

    sent.clear()
    record(client, first.id, 6, 4)  # a correction changes nobody's turn
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


def test_real_signing_reaches_the_push_service(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """pywebpush signs and encrypts for real; only the HTTP request to the push service is faked."""
    client.get("/push/chiave")
    client.post("/push/iscrivi", json={"endpoint": "https://fcm.googleapis.com/fcm/send/real", "keys": browser_keys()})
    client.post("/push/iscrivi", json={"endpoint": "https://fcm.googleapis.com/fcm/send/2", "keys": browser_keys()})
    client.post("/push/iscrivi", json={"endpoint": "https://fcm.googleapis.com/fcm/send/3", "keys": browser_keys()})
    posts: list[dict] = []

    class Created:
        status_code = 201
        text = ""
        headers: dict = {}

    def post(url: str, **kwargs: object) -> Created:
        if url.endswith("/2"):
            raise requests.ConnectionError("unreachable")
        posts.append({"url": url, **kwargs})
        return Created()

    monkeypatch.setattr(requests, "post", post)
    login(client, ADMIN_PIN)
    client.post("/admin/impostazioni", data={"title": "T", "fields": 2, "group_min": 5, "ko_min": 6, "buffer_min": 1,
                                             "public_url": "https://torneo.example.it:8443/"})
    client.post("/admin/notifica", data={"testo": "Prova"})
    # the unreachable service (/2) is skipped and the next one (/3) still gets its notice
    assert [p["url"].rsplit("/", 1)[1] for p in posts] == ["real", "3"] and posts[0]["timeout"] == push.SEND_TIMEOUT_S
    headers = posts[0]["headers"]
    assert headers["Authorization"].startswith("vapid t=") and headers["Content-Encoding"] == "aes128gcm"
    assert push._contact("https://torneo.example.it:8443/") == "https://torneo.example.it"
    assert push._contact("http://192.168.1.5:8000") == "https://localhost"


def test_leaving_a_couple_unlinks_the_phone(client: TestClient) -> None:
    import_teams(client, 4)
    client.post("/logout")
    join_as(client, 1, 1)
    assert db_call(push.subscriber_count) == (1, 1)
    client.post("/esci-squadra")
    client.post("/push/iscrivi", json=fake_sub(1))  # the page re-posts the subscription on every load
    assert db_call(push.subscriber_count) == (1, 0)


def test_bad_subscriptions_do_not_lock_staff_out(client: TestClient) -> None:
    for _ in range(12):
        client.post("/push/iscrivi", json={**fake_sub(1), "endpoint": "https://evil.example/x"})
    login(client, ADMIN_PIN)
