"""Workflow state and node boundary tests. / 工作流状态与节点边界测试。"""

from uuid import UUID

import pytest
from pydantic import TypeAdapter, ValidationError

from app.agent.state import (
    AskClarificationInput,
    BuildRetrievalQueryInput,
    CheckEvidenceOutput,
    ControlledFailureOutput,
    EvidenceItem,
    FinishOutput,
    GenerateSupportAnswerInput,
    ParseRequestOutput,
    RetrieveEvidenceResult,
    SupportState,
    ValidateRequiredFieldsOutput,
)

SESSION_ID = "8a7b1e5f-14e8-4bd8-a79b-b3a7f70fe93b"
CONTENT_HASH = "a" * 64


def evidence_payload() -> dict[str, object]:
    """Return one valid candidate fragment. / 返回一条合法候选片段。"""

    return {
        "document_id": "manual-e200-v2",
        "source_name": "E-200维修手册.pdf",
        "page": 12,
        "section": "故障码 E01",
        "text": "检查主电源和保险丝。",
        "content_hash": CONTENT_HASH,
    }


def answer_payload() -> dict[str, object]:
    """Return one valid cited answer. / 返回一个带引用的合法回答。"""

    return {
        "summary": "建议先检查主电源连接和保险丝。",
        "steps": [
            {
                "order": 1,
                "instruction": "确认主电源开关处于开启状态。",
                "reason": "候选手册把电源检查列为第一步。",
                "citation": {
                    "source_name": "E-200维修手册.pdf",
                    "page": 12,
                    "section": "故障码 E01",
                },
            }
        ],
        "confidence": "mock",
        "handoff_required": False,
    }


def test_support_state_normalizes_fields_and_uses_independent_lists() -> None:
    """Each workflow owns normalized mutable state. / 每个工作流拥有独立且已规范化的可变状态。"""

    first = SupportState(
        session_id=SESSION_ID,
        user_message="  无法启动  ",
        device_model=" E-200 ",
        fault_code=" E01 ",
        trace_id="trace-c2-001",
    )
    second = SupportState(
        session_id=SESSION_ID,
        user_message="另一个问题",
        trace_id="trace-c2-002",
    )

    first.symptoms.append("无法启动")
    first.evidence.append(EvidenceItem.model_validate(evidence_payload()))

    assert first.session_id == UUID(SESSION_ID)
    assert first.user_message == "无法启动"
    assert first.device_model == "E-200"
    assert first.fault_code == "E01"
    assert second.symptoms == []
    assert second.evidence == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("parsed_confidence", -0.01),
        ("parsed_confidence", 1.01),
        ("generation_retry_count", -1),
        ("generation_retry_count", 2),
    ],
)
def test_support_state_rejects_invalid_bounded_values(field: str, value: float) -> None:
    """Confidence and retry limits cannot leave their safe range. / 置信度和重试次数不能越界。"""

    payload: dict[str, object] = {
        "session_id": SESSION_ID,
        "user_message": "无法启动",
        "trace_id": "trace-c2-003",
        field: value,
    }

    with pytest.raises(ValidationError):
        SupportState.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("session_id", "not-a-uuid"),
        ("user_message", "   "),
        ("trace_id", "contains spaces"),
    ],
)
def test_support_state_rejects_invalid_core_inputs(field: str, value: str) -> None:
    """Identity and original input remain trustworthy. / 身份与原始输入必须可靠。"""

    payload: dict[str, object] = {
        "session_id": SESSION_ID,
        "user_message": "无法启动",
        "trace_id": "trace-c2-003",
        field: value,
    }

    with pytest.raises(ValidationError):
        SupportState.model_validate(payload)


def test_workflow_models_reject_unknown_fields_and_hide_values() -> None:
    """Unexpected data is rejected without echoing its value. / 拒绝意外字段且不回显字段值。"""

    sentinel = "must-not-appear-in-workflow-error"
    with pytest.raises(ValidationError) as exc_info:
        SupportState.model_validate(
            {
                "session_id": SESSION_ID,
                "user_message": "无法启动",
                "trace_id": "trace-c2-004",
                "unexpected": sentinel,
            }
        )

    assert sentinel not in str(exc_info.value)


