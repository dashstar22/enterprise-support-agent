"""Cycle 1 success and error contract tests. / Cycle 1 成功与错误接口契约测试。"""

from fastapi.testclient import TestClient
from httpx2 import Response
from pydantic import TypeAdapter

from app.api.schemas import (
    ErrorResponse,
    FeedbackCreatedResponse,
    MessageResponse,
    SessionCreatedResponse,
    SessionDetailResponse,
)
from app.config import Settings
from app.main import SERVICE_NAME, create_app
from app.observability.request_context import REQUEST_ID_HEADER, TRACE_ID_HEADER

message_response_adapter: TypeAdapter[MessageResponse] = TypeAdapter(MessageResponse)


def assert_controlled_error(
    response: Response,
    *,
    expected_status: int,
    expected_code: str,
) -> ErrorResponse:
    """Validate one error body and its trace header. / 校验一个错误响应体及其追踪响应头。"""

    error = ErrorResponse.model_validate(response.json())
    assert response.status_code == expected_status
    assert error.error.code == expected_code
    assert error.trace_id == response.headers[TRACE_ID_HEADER]
    assert response.headers[REQUEST_ID_HEADER]
    return error


def test_cycle_one_success_responses_follow_public_schemas() -> None:
    """Every implemented Cycle 1 success route keeps its public contract. / Cycle 1 成功接口保持公开结构。"""

    application = create_app(Settings(_env_file=None, environment="test"))

    with TestClient(application) as client:
        health = client.get("/api/v1/health")
        created = client.post("/api/v1/sessions", json={"device_model": " E-200 "})
        session = SessionCreatedResponse.model_validate(created.json())
        message = client.post(
            f"/api/v1/sessions/{session.session_id}/messages",
            json={"message": "无法启动", "fault_code": "E01"},
        )
        query = client.get(f"/api/v1/sessions/{session.session_id}")
        feedback = client.post(
            "/api/v1/feedback",
            json={
                "session_id": str(session.session_id),
                "target_trace_id": message.json()["trace_id"],
                "rating": "helpful",
            },
        )

    assert health.status_code == 200
    assert health.json() == {
        "status": "ok",
        "service": SERVICE_NAME,
        "environment": "test",
    }
    assert created.status_code == 201
    assert session.device_model == "E-200"
    assert message.status_code == 200
    message_result = message_response_adapter.validate_python(message.json())
    assert message_result.trace_id == message.headers[TRACE_ID_HEADER]
    assert query.status_code == 200
    detail = SessionDetailResponse.model_validate(query.json())
    assert len(detail.messages) == 1
    assert feedback.status_code == 201
    feedback_result = FeedbackCreatedResponse.model_validate(feedback.json())
    assert feedback_result.target_trace_id == message_result.trace_id
    assert feedback_result.trace_id == feedback.headers[TRACE_ID_HEADER]


def test_unknown_route_uses_controlled_not_found_response() -> None:
    """Framework 404 errors use the same public error shape. / 框架产生的 404 也使用统一错误结构。"""

    application = create_app(Settings(_env_file=None, environment="test"))

    with TestClient(application) as client:
        response = client.get(
            "/api/v1/not-a-real-route",
            headers={REQUEST_ID_HEADER: "route-error-001"},
        )

    error = assert_controlled_error(
        response,
        expected_status=404,
        expected_code="ROUTE_NOT_FOUND",
    )
    assert error.error.message == "请求的接口不存在。"
    assert error.error.retryable is False
    assert response.headers[REQUEST_ID_HEADER] == "route-error-001"


def test_validation_errors_hide_submitted_values_and_do_not_write() -> None:
    """Body and path validation failures hide raw input. / 请求体和路径校验失败不回显原始输入。"""

    application = create_app(Settings(_env_file=None, environment="test"))
    secret_body = "secret-device-value-must-not-leak"
    invalid_path = "secret-invalid-session-id"

    with TestClient(application) as client:
        invalid_body = client.post(
            "/api/v1/sessions",
            json={"device_model": secret_body * 5},
        )
        invalid_session_path = client.get(f"/api/v1/sessions/{invalid_path}")

    for response in (invalid_body, invalid_session_path):
        error = assert_controlled_error(
            response,
            expected_status=422,
            expected_code="REQUEST_VALIDATION_ERROR",
        )
        assert error.error.message == "请求参数格式无效。"
        assert error.error.retryable is False

    combined_response_text = invalid_body.text + invalid_session_path.text
    assert secret_body not in combined_response_text
    assert invalid_path not in combined_response_text
    assert application.state.session_store.count == 0


def test_unexpected_exception_is_safe_and_retryable() -> None:
    """Unknown failures return a safe 500 without exception details. / 未知失败返回安全的 500。"""

    application = create_app(Settings(_env_file=None, environment="test"))
    secret_exception_text = "secret-internal-diagnostic-must-not-leak"

    @application.get("/test/unexpected-error")
    def raise_unexpected_error() -> None:
        raise RuntimeError(secret_exception_text)

    with TestClient(application, raise_server_exceptions=False) as client:
        response = client.get(
            "/test/unexpected-error",
            headers={REQUEST_ID_HEADER: "unexpected-error-001"},
        )

    error = assert_controlled_error(
        response,
        expected_status=500,
        expected_code="INTERNAL_SERVER_ERROR",
    )
    assert error.error.message == "服务暂时无法处理请求，请稍后重试。"
    assert error.error.retryable is True
    assert secret_exception_text not in response.text
    assert "Traceback" not in response.text
    assert response.headers[REQUEST_ID_HEADER] == "unexpected-error-001"
