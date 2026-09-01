"""Contract tests for C3-T03 fixture ingestion."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from app.rag.ingestion import (
    RAGFlowFixtureIngestionClient,
    RAGFlowIngestionTransport,
    UploadFile,
)
from app.rag.ragflow_client import RAGFlowClientError, RAGFlowHttpResponse

FIXTURE_MANIFEST = Path(__file__).resolve().parents[1] / "data" / "fixtures" / "MANIFEST.json"


class RecordingIngestionTransport(RAGFlowIngestionTransport):
    """No-network transport with a separate response for each lifecycle call. / 每个生命周期调用使用独立响应的无网络传输替身。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.upload_response = response({"code": 0, "data": documents()})
        self.parse_response = response({"code": 0, "data": None})
        self.status_response = response({"code": 0, "data": {"docs": [documents()[0]]}})

    def post_multipart(
        self, url: str, *, headers: dict[str, str], files: list[UploadFile], timeout_seconds: float
    ) -> RAGFlowHttpResponse:
        self.calls.append(
            {
                "kind": "upload",
                "url": url,
                "headers": headers,
                "files": files,
                "timeout": timeout_seconds,
            }
        )
        return self.upload_response

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
                "kind": "parse",
                "url": url,
                "headers": headers,
                "payload": payload,
                "timeout": timeout_seconds,
            }
        )
        return self.parse_response

    def get_json(
        self, url: str, *, headers: dict[str, str], timeout_seconds: float
    ) -> RAGFlowHttpResponse:
        self.calls.append(
            {"kind": "status", "url": url, "headers": headers, "timeout": timeout_seconds}
        )
        return self.status_response


def response(payload: dict[str, object]) -> RAGFlowHttpResponse:
    return RAGFlowHttpResponse(status_code=200, body=json.dumps(payload).encode())


def documents() -> list[dict[str, object]]:
    return [
        {"id": "doc-pdf", "name": "e200-synthetic-safety-notice.pdf", "run": "0", "progress": 0.0},
        {
            "id": "doc-markdown",
            "name": "e200-synthetic-maintenance-guide.md",
            "run": "1",
            "progress": 0.5,
        },
        {
            "id": "doc-text",
            "name": "e200-synthetic-fault-codes.txt",
            "run": "DONE",
            "progress": 1.0,
        },
    ]


def client(transport: RAGFlowIngestionTransport) -> RAGFlowFixtureIngestionClient:
    return RAGFlowFixtureIngestionClient(
        base_url="https://ragflow.example.test",
        api_key=SecretStr("secret-value"),
        dataset_id="dataset-e200",
        timeout_seconds=7.5,
        transport=transport,
    )


def test_upload_parse_and_status_use_separate_official_lifecycle_calls() -> None:
    transport = RecordingIngestionTransport()
    ingestion = client(transport)

    uploaded = ingestion.upload_registered_fixtures(FIXTURE_MANIFEST)
    ingestion.start_parsing([document.id for document in uploaded])
    status = ingestion.get_document_status("doc-pdf")

    upload_call, parse_call, status_call = transport.calls
    uploaded_files = upload_call["files"]
    upload_headers = upload_call["headers"]
    parse_payload = parse_call["payload"]
    status_url = status_call["url"]
    assert isinstance(uploaded_files, list)
    assert all(isinstance(file, UploadFile) for file in uploaded_files)
    assert isinstance(upload_headers, dict)
    assert isinstance(parse_payload, dict)
    assert isinstance(status_url, str)
    assert [file.name for file in uploaded_files] == [document["name"] for document in documents()]
    assert upload_headers == {"Authorization": "Bearer secret-value"}
    assert parse_payload == {"document_ids": ["doc-pdf", "doc-markdown", "doc-text"]}
    assert status_url.endswith("/documents?id=doc-pdf")
    assert status.run == "0"
    assert status.progress == 0.0


@pytest.mark.parametrize("path", [Path("../outside.txt"), Path("missing.txt")])
def test_upload_rejects_unregistered_or_missing_fixture_paths(tmp_path: Path, path: Path) -> None:
    manifest = tmp_path / "MANIFEST.json"
    manifest.write_text(
        json.dumps({"fixtures": [{"path": str(path), "sha256": "0" * 64}]}), encoding="utf-8"
    )

    with pytest.raises(ValueError):
        client(RecordingIngestionTransport()).upload_registered_fixtures(manifest)


@pytest.mark.parametrize(
    "response_payload", [{"code": 102, "message": "rejected"}, {"code": 0, "data": {}}]
)
def test_upload_does_not_accept_provider_rejection_or_malformed_response(
    response_payload: dict[str, object],
) -> None:
    transport = RecordingIngestionTransport()
    transport.upload_response = response(response_payload)

    with pytest.raises(RAGFlowClientError):
        client(transport).upload_registered_fixtures(FIXTURE_MANIFEST)


def test_document_status_accepts_current_provider_metadata_and_total() -> None:
    transport = RecordingIngestionTransport()
    transport.status_response = response(
        {
            "code": 0,
            "data": {
                "total": 1,
                "docs": [
                    {
                        **documents()[0],
                        "location": documents()[0]["name"],
                        "chunk_count": 1,
                        "status": "1",
                        "parser_config": {},
                    }
                ],
            },
        }
    )

    status = client(transport).get_document_status("doc-pdf")

    assert status.name == "e200-synthetic-safety-notice.pdf"
    assert status.run == "0"
