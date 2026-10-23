import random
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db, service
from tests.test_app import client, db_call, import_teams  # noqa: F401
from tools import demo_sim
from tools.populate_demo import setup_demo


def test_demo_shortcuts_only_exist_on_the_demo(client: TestClient) -> None:
    import_teams(client, 4)
    assert client.post("/demo/coppia").status_code == 404
    assert client.post("/demo/admin").status_code == 404
    assert "demo-bar" not in client.get("/").text


def test_demo_shortcuts_enter_as_couple_or_admin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BP_DB", str(tmp_path / "demo.db"))
    db.init_db()
    conn = db.connect()
    setup_demo(conn)
    conn.close()
    from app.main import create_app

    with TestClient(create_app()) as c:
        assert "Entra come coppia" in c.get("/").text
        r = c.post("/demo/coppia", follow_redirects=False)
        assert r.headers["location"] == "/io" and "★ VOI" in c.get("/io").text
        assert c.post("/demo/admin", follow_redirects=False).headers["location"] == "/admin"
        assert c.get("/admin").status_code == 200


def test_simulator_plays_a_whole_tournament(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BP_DB", str(tmp_path / "demo.db"))
    db.init_db()
    conn = db.connect()
    setup_demo(conn)
    rng = random.Random(5)
    now = time.time()
    for _ in range(5000):
        if db.get_setting(conn, "phase") == "finished":
            break
        demo_sim.tick(conn, rng, now)
        now += 3
    assert db.get_setting(conn, "phase") == "finished"
    ms = service.matches(conn)
    assert all(m.done for m in ms if not m.bye)
    final = service.knockout_rounds(conn)[-1][1][0]
    assert final.done and final.winner in (final.t1, final.t2)
    conn.close()


def test_simulator_refuses_the_real_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BP_DB", raising=False)
    monkeypatch.setattr("sys.argv", ["demo_sim"])
    monkeypatch.setattr(db, "init_db", lambda: None)
    with pytest.raises(SystemExit, match="Only for the demo database"):
        demo_sim.main()
