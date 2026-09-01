"""Strict HTTP request and response schemas. / 严格的 HTTP 请求与响应结构。"""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

DeviceModel = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
FaultCode = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]
MessageText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)
]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
SourceName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
RequestId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        max_length=128,
    ),
]
TraceId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        max_length=128,
    ),
]
ErrorCode = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^[A-Z][A-Z0-9_]*$", max_length=64),
]
MissingField = Literal["device_model", "fault_code"]
FeedbackRating = Literal["helpful", "not_helpful"]


class ApiSchema(BaseModel):
    """Reject unknown fields and hide submitted values in errors. / 拒绝未知字段并在错误中隐藏输入值。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class CreateSessionRequest(ApiSchema):
    """Optional context supplied when a support session starts. / 创建售后会话时可选的上下文。"""

    device_model: DeviceModel | None = None


class SessionCreatedResponse(ApiSchema):
    """Stable result returned after a session is created. / 会话创建后的稳定返回结构。"""

    session_id: UUID
    status: Literal["active"]
    device_model: DeviceModel | None = None
    created_at: AwareDatetime


class SubmitMessageRequest(ApiSchema):
    """One user message and optional structured equipment fields. / 一条用户消息和可选设备字段。"""

    message: MessageText
    device_model: DeviceModel | None = None
    fault_code: FaultCode | None = None


class Citation(ApiSchema):
    """A document source with at least one stable locator. / 至少带一个稳定定位的文档来源。"""

    source_name: SourceName
    page: int | None = Field(default=None, ge=1)
    section: ShortText | None = None

    @model_validator(mode="after")
    def require_page_or_section(self) -> Self:
        if self.page is None and self.section is None:
            raise ValueError("引用必须包含 page 或 section")
        return self


class SupportStep(ApiSchema):
    """One ordered troubleshooting step backed by a citation. / 一条带引用的有序排障步骤。"""

    order: int = Field(ge=1)
    instruction: ShortText
    reason: ShortText
    citation: Citation


class SupportAnswer(ApiSchema):
    """A supported answer with one or more cited steps. / 至少包含一条引用步骤的有依据回答。"""

    summary: ShortText
    steps: list[SupportStep] = Field(min_length=1, max_length=20)
    confidence: Literal["supported", "mock"]
    handoff_required: Literal[False]


class CompletedResponse(ApiSchema):
    """Message result used only for a supported completed answer. / 仅用于有证据成功回答的消息结果。"""

    session_id: UUID
    status: Literal["completed"]
    answer: SupportAnswer
    trace_id: TraceId


class ClarificationResponse(ApiSchema):
    """Message result used when required fields are missing. / 缺少必要字段时使用的追问结果。"""

    session_id: UUID
    status: Literal["needs_clarification"]
    question: ShortText
    missing_fields: list[MissingField] = Field(min_length=1, max_length=2)
    trace_id: TraceId


class InsufficientEvidenceResponse(ApiSchema):
    """Message result used when evidence cannot support an answer. / 证据不足以支持回答时使用的结果。"""

    session_id: UUID
    status: Literal["insufficient_evidence"]
    message: ShortText
    handoff_required: Literal[True]
    trace_id: TraceId


MessageResponse = Annotated[
    CompletedResponse | ClarificationResponse | InsufficientEvidenceResponse,
    Field(discriminator="status"),
]


class MessageHistoryItem(ApiSchema):
    """One stored request-response exchange. / 一条已保存的请求与响应记录。"""

    request_id: RequestId
    request: SubmitMessageRequest
    response: MessageResponse
    created_at: AwareDatetime


class SessionDetailResponse(SessionCreatedResponse):
    """A session snapshot with ordered message history. / 带有序消息历史的会话快照。"""

    messages: list[MessageHistoryItem]


class FeedbackRequest(ApiSchema):
    """Feedback targeting one response trace in a session. / 指向会话内某次响应的反馈。"""

    session_id: UUID
    target_trace_id: TraceId
    rating: FeedbackRating
    reason: ShortText | None = None


class FeedbackCreatedResponse(ApiSchema):
    """Stable result returned after feedback is recorded. / 反馈记录后的稳定返回结构。"""

    feedback_id: UUID
    status: Literal["recorded"]
    session_id: UUID
    request_id: RequestId
    trace_id: TraceId
    target_trace_id: TraceId
    rating: FeedbackRating
    reason: ShortText | None = None
    created_at: AwareDatetime


class ErrorDetail(ApiSchema):
    """Safe user-visible error details. / 可以安全展示给用户的错误详情。"""

    code: ErrorCode
    message: ShortText
    retryable: bool


class ErrorResponse(ApiSchema):
    """Stable API error without an internal stack trace. / 不包含内部堆栈的稳定接口错误。"""

    error: ErrorDetail
    trace_id: TraceId
