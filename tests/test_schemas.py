"""HTTP schema contract tests. / HTTP 数据结构契约测试。"""

from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import TypeAdapter, ValidationError

from app.api.schemas import (
    Citation,
    ClarificationResponse,
    CompletedResponse,
    CreateSessionRequest,
    ErrorResponse,
    InsufficientEvidenceResponse,
    MessageResponse,
    SessionCreatedResponse,
    SubmitMessageRequest,
)

SESSION_ID = "8a7b1e5f-14e8-4bd8-a79b-b3a7f70fe93b"


def test_session_request_allows_missing_model_and_trims_supplied_model() -> None:
    """A session can start before its device model is known. / 不知道设备型号时也能先创建会话。"""

    assert CreateSessionRequest().device_model is None
    assert CreateSessionRequest(device_model="  E-200  ").device_model == "E-200"


@pytest.mark.parametrize("payload", [{"device_model": "   "}, {"unexpected": "value"}])
def test_session_request_rejects_blank_model_and_unknown_fields(payload: dict[str, str]) -> None:
    """Blank values and unknown fields do not enter business code. / 空白值和未知字段不能进入业务代码。"""

    with pytest.raises(ValidationError):
        CreateSessionRequest.model_validate(payload)


def test_message_request_requires_text_and_normalizes_optional_fields() -> None:
    """A message is required while equipment fields remain optional. / 消息必填，设备字段仍可选。"""

    request = SubmitMessageRequest(
        message="  无法启动，应该怎么排查?  ",
        device_model=" E-200 ",
        fault_code=" E01 ",
    )

    assert request.message == "无法启动，应该怎么排查?"
    assert request.device_model == "E-200"
    assert request.fault_code == "E01"

    with pytest.raises(ValidationError):
        SubmitMessageRequest(message="   ")


def test_session_response_requires_uuid_and_timezone_aware_time() -> None:
    """Session identity and time are unambiguous. / 会话编号和时间不能含糊。"""

    response = SessionCreatedResponse(
        session_id=SESSION_ID,
        status="active",
        created_at=datetime(2026, 8, 29, 12, 0, tzinfo=UTC),
    )

    assert response.session_id == UUID(SESSION_ID)

    with pytest.raises(ValidationError):
        SessionCreatedResponse(
            session_id=SESSION_ID,
            status="active",
            created_at=datetime(2026, 8, 29, 12, 0),
        )


def test_completed_response_requires_a_located_citation() -> None:
    """A completed answer contains at least one located source. / 成功回答至少包含一个可定位来源。"""

    response = CompletedResponse.model_validate(
        {
            "session_id": SESSION_ID,
            "status": "completed",
            "answer": {
                "summary": "建议先检查主电源连接和保险丝。",
                "steps": [
                    {
                        "order": 1,
                        "instruction": "确认主电源开关处于开启状态。",
                        "reason": "手册将 E01 的第一步定义为电源检查。",
                        "citation": {
                            "source_name": "E-200维修手册.pdf",
                            "page": 12,
                            "section": "故障码 E01",
                        },
                    }
                ],
                "confidence": "supported",
                "handoff_required": False,
            },
            "trace_id": "trace-demo-001",
        }
    )

    assert response.answer.steps[0].citation.page == 12

    with pytest.raises(ValidationError, match="page 或 section"):
        Citation(source_name="E-200维修手册.pdf")


def test_message_response_uses_status_to_keep_branches_separate() -> None:
    """A clarification cannot carry completed-answer fields. / 追问结果不能夹带成功回答字段。"""

    adapter: TypeAdapter[MessageResponse] = TypeAdapter(MessageResponse)
    response = adapter.validate_python(
        {
            "session_id": SESSION_ID,
            "status": "needs_clarification",
            "question": "请补充设备型号。",
            "missing_fields": ["device_model"],
            "trace_id": "trace-demo-002",
        }
    )

    assert isinstance(response, ClarificationResponse)

    with pytest.raises(ValidationError, match="answer"):
        adapter.validate_python(
            {
                "session_id": SESSION_ID,
                "status": "needs_clarification",
                "question": "请补充设备型号。",
                "missing_fields": ["device_model"],
                "trace_id": "trace-demo-002",
                "answer": {"summary": "不应出现在追问中"},
            }
        )


def test_insufficient_evidence_response_always_requires_handoff() -> None:
    """No-evidence results cannot pretend that support is complete. / 无证据结果不能假装已经处理完成。"""

    response = InsufficientEvidenceResponse(
        session_id=SESSION_ID,
        status="insufficient_evidence",
        message="当前资料没有足够证据支持该故障判断，建议转人工技术支持。",
        handoff_required=True,
        trace_id="trace-demo-003",
    )

    assert response.handoff_required is True

    with pytest.raises(ValidationError):
        InsufficientEvidenceResponse(
            session_id=SESSION_ID,
            status="insufficient_evidence",
            message="当前资料没有足够证据。",
            handoff_required=False,
            trace_id="trace-demo-003",
        )


def test_error_response_rejects_stack_trace_and_hides_submitted_values() -> None:
    """Errors expose only the public contract and hide bad input. / 错误只暴露公开字段并隐藏错误输入。"""

    with pytest.raises(ValidationError, match="stack_trace"):
        ErrorResponse.model_validate(
            {
                "error": {
                    "code": "RAGFLOW_TIMEOUT",
                    "message": "知识库暂时不可用，请稍后重试。",
                    "retryable": True,
                },
                "trace_id": "trace-demo-004",
                "stack_trace": "internal details",
            }
        )

    sentinel = "must-not-appear-in-schema-error"
    with pytest.raises(ValidationError) as exc_info:
        ErrorResponse.model_validate(
            {
                "error": {
                    "code": sentinel,
                    "message": "错误",
                    "retryable": False,
                },
                "trace_id": "trace-demo-005",
            }
        )

    assert sentinel not in str(exc_info.value)
