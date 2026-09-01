"""Contract tests for the C3-T02 RAGFlow retrieval boundary."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest
from pydantic import SecretStr

from app.agent.adapters import AdapterError
from app.agent.state import RetrieveEvidenceInput
from app.config import Settings
from app.rag.adapters import RAGFlowRetrieverAdapter
from app.rag.evidence import EvidenceRegistry
from app.rag.ragflow_client import (
    RAGFlowClient,
    RAGFlowClientError,
    RAGFlowHttpResponse,
    RAGFlowHttpTransport,
    RAGFlowRetrievedChunk,
)

FIXTURE_MANIFEST = Path(__file__).resolve().parents[1] / "data" / "fixtures" / "MANIFEST.json"


@contextmanager
def local_ragflow_server(response_body: bytes) -> Iterator[tuple[str, list[dict[str, object]]]]:
    """Run one local HTTP endpoint for stdlib transport verification. / 启动一个本地 HTTP 端点验证标准库传输。"""
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
            self.send_response(200)
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
        port = server.server_address[1]
        yield f"http://127.0.0.1:{port}", calls
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


class RecordingTransport(RAGFlowHttpTransport):
    """Deterministic transport that never opens a network connection. / 永不访问网络的确定性传输替身。"""

    def __init__(self, response: RAGFlowHttpResponse) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    def post_json(
        self,
        url: str,
        *,
        headers: dict[str, str],
        payload: dict[str, object],
        timeout_seconds: float,
    ) -> RAGFlowHttpResponse:
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "payload": dict(payload),
                "timeout_seconds": timeout_seconds,
            }
        )
        return self.response


class TimeoutTransport(RAGFlowHttpTransport):
    """Controlled timeout transport. / 可控超时传输替身。"""

    def post_json(
        self,
        url: str,
        *,
        headers: dict[str, str],
        payload: dict[str, object],
        timeout_seconds: float,
    ) -> RAGFlowHttpResponse:
        del url, headers, payload, timeout_seconds
        raise RAGFlowClientError("RAGFLOW_TIMEOUT", diagnostic="private timeout diagnostic")


def response(payload: dict[str, object], status_code: int = 200) -> RAGFlowHttpResponse:
    return RAGFlowHttpResponse(
        status_code=status_code,
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    )


def make_client(transport: RAGFlowHttpTransport) -> RAGFlowClient:
    return RAGFlowClient(
        base_url="https://ragflow.example.test/",
        api_key=SecretStr("secret-value"),
        dataset_id="dataset-e200",
        timeout_seconds=7.5,
        top_k=3,
        transport=transport,
    )


def success_payload() -> dict[str, object]:
    return {
        "code": 0,
        "data": {
            "chunks": [
                {
                    "id": "chunk-e01",
                    "document_id": "document-e200",
                    "document_name": "e200-synthetic-maintenance-guide.md",
                    "content": "Confirm the main power switch is on.",
                    "positions": [[1, 2, 3, 4]],
                }
            ]
        },
    }


def test_client_uses_official_endpoint_bearer_auth_and_constrained_payload() -> None:
    transport = RecordingTransport(response(success_payload()))

    chunks = make_client(transport).retrieve("E-200 E01")

    assert chunks[0].document_name == "e200-synthetic-maintenance-guide.md"
    assert transport.calls == [
        {
            "url": "https://ragflow.example.test/api/v1/retrieval",
            "headers": {"Authorization": "Bearer secret-value"},
            "payload": {"dataset_ids": ["dataset-e200"], "question": "E-200 E01", "top_k": 3},
            "timeout_seconds": 7.5,
        }
    ]


def test_client_accepts_current_ragflow_chunk_metadata_and_document_keyword() -> None:
    payload = success_payload()
    chunk = payload["data"]["chunks"][0]  # type: ignore[index]
    assert isinstance(chunk, dict)
    chunk.pop("document_name", None)
    chunk.update(
        {
            "document_keyword": "e200-synthetic-maintenance-guide.md",
            "content_ltks": "e 200 synthet mainten guid",
            "dataset_id": "dataset-e200",
            "doc_type_kwd": "md",
            "image_id": None,
            "important_keywords": [],
            "mom_id": None,
            "row_id": "row-e01",
            "similarity": 0.31,
            "tag_kwd": [],
            "term_similarity": 0.09,
            "vector_similarity": 0.84,
        }
    )
    payload["data"]["doc_aggs"] = []  # type: ignore[index]
    payload["data"]["total"] = 1  # type: ignore[index]

    chunks = make_client(RecordingTransport(response(payload))).retrieve("E-200 E01")

    assert chunks[0].document_name == "e200-synthetic-maintenance-guide.md"
    assert chunks[0].content == "Confirm the main power switch is on."


def test_stdlib_transport_sends_the_same_request_to_a_local_http_endpoint() -> None:
    with local_ragflow_server(json.dumps(success_payload()).encode("utf-8")) as (base_url, calls):
        client = RAGFlowClient(
            base_url=base_url,
            api_key=SecretStr("local-secret"),
            dataset_id="dataset-e200",
            timeout_seconds=1.0,
            top_k=2,
        )

        chunks = client.retrieve("E-200 E01")

    assert chunks[0].id == "chunk-e01"
    assert calls == [
        {
            "path": "/api/v1/retrieval",
            "authorization": "Bearer local-secret",
            "payload": {"dataset_ids": ["dataset-e200"], "question": "E-200 E01", "top_k": 2},
        }
    ]


@pytest.mark.parametrize(
    ("transport", "expected_code"),
    [
        (TimeoutTransport(), "RAGFLOW_TIMEOUT"),
        (
            RecordingTransport(response({"code": 0, "data": {"chunks": []}}, status_code=503)),
            "RAGFLOW_HTTP_ERROR",
        ),
        (
            RecordingTransport(response({"code": 102, "message": "denied"})),
            "RAGFLOW_REQUEST_REJECTED",
        ),
        (
            RecordingTransport(RAGFlowHttpResponse(status_code=200, body=b"not-json")),
            "RAGFLOW_INVALID_RESPONSE",
        ),
    ],
)
def test_client_maps_expected_failure_classes_to_stable_codes(
    transport: RAGFlowHttpTransport, expected_code: str
) -> None:
    with pytest.raises(RAGFlowClientError) as exc_info:
        make_client(transport).retrieve("E-200 E01")

    assert exc_info.value.code == expected_code
    assert "secret-value" not in str(exc_info.value)


def test_adapter_forwards_only_a_validated_workflow_query() -> None:
    transport = RecordingTransport(response(success_payload()))
    adapter = RAGFlowRetrieverAdapter(
        make_client(transport), EvidenceRegistry.from_manifest(FIXTURE_MANIFEST)
    )

    candidates = adapter.retrieve_candidates(RetrieveEvidenceInput(retrieval_query="E-200 E01"))

    assert candidates[0].document_id == "document-e200"
    assert candidates[0].positions == [[1, 2, 3, 4]]


@pytest.mark.parametrize(
    ("client_code", "public_code"),
    [
        ("RAGFLOW_TIMEOUT", "RAGFLOW_TIMEOUT"),
        ("RAGFLOW_HTTP_ERROR", "RAGFLOW_UNAVAILABLE"),
        ("RAGFLOW_REQUEST_REJECTED", "RAGFLOW_REJECTED"),
        ("RAGFLOW_INVALID_RESPONSE", "RAGFLOW_INVALID_RESPONSE"),
    ],
)
def test_adapter_hides_provider_diagnostics_behind_stable_error_codes(
    client_code: str, public_code: str
) -> None:
    class FailingClient:
        def retrieve(self, question: str) -> list[RAGFlowRetrievedChunk]:
            del question
            raise RAGFlowClientError(client_code, diagnostic="private provider detail")

    adapter = RAGFlowRetrieverAdapter(
        FailingClient(), EvidenceRegistry.from_manifest(FIXTURE_MANIFEST)
    )

    with pytest.raises(AdapterError) as exc_info:
        adapter.retrieve_candidates(RetrieveEvidenceInput(retrieval_query="E-200 E01"))

    assert exc_info.value.error_code == public_code
    assert "private provider detail" not in str(exc_info.value)


def test_enabled_settings_require_dataset_id_without_leaking_api_key() -> None:
    secret = "must-not-appear-in-validation-error"

    with pytest.raises(ValueError) as exc_info:
        Settings(
            _env_file=None,
            ragflow_enabled=True,
            ragflow_base_url="https://ragflow.example.test",
            ragflow_api_key=secret,
        )

    assert "ESA_RAGFLOW_DATASET_ID" in str(exc_info.value)
    assert secret not in str(exc_info.value)
