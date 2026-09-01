"""Local FastAPI mock of an external equipment business system. / 外部设备业务系统的本地 FastAPI 模拟服务。"""

from __future__ import annotations

from time import sleep
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Query, status
from fastapi.responses import JSONResponse

from app.business.schemas import (
    MockDeviceResponse,
    MockFaultResponse,
    MockInventoryResponse,
    MockTicketRequest,
    MockTicketResponse,
)

FailureMode = Literal[
    "normal",
    "not_found",
    "rate_limited",
    "service_error",
    "delay_100ms",
    "delay_1s",
    "timeout",
    "malformed",
]
FailureModeQuery = Annotated[FailureMode, Query()]

DEVICES: dict[str, MockDeviceResponse] = {
    "E-200": MockDeviceResponse(model="E-200", firmware_version="3.1.4"),
}
KNOWN_FAULTS = {("E-200", "E01")}
INVENTORY: dict[str, bool] = {"E-200": True}


def create_mock_business_app() -> FastAPI:
    """Build a separately runnable local mock service. / 创建可独立运行的本地模拟服务。"""

    application = FastAPI(title="Enterprise Support Mock Business API", version="0.1.0")

    @application.get("/mock/health", tags=["system"])
    def health_check() -> dict[str, str]:
        """Report only this local mock process. / 只报告本地模拟进程状态。"""

        return {"status": "ok", "service": "enterprise-support-mock-business-api"}

    @application.get("/mock/devices/{model}")
    def get_device(model: str, failure_mode: FailureModeQuery = "normal") -> JSONResponse:
        injected = _injected_response(failure_mode)
        if injected is not None:
            return injected
        device = DEVICES.get(model.upper())
        if device is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="device not found")
        return JSONResponse(device.model_dump(mode="json"))

    @application.get("/mock/devices/{model}/faults/{code}")
    def get_fault(model: str, code: str, failure_mode: FailureModeQuery = "normal") -> JSONResponse:
        injected = _injected_response(failure_mode)
        if injected is not None:
            return injected
        identity = (model.upper(), code.upper())
        if model.upper() not in DEVICES:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="device not found")
        response = MockFaultResponse(
            model=identity[0], code=identity[1], known=identity in KNOWN_FAULTS
        )
        return JSONResponse(response.model_dump(mode="json"))

    @application.get("/mock/inventory/{model}")
    def get_inventory(model: str, failure_mode: FailureModeQuery = "normal") -> JSONResponse:
        injected = _injected_response(failure_mode)
        if injected is not None:
            return injected
        normalized_model = model.upper()
        if normalized_model not in DEVICES:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="device not found")
        response = MockInventoryResponse(
            model=normalized_model,
            spare_part_available=INVENTORY[normalized_model],
        )
        return JSONResponse(response.model_dump(mode="json"))

    @application.post("/mock/tickets")
    def create_ticket(
        request: MockTicketRequest, failure_mode: FailureModeQuery = "normal"
    ) -> JSONResponse:
        del request
        injected = _injected_response(failure_mode)
        if injected is not None:
            return injected
        return JSONResponse(MockTicketResponse().model_dump(mode="json"), status_code=201)

    return application


def _injected_response(failure_mode: FailureMode) -> JSONResponse | None:
    """Apply explicit test-only failures before normal business logic. / 在正常业务逻辑前注入显式的仅测试故障。"""

    if failure_mode == "normal":
        return None
    if failure_mode == "not_found":
        return JSONResponse({"detail": "injected not found"}, status_code=404)
    if failure_mode == "rate_limited":
        return JSONResponse({"detail": "injected rate limit"}, status_code=429)
    if failure_mode == "service_error":
        return JSONResponse({"detail": "injected service failure"}, status_code=500)
    if failure_mode == "delay_100ms":
        sleep(0.1)
        return None
    if failure_mode == "delay_1s":
        sleep(1.0)
        return None
    if failure_mode == "timeout":
        sleep(2.0)
        return None
    return JSONResponse({"model": 42, "firmware_version": None})


app = create_mock_business_app()
