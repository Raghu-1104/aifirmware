"""HTTP surface of the browser UI (no model calls)."""

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from fwcopilot.server import create_app  # noqa: E402


@pytest.fixture()
def client(indexed):
    cfg, store = indexed
    store.close()  # the app opens its own connections
    return TestClient(create_app(cfg))


def test_index_page_renders(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "fwcopilot" in res.text
    assert "<script>" in res.text  # UI is self-contained, no external assets


def test_status_exposes_board_and_index(client):
    data = client.get("/api/status").json()
    assert data["board"]["name"] == "sensor-node"
    assert data["board"]["mcu"] == "STM32F411CEU6"
    assert any("ACME1234" in c for c in data["board"]["components"])
    assert data["index"]["datasheets"] == 1
    assert data["datasheets"][0]["component"] == "U2"
    assert data["permissions"] == {"write": False, "build": False}


def test_status_reports_detected_project_shape(client):
    profile = client.get("/api/status").json()["project_profile"]
    assert "I2C" in profile["peripherals"]
    assert any("arm-none-eabi" in t for t in profile["toolchains"])


def test_search_endpoint(client):
    hits = client.get("/api/search", params={"q": "power up sequence"}).json()["hits"]
    assert hits
    assert hits[0]["locator"].startswith("ACME1234 p.")


def test_search_survives_hostile_query(client):
    res = client.get("/api/search", params={"q": 'NEAR("a" "b") OR *'})
    assert res.status_code == 200


def test_empty_message_is_rejected(client):
    assert client.post("/api/chat", json={"message": "   "}).status_code == 400


def test_write_tools_are_refused_in_read_only_server(indexed):
    from fwcopilot.tools import ToolRunner

    cfg, store = indexed
    app = create_app(cfg)  # allow_write defaults to False
    # The server's approval policy is the flags, so a write must be refused
    # even though no interactive prompt exists.
    runner = ToolRunner(cfg, store, approve=lambda n, a: False, allow_write=True)
    assert runner.run("write_file", {"path": "x.c", "content": "int x;"}).is_error
    assert not (cfg.root / "x.c").exists()
