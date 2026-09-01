"""Cycle 5 SQLAlchemy audit and observability tests. / Cycle 5 的 SQLAlchemy 审计与可观测性测试。"""

import logging
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.agent.adapters import AdapterError
from app.agent.nodes import persist_audit
from app.agent.state import (
    ControlledFailureOutput,
    EvidenceItem,
    PersistAuditInput,
    PersistAuditOutput,
    SupportState,
)
from app.api.schemas import FeedbackCreatedResponse, SupportAnswer
from app.db.audit import SqlAlchemyAuditRepository
from app.db.models import (
    EvidenceItemRecord,
    ExternalApiCallRecord,
    SupportMessageRecord,
    SupportStepRecord,
    UserFeedbackRecord,
    WorkflowRunRecord,
    WorkflowTimingRecord,
)
from app.db.session import create_database_engine, create_schema_for_test, create_session_factory
from app.observability.logging import StructuredLogger
from app.observability.metrics import AuditMetadata, TokenPricing


@pytest.fixture
def repository() -> SqlAlchemyAuditRepository:
    """Build a transaction-isolated database repository. / 构造事务隔离的数据库审计仓库。"""

    engine = create_database_engine("sqlite+pysqlite://")
    create_schema_for_test(engine)
    return SqlAlchemyAuditRepository(
        create_session_factory(engine),
        token_pricing=TokenPricing(input_per_1k=Decimal("0.002"), output_per_1k=Decimal("0.004")),
    )


def evidence(source_name: str = "E-200维修手册.pdf") -> EvidenceItem:
    """Build one current evidence item. / 构造一条当前证据。"""

    return EvidenceItem(
        document_id="manual-e200-v2",
        source_name=source_name,
        page=12,
        section="故障码 E01",
        text="检查主电源和保险丝。",
        content_hash="a" * 64,
    )


def answer(source_name: str = "E-200维修手册.pdf") -> SupportAnswer:
    """Build one evidence-bound answer. / 构造一条绑定证据的回答。"""

    return SupportAnswer.model_validate(
        {
            "summary": "先检查电源。",
            "steps": [
                {
                    "order": 1,
                    "instruction": "检查 Authorization=Bearer top-secret 后再检查电源。",
                    "reason": "手册中的 support@example.com 联系方式不应进入审计。",
                    "citation": {"source_name": source_name, "page": 12, "section": "故障码 E01"},
                }
            ],
            "confidence": "supported",
            "handoff_required": False,
        }
    )


def state(**updates: object) -> SupportState:
    """Build one auditable workflow state. / 构造一条可审计工作流状态。"""

    payload: dict[str, object] = {
        "session_id": "8a7b1e5f-14e8-4bd8-a79b-b3a7f70fe93b",
        "user_message": "设备无法启动，api_key=private-value，请联系 user@example.com 或 13800138000。",
        "device_model": "E-200",
        "fault_code": "E01",
        "retrieval_query": "E-200 E01 无法启动",
        "evidence": [evidence()],
        "evidence_sufficient": True,
        "answer": answer(),
        "trace_id": "trace-c5-audit-001",
    }
    payload.update(updates)
    return SupportState.model_validate(payload)


def metadata() -> AuditMetadata:
    """Build all required C5 observability dimensions. / 构造 C5 所需全部可观测性维度。"""

    return AuditMetadata.model_validate(
        {
            "request_id": "request-c5-001",
            "external_api_calls": [
                {
                    "service": "ragflow",
                    "status_code": 200,
                    "latency_ms": 11.0,
                    "outcome": "success",
                },
                {
                    "service": "business_api",
                    "status_code": 200,
                    "latency_ms": 4.0,
                    "outcome": "success",
                },
                {"service": "ocr", "status_code": 200, "latency_ms": 3.0, "outcome": "success"},
            ],
            "timings": [
                {"component": "model", "latency_ms": 50.0, "outcome": "success"},
                {"component": "retrieval", "latency_ms": 11.0, "outcome": "success"},
                {"component": "ocr", "latency_ms": 3.0, "outcome": "success"},
                {"component": "business_api", "latency_ms": 4.0, "outcome": "success"},
            ],
            "token_usage": {
                "model_name": "configured-model",
                "input_tokens": 120,
                "output_tokens": 60,
            },
        }
    )


def test_persisted_run_links_all_required_records_and_redacts_sensitive_text(
    repository: SqlAlchemyAuditRepository,
) -> None:
    """One trace links session, evidence, steps, calls, timings, and token cost. / 一个追踪号关联会话、证据、步骤、调用、耗时和 Token 成本。"""

    result = persist_audit(state(), repository, metadata())
    assert isinstance(result, PersistAuditOutput)
    assert result.audit_record_id.version == 4

    with repository._session_factory() as session:
        run = session.scalar(select(WorkflowRunRecord))
        message = session.scalar(select(SupportMessageRecord))
        step = session.scalar(select(SupportStepRecord))
        timing_components = set(session.scalars(select(WorkflowTimingRecord.component)).all())

    assert run is not None
    assert run.trace_id == "trace-c5-audit-001"
    assert run.request_id == "request-c5-001"
    assert run.status == "completed"
    assert run.input_tokens == 120
    assert run.output_tokens == 60
    assert run.estimated_token_cost == Decimal("0.00048000")
    assert message is not None
    assert "private-value" not in message.content
    assert "user@example.com" not in message.content
    assert "13800138000" not in message.content
    assert step is not None
    assert "top-secret" not in step.instruction
    assert "support@example.com" not in step.reason
    assert timing_components == {"model", "retrieval", "ocr", "business_api", "database"}
    with repository._session_factory() as session:
        assert len(session.scalars(select(EvidenceItemRecord)).all()) == 1
        assert len(session.scalars(select(SupportStepRecord)).all()) == 1
        assert len(session.scalars(select(ExternalApiCallRecord)).all()) == 3


