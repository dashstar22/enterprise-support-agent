"""Create-session API tests. / 创建会话接口测试。"""

from datetime import datetime, timedelta
from typing import cast
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.sessions import InMemorySessionStore
from app.config import Settings
from app.main import create_app


def get_store(application: FastAPI) -> InMemorySessionStore:
    """Return the typed store attached by the application factory. / 返回应用工厂挂载的带类型存储。"""

    return cast(InMemorySessionStore, application.state.session_store)


def test_create_session_returns_uuid_and_utc_timestamp() -> None:
    """A successful request returns a stable created-session contract. / 成功请求返回稳定会话结构。"""

    application = create_app(Settings(_env_file=None, environment="test"))

    with TestClient(application) as client:
        response = client.post("/api/v1/sessions", json={"device_model": " E-200 "})

    payload = response.json()
    session_id = UUID(payload["session_id"])
    created_at = datetime.fromisoformat(payload["created_at"])

    assert response.status_code == 201
    assert session_id.version == 4
    assert created_at.utcoffset() == timedelta(0)
    assert payload == {
        "session_id": str(session_id),
        "status": "active",
        "device_model": "E-200",
        "created_at": payload["created_at"],
    }
    assert get_store(application).count == 1


def test_create_session_accepts_empty_context_and_generates_unique_ids() -> None:
    """Optional context does not prevent unique session creation. / 没有可选上下文也能创建唯一会话。"""

    application = create_app(Settings(_env_file=None, environment="test"))

    with TestClient(application) as client:
        first_response = client.post("/api/v1/sessions", json={})
        second_response = client.post("/api/v1/sessions", json={})

    assert first_response.status_code == 201
    assert first_response.json()["device_model"] is None
    assert first_response.json()["session_id"] != second_response.json()["session_id"]
    assert get_store(application).count == 2


def test_invalid_session_request_does_not_write_to_store() -> None:
    """Rejected input leaves the in-memory store unchanged. / 被拒绝的输入不会污染内存存储。"""

    application = create_app(Settings(_env_file=None, environment="test"))

    with TestClient(application) as client:
        response = client.post("/api/v1/sessions", json={"device_model": "   "})

    assert response.status_code == 422
    assert get_store(application).count == 0


def test_session_store_is_isolated_per_app_and_uses_configured_prefix() -> None:
    """Each application owns its store and configured route prefix. / 每个应用拥有自己的存储和路由前缀。"""

    settings = Settings(_env_file=None, environment="test", api_v1_prefix="/internal/v1")
    first_application = create_app(settings)
    second_application = create_app(settings)

    with TestClient(first_application) as client:
        configured_response = client.post("/internal/v1/sessions", json={})
        default_response = client.post("/api/v1/sessions", json={})

    assert configured_response.status_code == 201
    assert default_response.status_code == 404
    assert get_store(first_application).count == 1
    assert get_store(second_application).count == 0
