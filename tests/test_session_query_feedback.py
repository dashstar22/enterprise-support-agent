"""Session-query and feedback API tests. / 会话查询和反馈接口测试。"""

from datetime import datetime, timedelta
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
    """Create a session through its public HTTP route. / 通过公开 HTTP 路由创建会话。"""

    body = {} if device_model is None else {"device_model": device_model}
    response = client.post("/api/v1/sessions", json=body)
    assert response.status_code == 201
    return UUID(response.json()["session_id"])


def submit_completed_message(client: TestClient, session_id: UUID) -> str:
    """Submit complete structured fields and return the response trace. / 提交完整字段并返回响应追踪号。"""

    response = client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={
            "message": "结构化字段已补充",
            "device_model": "E-200",
            "fault_code": "E01",
        },
    )
    assert response.status_code == 200
    return str(response.json()["trace_id"])


def test_query_new_session_returns_empty_history() -> None:
    """A new session has a stable empty history. / 新会话返回稳定的空历史。"""

    application = create_app(Settings(_env_file=None, environment="test"))

    with TestClient(application) as client:
        session_id = create_session(client, device_model="E-200")
        response = client.get(f"/api/v1/sessions/{session_id}")

    assert response.status_code == 200
    assert response.json()["session_id"] == str(session_id)
    assert response.json()["device_model"] == "E-200"
    assert response.json()["messages"] == []


def test_query_session_returns_context_and_messages_in_received_order() -> None:
    """History preserves both request-response pairs in order. / 历史按顺序保留请求与响应。"""

    application = create_app(Settings(_env_file=None, environment="test"))

    with TestClient(application) as client:
        session_id = create_session(client)
        first = client.post(
            f"/api/v1/sessions/{session_id}/messages",
            json={"message": "第一条", "device_model": "E-200"},
        )
        second = client.post(
            f"/api/v1/sessions/{session_id}/messages",
            json={"message": "第二条", "fault_code": "E01"},
        )
        query = client.get(f"/api/v1/sessions/{session_id}")

    payload = query.json()
    messages = payload["messages"]

    assert first.json()["status"] == "needs_clarification"
    assert second.json()["status"] == "completed"
    assert payload["device_model"] == "E-200"
    assert [item["request"]["message"] for item in messages] == ["第一条", "第二条"]
    assert [item["response"]["status"] for item in messages] == [
        "needs_clarification",
        "completed",
    ]
    assert all(
        datetime.fromisoformat(item["created_at"]).utcoffset() == timedelta(0) for item in messages
    )


def test_query_unknown_session_returns_not_found() -> None:
    """Unknown sessions do not produce fabricated history. / 不存在的会话不会产生伪造历史。"""

    application = create_app(Settings(_env_file=None, environment="test"))

    with TestClient(application) as client:
        response = client.get(f"/api/v1/sessions/{uuid4()}")

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "SESSION_NOT_FOUND",
            "message": "会话不存在。",
            "retryable": False,
        },
        "trace_id": response.headers["X-Trace-ID"],
    }


def test_feedback_records_only_an_existing_response_trace() -> None:
    """Valid feedback targets one existing message response. / 合法反馈精确指向一次已有消息响应。"""

    application = create_app(Settings(_env_file=None, environment="test"))

    with TestClient(application) as client:
        session_id = create_session(client)
        trace_id = submit_completed_message(client, session_id)
        response = client.post(
            "/api/v1/feedback",
            json={
                "session_id": str(session_id),
                "target_trace_id": trace_id,
                "rating": "helpful",
                "reason": "  结构清楚  ",
            },
        )

    payload = response.json()
    feedback_id = UUID(payload["feedback_id"])
    created_at = datetime.fromisoformat(payload["created_at"])

    assert response.status_code == 201
    assert feedback_id.version == 4
    assert created_at.utcoffset() == timedelta(0)
    assert payload["status"] == "recorded"
    assert payload["target_trace_id"] == trace_id
    assert payload["reason"] == "结构清楚"
    assert get_store(application).feedback_count == 1


def test_feedback_rejects_unknown_session_and_trace_without_writing() -> None:
    """Missing targets leave feedback storage unchanged. / 不存在的目标不会污染反馈存储。"""

    application = create_app(Settings(_env_file=None, environment="test"))

    with TestClient(application) as client:
        session_id = create_session(client)
        unknown_trace = client.post(
            "/api/v1/feedback",
            json={
                "session_id": str(session_id),
                "target_trace_id": "trace-missing",
                "rating": "not_helpful",
            },
        )
        unknown_session = client.post(
            "/api/v1/feedback",
            json={
                "session_id": str(uuid4()),
                "target_trace_id": "trace-missing",
                "rating": "not_helpful",
            },
        )

    assert unknown_trace.status_code == 404
    assert unknown_trace.json()["error"] == {
        "code": "FEEDBACK_TARGET_NOT_FOUND",
        "message": "反馈目标不存在。",
        "retryable": False,
    }
    assert unknown_trace.json()["trace_id"] == unknown_trace.headers["X-Trace-ID"]
    assert unknown_session.status_code == 404
    assert unknown_session.json()["error"] == {
        "code": "SESSION_NOT_FOUND",
        "message": "会话不存在。",
        "retryable": False,
    }
    assert unknown_session.json()["trace_id"] == unknown_session.headers["X-Trace-ID"]
    assert get_store(application).feedback_count == 0


def test_invalid_feedback_does_not_write_to_store() -> None:
    """Invalid rating and blank reason are rejected before storage. / 非法评分和空白原因在存储前被拒绝。"""

    application = create_app(Settings(_env_file=None, environment="test"))

    with TestClient(application) as client:
        session_id = create_session(client)
        trace_id = submit_completed_message(client, session_id)
        invalid_rating = client.post(
            "/api/v1/feedback",
            json={
                "session_id": str(session_id),
                "target_trace_id": trace_id,
                "rating": "maybe",
            },
        )
        blank_reason = client.post(
            "/api/v1/feedback",
            json={
                "session_id": str(session_id),
                "target_trace_id": trace_id,
                "rating": "helpful",
                "reason": "   ",
            },
        )

    assert invalid_rating.status_code == 422
    assert blank_reason.status_code == 422
    assert get_store(application).feedback_count == 0
