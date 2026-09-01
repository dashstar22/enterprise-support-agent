"""Fake adapter and external-node tests. / 测试适配器与外部节点测试。"""

from typing import cast
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.agent.adapters import AdapterError
from app.agent.fake_adapters import (
    FakeAuditRepository,
    FakeBusinessContextProvider,
    FakeEvidenceRetriever,
)
from app.agent.nodes import persist_audit, query_business_context, retrieve_evidence
from app.agent.state import (
    BusinessContext,
    ControlledFailureOutput,
    EvidenceItem,
    PersistAuditOutput,
    QueryBusinessContextOutput,
    RetrieveEvidenceOutput,
    SupportState,
)

SESSION_ID = "8a7b1e5f-14e8-4bd8-a79b-b3a7f70fe93b"
AUDIT_ID = UUID("22222222-2222-4222-8222-222222222222")


def evidence() -> EvidenceItem:
    """Build one valid evidence item. / 构造一条合法证据。"""

    return EvidenceItem(
        document_id="manual-e200-v2",
        source_name="E-200维修手册.pdf",
        page=12,
        section="故障码 E01",
        text="检查主电源和保险丝。",
        content_hash="a" * 64,
    )


def make_state(**updates: object) -> SupportState:
    """Build one valid workflow state. / 构造一个合法工作流状态。"""

    payload: dict[str, object] = {
        "session_id": SESSION_ID,
        "user_message": "设备无法启动",
        "device_model": "E-200",
        "fault_code": "E01",
        "retrieval_query": "E-200 E01 无法启动",
        "trace_id": "trace-c2-adapter-001",
    }
    payload.update(updates)
    return SupportState.model_validate(payload)


class MalformedRetriever:
    """Return a malformed runtime response. / 返回运行时畸形响应。"""

    def retrieve(self, request: object, /) -> list[EvidenceItem]:
        del request
        return [
            EvidenceItem.model_construct(
                document_id="missing-fields",
                source_name="missing-fields",
                text="missing-fields",
                content_hash="invalid",
            )
        ]


class MalformedBusinessProvider:
    """Return a malformed runtime response. / 返回运行时畸形响应。"""

    def get_context(self, request: object, /) -> BusinessContext:
        del request
        return cast(
            BusinessContext,
            {
                "device_model": "E-200",
                "fault_code": 123,
            },
        )


class MalformedAuditRepository:
    """Return an invalid audit identifier. / 返回无效审计编号。"""

    def persist(self, request: object, /) -> str:
        del request
        return "not-a-uuid"


class CrashingRetriever:
    """Raise an unexpected programming error. / 抛出未预期程序错误。"""

    def retrieve(self, request: object, /) -> list[EvidenceItem]:
        del request
        raise RuntimeError("unexpected adapter bug")


def test_fake_retriever_returns_valid_independent_results_and_records_input() -> None:
    """Fake retrieval is deterministic and detached. / 假检索器确定且输入输出相互隔离。"""

    adapter = FakeEvidenceRetriever(result=[evidence()])
    state = make_state()

    first = retrieve_evidence(state, adapter)
    assert isinstance(first, RetrieveEvidenceOutput)
    first.evidence[0].text = "调用方修改"
    second = retrieve_evidence(state, adapter)

    assert isinstance(second, RetrieveEvidenceOutput)
    assert second.evidence[0].text == "检查主电源和保险丝。"
    assert len(adapter.requests) == 2
    assert adapter.requests[0].retrieval_query == "E-200 E01 无法启动"


def test_retrieve_evidence_accepts_empty_success_without_deciding_evidence_sufficiency() -> None:
    """Empty retrieval is still a successful adapter call. / 空证据仍表示检索调用成功。"""

    result = retrieve_evidence(make_state(), FakeEvidenceRetriever())

    assert isinstance(result, RetrieveEvidenceOutput)
    assert result.evidence == []


def test_retrieve_evidence_failure_sets_handoff_and_hides_diagnostic() -> None:
    """Retrieval failure becomes a handoff-safe result. / 检索失败转成人工且不泄露诊断。"""

    result = retrieve_evidence(
        make_state(),
        FakeEvidenceRetriever(
            error=AdapterError(
                "RAGFLOW_TIMEOUT",
                handoff_required=True,
                diagnostic="secret provider detail",
            )
        ),
    )

    assert isinstance(result, ControlledFailureOutput)
    assert result.error_code == "RAGFLOW_TIMEOUT"
    assert result.handoff_required is True
    assert "secret provider detail" not in str(result)


def test_retrieve_evidence_requires_query_before_calling_adapter() -> None:
    """Retrieval cannot run without a normalized query. / 没有检索词时不能调用检索器。"""

    adapter = FakeEvidenceRetriever()
    with pytest.raises(ValidationError):
        retrieve_evidence(make_state(retrieval_query=None), adapter)
    assert adapter.requests == []


