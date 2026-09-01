"""Small, validated RAGFlow retrieval client. / 小型且经过校验的 RAGFlow 检索客户端。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, SecretStr, ValidationError

from app.config import Settings


class RAGFlowClientError(Exception):
    """Expected RAGFlow boundary failure with a stable internal code. / 带稳定内部错误码的预期 RAGFlow 边界失败。"""

    def __init__(self, code: str, *, diagnostic: str = "") -> None:
        super().__init__(diagnostic)
        self.code = code


class RAGFlowSchema(BaseModel):
    """Reject undocumented provider fields. / 拒绝未记录的服务字段。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class RAGFlowRetrievalRequest(RAGFlowSchema):
    """Subset of the official `/api/v1/retrieval` request. / 官方检索接口所需的最小请求字段。"""

    dataset_id: str = Field(min_length=1, max_length=128)
    question: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=5, ge=1, le=20)

    def to_payload(self) -> dict[str, object]:
        return {
            "dataset_ids": [self.dataset_id],
            "question": self.question,
            "top_k": self.top_k,
        }


class RAGFlowRetrievedChunk(RAGFlowSchema):
    """Raw RAGFlow candidate, not yet verified application evidence. / 原始候选片段，尚不是应用已验证证据。"""

    id: str = Field(min_length=1, max_length=128)
    document_id: str = Field(min_length=1, max_length=128)
    document_name: str = Field(
        min_length=1,
        max_length=255,
        validation_alias=AliasChoices("document_name", "document_keyword"),
    )
    content: str = Field(min_length=1, max_length=4000)
    positions: list[list[float | int]] = Field(default_factory=list, max_length=100)
    # RAGFlow includes provider metadata beside the fields consumed by the app.
    # / RAGFlow 会在应用使用的字段旁附带服务元数据。
    content_ltks: object | None = None
    dataset_id: object | None = None
    doc_type_kwd: object | None = None
    image_id: object | None = None
    important_keywords: object | None = None
    mom_id: object | None = None
    row_id: object | None = None
    similarity: object | None = None
    tag_kwd: object | None = None
    term_similarity: object | None = None
    vector_similarity: object | None = None


class RAGFlowRetrievalData(RAGFlowSchema):
    """Successful provider retrieval payload. / 服务端成功检索载荷。"""

    chunks: list[RAGFlowRetrievedChunk] = Field(default_factory=list, max_length=20)
    doc_aggs: list[object] = Field(default_factory=list, max_length=100)
    total: int = Field(default=0, ge=0)


class RAGFlowEnvelope(RAGFlowSchema):
    """Envelope returned by the RAGFlow HTTP API. / RAGFlow HTTP 接口返回的外层结构。"""

    code: int
    message: str = ""
    data: RAGFlowRetrievalData | None = None


@dataclass(frozen=True)
class RAGFlowHttpResponse:
    """Transport response independent of a particular HTTP package. / 不依赖特定 HTTP 包的传输响应。"""

    status_code: int
    body: bytes


class RAGFlowHttpTransport(Protocol):
    """Replaceable JSON HTTP transport. / 可替换的 JSON HTTP 传输层。"""

    def post_json(
        self,
        url: str,
        *,
        headers: dict[str, str],
        payload: dict[str, object],
        timeout_seconds: float,
    ) -> RAGFlowHttpResponse:
        """Send one JSON POST request. / 发送一次 JSON POST 请求。"""

        ...


class UrlLibRAGFlowTransport:
    """Stdlib implementation used at runtime. / 运行时使用的 Python 标准库实现。"""

    def post_json(
        self,
        url: str,
        *,
        headers: dict[str, str],
        payload: dict[str, object],
        timeout_seconds: float,
    ) -> RAGFlowHttpResponse:
        request = Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={**headers, "Content-Type": "application/json"},
            method="POST",
        )
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


class RAGFlowClient:
    """Call the official RAGFlow retrieval endpoint. / 调用官方 RAGFlow 检索接口。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: SecretStr,
        dataset_id: str,
        timeout_seconds: float,
        top_k: int,
        transport: RAGFlowHttpTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._dataset_id = dataset_id
        self._timeout_seconds = timeout_seconds
        self._top_k = top_k
        self._transport = transport or UrlLibRAGFlowTransport()

    @classmethod
    def from_settings(
        cls, settings: Settings, *, transport: RAGFlowHttpTransport | None = None
    ) -> RAGFlowClient:
        """Construct only from enabled and complete RAGFlow settings. / 仅从已启用且完整的 RAGFlow 配置创建。"""
        if (
            not settings.ragflow_enabled
            or settings.ragflow_base_url is None
            or settings.ragflow_api_key is None
            or settings.ragflow_dataset_id is None
        ):
            raise ValueError("RAGFlow 未启用或配置不完整")
        return cls(
            base_url=str(settings.ragflow_base_url),
            api_key=settings.ragflow_api_key,
            dataset_id=settings.ragflow_dataset_id,
            timeout_seconds=settings.ragflow_timeout_seconds,
            top_k=settings.ragflow_top_k,
            transport=transport,
        )

    def retrieve(self, question: str) -> list[RAGFlowRetrievedChunk]:
        """Return validated raw candidates without inventing citations. / 返回已校验原始候选，不伪造引用。"""
        request = RAGFlowRetrievalRequest(
            dataset_id=self._dataset_id,
            question=question,
            top_k=self._top_k,
        )
        response = self._transport.post_json(
            f"{self._base_url}/api/v1/retrieval",
            headers={"Authorization": f"Bearer {self._api_key.get_secret_value()}"},
            payload=request.to_payload(),
            timeout_seconds=self._timeout_seconds,
        )
        if response.status_code != 200:
            raise RAGFlowClientError(
                "RAGFLOW_HTTP_ERROR", diagnostic=f"status={response.status_code}"
            )
        try:
            raw_envelope: Any = json.loads(response.body)
            envelope = RAGFlowEnvelope.model_validate(raw_envelope)
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as error:
            raise RAGFlowClientError(
                "RAGFLOW_INVALID_RESPONSE", diagnostic=type(error).__name__
            ) from error
        if envelope.code != 0:
            raise RAGFlowClientError("RAGFLOW_REQUEST_REJECTED", diagnostic=f"code={envelope.code}")
        if envelope.data is None:
            raise RAGFlowClientError("RAGFLOW_INVALID_RESPONSE", diagnostic="missing data")
        return list(envelope.data.chunks)
