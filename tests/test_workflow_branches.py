"""C2-T07 local workflow branch tests. / C2-T07 本地工作流分支测试。"""

from uuid import UUID

from pydantic import BaseModel

from app.agent.adapters import AdapterError
from app.agent.fake_adapters import (
    FakeAuditRepository,
    FakeBusinessContextProvider,
    FakeEvidenceRetriever,
    FakeSupportAnswerGenerator,
)
from app.agent.nodes import (
    ask_clarification,
    check_evidence,
    finish,
    generate_support_answer,
    persist_audit,
    query_business_context,
    retrieve_evidence,
    validate_required_fields,
)
from app.agent.state import (
    BusinessContext,
    ControlledFailureOutput,
    EvidenceItem,
    SupportState,
)
from app.api.schemas import (
    ClarificationResponse,
    CompletedResponse,
    ErrorResponse,
    InsufficientEvidenceResponse,
)

SESSION_ID = UUID("8a7b1e5f-14e8-4bd8-a79b-b3a7f70fe93b")
AUDIT_ID = UUID("22222222-2222-4222-8222-222222222222")


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


def answer_payload() -> dict[str, object]:
    """Return one valid cited answer. / 返回一个合法的带引用回答。"""

    return {
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


def make_state(**updates: object) -> SupportState:
    """Build one valid workflow state. / 构造一个合法工作流状态。"""

    payload: dict[str, object] = {
        "session_id": SESSION_ID,
        "user_message": "设备无法启动",
        "device_model": "E-200",
        "fault_code": "E01",
        "retrieval_query": "E-200 E01 无法启动",
        "trace_id": "trace-c2-workflow-001",
    }
    payload.update(updates)
    return SupportState.model_validate(payload)


def apply_update(state: SupportState, update: BaseModel) -> SupportState:
    """Apply a validated node result as a new state. / 把已校验节点结果合并成新状态。"""

    data = update.model_dump()
    return SupportState.model_validate({**state.model_dump(), **data})


def test_clarification_branch_stops_before_external_nodes() -> None:
    """Missing fields only ask a question and make no adapter calls. / 缺字段只追问且不调用外部节点。"""

    state = make_state(device_model=None, fault_code=None, retrieval_query=None)
    retriever = FakeEvidenceRetriever()
    business = FakeBusinessContextProvider()
    generator = FakeSupportAnswerGenerator(result=answer_payload())

    validated = apply_update(state, validate_required_fields(state))
    clarified = apply_update(validated, ask_clarification(validated))
    response = finish(clarified).response

    assert isinstance(response, ClarificationResponse)
    assert response.missing_fields == ["device_model", "fault_code"]
    assert retriever.requests == []
    assert business.requests == []
    assert generator.requests == []


def test_success_branch_runs_all_local_nodes_to_completed() -> None:
    """Complete local path reaches a cited completed response. / 完整本地链路最终返回带引用的成功响应。"""

    state = make_state()
    retriever = FakeEvidenceRetriever(result=[evidence()])
    business = FakeBusinessContextProvider(
        result=BusinessContext(device_model="E-200", fault_code="E01", inventory_available=True)
    )
    generator = FakeSupportAnswerGenerator(result=answer_payload())
    audit = FakeAuditRepository(result=AUDIT_ID)

    state = apply_update(state, retrieve_evidence(state, retriever))
    state = apply_update(state, check_evidence(state))
    state = apply_update(state, query_business_context(state, business))
    state = apply_update(state, generate_support_answer(state, generator))
    state = apply_update(state, persist_audit(state, audit))
    response = finish(state).response

    assert isinstance(response, CompletedResponse)
    assert response.answer.steps[0].citation.source_name == "E-200维修手册.pdf"
    assert state.audit_record_id == AUDIT_ID
    assert len(retriever.requests) == 1
    assert len(business.requests) == 1
    assert len(generator.requests) == 1
    assert len(audit.requests) == 1


def test_empty_evidence_branch_handoffs_without_answer_generation() -> None:
    """Empty retrieval becomes insufficient evidence and skips generation. / 空检索结果转人工且跳过回答生成。"""

    state = make_state()
    retriever = FakeEvidenceRetriever()
    generator = FakeSupportAnswerGenerator(result=answer_payload())

    state = apply_update(state, retrieve_evidence(state, retriever))
    state = apply_update(state, check_evidence(state))
    response = finish(state).response

    assert isinstance(response, InsufficientEvidenceResponse)
    assert state.handoff_required is True
    assert generator.requests == []


def test_retrieval_failure_branch_returns_controlled_error() -> None:
    """Retrieval failure maps to a stable public error. / 检索失败映射为稳定公开错误。"""

    state = make_state()
    result = retrieve_evidence(
        state,
        FakeEvidenceRetriever(
            error=AdapterError(
                "RAGFLOW_TIMEOUT",
                handoff_required=True,
                diagnostic="private retrieval detail",
            )
        ),
    )
    assert isinstance(result, ControlledFailureOutput)
    failed_state = apply_update(state, result)
    response = finish(failed_state).response

    assert isinstance(response, ErrorResponse)
    assert response.error.code == "RAGFLOW_TIMEOUT"
    assert "private retrieval detail" not in response.model_dump_json()


def test_generation_failure_branch_stops_before_audit() -> None:
    """Repeated invalid answers hand off and do not persist audit. / 回答连续非法时转人工且不写审计。"""

    state = make_state(evidence=[evidence()])
    generator = FakeSupportAnswerGenerator(results=[{"summary": "bad-1"}, {"summary": "bad-2"}])
    audit = FakeAuditRepository(result=AUDIT_ID)

    result = generate_support_answer(state, generator)
    assert isinstance(result, ControlledFailureOutput)
    failed_state = apply_update(state, result)
    response = finish(failed_state).response

    assert isinstance(response, ErrorResponse)
    assert response.error.code == "ANSWER_GENERATION_INVALID_RESPONSE"
    assert audit.requests == []


def test_audit_failure_branch_has_no_fake_record_id() -> None:
    """Audit failure remains controlled and cannot claim persistence. / 审计失败受控返回且不伪造编号。"""

    state = make_state()
    result = persist_audit(
        state,
        FakeAuditRepository(
            result=AUDIT_ID,
            error=AdapterError(
                "DATABASE_UNAVAILABLE",
                handoff_required=True,
                diagnostic="private database detail",
            ),
        ),
    )
    assert isinstance(result, ControlledFailureOutput)
    failed_state = apply_update(state, result)
    response = finish(failed_state).response

    assert isinstance(response, ErrorResponse)
    assert response.error.code == "DATABASE_UNAVAILABLE"
    assert failed_state.audit_record_id is None
    assert "private database detail" not in response.model_dump_json()
