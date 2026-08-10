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


def test_health_probe_is_always_open(client):
    body = client.get("/healthz").json()
    assert body["status"] == "ok"
    assert "version" in body


def test_readiness_reports_problems(indexed, monkeypatch):
    cfg, store = indexed
    store.close()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    res = TestClient(create_app(cfg)).get("/readyz")
    assert res.status_code == 503
    assert any("credentials" in p for p in res.json()["problems"])


def test_readiness_is_ok_when_indexed_and_credentialed(indexed, monkeypatch):
    cfg, store = indexed
    store.close()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    res = TestClient(create_app(cfg)).get("/readyz")
    assert res.status_code == 200 and res.json()["ready"] is True


def test_lint_endpoint_returns_findings(indexed):
    cfg, store = indexed
    store.close()
    (cfg.root / "src" / "bad.c").write_text(
        "void TIM2_IRQHandler(void)\n{\n    HAL_Delay(1);\n}\n", encoding="utf-8"
    )
    body = TestClient(create_app(cfg)).get("/api/lint").json()
    assert body["counts"]["error"] >= 1
    assert any(f["rule"] == "FW001" for f in body["findings"])


class TestAuth:
    @pytest.fixture()
    def secured(self, indexed):
        cfg, store = indexed
        store.close()
        return TestClient(create_app(cfg, auth_token="s3cret"))

    def test_meta_says_a_token_is_required(self, secured):
        assert secured.get("/api/meta").json()["auth_required"] is True

    def test_meta_is_open_without_a_token_configured(self, client):
        assert client.get("/api/meta").json()["auth_required"] is False

    def test_api_rejects_a_missing_token(self, secured):
        assert secured.get("/api/status").status_code == 401

    def test_api_rejects_a_wrong_token(self, secured):
        res = secured.get("/api/status", headers={"Authorization": "Bearer nope"})
        assert res.status_code == 401

    def test_api_accepts_the_right_token(self, secured):
        res = secured.get("/api/status", headers={"Authorization": "Bearer s3cret"})
        assert res.status_code == 200

    def test_chat_is_protected_too(self, secured):
        assert secured.post("/api/chat", json={"message": "hi"}).status_code == 401

    def test_probes_stay_open_for_orchestrators(self, secured):
        assert secured.get("/healthz").status_code == 200

    def test_token_can_come_from_the_environment(self, indexed, monkeypatch):
        cfg, store = indexed
        store.close()
        monkeypatch.setenv("FWCOPILOT_AUTH_TOKEN", "from-env")
        app = TestClient(create_app(cfg))
        assert app.get("/api/status").status_code == 401
        assert (
            app.get("/api/status", headers={"Authorization": "Bearer from-env"}).status_code == 200
        )


def test_oversized_message_is_rejected(client):
    res = client.post("/api/chat", json={"message": "x" * 40000})
    assert res.status_code == 422  # pydantic max_length guard


def test_write_tools_are_refused_in_read_only_server(indexed):
    from fwcopilot.tools import ToolRunner

    cfg, store = indexed
    # The server's approval policy is the flags, so a write must be refused
    # even though no interactive prompt exists.
    runner = ToolRunner(cfg, store, approve=lambda n, a: False, allow_write=True)
    assert runner.run("write_file", {"path": "x.c", "content": "int x;"}).is_error
    assert not (cfg.root / "x.c").exists()
