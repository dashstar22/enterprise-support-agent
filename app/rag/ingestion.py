"""Fixed-fixture upload and parsing boundaries for RAGFlow. / RAGFlow 固定样本上传与解析边界。"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from app.rag.ragflow_client import RAGFlowClientError, RAGFlowHttpResponse


class RAGFlowIngestionSchema(BaseModel):
    """Strict provider models for the document lifecycle. / 文档生命周期的严格服务端模型。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class RAGFlowDocument(RAGFlowIngestionSchema):
    """Provider document and its current parsing status. / 服务端文档及其当前解析状态。"""

    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)

    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=255)
    run: str = "0"
    progress: float = Field(default=0.0, ge=0.0, le=1.0)
    progress_msg: str = ""


class RAGFlowDocumentList(RAGFlowIngestionSchema):
    """Document list payload returned by RAGFlow. / RAGFlow 返回的文档列表载荷。"""

    docs: list[RAGFlowDocument] = Field(default_factory=list, max_length=100)
    total: int = Field(default=0, ge=0)


class RAGFlowIngestionEnvelope(RAGFlowIngestionSchema):
    """Envelope shared by document upload and parsing calls. / 文档上传和解析调用共用的外层结构。"""

    code: int
    message: str = ""
    data: list[RAGFlowDocument] | RAGFlowDocumentList | None = None


@dataclass(frozen=True)
class UploadFile:
    """One verified local file sent as multipart data. / 作为 multipart 数据发送的一份已核验本地文件。"""

    name: str
    content_type: str
    content: bytes


