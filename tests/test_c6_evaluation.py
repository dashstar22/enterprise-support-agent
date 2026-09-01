"""C6 fixed evaluation, system-branch, and report tests. / C6 固定评测、系统分支和报告测试。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from app.evaluation.models import SemanticReviewSheet
from app.evaluation.runner import (
    C6EvaluationRunner,
    export_result,
    load_question_set,
    percentile,
)


def test_fixed_question_set_has_20_versioned_cases_and_required_categories() -> None:
    """C6 keeps the required scenarios in a versioned, validated input. / C6 将必须场景固定为带版本且可校验的输入。"""

    question_set = load_question_set()

    assert question_set.version == "C6-v1"
    assert len(question_set.questions) == 20
    assert {question.category for question in question_set.questions} == {
        "success",
        "clarification",
        "ocr",
        "cross_page",
        "model_confusion",
        "business_api",
        "no_evidence",
        "external_failure",
    }


def test_runner_repeats_fixed_workflow_branches_and_keeps_quality_layers_separate() -> None:
    """One local run covers normal, OCR, refusal, inventory, and external-error branches. / 一次本地运行覆盖正常、OCR、拒答、库存和外部错误分支。"""

    result = cast(dict[str, Any], C6EvaluationRunner(load_question_set()).run())
    metrics = cast(dict[str, Any], result["metrics"])

    assert result["evaluation_version"] == "C6-v1"
    assert result["runtime"]["answer_generator"] == "FakeSupportAnswerGenerator"
    assert result["runtime"]["retriever"] == "FixtureEvidenceRegistry"
    configuration = result["runtime"]["configuration"]
    assert len(configuration["question_set_sha256"]) == 64
    assert len(configuration["fixture_manifest_sha256"]) == 64
    assert len(configuration["ocr_fixture_sha256"]) == 64
    assert result["runtime"]["model"]["mode"] == "fixed_fake_generator"
    assert result["runtime"]["token_usage"] is None
    assert metrics["candidate_retrieval_hit_at_5"] == {
        "passed": 12,
        "total": 12,
        "rate": 1.0,
    }
    assert metrics["current_citation_accuracy"] == {"passed": 12, "total": 12, "rate": 1.0}
    assert metrics["no_evidence_refusal_rate"] == {"passed": 4, "total": 4, "rate": 1.0}
    assert metrics["clarification_accuracy"] == {"passed": 3, "total": 3, "rate": 1.0}
    assert metrics["structured_output_pass_rate"] == {"passed": 20, "total": 20, "rate": 1.0}
    assert metrics["business_api_integration_success_rate"] == {
        "passed": 2,
        "total": 2,
        "rate": 1.0,
    }
    assert metrics["business_inventory_context_accuracy"] == {
        "passed": 2,
        "total": 2,
        "rate": 1.0,
    }
    assert metrics["ocr_field_accuracy"] == {"passed": 3, "total": 3, "rate": 1.0}
    assert metrics["system_status_pass_rate"] == {"passed": 20, "total": 20, "rate": 1.0}
    assert metrics["semantic_answer_review"] == {
        "status": "pending_human_review",
        "approved": None,
        "total": 20,
        "rate": None,
    }

    questions = cast(list[dict[str, Any]], result["questions"])
    by_id = {item["question_id"]: item for item in questions}
    assert by_id["C6-Q17"]["business_inventory_actual"] is False
    assert by_id["C6-Q18"]["actual_status"] == "insufficient_evidence"
    assert by_id["C6-Q20"]["actual_status"] == "error"
    q01_evidence = by_id["C6-Q01"]["accepted_evidence"][0]
    assert q01_evidence["text"] == "Confirm that the main power switch is on."
    assert q01_evidence["content_hash"] == (
        "fd11b362abeca1ceaf96e8622f65111f572b7bd2e06b4d6b93b4507b88a1c346"
    )
    assert by_id["C6-Q12"]["accepted_evidence"][0]["text"].startswith("Fault code E01")
    latency = metrics["latency_ms"]
    assert {"model", "retrieval", "business_api", "ocr", "database"} <= set(latency)
    assert latency["model"]["count"] == 12
    assert latency["retrieval"]["count"] == 17
    assert latency["business_api"]["count"] == 2
    assert latency["ocr"]["count"] == 1
    assert latency["database"] == {
        "p50": None,
        "p95": None,
        "count": 0,
        "note": "固定评测不执行持久化; 数据库事务由 C6 集成测试单独验证。",
    }


def test_semantic_review_contract_rejects_nonhuman_or_incomplete_review() -> None:
    """Automatic citation checks cannot impersonate a human semantic decision. / 自动引用检查不能冒充人工语义结论。"""

    with pytest.raises(ValidationError):
        SemanticReviewSheet.model_validate(
            {
                "question_set_version": "C6-v1",
                "reviewer": "自动脚本",
                "reviewer_type": "automation",
                "reviews": [],
            }
        )


def test_all_approved_human_review_is_reported_separately_from_automatic_metrics() -> None:
    """A complete human review remains a distinct quality layer. / 完整人工复核仍是独立质量层。"""

    question_set = load_question_set()
    review_sheet = SemanticReviewSheet.model_validate(
        {
            "question_set_version": question_set.version,
            "reviewer": "测试人工复核人",
            "reviewer_type": "human",
            "reviews": [
                {
                    "question_id": question.id,
                    "decision": "approved",
                    "rationale": "人工核对完成。",
                }
                for question in question_set.questions
            ],
        }
    )

    result = cast(dict[str, Any], C6EvaluationRunner(question_set).run(review_sheet))
    metrics = cast(dict[str, Any], result["metrics"])

    assert metrics["current_citation_accuracy"]["rate"] == 1.0
    assert metrics["semantic_answer_review"] == {
        "status": "human_review_complete",
        "approved": 20,
        "total": 20,
        "rate": 1.0,
    }


def test_current_fixture_mismatch_fails_closed_before_any_answer_is_generated() -> None:
    """A changed expected snippet cannot impersonate current fixture evidence. / 篡改的预期片段不能伪装成当前夹具证据。"""

    question_set = load_question_set()
    changed_question = question_set.questions[0].model_copy(
        update={"expected_evidence_snippet": "This sentence is absent from the current fixture."}
    )
    changed_set = question_set.model_copy(
        update={"questions": [changed_question, *question_set.questions[1:]]}
    )

    with pytest.raises(ValueError, match="候选正文不在当前来源中"):
        C6EvaluationRunner(changed_set).run()


def test_result_export_and_nearest_rank_percentiles_are_inspectable(tmp_path: Path) -> None:
    """Exports remain JSON and percentile definition stays deterministic. / 导出保持 JSON 且分位数定义确定。"""

    output = tmp_path / "result.json"
    result = C6EvaluationRunner(load_question_set()).run()
    export_result(result, output)

    exported = json.loads(output.read_text(encoding="utf-8"))
    assert exported["questions"][0]["response"]["session_id"]
    assert exported["questions"][0]["accepted_evidence"][0]["text"]
    assert percentile([10.0, 20.0, 30.0, 40.0], 0.5) == 20.0
    assert percentile([10.0, 20.0, 30.0, 40.0], 0.95) == 40.0
    assert percentile([], 0.95) is None
