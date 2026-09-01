"""C2-T05 branch mapping tests. / C2-T05 分支映射测试。"""

from uuid import UUID

import pytest
from pydantic import ValidationError

from app.agent.nodes import check_evidence, finish
from app.agent.state import (
    ControlledFailureOutput,
    EvidenceItem,
    SupportState,
)
from app.api.schemas import (
    ClarificationResponse,
    CompletedResponse,
    ErrorResponse,
    InsufficientEvidenceResponse,
    SupportAnswer,
)

SESSION_ID = UUID("8a7b1e5f-14e8-4bd8-a79b-b3a7f70fe93b")


def evidence() -> EvidenceItem:
    """Build one valid candidate. / 构造一条合法候选证据。"""

    return EvidenceItem(
        document_id="manual-e200-v2",
        source_name="E-200维修手册.pdf",
        page=12,
        section="故障码 E01",
        text="检查主电源和保险丝。",
        content_hash="a" * 64,
    )


def answer() -> SupportAnswer:
    """Build one cited support answer. / 构造一个带引用的排障回答。"""

    return SupportAnswer.model_validate(
        {
            "summary": "建议先检查主电源连接和保险丝。",
            "steps": [
                {
                    "order": 1,
                    "instruction": "确认主电源开关处于开启状态。",
                    "reason": "手册把电源检查列为第一步。",
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
    )


def make_state(**updates: object) -> SupportState:
    """Build one valid workflow state. / 构造一个合法工作流状态。"""

    payload: dict[str, object] = {
        "session_id": SESSION_ID,
        "user_message": "设备无法启动",
        "device_model": "E-200",
        "fault_code": "E01",
        "trace_id": "trace-c2-branch-001",
    }
    payload.update(updates)
    return SupportState.model_validate(payload)


def test_check_evidence_maps_empty_and_non_empty_candidates() -> None:
    """Empty evidence hands off; one candidate permits continuation. / 空证据转人工，有证据才允许继续。"""

    empty = check_evidence(make_state())
    sufficient = check_evidence(make_state(evidence=[evidence()]))

    assert empty.evidence_sufficient is False
    assert empty.handoff_required is True
    assert sufficient.evidence_sufficient is True
    assert sufficient.handoff_required is False


@pytest.mark.parametrize(
    "state",
    [
        make_state(
            missing_fields=["device_model"],
            clarification_question="请补充设备型号。",
        ),
        make_state(
            missing_fields=["device_model", "fault_code"],
            clarification_question="请补充设备型号和故障码。",
        ),
    ],
)
def test_finish_maps_missing_fields_to_clarification(state: SupportState) -> None:
    """Missing fields produce only a clarification response. / 缺字段只产生追问响应。"""

    response = finish(state).response

    assert isinstance(response, ClarificationResponse)
    assert response.status == "needs_clarification"
    assert response.missing_fields
    assert response.trace_id == state.trace_id


def test_finish_maps_answer_to_completed_without_conflicting_fields() -> None:
    """A supported answer produces the completed response branch. / 有依据回答进入成功分支。"""

    response = finish(make_state(answer=answer(), evidence_sufficient=True)).response

    assert isinstance(response, CompletedResponse)
    assert response.status == "completed"
    assert response.answer.handoff_required is False


def test_finish_maps_insufficient_evidence_to_handoff() -> None:
    """Evidence shortage is explicit and never a guessed answer. / 证据不足明确转人工，不猜答案。"""

    response = finish(make_state(evidence_sufficient=False, handoff_required=True)).response

    assert isinstance(response, InsufficientEvidenceResponse)
    assert response.status == "insufficient_evidence"
    assert response.handoff_required is True
    assert response.message == "当前资料没有足够证据，建议转人工。"


def test_finish_maps_controlled_failure_without_diagnostic_text() -> None:
    """Controlled errors expose only stable code and safe text. / 受控错误只暴露稳定码和安全提示。"""

    state = make_state(error_code="RAGFLOW_TIMEOUT", handoff_required=True)
    response = finish(state).response

    assert isinstance(response, ErrorResponse)
    assert response.error.code == "RAGFLOW_TIMEOUT"
    assert response.error.retryable is True
    assert "RAGFlow" not in response.error.message
    assert "RAGFLOW_TIMEOUT" not in response.error.message


def test_business_failure_can_degrade_into_success() -> None:
    """A non-handoff business failure does not block a cited answer. / 不转人工的业务失败不阻断有引用回答。"""

    response = finish(
        make_state(
            answer=answer(),
            evidence_sufficient=True,
            error_code="BUSINESS_API_TIMEOUT",
            handoff_required=False,
        )
    ).response

    assert isinstance(response, CompletedResponse)


@pytest.mark.parametrize(
    "updates",
    [
        {
            "missing_fields": ["fault_code"],
            "clarification_question": "请补充故障码。",
            "answer": answer(),
        },
        {"evidence_sufficient": False, "handoff_required": False},
        {"error_code": "RAGFLOW_TIMEOUT", "handoff_required": True, "answer": answer()},
        {"error_code": "RAGFLOW_TIMEOUT", "handoff_required": True, "evidence_sufficient": True},
    ],
)
def test_finish_rejects_conflicting_state_signals(updates: dict[str, object]) -> None:
    """Conflicting branch markers are rejected before public mapping. / 冲突分支标记在映射前被拒绝。"""

    with pytest.raises(ValidationError):
        finish(make_state(**updates))


def test_adapter_failure_shape_remains_distinct_from_success() -> None:
    """Adapter failures cannot be mistaken for successful output. / 适配器失败不能冒充成功输出。"""

    failure = ControlledFailureOutput(error_code="RAGFLOW_TIMEOUT", handoff_required=True)
    assert "evidence" not in failure.model_dump()
    assert "answer" not in failure.model_dump()