def test_retrieve_evidence_maps_malformed_response_to_stable_failure() -> None:
    """Malformed candidate data is never accepted as evidence. / 畸形候选数据不能冒充证据。"""

    result = retrieve_evidence(make_state(), MalformedRetriever())

    assert isinstance(result, ControlledFailureOutput)
    assert result.error_code == "RETRIEVAL_INVALID_RESPONSE"
    assert result.handoff_required is True


def test_fake_business_provider_supports_context_and_explicit_empty_result() -> None:
    """Business context may be present or explicitly absent. / 业务上下文可以有，也可以明确为空。"""

    context = BusinessContext(device_model="E-200", fault_code="E01", inventory_available=True)
    adapter = FakeBusinessContextProvider(result=context)

    result = query_business_context(make_state(), adapter)
    empty = query_business_context(make_state(), FakeBusinessContextProvider())

    assert isinstance(result, QueryBusinessContextOutput)
    assert result.business_context is not None
    assert result.business_context.inventory_available is True
    assert isinstance(empty, QueryBusinessContextOutput)
    assert empty.business_context is None
    assert adapter.requests[0].device_model == "E-200"


def test_business_failure_degrades_without_handoff_or_fake_context() -> None:
    """Business failure can continue without invented facts. / 业务失败可以降级且不编造事实。"""

    result = query_business_context(
        make_state(),
        FakeBusinessContextProvider(
            error=AdapterError(
                "BUSINESS_API_TIMEOUT",
                handoff_required=False,
                diagnostic="private business detail",
            )
        ),
    )

    assert isinstance(result, ControlledFailureOutput)
    assert result.error_code == "BUSINESS_API_TIMEOUT"
    assert result.handoff_required is False
    assert "private business detail" not in str(result)


def test_business_provider_requires_both_identity_fields_before_calling() -> None:
    """Business lookup needs both equipment identity fields. / 业务查询必须同时有两个身份字段。"""

    adapter = FakeBusinessContextProvider()
    with pytest.raises(ValidationError):
        query_business_context(make_state(fault_code=None), adapter)
    assert adapter.requests == []


def test_business_malformed_response_degrades_with_stable_failure() -> None:
    """Malformed business data is discarded rather than trusted. / 畸形业务数据会被丢弃。"""

    result = query_business_context(make_state(), MalformedBusinessProvider())

    assert isinstance(result, ControlledFailureOutput)
    assert result.error_code == "BUSINESS_API_INVALID_RESPONSE"
    assert result.handoff_required is False


def test_fake_audit_repository_persists_detached_complete_state() -> None:
    """Audit fake receives a deep copy and returns a stable identifier. / 审计假适配器接收深拷贝并返回稳定编号。"""

    adapter = FakeAuditRepository(result=AUDIT_ID)
    state = make_state()

    result = persist_audit(state, adapter)
    assert isinstance(result, PersistAuditOutput)
    assert result.audit_record_id == AUDIT_ID
    assert adapter.requests[0].state.session_id == state.session_id
    assert adapter.requests[0].state is not state

    adapter.requests[0].state.user_message = "调用方修改"
    assert state.user_message == "设备无法启动"


def test_audit_failure_has_no_identifier_and_requires_handoff() -> None:
    """Failed persistence cannot claim that a record exists. / 保存失败不能伪造记录编号。"""

    result = persist_audit(
        make_state(),
        FakeAuditRepository(
            error=AdapterError(
                "DATABASE_UNAVAILABLE",
                handoff_required=True,
                diagnostic="secret database detail",
            )
        ),
    )

    assert isinstance(result, ControlledFailureOutput)
    assert result.error_code == "DATABASE_UNAVAILABLE"
    assert result.handoff_required is True
    assert "audit_record_id" not in result.model_dump()
    assert "secret database detail" not in str(result)


def test_audit_requires_valid_uuid_response() -> None:
    """Only a valid UUID becomes an audit identifier. / 只有合法 UUID 才能成为审计编号。"""

    result = persist_audit(make_state(), MalformedAuditRepository())

    assert isinstance(result, ControlledFailureOutput)
    assert result.error_code == "AUDIT_INVALID_RESPONSE"
    assert result.handoff_required is True


def test_unexpected_adapter_errors_are_not_silently_swallowed() -> None:
    """Unknown adapter bugs remain visible to the outer boundary. / 未知适配器错误继续交给外层边界。"""

    with pytest.raises(RuntimeError, match="unexpected adapter bug"):
        retrieve_evidence(make_state(), CrashingRetriever())
