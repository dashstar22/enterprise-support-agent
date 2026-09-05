"""Public support workflow wiring. / 公开售后工作流编排。"""

from __future__ import annotations

from pathlib import Path
from time import perf_counter
from typing import Any, Literal, TypedDict, cast
from uuid import UUID

from app.agent.adapters import (
    AuditRepository,
    BusinessContextProvider,
    EvidenceRetriever,
    SupportAnswerGenerator,
)
from app.agent.fake_adapters import FakeSupportAnswerGenerator
from app.agent.nodes import (
    ask_clarification,
    build_retrieval_query,
    check_evidence,
    finish,
    generate_support_answer,
    parse_request,
    persist_audit,
    query_business_context,
    retrieve_evidence,
    validate_required_fields,
)
from app.agent.parsing import RequestParser
from app.agent.state import (
    ControlledFailureOutput,
    EvidenceItem,
    FinalResponse,
    ParseRequestOutput,
    SupportState,
)
from app.observability.metrics import (
    AuditMetadata,
    ComponentTiming,
    ExternalApiCallAudit,
    TokenUsage,
)
from app.rag.evidence import CitationBinder, EvidenceGate, EvidenceRegistry
from app.rag.ragflow_client import RAGFlowRetrievedChunk

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FIXTURE_MANIFEST = PROJECT_ROOT / "data" / "fixtures" / "MANIFEST.json"


class FixtureEvidenceRetriever:
    """Retrieve only known synthetic fixture evidence for the offline demo. / 离线演示只检索已登记的合成资料证据。"""

    def __init__(self, registry: EvidenceRegistry) -> None:
        self._registry = registry

    def retrieve(self, request: object, /) -> list[EvidenceItem]:
        query = getattr(request, "retrieval_query", "")
        if not isinstance(query, str) or "E-200" not in query:
            return []
        if "E01" in query:
            candidate = RAGFlowRetrievedChunk(
                id="fixture-e200-e01",
                document_id="fixture-e200-maintenance-guide",
                document_name="e200-synthetic-maintenance-guide.md",
                content="Confirm that the main power switch is on.",
            )
        elif "E02" in query:
            candidate = RAGFlowRetrievedChunk(
                id="fixture-e200-e02",
                document_id="fixture-e200-fault-codes",
                document_name="e200-synthetic-fault-codes.txt",
                content="E02 | Cooling check | Stop the device and hand off to technical support.",
            )
        else:
            return []
        return [self._registry.to_evidence(candidate)]


class _PassthroughRequestParser:
    """Keep explicit HTTP fields when no parser is injected. / 未注入解析器时保留 HTTP 明确字段。"""

    def parse(self, request: object, /) -> ParseRequestOutput:
        return ParseRequestOutput(
            device_model=getattr(request, "device_model", None),
            fault_code=getattr(request, "fault_code", None),
            symptoms=list(getattr(request, "symptoms", [])),
            parsed_confidence=1.0,
        )


class _GraphState(TypedDict, total=False):
    """LangGraph channels kept separate from the validated support state. / 与严格售后状态分开的图通道。"""

    support_state: dict[str, object]
    timings: list[ComponentTiming]
    external_api_calls: list[ExternalApiCallAudit]
    token_usage: TokenUsage | None
    request_id: str
    response: FinalResponse


