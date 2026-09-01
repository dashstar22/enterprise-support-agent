"""Deterministic C6 workflow evaluation and JSON export. / 确定性 C6 工作流评测和 JSON 导出。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from time import perf_counter
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from app.agent.adapters import AdapterError
from app.agent.fake_adapters import (
    FakeBusinessContextProvider,
    FakeEvidenceRetriever,
    FakeSupportAnswerGenerator,
)
from app.agent.nodes import (
    ask_clarification,
    check_evidence,
    finish,
    generate_support_answer,
    query_business_context,
    retrieve_evidence,
    validate_required_fields,
)
from app.agent.state import (
    BusinessContext,
    ControlledFailureOutput,
    EvidenceItem,
    FinalResponse,
    QueryBusinessContextOutput,
    SupportState,
)
from app.api.schemas import CompletedResponse, ErrorResponse
from app.evaluation.models import (
    CitationTarget,
    EvaluationQuestion,
    EvaluationQuestionSet,
    SemanticReviewSheet,
)
from app.ocr.models import OcrDocumentResult
from app.ocr.pipeline import OcrPipeline, RapidOcrEngine
from app.rag.evidence import CitationBinder, EvidenceGate, EvidenceRegistry
from app.rag.ragflow_client import RAGFlowRetrievedChunk

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_QUESTIONS_PATH = PROJECT_ROOT / "data" / "evaluation" / "c6_fixed_questions.v1.json"
DEFAULT_REVIEW_PATH = PROJECT_ROOT / "data" / "evaluation" / "c6_manual_semantic_review.v1.json"
DEFAULT_OCR_FIXTURE = PROJECT_ROOT / "data" / "fixtures" / "e200-synthetic-control-panel.png"
DEFAULT_FIXTURE_MANIFEST = PROJECT_ROOT / "data" / "fixtures" / "MANIFEST.json"


def load_question_set(path: Path = DEFAULT_QUESTIONS_PATH) -> EvaluationQuestionSet:
    """Load and validate the versioned fixed question set. / 读取并校验带版本的固定题集。"""

    return EvaluationQuestionSet.model_validate_json(path.read_text(encoding="utf-8"))


def load_review_sheet(path: Path) -> SemanticReviewSheet:
    """Load a human-only semantic review sheet. / 读取只能由人工确认的语义复核表。"""

    return SemanticReviewSheet.model_validate_json(path.read_text(encoding="utf-8"))


def percentile(values: list[float], fraction: float) -> float | None:
    """Return the nearest-rank percentile for a nonempty sample. / 返回非空样本的最近秩分位数。"""

    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(len(ordered) * fraction + 0.999999) - 1))
    return ordered[index]


class C6EvaluationRunner:
    """Run fixed questions through real nodes with deterministic local adapters. / 用确定性本地适配器运行真实节点。"""

    def __init__(self, question_set: EvaluationQuestionSet) -> None:
        self._question_set = question_set
        self._registry = EvidenceRegistry.from_manifest(DEFAULT_FIXTURE_MANIFEST)
        self._gate = EvidenceGate(self._registry)
        self._binder = CitationBinder(self._gate)
        self._ocr_result: OcrDocumentResult | None = None
        self._ocr_latency_ms: float | None = None

    def run(self, review_sheet: SemanticReviewSheet | None = None) -> dict[str, object]:
        """Evaluate all fixed questions and make every quality layer explicit. / 评测全部固定题并显式分层。"""

        if (
            review_sheet is not None
            and review_sheet.question_set_version != self._question_set.version
        ):
            raise ValueError("人工复核表版本与固定题集不一致")
        if review_sheet is not None:
            expected_ids = {question.id for question in self._question_set.questions}
            review_ids = {review.question_id for review in review_sheet.reviews}
            if review_ids != expected_ids or len(review_sheet.reviews) != len(expected_ids):
                raise ValueError("人工复核表必须恰好覆盖固定题集的每一道题")

        observations = [self._run_question(question) for question in self._question_set.questions]
        ocr_observations = self._run_ocr_questions()
        metrics = self._build_metrics(observations, ocr_observations, review_sheet)
        return {
            "evaluation_version": self._question_set.version,
            "generated_at": datetime.now(UTC).isoformat(),
            "runtime": {
                "answer_generator": "FakeSupportAnswerGenerator",
                "answer_generator_note": "本次只验证固定本地工作流，不代表真实 LLM (大模型) 回答。",
                "retriever": "FixtureEvidenceRegistry",
                "retriever_note": "候选正文必须匹配当前固定资料、SHA-256 和稳定定位，不代表远程 RAGFlow 线上评测。",
                "knowledge_base_version": "fixture-manifest-v"
                f"{self._question_set.fixture_manifest_version}",
                "configuration": {
                    "question_set_sha256": _sha256_file(DEFAULT_QUESTIONS_PATH),
                    "fixture_manifest_sha256": _sha256_file(DEFAULT_FIXTURE_MANIFEST),
                    "ocr_fixture_sha256": _sha256_file(DEFAULT_OCR_FIXTURE),
                },
                "model": {
                    "name": None,
                    "provider": None,
                    "mode": "fixed_fake_generator",
                    "note": "未调用真实 LLM (大模型)，模型耗时仅表示本地结构化回答校验开销。",
                },
                "token_usage": None,
                "estimated_token_cost": None,
            },
            "questions": observations,
            "ocr": ocr_observations,
            "metrics": metrics,
        }

    def _run_question(self, question: EvaluationQuestion) -> dict[str, Any]:
        started = perf_counter()
        state = SupportState(
            session_id=uuid5(NAMESPACE_URL, question.id),
            user_message=question.user_message,
            device_model=question.device_model,
            fault_code=question.fault_code,
            trace_id=f"trace-{question.id.lower()}",
        )
        required = validate_required_fields(state)
        state = _apply_update(state, required.model_dump())

        component_latencies: dict[str, float] = {}
        business_behavior_actual = "not_called"
        business_inventory_available: bool | None = None
        candidate_sources: list[str] = []

        if state.missing_fields:
            clarification = ask_clarification(state)
            state = _apply_update(state, clarification.model_dump())
            response = finish(state).response
        else:
            state = _apply_update(
                state,
                {"retrieval_query": f"{state.device_model} {state.fault_code}"},
            )
            retrieval_started = perf_counter()
            retriever = _retriever_for(question, self._registry)
            retrieval = retrieve_evidence(state, retriever)
            component_latencies["retrieval"] = _elapsed_ms(retrieval_started)
            state = _apply_update(state, retrieval.model_dump())
            candidate_sources = [item.source_name for item in state.evidence]

            if isinstance(retrieval, ControlledFailureOutput):
                response = finish(state).response
            else:
                evidence_check = check_evidence(state, self._gate)
                state = _apply_update(state, evidence_check.model_dump())
                if not evidence_check.evidence_sufficient:
                    response = finish(state).response
                else:
                    if question.business_api_behavior != "not_called":
                        business_started = perf_counter()
                        business_result = query_business_context(state, _business_for(question))
                        component_latencies["business_api"] = _elapsed_ms(business_started)
                        state = _apply_update(state, business_result.model_dump())
                        business_behavior_actual = _business_behavior_from(business_result)
                        if isinstance(business_result, QueryBusinessContextOutput):
                            context = business_result.business_context
                            business_inventory_available = (
                                context.inventory_available if context is not None else None
                            )

                    model_started = perf_counter()
                    generated = generate_support_answer(
                        state,
                        FakeSupportAnswerGenerator(
                            result=_answer_for(state.evidence, state.business_context)
                        ),
                        self._binder,
                    )
                    component_latencies["model"] = _elapsed_ms(model_started)
                    state = _apply_update(state, generated.model_dump())
                    response = finish(state).response

        status = _response_status(response)
        return {
            "question_id": question.id,
            "category": question.category,
            "expected_status": question.expected_status,
            "actual_status": status,
            "status_passed": status == question.expected_status,
            "candidate_sources": candidate_sources,
            "candidate_hit_at_5": _candidate_hit(question.target, candidate_sources),
            "citation_valid": _citation_matches_target(question.target, response)
            and self._gate.accepts(state.evidence),
            "accepted_evidence": [item.model_dump(mode="json") for item in state.evidence],
            "accepted_evidence_hashes": [item.content_hash for item in state.evidence],
            "response": response.model_dump(mode="json"),
            "structured_output_valid": _is_structured_response(response),
            "business_api_expected": question.business_api_behavior,
            "business_api_actual": business_behavior_actual,
            "business_api_passed": business_behavior_actual == question.business_api_behavior,
            "business_inventory_expected": question.expected_inventory_available,
            "business_inventory_actual": business_inventory_available,
            "business_inventory_passed": (
                business_inventory_available == question.expected_inventory_available
                if question.expected_inventory_available is not None
                else None
            ),
            "component_latency_ms": component_latencies,
            "total_latency_ms": _elapsed_ms(started),
        }

    def _run_ocr_questions(self) -> list[dict[str, Any]]:
        ocr_questions = [
            question for question in self._question_set.questions if question.ocr_expectations
        ]
        if not ocr_questions:
            return []
        if self._ocr_result is None:
            started = perf_counter()
            self._ocr_result = OcrPipeline(RapidOcrEngine()).extract(DEFAULT_OCR_FIXTURE)
            self._ocr_latency_ms = _elapsed_ms(started)

        result = self._ocr_result
        if result is None:
            raise RuntimeError("OCR 结果未初始化")
        fields = result.pages[0].extracted_fields
        observations: list[dict[str, Any]] = []
        for question in ocr_questions:
            checks = []
            for expectation in question.ocr_expectations:
                extracted = getattr(fields, expectation.field)
                checks.append(
                    {
                        "field": expectation.field,
                        "expected": expectation.expected_value,
                        "actual": extracted.value if extracted is not None else None,
                        "passed": extracted is not None
                        and extracted.value == expectation.expected_value
                        and not extracted.requires_confirmation,
                    }
                )
            observations.append(
                {
                    "question_id": question.id,
                    "source": DEFAULT_OCR_FIXTURE.name,
                    "latency_ms": self._ocr_latency_ms,
                    "checks": checks,
                }
            )
        return observations

    def _build_metrics(
        self,
        observations: list[dict[str, Any]],
        ocr_observations: list[dict[str, Any]],
        review_sheet: SemanticReviewSheet | None,
    ) -> dict[str, object]:
        targeted = [item for item in observations if item["candidate_hit_at_5"] is not None]
        cited = [item for item in observations if item["citation_valid"] is not None]
        refusals = [
            item for item in observations if item["expected_status"] == "insufficient_evidence"
        ]
        clarifications = [
            item for item in observations if item["expected_status"] == "needs_clarification"
        ]
        success_calls = [
            item for item in observations if item["business_api_expected"] == "success"
        ]
        inventory_cases = [
            item for item in observations if item["business_inventory_expected"] is not None
        ]
        component_samples: dict[str, list[float]] = {}
        for observation in observations:
            for component, latency in observation["component_latency_ms"].items():
                component_samples.setdefault(component, []).append(float(latency))
        ocr_latencies = {
            float(observation["latency_ms"])
            for observation in ocr_observations
            if observation["latency_ms"] is not None
        }
        if ocr_latencies:
            component_samples["ocr"] = sorted(ocr_latencies)
        ocr_checks = [check for item in ocr_observations for check in item["checks"]]

        semantic: dict[str, object] = {
            "status": "pending_human_review",
            "approved": None,
            "total": len(self._question_set.questions),
            "rate": None,
        }
        if review_sheet is not None:
            by_id = {review.question_id: review for review in review_sheet.reviews}
            approved = sum(
                1
                for question in self._question_set.questions
                if by_id.get(question.id) and by_id[question.id].decision == "approved"
            )
            semantic = {
                "status": (
                    "human_review_complete"
                    if all(review.decision != "pending" for review in review_sheet.reviews)
                    else "human_review_pending"
                ),
                "approved": approved,
                "total": len(self._question_set.questions),
                "rate": _rate(approved, len(self._question_set.questions)),
            }

        return {
            "candidate_retrieval_hit_at_5": _score(targeted, "candidate_hit_at_5"),
            "current_citation_accuracy": _score(cited, "citation_valid"),
            "no_evidence_refusal_rate": _score(refusals, "status_passed"),
            "clarification_accuracy": _score(clarifications, "status_passed"),
            "structured_output_pass_rate": _score(observations, "structured_output_valid"),
            "business_api_integration_success_rate": _score(success_calls, "business_api_passed"),
            "business_inventory_context_accuracy": _score(
                inventory_cases, "business_inventory_passed"
            ),
            "ocr_field_accuracy": _score(ocr_checks, "passed"),
            "system_status_pass_rate": _score(observations, "status_passed"),
            "semantic_answer_review": semantic,
            "latency_ms": {
                component: {
                    "p50": median(values),
                    "p95": percentile(values, 0.95),
                    "count": len(values),
                }
                for component, values in component_samples.items()
            }
            | {
                "database": {
                    "p50": None,
                    "p95": None,
                    "count": 0,
                    "note": "固定评测不执行持久化; 数据库事务由 C6 集成测试单独验证。",
                }
            },
            "token_accounting": {
                "input_tokens": None,
                "output_tokens": None,
                "estimated_cost": None,
                "note": "固定本地回答生成器不调用模型，因此不虚构 Token 或成本。",
            },
        }


def export_result(result: dict[str, object], output_path: Path) -> None:
    """Write one inspectable UTF-8 evaluation record. / 写出一份可检查的 UTF-8 评测记录。"""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _apply_update(state: SupportState, update: dict[str, object]) -> SupportState:
    return SupportState.model_validate({**state.model_dump(), **update})


def _retriever_for(
    question: EvaluationQuestion, registry: EvidenceRegistry
) -> FakeEvidenceRetriever:
    if question.category == "external_failure":
        return FakeEvidenceRetriever(
            error=AdapterError("RAGFLOW_UNAVAILABLE", handoff_required=True)
        )
    if question.target is None:
        return FakeEvidenceRetriever()
    target = question.target
    snippet = question.expected_evidence_snippet
    if snippet is None:
        raise ValueError("完成题缺少预期证据原文")
    evidence = registry.to_evidence(
        RAGFlowRetrievedChunk(
            id=f"chunk-{question.id.lower()}",
            document_id=f"fixture-{question.id.lower()}",
            document_name=target.source_name,
            content=snippet,
            positions=[],
        )
    )
    if evidence.page != target.page or evidence.section != target.section:
        raise ValueError("题集目标定位与当前夹具登记信息不一致")
    return FakeEvidenceRetriever(result=[evidence])


def _business_for(question: EvaluationQuestion) -> FakeBusinessContextProvider:
    if question.business_api_behavior == "failure":
        return FakeBusinessContextProvider(
            error=AdapterError("BUSINESS_API_UNAVAILABLE", handoff_required=False)
        )
    if question.business_api_behavior == "success":
        return FakeBusinessContextProvider(
            result=BusinessContext(
                device_model=question.device_model or "E-200",
                fault_code=question.fault_code or "E01",
                firmware_version="3.1.4",
                inventory_available=(
                    question.expected_inventory_available
                    if question.expected_inventory_available is not None
                    else True
                ),
            )
        )
    return FakeBusinessContextProvider()


def _answer_for(
    evidence: list[EvidenceItem], business_context: BusinessContext | None
) -> dict[str, object]:
    if not evidence:
        return {"summary": "此题不应生成回答。"}
    source = evidence[0]
    text = source.text
    summary = text
    if business_context is not None and business_context.inventory_available is False:
        summary = f"{text} 业务接口已确认备件库存不足，建议转人工。"
    return {
        "summary": summary,
        "steps": [
            {
                "order": 1,
                "instruction": text,
                "reason": "固定题的排障步骤逐字复用当前已接受证据。",
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


def _business_behavior_from(result: object) -> str:
    if isinstance(result, QueryBusinessContextOutput):
        return "success" if result.business_context is not None else "not_called"
    return "failure" if isinstance(result, ControlledFailureOutput) else "not_called"


def _response_status(response: FinalResponse) -> str:
    if isinstance(response, ErrorResponse):
        return "error"
    return response.status


def _candidate_hit(target: CitationTarget | None, candidates: list[str]) -> bool | None:
    if target is None:
        return None
    return target.source_name in candidates[:5]


def _citation_matches_target(target: CitationTarget | None, response: FinalResponse) -> bool | None:
    if target is None:
        return None
    if not isinstance(response, CompletedResponse):
        return False
    return all(
        step.citation.source_name == target.source_name
        and step.citation.page == target.page
        and step.citation.section == target.section
        for step in response.answer.steps
    )


def _is_structured_response(response: FinalResponse) -> bool:
    try:
        response.__class__.model_validate(response.model_dump())
    except ValueError:
        return False
    return True


def _elapsed_ms(started: float) -> float:
    return round((perf_counter() - started) * 1000, 3)


def _score(items: list[dict[str, Any]], field: str) -> dict[str, object]:
    passed = sum(1 for item in items if item[field] is True)
    return {"passed": passed, "total": len(items), "rate": _rate(passed, len(items))}


def _rate(passed: int, total: int) -> float | None:
    return round(passed / total, 4) if total else None


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