class RAGFlowIngestionTransport(Protocol):
    """Replaceable transport for multipart, JSON, and query requests. / multipart、JSON 和查询请求的可替换传输层。"""

    def post_multipart(
        self,
        url: str,
        *,
        headers: dict[str, str],
        files: list[UploadFile],
        timeout_seconds: float,
    ) -> RAGFlowHttpResponse: ...

    def post_json(
        self,
        url: str,
        *,
        headers: dict[str, str],
        payload: dict[str, object],
        timeout_seconds: float,
    ) -> RAGFlowHttpResponse: ...

    def get_json(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> RAGFlowHttpResponse: ...


class UrlLibRAGFlowIngestionTransport:
    """Runtime transport implemented only with the Python standard library. / 只使用 Python 标准库的运行时传输实现。"""

    def post_multipart(
        self,
        url: str,
        *,
        headers: dict[str, str],
        files: list[UploadFile],
        timeout_seconds: float,
    ) -> RAGFlowHttpResponse:
        boundary = f"----enterprise-support-agent-{uuid.uuid4().hex}"
        body = bytearray()
        for file in files:
            body.extend(f"--{boundary}\r\n".encode())
            body.extend(
                f'Content-Disposition: form-data; name="file"; filename="{file.name}"\r\n'.encode()
            )
            body.extend(f"Content-Type: {file.content_type}\r\n\r\n".encode())
            body.extend(file.content)
            body.extend(b"\r\n")
        body.extend(f"--{boundary}--\r\n".encode())
        return self._request(
            url,
            method="POST",
            headers={**headers, "Content-Type": f"multipart/form-data; boundary={boundary}"},
            body=bytes(body),
            timeout_seconds=timeout_seconds,
        )

    def post_json(
        self,
        url: str,
        *,
        headers: dict[str, str],
        payload: dict[str, object],
        timeout_seconds: float,
    ) -> RAGFlowHttpResponse:
        return self._request(
            url,
            method="POST",
            headers={**headers, "Content-Type": "application/json"},
            body=json.dumps(payload).encode("utf-8"),
            timeout_seconds=timeout_seconds,
        )

    def get_json(
        self, url: str, *, headers: dict[str, str], timeout_seconds: float
    ) -> RAGFlowHttpResponse:
        return self._request(
            url, method="GET", headers=headers, body=None, timeout_seconds=timeout_seconds
        )

    def _request(
        self,
        url: str,
        *,
        method: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> RAGFlowHttpResponse:
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                return RAGFlowHttpResponse(status_code=response.status, body=response.read())
        except HTTPError as error:
            return RAGFlowHttpResponse(status_code=error.code, body=error.read())
        except TimeoutError as error:
            raise RAGFlowClientError("RAGFLOW_TIMEOUT", diagnostic=type(error).__name__) from error
        except URLError as error:
            raise RAGFlowClientError(
                "RAGFLOW_CONNECTION_ERROR", diagnostic=type(error.reason).__name__
            ) from error


class RAGFlowFixtureIngestionClient:
    """Upload only registered fixtures and observe their parsing lifecycle. / 仅上传已登记样本并观察其解析生命周期。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: SecretStr,
        dataset_id: str,
        timeout_seconds: float,
        transport: RAGFlowIngestionTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._dataset_id = dataset_id
        self._timeout_seconds = timeout_seconds
        self._transport = transport or UrlLibRAGFlowIngestionTransport()

    def upload_registered_fixtures(self, manifest_path: Path) -> list[RAGFlowDocument]:
        """Verify and upload every fixture registered in one manifest. / 核验并上传一个清单中的全部样本。"""
        files = _load_registered_files(manifest_path)
        response = self._transport.post_multipart(
            self._endpoint("documents"),
            headers=self._headers(),
            files=files,
            timeout_seconds=self._timeout_seconds,
        )
        envelope = self._parse_response(response)
        if not isinstance(envelope.data, list):
            raise RAGFlowClientError("RAGFLOW_INVALID_RESPONSE", diagnostic="upload data")
        return list(envelope.data)

    def start_parsing(self, document_ids: list[str]) -> None:
        """Request parsing after upload; acceptance is not completion. / 上传后请求解析，已接受不等于已完成。"""
        if not document_ids:
            raise ValueError("至少需要一个文档编号才能启动解析")
        envelope = self._parse_response(
            self._transport.post_json(
                self._endpoint("chunks"),
                headers=self._headers(),
                payload={"document_ids": document_ids},
                timeout_seconds=self._timeout_seconds,
            )
        )
        if envelope.data is not None:
            raise RAGFlowClientError("RAGFLOW_INVALID_RESPONSE", diagnostic="parse data")

    def get_document_status(self, document_id: str) -> RAGFlowDocument:
        """Return server-reported parsing status for one document. / 返回服务端报告的一份文档解析状态。"""
        envelope = self._parse_response(
            self._transport.get_json(
                f"{self._endpoint('documents')}?{urlencode({'id': document_id})}",
                headers=self._headers(),
                timeout_seconds=self._timeout_seconds,
            )
        )
        if not isinstance(envelope.data, RAGFlowDocumentList) or len(envelope.data.docs) != 1:
            raise RAGFlowClientError("RAGFLOW_INVALID_RESPONSE", diagnostic="document status")
        return envelope.data.docs[0]

    def _endpoint(self, suffix: str) -> str:
        return f"{self._base_url}/api/v1/datasets/{self._dataset_id}/{suffix}"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key.get_secret_value()}"}

    @staticmethod
    def _parse_response(response: RAGFlowHttpResponse) -> RAGFlowIngestionEnvelope:
        if response.status_code != 200:
            raise RAGFlowClientError(
                "RAGFLOW_HTTP_ERROR", diagnostic=f"status={response.status_code}"
            )
        try:
            raw_response: Any = json.loads(response.body)
            envelope = RAGFlowIngestionEnvelope.model_validate(raw_response)
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as error:
            raise RAGFlowClientError(
                "RAGFLOW_INVALID_RESPONSE", diagnostic=type(error).__name__
            ) from error
        if envelope.code != 0:
            raise RAGFlowClientError("RAGFLOW_REQUEST_REJECTED", diagnostic=f"code={envelope.code}")
        return envelope


def _load_registered_files(manifest_path: Path) -> list[UploadFile]:
    """Load only direct children listed by the fixture manifest. / 只加载样本清单列出的直接子文件。"""
    fixture_directory = manifest_path.parent.resolve()
    try:
        manifest: Any = json.loads(manifest_path.read_text(encoding="utf-8"))
        fixtures = manifest["fixtures"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("固定样本清单无效") from error
    if not isinstance(fixtures, list) or not fixtures:
        raise ValueError("固定样本清单没有资料")

    files: list[UploadFile] = []
    for fixture in fixtures:
        if not isinstance(fixture, dict):
            raise ValueError("固定样本清单条目无效")
        path_value = fixture.get("path")
        expected_hash = fixture.get("sha256")
        ingestible = fixture.get("ingestible", True)
        if not isinstance(path_value, str) or not isinstance(expected_hash, str):
            raise ValueError("固定样本清单缺少路径或内容哈希")
        if not isinstance(ingestible, bool):
            raise ValueError("固定样本导入标记无效")
        relative_path = Path(path_value)
        if relative_path.name != path_value:
            raise ValueError("固定样本路径不允许包含目录")
        source_path = (fixture_directory / relative_path).resolve()
        if source_path.parent != fixture_directory or not source_path.is_file():
            raise ValueError("固定样本文件不存在或不在允许目录")
        content = source_path.read_bytes()
        if hashlib.sha256(content).hexdigest() != expected_hash:
            raise ValueError("固定样本内容哈希不匹配")
        if not ingestible:
            continue
        if relative_path.suffix.lower() not in {".pdf", ".md", ".txt"}:
            raise ValueError("固定样本直接导入仅允许 PDF、Markdown 或 TXT")
        content_type = mimetypes.guess_type(source_path.name)[0] or "application/octet-stream"
        files.append(UploadFile(name=source_path.name, content_type=content_type, content=content))
    return files
