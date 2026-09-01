"""Validated HTTP adapter for the local or external business API. / 本地或外部业务接口的经过校验 HTTP 适配器。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol, overload
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from pydantic import ValidationError

from app.agent.adapters import AdapterError
from app.agent.state import BusinessContext, QueryBusinessContextInput
from app.business.schemas import MockDeviceResponse, MockFaultResponse, MockInventoryResponse
from app.config import Settings


class BusinessApiClientError(Exception):
    """Expected client-side failure with a stable code. / 带稳定错误码的预期客户端失败。"""

    def __init__(self, code: str, *, diagnostic: str = "") -> None:
        super().__init__(diagnostic)
        self.code = code
        self.diagnostic = diagnostic


@dataclass(frozen=True)
class BusinessHttpResponse:
    """Raw HTTP response retained only until schema validation. / 只保留到结构校验完成前的原始 HTTP 响应。"""

    status_code: int
    body: bytes


class BusinessHttpTransport(Protocol):
    """Replaceable read-only JSON transport. / 可替换的只读 JSON 传输层。"""

    def get_json(self, url: str, *, timeout_seconds: float) -> BusinessHttpResponse:
        """Read one JSON endpoint. / 读取一个 JSON 接口。"""

        ...


class UrlLibBusinessTransport:
    """Standard-library runtime transport. / 运行时使用的 Python 标准库传输实现。"""

    def get_json(self, url: str, *, timeout_seconds: float) -> BusinessHttpResponse:
        request = Request(url, headers={"Accept": "application/json"}, method="GET")
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                return BusinessHttpResponse(status_code=response.status, body=response.read())
        except HTTPError as error:
            return BusinessHttpResponse(status_code=error.code, body=error.read())
        except TimeoutError as error:
            raise BusinessApiClientError(
                "BUSINESS_API_TIMEOUT", diagnostic=type(error).__name__
            ) from error
        except URLError as error:
            reason = error.reason
            if isinstance(reason, TimeoutError):
                raise BusinessApiClientError(
                    "BUSINESS_API_TIMEOUT", diagnostic=type(reason).__name__
                ) from error
            raise BusinessApiClientError(
                "BUSINESS_API_UNAVAILABLE", diagnostic=type(reason).__name__
            ) from error


class BusinessApiClient:
    """Read device, fault, and inventory facts and validate every response. / 读取设备、故障和库存事实，并校验每一份响应。"""

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float,
        transport: BusinessHttpTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._transport = transport or UrlLibBusinessTransport()

    @classmethod
    def from_settings(
        cls, settings: Settings, *, transport: BusinessHttpTransport | None = None
    ) -> BusinessApiClient:
        """Build a client from validated application settings. / 用已校验的应用配置构造客户端。"""

        return cls(
            base_url=str(settings.business_api_base_url),
            timeout_seconds=settings.business_api_timeout_seconds,
            transport=transport,
        )

    def get_context(self, request: QueryBusinessContextInput) -> BusinessContext:
        """Return facts only when every endpoint and identity agrees. / 仅当每个接口和身份字段都一致时返回事实。"""

        model = quote(request.device_model, safe="")
        code = quote(request.fault_code, safe="")
        device = self._read(f"/mock/devices/{model}", MockDeviceResponse)
        fault = self._read(f"/mock/devices/{model}/faults/{code}", MockFaultResponse)
        inventory = self._read(f"/mock/inventory/{model}", MockInventoryResponse)
        if (
            device.model != request.device_model
            or fault.model != request.device_model
            or fault.code != request.fault_code
            or inventory.model != request.device_model
        ):
            raise BusinessApiClientError(
                "BUSINESS_API_INVALID_RESPONSE", diagnostic="identity mismatch"
            )
        return BusinessContext(
            device_model=device.model,
            fault_code=fault.code,
            firmware_version=device.firmware_version,
            inventory_available=inventory.spare_part_available,
        )

    @overload
    def _read(self, path: str, response_type: type[MockDeviceResponse]) -> MockDeviceResponse: ...

    @overload
    def _read(self, path: str, response_type: type[MockFaultResponse]) -> MockFaultResponse: ...

    @overload
    def _read(
        self, path: str, response_type: type[MockInventoryResponse]
    ) -> MockInventoryResponse: ...

    def _read(
        self,
        path: str,
        response_type: type[MockDeviceResponse]
        | type[MockFaultResponse]
        | type[MockInventoryResponse],
    ) -> MockDeviceResponse | MockFaultResponse | MockInventoryResponse:
        response = self._transport.get_json(
            f"{self._base_url}{path}", timeout_seconds=self._timeout_seconds
        )
        if response.status_code == 404:
            raise BusinessApiClientError("BUSINESS_API_NOT_FOUND", diagnostic="status=404")
        if response.status_code == 429:
            raise BusinessApiClientError("BUSINESS_API_RATE_LIMITED", diagnostic="status=429")
        if response.status_code >= 500:
            raise BusinessApiClientError(
                "BUSINESS_API_UNAVAILABLE", diagnostic=f"status={response.status_code}"
            )
        if response.status_code != 200:
            raise BusinessApiClientError(
                "BUSINESS_API_UNAVAILABLE", diagnostic=f"status={response.status_code}"
            )
        try:
            payload: Any = json.loads(response.body)
            return response_type.model_validate(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as error:
            raise BusinessApiClientError(
                "BUSINESS_API_INVALID_RESPONSE", diagnostic=type(error).__name__
            ) from error


class BusinessApiAdapter:
    """Adapt client errors to the workflow's safe optional-context boundary. / 把客户端错误适配到工作流的安全可选上下文边界。"""

    def __init__(self, client: BusinessApiClient) -> None:
        self._client = client

    def get_context(self, request: QueryBusinessContextInput, /) -> BusinessContext:
        try:
            return self._client.get_context(request)
        except BusinessApiClientError as error:
            raise AdapterError(
                error.code, handoff_required=False, diagnostic=error.diagnostic
            ) from error