def test_failed_workflow_persists_status_and_controlled_error(
    repository: SqlAlchemyAuditRepository,
) -> None:
    """A failed workflow is still auditable without exposing diagnostics. / 失败工作流仍可审计，且不暴露诊断。"""

    failed = state(
        evidence=[],
        evidence_sufficient=None,
        answer=None,
        error_code="RAGFLOW_TIMEOUT",
        handoff_required=True,
        trace_id="trace-c5-failed-001",
    )
    result = persist_audit(
        failed, repository, metadata().model_copy(update={"request_id": "request-c5-002"})
    )
    assert isinstance(result, PersistAuditOutput)
    assert result.audit_record_id.version == 4

    with repository._session_factory() as session:
        run = session.scalar(
            select(WorkflowRunRecord).where(WorkflowRunRecord.trace_id == failed.trace_id)
        )
    assert run is not None
    assert run.status == "failed"
    assert run.error_code == "RAGFLOW_TIMEOUT"


def test_citation_write_failure_rolls_back_every_row(repository: SqlAlchemyAuditRepository) -> None:
    """A missing accepted citation cannot leave a half-written audit. / 缺失已接受引用时不能留下半套审计记录。"""

    mismatched = state(answer=answer("另一本手册.pdf"), trace_id="trace-c5-rollback-001")
    with pytest.raises(AdapterError) as exc_info:
        repository.persist(PersistAuditInput(state=mismatched, metadata=metadata()))

    assert exc_info.value.error_code == "AUDIT_PERSISTENCE_ERROR"
    with repository._session_factory() as session:
        assert session.scalars(select(WorkflowRunRecord)).all() == []
        assert session.scalars(select(SupportMessageRecord)).all() == []
        assert session.scalars(select(EvidenceItemRecord)).all() == []


def test_feedback_is_linked_to_target_trace_and_reason_is_redacted(
    repository: SqlAlchemyAuditRepository,
) -> None:
    """Feedback points at an existing run, not merely a session. / 反馈指向已有运行，而不只是会话。"""

    persisted = persist_audit(state(), repository, metadata())
    assert isinstance(persisted, PersistAuditOutput)
    feedback = FeedbackCreatedResponse(
        feedback_id=uuid4(),
        status="recorded",
        session_id=state().session_id,
        request_id="feedback-c5-001",
        trace_id="trace-c5-feedback-001",
        target_trace_id="trace-c5-audit-001",
        rating="helpful",
        reason="Authorization=Bearer private-feedback-token",
        created_at=datetime.now(UTC),
    )
    repository.record_feedback(feedback)

    with repository._session_factory() as session:
        stored = session.scalar(select(UserFeedbackRecord))
    assert stored is not None
    assert stored.run_id == persisted.audit_record_id
    assert stored.target_trace_id == "trace-c5-audit-001"
    assert "private-feedback-token" not in (stored.reason or "")


def test_missing_audit_metadata_returns_controlled_failure(
    repository: SqlAlchemyAuditRepository,
) -> None:
    """An audit cannot claim complete correlation without request_id. / 缺 request_id 时审计不能假装关联完整。"""

    result = persist_audit(state(), repository)
    assert isinstance(result, ControlledFailureOutput)
    assert result.error_code == "AUDIT_CONTEXT_MISSING"
    with repository._session_factory() as session:
        assert session.scalars(select(WorkflowRunRecord)).all() == []


def test_missing_price_records_tokens_but_not_an_invented_cost(
    repository: SqlAlchemyAuditRepository,
) -> None:
    """Token counts remain useful when the price table is intentionally absent. / 故意未配价格表时，Token 数仍有用但不能虚构金额。"""

    unpriced_repository = SqlAlchemyAuditRepository(repository._session_factory)
    result = persist_audit(state(), unpriced_repository, metadata())
    assert isinstance(result, PersistAuditOutput)

    with repository._session_factory() as session:
        run = session.scalar(select(WorkflowRunRecord))
    assert run is not None
    assert run.input_tokens == 120
    assert run.output_tokens == 60
    assert run.estimated_token_cost is None


def test_structured_logger_redacts_secrets_and_contact_details(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Structured logs carry correlation IDs but no raw secrets or contact details. / 结构化日志保留关联编号，但不能留下明文密钥或联系方式。"""

    logger = logging.getLogger("tests.c5.structured")
    structured = StructuredLogger(logger)
    with caplog.at_level(logging.INFO, logger=logger.name):
        structured.emit(
            "request_received",
            request_id="request-c5-log-001",
            trace_id="trace-c5-log-001",
            session_id="8a7b1e5f-14e8-4bd8-a79b-b3a7f70fe93b",
            workflow_run_id="11111111-1111-4111-8111-111111111111",
            fields={
                "Authorization": "Bearer log-secret",
                "message": "请联系 log@example.com 或 13800138000",
            },
        )

    payload = caplog.messages[0]
    assert "request-c5-log-001" in payload
    assert "trace-c5-log-001" in payload
    assert "log-secret" not in payload
    assert "log@example.com" not in payload
    assert "13800138000" not in payload
