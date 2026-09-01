"""SQLAlchemy audit tables. / SQLAlchemy 审计数据表。"""

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid


class Base(DeclarativeBase):
    """Base class shared by all relational models. / 所有关系模型共用的基类。"""


class SupportSessionRecord(Base):
    """One persisted support session. / 一条已保存的售后会话。"""

    __tablename__ = "support_sessions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    device_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    messages: Mapped[list["SupportMessageRecord"]] = relationship(back_populates="session")
    runs: Mapped[list["WorkflowRunRecord"]] = relationship(back_populates="session")


class SupportMessageRecord(Base):
    """One redacted user or agent message. / 一条已脱敏的用户或 Agent 消息。"""

    __tablename__ = "support_messages"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(ForeignKey("support_sessions.id"), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    session: Mapped[SupportSessionRecord] = relationship(back_populates="messages")


class WorkflowRunRecord(Base):
    """One auditable workflow execution. / 一次可审计的工作流执行。"""

    __tablename__ = "workflow_runs"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(ForeignKey("support_sessions.id"), nullable=False)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    estimated_token_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 8), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    session: Mapped[SupportSessionRecord] = relationship(back_populates="runs")
    evidence_items: Mapped[list["EvidenceItemRecord"]] = relationship(back_populates="run")
    steps: Mapped[list["SupportStepRecord"]] = relationship(back_populates="run")
    external_calls: Mapped[list["ExternalApiCallRecord"]] = relationship(back_populates="run")
    timings: Mapped[list["WorkflowTimingRecord"]] = relationship(back_populates="run")


class EvidenceItemRecord(Base):
    """Evidence metadata without storing the full source text. / 不保存完整原文的证据元数据。"""

    __tablename__ = "evidence_items"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("workflow_runs.id"), nullable=False)
    document_id: Mapped[str] = mapped_column(String(1000), nullable=False)
    source_name: Mapped[str] = mapped_column(String(255), nullable=False)
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    section: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    parser: Mapped[str] = mapped_column(String(16), nullable=False)

    run: Mapped[WorkflowRunRecord] = relationship(back_populates="evidence_items")
    cited_steps: Mapped[list["SupportStepRecord"]] = relationship(back_populates="citation")


class SupportStepRecord(Base):
    """A generated troubleshooting step tied to evidence. / 绑定证据的排障步骤。"""

    __tablename__ = "support_steps"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("workflow_runs.id"), nullable=False)
    step_order: Mapped[int] = mapped_column(Integer, nullable=False)
    instruction: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    citation_id: Mapped[UUID] = mapped_column(ForeignKey("evidence_items.id"), nullable=False)

    run: Mapped[WorkflowRunRecord] = relationship(back_populates="steps")
    citation: Mapped[EvidenceItemRecord] = relationship(back_populates="cited_steps")

    __table_args__ = (UniqueConstraint("run_id", "step_order", name="uq_support_steps_run_order"),)


class ExternalApiCallRecord(Base):
    """A minimal, redacted external service audit row. / 最小化且脱敏的外部服务审计行。"""

    __tablename__ = "external_api_calls"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("workflow_runs.id"), nullable=False)
    service: Mapped[str] = mapped_column(String(64), nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[float] = mapped_column(nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    run: Mapped[WorkflowRunRecord] = relationship(back_populates="external_calls")


class WorkflowTimingRecord(Base):
    """One component timing, including the database write itself. / 一项组件耗时，包含数据库写入。"""

    __tablename__ = "workflow_timings"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("workflow_runs.id"), nullable=False)
    component: Mapped[str] = mapped_column(String(32), nullable=False)
    latency_ms: Mapped[float] = mapped_column(nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)

    run: Mapped[WorkflowRunRecord] = relationship(back_populates="timings")

    __table_args__ = (
        UniqueConstraint("run_id", "component", name="uq_workflow_timings_run_component"),
    )


class UserFeedbackRecord(Base):
    """Feedback linked to the run selected by its target trace. / 通过目标追踪号关联运行记录的反馈。"""

    __tablename__ = "user_feedback"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("workflow_runs.id"), nullable=False)
    session_id: Mapped[UUID] = mapped_column(ForeignKey("support_sessions.id"), nullable=False)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    target_trace_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    rating: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
