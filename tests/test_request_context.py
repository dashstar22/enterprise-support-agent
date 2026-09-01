"""Request and trace identifier tests. / 请求编号和追踪编号测试。"""

import re
from typing import cast
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.sessions import InMemorySessionStore
from app.config import Settings
from app.main import create_app
from app.observability.request_context import REQUEST_ID_HEADER, TRACE_ID_HEADER

GENERATED_REQUEST_ID_PATTERN = re.compile(r"^req-[0-9a-f-]{36}$")
GENERATED_TRACE_ID_PATTERN = re.compile(r"^trace-[0-9a-f-]{36}$")


def get_store(application: FastAPI) -> InMemorySessionStore:
    """Return the typed application-local store. / 返回带类型的应用本地存储。"""

    return cast(InMemorySessionStore, application.state.session_store)


def create_session(client: TestClient) -> UUID:
    """Create a session for identifier tests. / 创建关联编号测试所需会话。"""

    response = client.post("/api/v1/sessions", json={})
    assert response.status_code == 201
    return UUID(response.json()["session_id"])


def test_missing_request_id_generates_safe_unique_headers() -> None:
    """The server generates safe identifiers when the client omits them. / 客户端省略时服务端生成安全编号。"""

    application = create_app(Settings(_env_file=None, environment="test"))

    with TestClient(application) as client:
        first = client.get("/api/v1/health")
        second = client.get("/api/v1/health")

    assert GENERATED_REQUEST_ID_PATTERN.fullmatch(first.headers[REQUEST_ID_HEADER])
    assert GENERATED_TRACE_ID_PATTERN.fullmatch(first.headers[TRACE_ID_HEADER])
    assert first.headers[REQUEST_ID_HEADER] != second.headers[REQUEST_ID_HEADER]
    assert first.headers[TRACE_ID_HEADER] != second.headers[TRACE_ID_HEADER]


def test_client_request_id_is_echoed_but_client_trace_id_is_ignored() -> None:
    """Only the safe client request label is trusted. / 只信任安全的客户端请求标签。"""

    application = create_app(Settings(_env_file=None, environment="test"))
    headers = {
        REQUEST_ID_HEADER: "demo-request-001",
        TRACE_ID_HEADER: "client-trace-must-not-win",
    }

    with TestClient(application) as client:
        first = client.get("/api/v1/health", headers=headers)
        second = client.get("/api/v1/health", headers=headers)

    assert first.headers[REQUEST_ID_HEADER] == "demo-request-001"
    assert second.headers[REQUEST_ID_HEADER] == "demo-request-001"
    assert first.headers[TRACE_ID_HEADER] != "client-trace-must-not-win"
    assert first.headers[TRACE_ID_HEADER] != second.headers[TRACE_ID_HEADER]


def test_invalid_request_id_is_rejected_before_route_execution() -> None:
    """Unsafe request labels do not reach business routes. / 不安全的请求标签不能进入业务路由。"""

    application = create_app(Settings(_env_file=None, environment="test"))

    with TestClient(application) as client:
        response = client.post(
            "/api/v1/sessions",
            json={},
            headers={REQUEST_ID_HEADER: "contains spaces"},
        )

    assert response.status_code == 400
    assert response.json() == {
        "error": {
            "code": "INVALID_REQUEST_ID",
            "message": "X-Request-ID 格式无效。",
            "retryable": False,
        },
        "trace_id": response.headers[TRACE_ID_HEADER],
    }
    assert response.headers[REQUEST_ID_HEADER] != "contains spaces"
    assert GENERATED_REQUEST_ID_PATTERN.fullmatch(response.headers[REQUEST_ID_HEADER])
    assert GENERATED_TRACE_ID_PATTERN.fullmatch(response.headers[TRACE_ID_HEADER])
    assert get_store(application).count == 0


def test_message_body_and_history_match_correlation_headers() -> None:
    """Stored message identifiers match the public response headers. / 保存的消息编号与公开响应头一致。"""

    application = create_app(Settings(_env_file=None, environment="test"))

    with TestClient(application) as client:
        session_id = create_session(client)
        message = client.post(
            f"/api/v1/sessions/{session_id}/messages",
            json={
                "message": "结构化字段已补充",
                "device_model": "E-200",
                "fault_code": "E01",
            },
            headers={REQUEST_ID_HEADER: "message-request-001"},
        )
        query = client.get(f"/api/v1/sessions/{session_id}")

    assert message.json()["trace_id"] == message.headers[TRACE_ID_HEADER]
    assert message.headers[REQUEST_ID_HEADER] == "message-request-001"
    assert query.json()["messages"][0]["request_id"] == "message-request-001"
    assert query.json()["messages"][0]["response"]["trace_id"] == message.headers[TRACE_ID_HEADER]


def test_error_responses_keep_safe_correlation_headers() -> None:
    """Not-found and validation responses remain traceable. / 未找到和校验错误响应仍可追踪。"""

    application = create_app(Settings(_env_file=None, environment="test"))
    headers = {REQUEST_ID_HEADER: "error-request-001"}

    with TestClient(application) as client:
        not_found = client.get(f"/api/v1/sessions/{uuid4()}", headers=headers)
        validation_error = client.post(
            "/api/v1/sessions",
            json={"device_model": "   "},
            headers=headers,
        )

    assert not_found.status_code == 404
    assert validation_error.status_code == 422
    assert not_found.headers[REQUEST_ID_HEADER] == "error-request-001"
    assert validation_error.headers[REQUEST_ID_HEADER] == "error-request-001"
    assert not_found.headers[TRACE_ID_HEADER] != validation_error.headers[TRACE_ID_HEADER]


def test_feedback_separates_current_and_target_trace_ids() -> None:
    """Feedback request identifiers differ from the rated response trace. / 反馈请求编号与被评价响应追踪号分离。"""

    application = create_app(Settings(_env_file=None, environment="test"))

    with TestClient(application) as client:
        session_id = create_session(client)
        message = client.post(
            f"/api/v1/sessions/{session_id}/messages",
            json={
                "message": "结构化字段已补充",
                "device_model": "E-200",
                "fault_code": "E01",
            },
        )
        target_trace_id = str(message.json()["trace_id"])
        feedback = client.post(
            "/api/v1/feedback",
            json={
                "session_id": str(session_id),
                "target_trace_id": target_trace_id,
                "rating": "helpful",
            },
            headers={REQUEST_ID_HEADER: "feedback-request-001"},
        )

    payload = feedback.json()
    assert feedback.status_code == 201
    assert payload["request_id"] == feedback.headers[REQUEST_ID_HEADER]
    assert payload["trace_id"] == feedback.headers[TRACE_ID_HEADER]
    assert payload["target_trace_id"] == target_trace_id
    assert payload["trace_id"] != payload["target_trace_id"]