class SupportWorkflowExecutor:
    """Run the existing evidence-gated nodes for one public message. / 对一条公开消息执行已有的证据门禁节点。"""

    def __init__(
        self,
        *,
        retriever: EvidenceRetriever,
        business_provider: BusinessContextProvider,
        audit_repository: AuditRepository | None,
        gate: EvidenceGate,
        parser: RequestParser | None = None,
        answer_generator: SupportAnswerGenerator | None = None,
    ) -> None:
        self._retriever = retriever
        self._business_provider = business_provider
        self._audit_repository = audit_repository
        self._gate = gate
        self._binder = CitationBinder(gate)
        self._parser = parser or _PassthroughRequestParser()
        self._answer_generator = answer_generator
        self._graph = self._build_graph()

    @property
    def graph(self) -> Any:
        """Expose the compiled graph for tracing and inspection. / 暴露已编译图以便追踪和检查。"""

        return self._graph

    def run(
        self,
        *,
        session_id: UUID,
        user_message: str,
        device_model: str | None,
        fault_code: str | None,
        request_id: str,
        trace_id: str,
    ) -> FinalResponse:
        state = SupportState(
            session_id=session_id,
            user_message=user_message,
            device_model=device_model,
            fault_code=fault_code,
            trace_id=trace_id,
        )
        result = self._graph.invoke(
            {
                "support_state": state.model_dump(),
                "timings": [],
                "external_api_calls": [],
                "token_usage": None,
                "request_id": request_id,
            },
        )
        return cast(FinalResponse, result["response"])

    def _build_graph(self) -> Any:
        """Compile the real LangGraph state machine. / 编译真实 LangGraph 状态机。"""

        from langgraph.graph import END, START, StateGraph

        graph = StateGraph(_GraphState)
        graph.add_node("parse_request", cast(Any, self._parse_request_node))
        graph.add_node("validate_required_fields", cast(Any, self._validate_node))
        graph.add_node("ask_clarification", cast(Any, self._clarification_node))
        graph.add_node("build_retrieval_query", cast(Any, self._query_node))
        graph.add_node("retrieve_evidence", cast(Any, self._retrieval_node))
        graph.add_node("check_evidence", cast(Any, self._evidence_node))
        graph.add_node("query_business_context", cast(Any, self._business_node))
        graph.add_node("generate_support_answer", cast(Any, self._generation_node))
        graph.add_node("persist_audit", cast(Any, self._audit_node))
        graph.add_node("finish", cast(Any, self._finish_node))

        graph.add_edge(START, "parse_request")
        graph.add_edge("parse_request", "validate_required_fields")
        graph.add_conditional_edges(
            "validate_required_fields",
            self._route_validation,
            {"clarification": "ask_clarification", "ready": "build_retrieval_query"},
        )
        graph.add_edge("ask_clarification", "persist_audit")
        graph.add_edge("build_retrieval_query", "retrieve_evidence")
        graph.add_conditional_edges(
            "retrieve_evidence",
            self._route_retrieval,
            {"failed": "persist_audit", "retrieved": "check_evidence"},
        )
        graph.add_conditional_edges(
            "check_evidence",
            self._route_evidence,
            {"insufficient": "persist_audit", "sufficient": "query_business_context"},
        )
        graph.add_edge("query_business_context", "generate_support_answer")
        graph.add_conditional_edges(
            "generate_support_answer",
            self._route_generation,
            {"failed": "persist_audit", "generated": "persist_audit"},
        )
        graph.add_edge("persist_audit", "finish")
        graph.add_edge("finish", END)
        return graph.compile()

    @staticmethod
    def _support_state(values: _GraphState) -> SupportState:
        return SupportState.model_validate(values["support_state"])

    @staticmethod
    def _replace(values: _GraphState, state: SupportState) -> dict[str, object]:
        return {"support_state": state.model_dump()}

    def _parse_request_node(self, values: _GraphState) -> dict[str, object]:
        state = self._support_state(values)
        return self._replace(
            values, _update(state, parse_request(state, self._parser).model_dump())
        )

    def _validate_node(self, values: _GraphState) -> dict[str, object]:
        state = self._support_state(values)
        return self._replace(values, _update(state, validate_required_fields(state).model_dump()))

    def _clarification_node(self, values: _GraphState) -> dict[str, object]:
        state = self._support_state(values)
        return self._replace(values, _update(state, ask_clarification(state).model_dump()))

    def _query_node(self, values: _GraphState) -> dict[str, object]:
        state = self._support_state(values)
        return self._replace(values, _update(state, build_retrieval_query(state).model_dump()))

    def _retrieval_node(self, values: _GraphState) -> dict[str, object]:
        state = self._support_state(values)
        started = perf_counter()
        result = retrieve_evidence(state, self._retriever)
        latency = _elapsed_ms(started)
        timings = list(values.get("timings", []))
        calls = list(values.get("external_api_calls", []))
        outcome = "failure" if isinstance(result, ControlledFailureOutput) else "success"
        timings.append(ComponentTiming(component="retrieval", latency_ms=latency, outcome=outcome))
        calls.append(
            ExternalApiCallAudit(
                service="ragflow",
                status_code=200 if outcome == "success" else None,
                latency_ms=latency,
                outcome=outcome,
                error_code=result.error_code
                if isinstance(result, ControlledFailureOutput)
                else None,
            )
        )
        return {
            "support_state": _update(state, result.model_dump()).model_dump(),
            "timings": timings,
            "external_api_calls": calls,
        }

    def _evidence_node(self, values: _GraphState) -> dict[str, object]:
        state = self._support_state(values)
        return self._replace(values, _update(state, check_evidence(state, self._gate).model_dump()))

    def _business_node(self, values: _GraphState) -> dict[str, object]:
        state = self._support_state(values)
        started = perf_counter()
        result = query_business_context(state, self._business_provider)
        latency = _elapsed_ms(started)
        timings = list(values.get("timings", []))
        calls = list(values.get("external_api_calls", []))
        failed = isinstance(result, ControlledFailureOutput)
        timings.append(
            ComponentTiming(
                component="business_api",
                latency_ms=latency,
                outcome="failure" if failed else "success",
            )
        )
        calls.append(
            ExternalApiCallAudit(
                service="business_api",
                status_code=None if failed else 200,
                latency_ms=latency,
                outcome="failure" if failed else "success",
                error_code=result.error_code
                if isinstance(result, ControlledFailureOutput)
                else None,
            )
        )
        return {
            "support_state": _update(state, result.model_dump()).model_dump(),
            "timings": timings,
            "external_api_calls": calls,
        }

    def _generation_node(self, values: _GraphState) -> dict[str, object]:
        state = self._support_state(values)
        generator = self._answer_generator or FakeSupportAnswerGenerator(
            result=_fixture_answer(state.evidence, state.business_context)
        )
        started = perf_counter()
        result = generate_support_answer(state, generator, self._binder)
        latency = _elapsed_ms(started)
        timings = list(values.get("timings", []))
        token_usage = getattr(generator, "last_usage", None)
        outcome = "failure" if isinstance(result, ControlledFailureOutput) else "success"
        timings.append(ComponentTiming(component="model", latency_ms=latency, outcome=outcome))
        calls = list(values.get("external_api_calls", []))
        if getattr(generator, "is_external", False):
            calls.append(
                ExternalApiCallAudit(
                    service="llm",
                    status_code=getattr(generator, "last_status_code", None),
                    latency_ms=latency,
                    outcome=outcome,
                    error_code=(
                        result.error_code if isinstance(result, ControlledFailureOutput) else None
                    ),
                )
            )
        return {
            "support_state": _update(state, result.model_dump()).model_dump(),
            "timings": timings,
            "external_api_calls": calls,
            "token_usage": token_usage,
        }

    def _audit_node(self, values: _GraphState) -> dict[str, object]:
        state = self._support_state(values)
        if self._audit_repository is None:
            return {}
        metadata = AuditMetadata(
            request_id=values.get("request_id", ""),
            timings=list(values.get("timings", [])),
            external_api_calls=list(values.get("external_api_calls", [])),
            token_usage=values.get("token_usage"),
        )
        result = persist_audit(state, self._audit_repository, metadata)
        return self._replace(values, _update(state, result.model_dump()))

    def _finish_node(self, values: _GraphState) -> dict[str, object]:
        return {"response": finish(self._support_state(values)).response}

    @staticmethod
    def _route_validation(values: _GraphState) -> Literal["clarification", "ready"]:
        return (
            "clarification"
            if SupportWorkflowExecutor._support_state(values).missing_fields
            else "ready"
        )

    @staticmethod
    def _route_retrieval(values: _GraphState) -> Literal["failed", "retrieved"]:
        return (
            "failed" if SupportWorkflowExecutor._support_state(values).error_code else "retrieved"
        )

    @staticmethod
    def _route_evidence(values: _GraphState) -> Literal["insufficient", "sufficient"]:
        return (
            "sufficient"
            if SupportWorkflowExecutor._support_state(values).evidence_sufficient
            else "insufficient"
        )

    @staticmethod
    def _route_generation(values: _GraphState) -> Literal["failed", "generated"]:
        return (
            "failed"
            if SupportWorkflowExecutor._support_state(values).error_code
            and SupportWorkflowExecutor._support_state(values).answer is None
            else "generated"
        )


def _fixture_answer(
    evidence: list[EvidenceItem], business_context: object | None
) -> dict[str, object]:
    source = evidence[0]
    summary = source.text
    if getattr(business_context, "inventory_available", None) is False:
        summary = f"{summary} 模拟业务接口显示备件库存不足，建议转人工。"
    return {
        "summary": summary,
        "steps": [
            {
                "order": 1,
                "instruction": source.text,
                "reason": "步骤直接引用当前已验证的公开合成资料。",
                "citation": {
                    "source_name": source.source_name,
                    "page": source.page,
                    "section": source.section,
                },
            }
        ],
        "confidence": "supported",
        "handoff_required": False,
    }


def _update(state: SupportState, update: dict[str, object]) -> SupportState:
    return SupportState.model_validate({**state.model_dump(), **update})


def _elapsed_ms(started: float) -> float:
    return round((perf_counter() - started) * 1000, 3)
