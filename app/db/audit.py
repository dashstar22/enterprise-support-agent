"""Transactional SQLAlchemy implementation of workflow auditing. / 工作流审计的事务性 SQLAlchemy 实现。"""

from datetime import UTC, datetime
from time import perf_counter
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.agent.adapters import AdapterError
from app.agent.state import EvidenceItem, PersistAuditInput, SupportState
from app.api.schemas import FeedbackCreatedResponse
from app.db.models import (
    EvidenceItemRecord,
    ExternalApiCallRecord,
    SupportMessageRecord,
    SupportSessionRecord,
    SupportStepRecord,
    UserFeedbackRecord,
    WorkflowRunRecord,
    WorkflowTimingRecord,
)
from app.db.session import DatabaseSessionFactory
from app.observability.metrics import TokenPricing, estimate_token_cost
from app.observability.redaction import redact_text


class SqlAlchemyAuditRepository:
    """Write the complete audit graph in one database transaction. / 在一个数据库事务中写入完整审计关系图。"""

    def __init__(
        self,
        session_factory: DatabaseSessionFactory,
        *,
        token_pricing: TokenPricing | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._token_pricing = token_pricing

    def persist(self, request: PersistAuditInput, /) -> str:
        """Persist a completed or failed run, or roll back all rows. / 保存成功或失败运行，任意出错就整体回滚。"""

        metadata = request.metadata
        if metadata is None:
            raise AdapterError(
                "AUDIT_CONTEXT_MISSING",
                handoff_required=True,
                diagnostic="request_id is required for persisted workflow audit",
            )

        run_id = uuid4()
        started_at = perf_counter()
        now = datetime.now(UTC)
        try:
            with self._session_factory.begin() as session:
                self._persist_run(session, request, run_id, now)
                self._persist_external_calls(session, run_id, request)
                self._persist_timings(session, run_id, request)
                session.flush()
                session.add(
                    WorkflowTimingRecord(
                        run_id=run_id,
                        component="database",
                        latency_ms=(perf_counter() - started_at) * 1000,
                        outcome="success",
                    )
                )
                session.flush()
        except AdapterError:
            raise
        except SQLAlchemyError as exc:
            raise AdapterError(
                "DATABASE_UNAVAILABLE",
                handoff_required=True,
                diagnostic=str(exc),
            ) from exc
        return str(run_id)

    def record_feedback(self, feedback: FeedbackCreatedResponse) -> None:
        """Persist redacted feedback only when its target workflow run exists. / 仅当目标工作流存在时保存脱敏反馈。"""

        try:
            with self._session_factory.begin() as session:
                run = session.scalar(
                    select(WorkflowRunRecord).where(
                        WorkflowRunRecord.trace_id == feedback.target_trace_id
                    )
                )
                if run is None or run.session_id != feedback.session_id:
                    raise AdapterError(
                        "FEEDBACK_TARGET_NOT_FOUND",
                        handoff_required=False,
                        diagnostic="no workflow run matches feedback target",
                    )
                session.add(
                    UserFeedbackRecord(
                        id=feedback.feedback_id,
                        run_id=run.id,
                        session_id=feedback.session_id,
                        request_id=feedback.request_id,
                        trace_id=feedback.trace_id,
                        target_trace_id=feedback.target_trace_id,
                        rating=feedback.rating,
                        reason=redact_text(feedback.reason)
                        if feedback.reason is not None
                        else None,
                        created_at=feedback.created_at,
                    )
                )
        except AdapterError:
            raise
        except SQLAlchemyError as exc:
            raise AdapterError(
                "DATABASE_UNAVAILABLE",
                handoff_required=True,
                diagnostic=str(exc),
            ) from exc

    def _persist_run(
        self, session: Session, request: PersistAuditInput, run_id: UUID, now: datetime
    ) -> None:
        metadata = request.metadata
        if metadata is None:
            raise AssertionError("metadata was checked before transaction")
        state = request.state
        workflow_status = _workflow_status(state)
        session_record = session.get(SupportSessionRecord, state.session_id)
        if session_record is None:
            session_record = SupportSessionRecord(
                id=state.session_id,
                status=workflow_status,
                device_model=state.device_model,
                created_at=now,
            )
            session.add(session_record)
        else:
            session_record.status = workflow_status
            session_record.device_model = state.device_model or session_record.device_model

        usage = metadata.token_usage
        session.add(
            SupportMessageRecord(
                session_id=state.session_id,
                role="user",
                content=redact_text(state.user_message),
                request_id=metadata.request_id,
                trace_id=state.trace_id,
                created_at=now,
            )
        )
        session.add(
            WorkflowRunRecord(
                id=run_id,
                session_id=state.session_id,
                request_id=metadata.request_id,
                trace_id=state.trace_id,
                status=workflow_status,
                error_code=state.error_code,
                model_name=usage.model_name if usage is not None else None,
                input_tokens=usage.input_tokens if usage is not None else None,
                output_tokens=usage.output_tokens if usage is not None else None,
                estimated_token_cost=(
                    estimate_token_cost(usage, self._token_pricing) if usage is not None else None
                ),
                created_at=now,
            )
        )

        evidence_by_locator: dict[tuple[str, int | None, str | None], EvidenceItemRecord] = {}
        for evidence in state.evidence:
            record = _evidence_record(run_id, evidence)
            locator = (record.source_name, record.page, record.section)
            if locator in evidence_by_locator:
                raise AdapterError(
                    "AUDIT_PERSISTENCE_ERROR",
                    handoff_required=True,
                    diagnostic="duplicate evidence locator cannot safely bind a support step",
                )
            evidence_by_locator[locator] = record
            session.add(record)

        if state.answer is not None:
            for step in state.answer.steps:
                locator = (step.citation.source_name, step.citation.page, step.citation.section)
                citation = evidence_by_locator.get(locator)
                if citation is None:
                    raise AdapterError(
                        "AUDIT_PERSISTENCE_ERROR",
                        handoff_required=True,
                        diagnostic="support step citation is missing from accepted evidence",
                    )
                session.add(
                    SupportStepRecord(
                        run_id=run_id,
                        step_order=step.order,
                        instruction=redact_text(step.instruction),
                        reason=redact_text(step.reason),
                        citation=citation,
                    )
                )

    @staticmethod
    def _persist_external_calls(session: Session, run_id: UUID, request: PersistAuditInput) -> None:
        if request.metadata is None:
            return
        for call in request.metadata.external_api_calls:
            session.add(
                ExternalApiCallRecord(
                    run_id=run_id,
                    service=call.service,
                    status_code=call.status_code,
                    latency_ms=call.latency_ms,
                    outcome=call.outcome,
                    error_code=call.error_code,
                )
            )

    @staticmethod
    def _persist_timings(session: Session, run_id: UUID, request: PersistAuditInput) -> None:
        if request.metadata is None:
            return
        for timing in request.metadata.timings:
            session.add(
                WorkflowTimingRecord(
                    run_id=run_id,
                    component=timing.component,
                    latency_ms=timing.latency_ms,
                    outcome=timing.outcome,
                )
            )


def _evidence_record(run_id: UUID, evidence: EvidenceItem) -> EvidenceItemRecord:
    """Convert accepted evidence to metadata-only storage. / 将已接受证据转换为只含元数据的存储行。"""

    return EvidenceItemRecord(
        run_id=run_id,
        document_id=evidence.document_id,
        source_name=evidence.source_name,
        page=evidence.page,
        section=evidence.section,
        content_hash=evidence.content_hash,
        parser=evidence.parser,
    )


def _workflow_status(state: SupportState) -> str:
    """Derive a stable audit status from workflow state. / 从工作流状态推导稳定审计状态。"""

    if state.error_code is not None:
        return "failed"
    if state.answer is not None:
        return "completed"
    if state.missing_fields:
        return "needs_clarification"
    return "insufficient_evidence"
