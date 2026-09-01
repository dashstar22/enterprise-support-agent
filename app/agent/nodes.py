"""Workflow node implementations. / 工作流节点实现。"""

from copy import deepcopy

from pydantic import ValidationError

from app.agent.adapters import (
    AdapterError,
    AuditRepository,
    BusinessContextProvider,
    EvidenceRetriever,
    SupportAnswerGenerator,
)
from app.agent.parsing import RequestParser, RequestParsingError
from app.agent.state import (
    MAX_SYMPTOMS,
    AskClarificationInput,
    AskClarificationOutput,
    BusinessContext,
    CheckEvidenceInput,
    CheckEvidenceOutput,
    ControlledFailureOutput,
    EvidenceItem,
    FinalResponse,
    FinishInput,
    FinishOutput,
    GenerateSupportAnswerInput,
    GenerateSupportAnswerOutput,
    GenerateSupportAnswerResult,
    ParseRequestInput,
    ParseRequestOutput,
    PersistAuditInput,
    PersistAuditOutput,
    PersistAuditResult,
    QueryBusinessContextInput,
    QueryBusinessContextOutput,
    QueryBusinessContextResult,
    RetrieveEvidenceInput,
    RetrieveEvidenceOutput,
    RetrieveEvidenceResult,
    SupportState,
    ValidateRequiredFieldsInput,
    ValidateRequiredFieldsOutput,
)
from app.api.schemas import (
    ClarificationResponse,
    CompletedResponse,
    ErrorDetail,
    ErrorResponse,
    InsufficientEvidenceResponse,
    MissingField,
    SupportAnswer,
)
from app.observability.metrics import AuditMetadata
from app.rag.evidence import CitationBinder, CitationBindingError, EvidenceGate

REQUIRED_FIELDS: tuple[MissingField, ...] = ("device_model", "fault_code")
FIELD_LABELS: dict[MissingField, str] = {
    "device_model": "设备型号",
    "fault_code": "故障码",
}


def parse_request(state: SupportState, parser: RequestParser) -> ParseRequestOutput:
    """Apply one replaceable parser without overwriting explicit fields. / 调用可替换解析器且不覆盖明确字段。"""

    node_input = ParseRequestInput(
        user_message=state.user_message,
        device_model=state.device_model,
        fault_code=state.fault_code,
        symptoms=state.symptoms,
    )

    try:
        raw_output: object = parser.parse(node_input)
        payload = (
            raw_output.model_dump() if isinstance(raw_output, ParseRequestOutput) else raw_output
        )
        parsed = ParseRequestOutput.model_validate(payload)
    except (RequestParsingError, ValidationError):
        return _fallback_parse_output(node_input)

    return ParseRequestOutput(
        device_model=node_input.device_model or parsed.device_model,
        fault_code=node_input.fault_code or parsed.fault_code,
        symptoms=_merge_symptoms(node_input.symptoms, parsed.symptoms),
        parsed_confidence=parsed.parsed_confidence,
    )


def _fallback_parse_output(node_input: ParseRequestInput) -> ParseRequestOutput:
    return ParseRequestOutput(
        device_model=node_input.device_model,
        fault_code=node_input.fault_code,
        symptoms=list(node_input.symptoms),
        parsed_confidence=0.0,
    )


