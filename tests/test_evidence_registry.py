"""C3-T04 tests for current-source evidence registration."""

from pathlib import Path
from uuid import UUID

import pytest

from app.agent.nodes import check_evidence
from app.agent.state import SupportState
from app.api.schemas import SupportAnswer
from app.rag.evidence import CitationBinder, EvidenceGate, EvidenceRegistry
from app.rag.ragflow_client import RAGFlowRetrievedChunk

MANIFEST = Path(__file__).resolve().parents[1] / "data" / "fixtures" / "MANIFEST.json"


def candidate(**updates: object) -> RAGFlowRetrievedChunk:
    payload: dict[str, object] = {
        "id": "chunk-e01",
        "document_id": "document-e200",
        "document_name": "e200-synthetic-maintenance-guide.md",
        "content": "Confirm that the main power switch is on.",
        "positions": [],
    }
    payload.update(updates)
    return RAGFlowRetrievedChunk.model_validate(payload)


def test_registry_maps_a_current_registered_candidate_to_evidence() -> None:
    evidence = EvidenceRegistry.from_manifest(MANIFEST).to_evidence(candidate())

    assert evidence.section == "Fault code E01"
    assert (
        evidence.content_hash == "fd11b362abeca1ceaf96e8622f65111f572b7bd2e06b4d6b93b4507b88a1c346"
    )
    assert evidence.parser == "text"


@pytest.mark.parametrize(
    "updates",
    [
        {"document_name": "unknown.md"},
        {"content": "not present in the current fixture"},
    ],
)
def test_registry_rejects_unregistered_or_noncurrent_candidates(updates: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        EvidenceRegistry.from_manifest(MANIFEST).to_evidence(candidate(**updates))


def test_registry_accepts_provider_whitespace_normalization() -> None:
    evidence = EvidenceRegistry.from_manifest(MANIFEST).to_evidence(
        candidate(content="Confirm that the main power\n\n switch is on.")
    )

    assert evidence.text == "Confirm that the main power\n\n switch is on."


def test_registry_still_rejects_substantive_text_changes() -> None:
    with pytest.raises(ValueError):
        EvidenceRegistry.from_manifest(MANIFEST).to_evidence(
            candidate(content="Confirm that the main power switch is off.")
        )


def test_registry_accepts_current_pdf_text_with_provider_extraction_noise() -> None:
    pdf_candidate = candidate(
        document_name="e200-synthetic-safety-notice.pdf",
        content=(
            "E-200 Synthetic SafetyNotice\n"
            "For retrieval fixture only. This is not a real maintenance instruction.\n"
            "Fault code Eo1: inspect the main power supply and fuse before escalation.\n"
            "If the equipment remains unavailable, stop and hand off to technical support."
        ),
    )

    evidence = EvidenceRegistry.from_manifest(MANIFEST).to_evidence(pdf_candidate)

    assert evidence.page == 1
    assert evidence.source_name == "e200-synthetic-safety-notice.pdf"


def test_evidence_gate_requires_a_current_unique_candidate() -> None:
    registry = EvidenceRegistry.from_manifest(MANIFEST)
    evidence = registry.to_evidence(candidate())
    gate = EvidenceGate(registry)

    assert gate.accepts([evidence])
    assert gate.accepts([evidence, evidence])
    assert not gate.accepts([])
    assert not gate.accepts([evidence.model_copy(update={"content_hash": "0" * 64})])


def test_evidence_gate_rejects_an_invalid_minimum() -> None:
    with pytest.raises(ValueError):
        EvidenceGate(EvidenceRegistry.from_manifest(MANIFEST), minimum_items=0)


def test_workflow_gate_refuses_stale_evidence() -> None:
    registry = EvidenceRegistry.from_manifest(MANIFEST)
    evidence = registry.to_evidence(candidate())
    state = SupportState(
        session_id=UUID("00000000-0000-4000-8000-000000000001"),
        user_message="E-200 E01",
        evidence=[evidence.model_copy(update={"content_hash": "0" * 64})],
        trace_id="trace-c3-evidence-gate",
    )

    output = check_evidence(state, EvidenceGate(registry))

    assert output.evidence_sufficient is False
    assert output.handoff_required is True


def test_citation_binder_requires_each_step_to_match_current_evidence() -> None:
    registry = EvidenceRegistry.from_manifest(MANIFEST)
    evidence = registry.to_evidence(candidate())
    binder = CitationBinder(EvidenceGate(registry))
    answer = SupportAnswer.model_validate(
        {
            "summary": "检查电源。",
            "confidence": "supported",
            "handoff_required": False,
            "steps": [
                {
                    "order": 1,
                    "instruction": "确认主电源开关已打开。",
                    "reason": "固定资料要求先检查电源。",
                    "citation": {"source_name": evidence.source_name, "section": evidence.section},
                }
            ],
        }
    )

    assert binder.supports(answer, [evidence])
    answer.steps[0].citation.section = "错误章节"
    assert not binder.supports(answer, [evidence])
