"""C3-T08 local RAGFlow-to-citation workflow integration tests. / C3-T08 本地 RAGFlow 到引用工作流集成测试。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from uuid import UUID

import pytest
from pydantic import BaseModel, SecretStr

from app.agent.fake_adapters import FakeSupportAnswerGenerator
from app.agent.nodes import check_evidence, finish, generate_support_answer, retrieve_evidence
from app.agent.state import ControlledFailureOutput, SupportState
from app.api.schemas import CompletedResponse, ErrorResponse, InsufficientEvidenceResponse
from app.rag.adapters import RAGFlowRetrieverAdapter
from app.rag.evidence import CitationBinder, EvidenceGate, EvidenceRegistry
from app.rag.ragflow_client import RAGFlowClient

MANIFEST = Path(__file__).resolve().parents[1] / "data" / "fixtures" / "MANIFEST.json"
SESSION_ID = UUID("00000000-0000-4000-8000-000000000008")


@contextmanager
def local_ragflow_server(
    *, status_code: int, payload: dict[str, object]
) -> Iterator[tuple[str, list[dict[str, object]]]]:
    """Serve one inspectable local RAGFlow response. / 提供一条可检查的本地 RAGFlow 响应。"""

    response_body = json.dumps(payload).encode("utf-8")
    calls: list[dict[str, object]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            content_length = int(self.headers["Content-Length"])
            calls.append(
                {
                    "path": self.path,
                    "authorization": self.headers["Authorization"],
                    "payload": json.loads(self.rfile.read(content_length)),
                }
            )
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", calls
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def success_payload() -> dict[str, object]:
    """Return one registered Markdown candidate. / 返回一条已登记的 Markdown 候选。"""

    return {
        "code": 0,
        "data": {
            "chunks": [
                {
                    "id": "chunk-e01",
                    "document_id": "document-e200",
                    "document_name": "e200-synthetic-maintenance-guide.md",
                    "content": "Confirm that the main power switch is on.",
                    "positions": [],
                }
            ]
        },
    }


def make_state(**updates: object) -> SupportState:
    """Build a complete retrieval state. / 构造字段齐全的检索状态。"""

    payload: dict[str, object] = {
        "session_id": SESSION_ID,
        "user_message": "设备 E-200 报 E01，无法启动。",
        "device_model": "E-200",
        "fault_code": "E01",
        "retrieval_query": "E-200 E01 无法启动",
        "trace_id": "trace-c3-ragflow-integration",
    }
    payload.update(updates)
    return SupportState.model_validate(payload)


def apply_update(state: SupportState, update: BaseModel) -> SupportState:
    """Merge a validated node result into a fresh state. / 将已校验节点结果合并为新状态。"""

    return SupportState.model_validate({**state.model_dump(), **update.model_dump()})


def adapter(base_url: str) -> tuple[RAGFlowRetrieverAdapter, EvidenceGate, CitationBinder]:
    """Build the real local transport and application evidence boundary. / 创建真实本地传输和应用证据边界。"""

    registry = EvidenceRegistry.from_manifest(MANIFEST)
    client = RAGFlowClient(
        base_url=base_url,
        api_key=SecretStr("local-test-key"),
        dataset_id="dataset-e200",
        timeout_seconds=1.0,
        top_k=3,
    )
    gate = EvidenceGate(registry)
    return RAGFlowRetrieverAdapter(client, registry), gate, CitationBinder(gate)


def supported_answer(section: str = "Fault code E01") -> dict[str, object]:
    """Return one answer whose citation can be made valid or invalid. / 返回引用可设为正确或错误的回答。"""

    return {
        "summary": "先检查主电源开关。",
        "steps": [
            {
                "order": 1,
                "instruction": "确认主电源开关已打开。",
                "reason": "固定资料将电源检查列为第一步。",
                "citation": {
                    "source_name": "e200-synthetic-maintenance-guide.md",
                    "section": section,
                },
            }
        ],
        "confidence": "supported",
        "handoff_required": False,
    }


def test_local_ragflow_candidate_reaches_a_cited_completed_response() -> None:
    """The full local chain accepts current evidence and a bound citation. / 完整本地链路接受当前证据和已绑定引用。"""

    with local_ragflow_server(status_code=200, payload=success_payload()) as (base_url, calls):
        retriever, gate, binder = adapter(base_url)
        state = apply_update(make_state(), retrieve_evidence(make_state(), retriever))
        state = apply_update(state, check_evidence(state, gate))
        generator = FakeSupportAnswerGenerator(result=supported_answer())
        state = apply_update(state, generate_support_answer(state, generator, binder))
        response = finish(state).response

    assert calls == [
        {
            "path": "/api/v1/retrieval",
            "authorization": "Bearer local-test-key",
            "payload": {
                "dataset_ids": ["dataset-e200"],
                "question": "E-200 E01 无法启动",
                "top_k": 3,
            },
        }
    ]
    assert isinstance(response, CompletedResponse)
    assert response.answer.confidence == "supported"
    assert response.answer.steps[0].citation.section == "Fault code E01"
    assert len(generator.requests) == 1


def test_local_ragflow_empty_candidates_refuse_without_answer_generation() -> None:
    """An empty provider result means insufficient evidence, not success. / 服务空结果表示证据不足，不是成功。"""

    with local_ragflow_server(status_code=200, payload={"code": 0, "data": {"chunks": []}}) as (
        base_url,
        _,
    ):
        retriever, gate, _ = adapter(base_url)
        state = apply_update(make_state(), retrieve_evidence(make_state(), retriever))
        state = apply_update(state, check_evidence(state, gate))
        generator = FakeSupportAnswerGenerator(result=supported_answer())
        response = finish(state).response

    assert isinstance(response, InsufficientEvidenceResponse)
    assert response.handoff_required is True
    assert generator.requests == []


@pytest.mark.parametrize(
    ("status_code", "payload", "error_code", "retryable"),
    [
        (503, {"code": 500, "message": "private provider detail"}, "RAGFLOW_UNAVAILABLE", True),
        (200, {"code": 102, "message": "private provider detail"}, "RAGFLOW_REJECTED", False),
    ],
)
def test_local_ragflow_failures_become_safe_error_responses(
    status_code: int, payload: dict[str, object], error_code: str, retryable: bool
) -> None:
    """Provider failures use stable public codes without provider details. / 服务失败使用稳定公开码且不泄露服务细节。"""

    with local_ragflow_server(status_code=status_code, payload=payload) as (base_url, _):
        retriever, _, _ = adapter(base_url)
        result = retrieve_evidence(make_state(), retriever)
        assert isinstance(result, ControlledFailureOutput)
        response = finish(apply_update(make_state(), result)).response

    assert isinstance(response, ErrorResponse)
    assert response.error.code == error_code
    assert response.error.retryable is retryable
    assert "private provider detail" not in response.model_dump_json()


def test_local_ragflow_rejects_a_candidate_not_in_current_registered_source() -> None:
    """Unregistered candidate text cannot reach answer generation. / 未登记候选正文不能进入回答生成。"""

    payload = success_payload()
    payload["data"] = {
        "chunks": [
            {
                "id": "chunk-injected",
                "document_id": "document-e200",
                "document_name": "e200-synthetic-maintenance-guide.md",
                "content": "private injected text",
                "positions": [],
            }
        ]
    }
    with local_ragflow_server(status_code=200, payload=payload) as (base_url, _):
        retriever, _, _ = adapter(base_url)
        result = retrieve_evidence(make_state(), retriever)
        assert isinstance(result, ControlledFailureOutput)
        response = finish(apply_update(make_state(), result)).response

    assert isinstance(response, ErrorResponse)
    assert response.error.code == "RAGFLOW_INVALID_RESPONSE"
    assert "private injected text" not in response.model_dump_json()


def test_citation_mismatch_retries_once_then_prevents_completion() -> None:
    """A wrong citation gets one retry and cannot produce a completion. / 错误引用只重试一次且不能生成成功结果。"""

    with local_ragflow_server(status_code=200, payload=success_payload()) as (base_url, _):
        retriever, gate, binder = adapter(base_url)
        state = apply_update(make_state(), retrieve_evidence(make_state(), retriever))
        state = apply_update(state, check_evidence(state, gate))
        generator = FakeSupportAnswerGenerator(
            results=[supported_answer("Wrong section"), supported_answer("Wrong section")]
        )
        result = generate_support_answer(state, generator, binder)
        assert isinstance(result, ControlledFailureOutput)
        response = finish(apply_update(state, result)).response

    assert isinstance(response, ErrorResponse)
    assert response.error.code == "ANSWER_GENERATION_INVALID_RESPONSE"
    assert len(generator.requests) == 2