def _merge_symptoms(existing: list[str], parsed: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for symptom in [*existing, *parsed]:
        if symptom not in seen:
            seen.add(symptom)
            merged.append(symptom)
            if len(merged) == MAX_SYMPTOMS:
                break
    return merged


def validate_required_fields(state: SupportState) -> ValidateRequiredFieldsOutput:
    """List missing equipment fields in stable business order. / 按固定业务顺序列出缺失设备字段。"""

    node_input = ValidateRequiredFieldsInput(
        device_model=state.device_model,
        fault_code=state.fault_code,
    )
    missing_fields: list[MissingField] = []
    if node_input.device_model is None:
        missing_fields.append("device_model")
    if node_input.fault_code is None:
        missing_fields.append("fault_code")
    return ValidateRequiredFieldsOutput(missing_fields=missing_fields)


def ask_clarification(state: SupportState) -> AskClarificationOutput:
    """Ask only for the required fields that remain missing. / 只追问仍然缺失的必要字段。"""

    node_input = AskClarificationInput(missing_fields=state.missing_fields)
    ordered_fields = [field for field in REQUIRED_FIELDS if field in node_input.missing_fields]
    missing_labels = "和".join(FIELD_LABELS[field] for field in ordered_fields)
    return AskClarificationOutput(
        clarification_question=f"请补充{missing_labels}。",
    )


def check_evidence(state: SupportState, gate: EvidenceGate | None = None) -> CheckEvidenceOutput:
    """Gate answer generation on at least one validated candidate. / 至少有一条已校验证据才允许生成回答。"""

    node_input = CheckEvidenceInput(evidence=state.evidence)
    sufficient = (
        gate.accepts(node_input.evidence) if gate is not None else bool(node_input.evidence)
    )
    return CheckEvidenceOutput(
        evidence_sufficient=sufficient,
        handoff_required=not sufficient,
    )


def generate_support_answer(
    state: SupportState, generator: SupportAnswerGenerator, binder: CitationBinder | None = None
) -> GenerateSupportAnswerResult:
    """Validate an answer and allow at most one retry. / 校验回答且最多允许重试一次。"""

    node_input = GenerateSupportAnswerInput(
        evidence=state.evidence,
        business_context=state.business_context,
    )
    attempts = 2 if state.generation_retry_count == 0 else 1
    retry_count = state.generation_retry_count
    last_error: AdapterError | None = None

    for attempt in range(attempts):
        try:
            raw_output = generator.generate(node_input)
            payload = (
                raw_output.model_dump() if isinstance(raw_output, SupportAnswer) else raw_output
            )
            answer = SupportAnswer.model_validate(payload)
            if binder is not None and not binder.supports(answer, node_input.evidence):
                raise CitationBindingError
            return GenerateSupportAnswerOutput(
                answer=answer,
                generation_retry_count=retry_count,
            )
        except AdapterError as exc:
            last_error = exc
        except (CitationBindingError, ValidationError):
            last_error = None

        if attempt + 1 < attempts:
            retry_count = 1

    return ControlledFailureOutput(
        error_code=(
            last_error.error_code
            if last_error is not None
            else "ANSWER_GENERATION_INVALID_RESPONSE"
        ),
        handoff_required=True,
    )


def retrieve_evidence(state: SupportState, retriever: EvidenceRetriever) -> RetrieveEvidenceResult:
    """Retrieve candidates or return a controlled handoff failure. / 检索候选证据或返回受控转人工失败。"""

    node_input = RetrieveEvidenceInput(retrieval_query=state.retrieval_query)
    try:
        raw_result: object = retriever.retrieve(node_input)
        payload = (
            [item.model_dump() if isinstance(item, EvidenceItem) else item for item in raw_result]
            if isinstance(raw_result, list)
            else raw_result
        )
        return RetrieveEvidenceOutput.model_validate({"evidence": payload})
    except AdapterError as exc:
        return _controlled_failure(exc, default_code="RETRIEVAL_ERROR", handoff_required=True)
    except (TypeError, ValidationError):
        return _controlled_failure(
            None,
            default_code="RETRIEVAL_INVALID_RESPONSE",
            handoff_required=True,
        )


def query_business_context(
    state: SupportState, provider: BusinessContextProvider
) -> QueryBusinessContextResult:
    """Load optional business facts or safely degrade without them. / 加载业务事实或安全降级为空。"""

    node_input = QueryBusinessContextInput(
        device_model=state.device_model,
        fault_code=state.fault_code,
    )
    try:
        raw_result: object = provider.get_context(node_input)
        payload = raw_result.model_dump() if isinstance(raw_result, BusinessContext) else raw_result
        return QueryBusinessContextOutput.model_validate({"business_context": payload})
    except AdapterError as exc:
        return _controlled_failure(exc, default_code="BUSINESS_API_ERROR", handoff_required=False)
    except (TypeError, ValidationError):
        return _controlled_failure(
            None,
            default_code="BUSINESS_API_INVALID_RESPONSE",
            handoff_required=False,
        )


def persist_audit(
    state: SupportState,
    repository: AuditRepository,
    metadata: AuditMetadata | None = None,
) -> PersistAuditResult:
    """Persist complete state or return a handoff failure without a fake id. / 保存完整状态或无假编号地转人工。"""

    node_input = PersistAuditInput(state=deepcopy(state), metadata=deepcopy(metadata))
    try:
        raw_result: object = repository.persist(node_input)
        return PersistAuditOutput.model_validate({"audit_record_id": raw_result})
    except AdapterError as exc:
        return _controlled_failure(
            exc, default_code="AUDIT_PERSISTENCE_ERROR", handoff_required=True
        )
    except (TypeError, ValidationError):
        return _controlled_failure(
            None,
            default_code="AUDIT_INVALID_RESPONSE",
            handoff_required=True,
        )


def _controlled_failure(
    error: AdapterError | None,
    *,
    default_code: str,
    handoff_required: bool,
) -> ControlledFailureOutput:
    return ControlledFailureOutput(
        error_code=error.error_code if error is not None else default_code,
        handoff_required=error.handoff_required if error is not None else handoff_required,
    )


_ERROR_MESSAGES: dict[str, str] = {
    "RAGFLOW_TIMEOUT": "知识库响应超时，请稍后重试或转人工。",
    "RAGFLOW_UNAVAILABLE": "知识库暂时不可用，请稍后重试或转人工。",
    "RAGFLOW_REJECTED": "知识库拒绝本次检索，请转人工。",
    "RAGFLOW_INVALID_RESPONSE": "知识库返回结果无法确认，请转人工。",
    "RETRIEVAL_ERROR": "知识库暂时不可用，请转人工。",
    "RETRIEVAL_INVALID_RESPONSE": "知识库返回结果无法确认，请转人工。",
    "BUSINESS_API_ERROR": "业务系统暂时不可用，请稍后重试。",
    "BUSINESS_API_NOT_FOUND": "业务系统中未找到该设备，请核对型号后重试。",
    "BUSINESS_API_RATE_LIMITED": "业务系统请求过多，请稍后重试。",
    "BUSINESS_API_TIMEOUT": "业务系统响应超时，请稍后重试。",
    "BUSINESS_API_UNAVAILABLE": "业务系统暂时不可用，请稍后重试。",
    "BUSINESS_API_INVALID_RESPONSE": "业务系统返回结果无法确认，请稍后重试。",
    "AUDIT_PERSISTENCE_ERROR": "审计记录保存失败，请转人工。",
    "AUDIT_INVALID_RESPONSE": "审计记录结果无法确认，请转人工。",
    "AUDIT_CONTEXT_MISSING": "审计关联信息不完整，请转人工。",
}
_NON_RETRYABLE_CODES = {
    "RAGFLOW_REJECTED",
    "RAGFLOW_INVALID_RESPONSE",
    "RETRIEVAL_INVALID_RESPONSE",
    "BUSINESS_API_NOT_FOUND",
    "BUSINESS_API_INVALID_RESPONSE",
    "AUDIT_INVALID_RESPONSE",
    "AUDIT_CONTEXT_MISSING",
}


def finish(state: SupportState) -> FinishOutput:
    """Map one validated state to exactly one public response. / 将状态映射为唯一一种公开响应。"""

    node_input = FinishInput(
        session_id=state.session_id,
        trace_id=state.trace_id,
        missing_fields=state.missing_fields,
        clarification_question=state.clarification_question,
        evidence_sufficient=state.evidence_sufficient,
        answer=state.answer,
        handoff_required=state.handoff_required,
        error_code=state.error_code,
    )

    if node_input.missing_fields:
        response: FinalResponse = ClarificationResponse(
            session_id=node_input.session_id,
            status="needs_clarification",
            question=node_input.clarification_question or "请补充必要信息。",
            missing_fields=node_input.missing_fields,
            trace_id=node_input.trace_id,
        )
    elif node_input.error_code is not None and node_input.handoff_required:
        code = node_input.error_code
        response = ErrorResponse(
            error=ErrorDetail(
                code=code,
                message=_ERROR_MESSAGES.get(code, "系统暂时无法完成请求，请稍后重试。"),
                retryable=code not in _NON_RETRYABLE_CODES,
            ),
            trace_id=node_input.trace_id,
        )
    elif node_input.answer is not None:
        response = CompletedResponse(
            session_id=node_input.session_id,
            status="completed",
            answer=node_input.answer,
            trace_id=node_input.trace_id,
        )
    else:
        response = InsufficientEvidenceResponse(
            session_id=node_input.session_id,
            status="insufficient_evidence",
            message="当前资料没有足够证据，建议转人工。",
            handoff_required=True,
            trace_id=node_input.trace_id,
        )

    return FinishOutput(response=response)
