"""Submit-message API tests. / 提交消息接口测试。"""

from typing import cast
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.sessions import InMemorySessionStore
from app.config import Settings
from app.main import create_app


def get_store(application: FastAPI) -> InMemorySessionStore:
    """Return the typed application-local store. / 返回带类型的应用本地存储。"""

    return cast(InMemorySessionStore, application.state.session_store)


def create_session(client: TestClient, device_model: str | None = None) -> UUID:
    """Create a session through HTTP for message tests. / 通过 HTTP 创建消息测试所需会话。"""

    body = {} if device_model is None else {"device_model": device_model}
    response = client.post("/api/v1/sessions", json=body)
    assert response.status_code == 201
    return UUID(response.json()["session_id"])


def test_message_for_unknown_session_returns_not_found() -> None:
    """Messages cannot be attached to a missing session. / 消息不能写入不存在的会话。"""

    application = create_app(Settings(_env_file=None, environment="test"))

    with TestClient(application) as client:
        response = client.post(
            f"/api/v1/sessions/{uuid4()}/messages",
            json={"message": "无法启动"},
        )

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "SESSION_NOT_FOUND",
            "message": "会话不存在。",
            "retryable": False,
        },
        "trace_id": response.headers["X-Trace-ID"],
    }


def test_natural_language_fields_are_not_guessed_and_trigger_clarification() -> None:
    """Cycle 1 does not parse structured fields from prose. / Cycle 1 不从自然语言猜结构化字段。"""

    application = create_app(Settings(_env_file=None, environment="test"))

    with TestClient(application) as client:
        session_id = create_session(client)
        response = client.post(
            f"/api/v1/sessions/{session_id}/messages",
            json={"message": "设备 E-200 报 E01，无法启动"},
        )

    payload = response.json()
    assert response.status_code == 200
    assert payload["status"] == "needs_clarification"
    assert payload["question"] == "请补充设备型号和故障码。"
    assert payload["missing_fields"] == ["device_model", "fault_code"]
    assert "answer" not in payload
    assert get_store(application).message_count(session_id) == 1


def test_follow_up_reuses_device_model_and_returns_supported_fixture_answer() -> None:
    """Structured context carries forward into a cited fixture answer. / 结构化上下文可续接且返回带引用的夹具回答。"""

    application = create_app(Settings(_env_file=None, environment="test"))

    with TestClient(application) as client:
        session_id = create_session(client)
        clarification = client.post(
            f"/api/v1/sessions/{session_id}/messages",
            json={"message": "无法启动", "device_model": "E-200"},
        )
        completed = client.post(
            f"/api/v1/sessions/{session_id}/messages",
            json={"message": "故障码是 E01", "fault_code": "E01"},
        )

    clarification_payload = clarification.json()
    completed_payload = completed.json()

    assert clarification_payload["missing_fields"] == ["fault_code"]
    assert clarification_payload["question"] == "请补充故障码。"
    assert completed.status_code == 200
    assert completed_payload["status"] == "completed"
    assert completed_payload["answer"]["confidence"] == "supported"
    assert "main power switch" in completed_payload["answer"]["summary"]
    assert completed_payload["answer"]["steps"][0]["citation"]["source_name"] == (
        "e200-synthetic-maintenance-guide.md"
    )
    assert get_store(application).message_count(session_id) == 2


def test_invalid_message_does_not_write_history() -> None:
    """Schema rejection happens before history is changed. / Schema 拒绝发生在历史写入之前。"""

    application = create_app(Settings(_env_file=None, environment="test"))

    with TestClient(application) as client:
        session_id = create_session(client, device_model="E-200")
        response = client.post(
            f"/api/v1/sessions/{session_id}/messages",
            json={"message": "   ", "fault_code": "E01"},
        )

    assert response.status_code == 422
    assert get_store(application).message_count(session_id) == 0
