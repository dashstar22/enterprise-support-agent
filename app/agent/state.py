"""Strict workflow state and node boundaries. / 严格的工作流状态与节点边界。"""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.api.schemas import (
    ClarificationResponse,
    CompletedResponse,
    DeviceModel,
    ErrorCode,
    ErrorResponse,
    FaultCode,
    InsufficientEvidenceResponse,
    MessageText,
    MissingField,
    ShortText,
    SourceName,
    SupportAnswer,
    TraceId,
)
from app.observability.metrics import AuditMetadata

ContentHash = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
MAX_SYMPTOMS = 20
FinalResponse = (
    CompletedResponse | ClarificationResponse | InsufficientEvidenceResponse | ErrorResponse
)


class WorkflowSchema(BaseModel):
    """Reject unknown workflow data and hide submitted values. / 拒绝未知流程数据并隐藏输入值。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class EvidenceItem(WorkflowSchema):
    """One candidate document fragment with a stable source. / 一条带稳定来源的候选文档片段。"""

    document_id: ShortText
    source_name: SourceName
    page: int | None = Field(default=None, ge=1)
    section: ShortText | None = None
    text: MessageText
    content_hash: ContentHash
    parser: Literal["text", "ocr"] = "text"

    @model_validator(mode="after")
    def require_page_or_section(self) -> Self:
        if self.page is None and self.section is None:
            raise ValueError("候选证据必须包含 page 或 section")
        return self


class BusinessContext(WorkflowSchema):
    """Validated business facts available to answer generation. / 可供生成回答使用的已校验业务事实。"""

    device_model: DeviceModel
    fault_code: FaultCode
    firmware_version: ShortText | None = None
    inventory_available: bool | None = None
    ticket_id: ShortText | None = None


class SupportState(WorkflowSchema):
    """Shared state passed between support workflow nodes. / 售后工作流节点之间传递的共享状态。"""

    session_id: UUID
    user_message: MessageText
    device_model: DeviceModel | None = None
    fault_code: FaultCode | None = None
    symptoms: list[ShortText] = Field(default_factory=list, max_length=MAX_SYMPTOMS)
    parsed_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    missing_fields: list[MissingField] = Field(default_factory=list, max_length=2)
    clarification_question: ShortText | None = None
    retrieval_query: ShortText | None = None
    evidence: list[EvidenceItem] = Field(default_factory=list, max_length=20)
    evidence_sufficient: bool | None = None
    business_context: BusinessContext | None = None
    answer: SupportAnswer | None = None
    generation_retry_count: int = Field(default=0, ge=0, le=1)
    audit_record_id: UUID | None = None
    handoff_required: bool = False
    error_code: ErrorCode | None = None
    trace_id: TraceId

    @model_validator(mode="after")
    def reject_duplicate_missing_fields(self) -> Self:
        if len(self.missing_fields) != len(set(self.missing_fields)):
            raise ValueError("missing_fields 不能重复")
        return self


class ParseRequestInput(WorkflowSchema):
    """Data read by parse_request. / parse_request 读取的数据。"""

    user_message: MessageText
    device_model: DeviceModel | None = None
    fault_code: FaultCode | None = None
    symptoms: list[ShortText] = Field(default_factory=list, max_length=MAX_SYMPTOMS)


class ParseRequestOutput(WorkflowSchema):
    """State fields updated by parse_request. / parse_request 允许更新的状态字段。"""

    device_model: DeviceModel | None = None
    fault_code: FaultCode | None = None
    symptoms: list[ShortText] = Field(default_factory=list, max_length=MAX_SYMPTOMS)
    parsed_confidence: float = Field(ge=0.0, le=1.0)


class ValidateRequiredFieldsInput(WorkflowSchema):
    """Data read by validate_required_fields. / validate_required_fields 读取的数据。"""

    device_model: DeviceModel | None = None
    fault_code: FaultCode | None = None


class ValidateRequiredFieldsOutput(WorkflowSchema):
    """Missing fields found by validation. / 必填校验发现的缺失字段。"""

    missing_fields: list[MissingField] = Field(max_length=2)

    @model_validator(mode="after")
    def reject_duplicate_fields(self) -> Self:
        if len(self.missing_fields) != len(set(self.missing_fields)):
            raise ValueError("missing_fields 不能重复")
        return self


class AskClarificationInput(WorkflowSchema):
    """Missing fields read by ask_clarification. / ask_clarification 读取的缺失字段。"""

    missing_fields: list[MissingField] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def reject_duplicate_fields(self) -> Self:
        if len(self.missing_fields) != len(set(self.missing_fields)):
            raise ValueError("missing_fields 不能重复")
        return self


class AskClarificationOutput(WorkflowSchema):
    """Question produced by ask_clarification. / ask_clarification 生成的追问。"""

    clarification_question: ShortText


class BuildRetrievalQueryInput(WorkflowSchema):
    """Confirmed fields read to build a query. / 构造检索词所读取的已确认字段。"""

    device_model: DeviceModel
    fault_code: FaultCode
    symptoms: list[ShortText] = Field(default_factory=list, max_length=MAX_SYMPTOMS)


class BuildRetrievalQueryOutput(WorkflowSchema):
    """Normalized query submitted to retrieval. / 交给检索步骤的规范化检索词。"""

    retrieval_query: ShortText


class RetrieveEvidenceInput(WorkflowSchema):
    """Query read by retrieve_evidence. / retrieve_evidence 读取的检索词。"""

    retrieval_query: ShortText


class RetrieveEvidenceOutput(WorkflowSchema):
    """Candidate fragments returned by retrieval. / 检索步骤返回的候选片段。"""

    evidence: list[EvidenceItem] = Field(default_factory=list, max_length=20)


class CheckEvidenceInput(WorkflowSchema):
    """Candidates read by check_evidence. / check_evidence 读取的候选证据。"""

    evidence: list[EvidenceItem] = Field(default_factory=list, max_length=20)


class CheckEvidenceOutput(WorkflowSchema):
    """Evidence decision and matching handoff flag. / 证据判断及对应的转人工标记。"""

    evidence_sufficient: bool
    handoff_required: bool

    @model_validator(mode="after")
    def keep_handoff_consistent(self) -> Self:
        if self.handoff_required == self.evidence_sufficient:
            raise ValueError("handoff_required 必须与 evidence_sufficient 相反")
        return self


class QueryBusinessContextInput(WorkflowSchema):
    """Equipment identity read by query_business_context. / query_business_context 读取的设备身份。"""

    device_model: DeviceModel
    fault_code: FaultCode


class QueryBusinessContextOutput(WorkflowSchema):
    """Optional validated business facts. / 可选的已校验业务事实。"""

    business_context: BusinessContext | None = None


class GenerateSupportAnswerInput(WorkflowSchema):
    """Grounded facts read by generate_support_answer. / generate_support_answer 读取的有依据事实。"""

    evidence: list[EvidenceItem] = Field(min_length=1, max_length=20)
    business_context: BusinessContext | None = None


class GenerateSupportAnswerOutput(WorkflowSchema):
    """Validated answer and retry count. / 已校验回答和生成重试次数。"""

    answer: SupportAnswer
    generation_retry_count: int = Field(ge=0, le=1)


class PersistAuditInput(WorkflowSchema):
    """Complete state read by persist_audit. / persist_audit 读取的完整状态。"""

    state: SupportState
    metadata: AuditMetadata | None = None


class PersistAuditOutput(WorkflowSchema):
    """Identifier written after audit persistence. / 审计保存后写回的记录编号。"""

    audit_record_id: UUID


class ControlledFailureOutput(WorkflowSchema):
    """Stable failure update shared by fallible nodes. / 可能失败节点共用的稳定失败更新。"""

    error_code: ErrorCode
    handoff_required: bool = True
    evidence_sufficient: None = None


RetrieveEvidenceResult = RetrieveEvidenceOutput | ControlledFailureOutput
QueryBusinessContextResult = QueryBusinessContextOutput | ControlledFailureOutput
GenerateSupportAnswerResult = GenerateSupportAnswerOutput | ControlledFailureOutput
PersistAuditResult = PersistAuditOutput | ControlledFailureOutput


class FinishInput(WorkflowSchema):
    """Final fields read when mapping the workflow result. / 映射工作流结果时读取的最终字段。"""

    session_id: UUID
    trace_id: TraceId
    missing_fields: list[MissingField] = Field(default_factory=list, max_length=2)
    clarification_question: ShortText | None = None
    evidence_sufficient: bool | None = None
    answer: SupportAnswer | None = None
    handoff_required: bool = False
    error_code: ErrorCode | None = None

    @model_validator(mode="after")
    def enforce_one_final_branch(self) -> Self:
        """Reject conflicting final-state signals. / 拒绝互相冲突的最终状态信号。"""

        if self.missing_fields:
            if self.clarification_question is None:
                raise ValueError("缺少字段时必须提供 clarification_question")
            if (
                self.answer is not None
                or self.handoff_required
                or self.error_code is not None
                or self.evidence_sufficient is not None
            ):
                raise ValueError("追问分支不能携带成功、证据或错误字段")
            return self

        if self.clarification_question is not None:
            raise ValueError("字段完整时不能携带 clarification_question")

        if self.error_code is not None and self.handoff_required:
            if self.answer is not None or self.evidence_sufficient is not None:
                raise ValueError("受控错误不能携带成功或证据不足字段")
            return self

        if self.answer is not None:
            if self.handoff_required or self.evidence_sufficient is False:
                raise ValueError("成功分支不能携带转人工或证据不足字段")
            return self

        if self.evidence_sufficient is False and self.handoff_required:
            return self

        if self.evidence_sufficient is False:
            raise ValueError("证据不足时必须设置 handoff_required")

        if self.handoff_required:
            raise ValueError("转人工分支必须说明证据不足或错误原因")
        if self.evidence_sufficient is True:
            raise ValueError("证据足够时必须提供成功回答")
        return self


class FinishOutput(WorkflowSchema):
    """One validated public response. / 一个经过校验的公开响应。"""

    response: FinalResponse
