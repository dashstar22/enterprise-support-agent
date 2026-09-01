"""Public support workflow wiring. / 公开售后工作流编排。"""

from __future__ import annotations

from pathlib import Path
from time import perf_counter
from uuid import UUID

from app.agent.adapters import AuditRepository, BusinessContextProvider, EvidenceRetriever
from app.agent.fake_adapters import FakeSupportAnswerGenerator
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
from app.agent.state import ControlledFailureOutput, EvidenceItem, FinalResponse, SupportState
from app.observability.metrics import AuditMetadata, ComponentTiming, ExternalApiCallAudit
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


class SupportWorkflowExecutor:
    """Run the existing evidence-gated nodes for one public message. / 对一条公开消息执行已有的证据门禁节点。"""

    def __init__(
        self,
        *,
        retriever: EvidenceRetriever,
        business_provider: BusinessContextProvider,
        audit_repository: AuditRepository | None,
        gate: EvidenceGate,
    ) -> None:
        self._retriever = retriever
        self._business_provider = business_provider
        self._audit_repository = audit_repository
        self._gate = gate
        self._binder = CitationBinder(gate)

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
        state = _update(state, validate_required_fields(state).model_dump())
        timings: list[ComponentTiming] = []
        calls: list[ExternalApiCallAudit] = []

        if state.missing_fields:
            state = _update(state, ask_clarification(state).model_dump())
            return self._finish_and_audit(state, request_id, timings, calls)

        state = _update(state, {"retrieval_query": f"{state.device_model} {state.fault_code}"})
        retrieval_started = perf_counter()
        retrieved = retrieve_evidence(state, self._retriever)
        retrieval_latency = _elapsed_ms(retrieval_started)
        if isinstance(retrieved, ControlledFailureOutput):
            state = _update(state, retrieved.model_dump())
            return self._finish_and_audit(state, request_id, timings, calls)
        timings.append(
            ComponentTiming(component="retrieval", latency_ms=retrieval_latency, outcome="success")
        )
        state = _update(state, retrieved.model_dump())
        evidence_check = check_evidence(state, self._gate)
        state = _update(state, evidence_check.model_dump())
        if not evidence_check.evidence_sufficient:
            return self._finish_and_audit(state, request_id, timings, calls)

        business_started = perf_counter()
        business = query_business_context(state, self._business_provider)
        business_latency = _elapsed_ms(business_started)
        if isinstance(business, ControlledFailureOutput):
            # Business facts are optional. Keep the evidence-backed answer and audit the failure.
            timings.append(
                ComponentTiming(
                    component="business_api", latency_ms=business_latency, outcome="failure"
                )
            )
            calls.append(
                ExternalApiCallAudit(
                    service="business_api",
                    status_code=None,
                    latency_ms=business_latency,
                    outcome="failure",
                    error_code=business.error_code,
                )
            )
        else:
            timings.append(
                ComponentTiming(
                    component="business_api", latency_ms=business_latency, outcome="success"
                )
            )
            calls.append(
                ExternalApiCallAudit(
                    service="business_api",
                    status_code=200,
                    latency_ms=business_latency,
                    outcome="success",
                )
            )
            state = _update(state, business.model_dump())

        model_started = perf_counter()
        generated = generate_support_answer(
            state,
            FakeSupportAnswerGenerator(
                result=_fixture_answer(state.evidence, state.business_context)
            ),
            self._binder,
        )
        model_latency = _elapsed_ms(model_started)
        if isinstance(generated, ControlledFailureOutput):
            state = _update(state, generated.model_dump())
            return self._finish_and_audit(state, request_id, timings, calls)
        timings.append(
            ComponentTiming(component="model", latency_ms=model_latency, outcome="success")
        )
        state = _update(state, generated.model_dump())
        return self._finish_and_audit(state, request_id, timings, calls)

    def _finish_and_audit(
        self,
        state: SupportState,
        request_id: str,
        timings: list[ComponentTiming],
        calls: list[ExternalApiCallAudit],
    ) -> FinalResponse:
        if self._audit_repository is not None:
            persisted = persist_audit(
                state,
                self._audit_repository,
                AuditMetadata(request_id=request_id, timings=timings, external_api_calls=calls),
            )
            if isinstance(persisted, ControlledFailureOutput):
                state = _update(state, persisted.model_dump())
            else:
                state = _update(state, persisted.model_dump())
        return finish(state).response


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