@pytest.mark.parametrize(
    "payload",
    [
        {**evidence_payload(), "page": None, "section": None},
        {**evidence_payload(), "content_hash": "not-a-sha256"},
    ],
)
def test_evidence_requires_a_locator_and_sha256(payload: dict[str, object]) -> None:
    """Candidate evidence stays locatable and versionable. / 候选证据必须可定位并可核对版本。"""

    with pytest.raises(ValidationError):
        EvidenceItem.model_validate(payload)


def test_required_field_and_handoff_outputs_enforce_branch_contracts() -> None:
    """Branch outputs reject duplicate or contradictory decisions. / 分支输出拒绝重复字段和矛盾决定。"""

    assert ValidateRequiredFieldsOutput(missing_fields=[]).missing_fields == []
    assert CheckEvidenceOutput(
        evidence_sufficient=False,
        handoff_required=True,
    ).handoff_required

    with pytest.raises(ValidationError, match="不能重复"):
        ValidateRequiredFieldsOutput(missing_fields=["device_model", "device_model"])
    with pytest.raises(ValidationError, match="不能重复"):
        AskClarificationInput(missing_fields=["fault_code", "fault_code"])
    with pytest.raises(ValidationError, match="必须与"):
        CheckEvidenceOutput(evidence_sufficient=False, handoff_required=False)


def test_node_inputs_require_only_their_confirmed_fields() -> None:
    """Later nodes cannot run without the fields they need. / 后续节点缺少必要字段时不能运行。"""

    with pytest.raises(ValidationError):
        AskClarificationInput(missing_fields=[])
    with pytest.raises(ValidationError):
        BuildRetrievalQueryInput.model_validate({"device_model": "E-200", "symptoms": ["无法启动"]})
    with pytest.raises(ValidationError):
        GenerateSupportAnswerInput(evidence=[])


def test_node_outputs_reject_updates_owned_by_other_nodes() -> None:
    """A node cannot update another node's fields. / 一个节点不能越权更新其他节点的字段。"""

    with pytest.raises(ValidationError, match="retrieval_query"):
        ParseRequestOutput.model_validate(
            {
                "device_model": "E-200",
                "fault_code": "E01",
                "parsed_confidence": 0.9,
                "retrieval_query": "不允许的越权更新",
            }
        )


def test_fallible_node_result_keeps_success_and_failure_separate() -> None:
    """Tool success cannot be mixed with a controlled failure. / 工具成功结果不能混入受控失败字段。"""

    adapter: TypeAdapter[RetrieveEvidenceResult] = TypeAdapter(RetrieveEvidenceResult)
    failure = adapter.validate_python({"error_code": "RAGFLOW_TIMEOUT", "handoff_required": True})

    assert isinstance(failure, ControlledFailureOutput)

    degraded = ControlledFailureOutput(
        error_code="BUSINESS_API_UNAVAILABLE",
        handoff_required=False,
    )
    assert degraded.handoff_required is False

    with pytest.raises(ValidationError):
        adapter.validate_python(
            {
                "evidence": [evidence_payload()],
                "error_code": "RAGFLOW_TIMEOUT",
                "handoff_required": True,
            }
        )


@pytest.mark.parametrize(
    "response",
    [
        {
            "session_id": SESSION_ID,
            "status": "needs_clarification",
            "question": "请补充设备型号。",
            "missing_fields": ["device_model"],
            "trace_id": "trace-c2-005",
        },
        {
            "session_id": SESSION_ID,
            "status": "completed",
            "answer": answer_payload(),
            "trace_id": "trace-c2-006",
        },
        {
            "session_id": SESSION_ID,
            "status": "insufficient_evidence",
            "message": "当前资料没有足够证据，建议转人工。",
            "handoff_required": True,
            "trace_id": "trace-c2-007",
        },
        {
            "error": {
                "code": "RAGFLOW_TIMEOUT",
                "message": "知识库暂时不可用。",
                "retryable": True,
            },
            "trace_id": "trace-c2-008",
        },
    ],
)
def test_finish_output_accepts_only_public_response_contracts(
    response: dict[str, object],
) -> None:
    """finish returns one existing public response shape. / finish 只能返回一种既有公开响应结构。"""

    output = FinishOutput.model_validate({"response": response})

    assert output.response.trace_id.startswith("trace-c2-")
