"""C4 mock-business API and adapter tests. / C4 模拟业务接口与适配器测试。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.agent.adapters import AdapterError
from app.agent.fake_adapters import FakeSupportAnswerGenerator
from app.agent.nodes import generate_support_answer, query_business_context
from app.agent.state import (
    ControlledFailureOutput,
    EvidenceItem,
    GenerateSupportAnswerOutput,
    QueryBusinessContextInput,
    QueryBusinessContextOutput,
    SupportState,
)
from app.business.adapter import (
    BusinessApiAdapter,
    BusinessApiClient,
    BusinessApiClientError,
    BusinessHttpResponse,
    BusinessHttpTransport,
)
from app.business.mock_api import create_mock_business_app
from app.business.schemas import MockTicketResponse


class RecordingTransport(BusinessHttpTransport):
    """Deterministic transport that records every business request. / 记录每次业务请求的确定性传输替身。"""

    def __init__(self, responses: list[BusinessHttpResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, float]] = []

    def get_json(self, url: str, *, timeout_seconds: float) -> BusinessHttpResponse:
        self.calls.append((url, timeout_seconds))
        return self.responses.pop(0)


class TimeoutTransport(BusinessHttpTransport):
    """Transport that models a private timeout diagnostic. / 模拟带私有诊断信息的超时传输。"""

    def get_json(self, url: str, *, timeout_seconds: float) -> BusinessHttpResponse:
        del url, timeout_seconds
        raise BusinessApiClientError("BUSINESS_API_TIMEOUT", diagnostic="private timeout detail")


def response(payload: object, status_code: int = 200) -> BusinessHttpResponse:
    """Build one JSON response fixture. / 构造一份 JSON 响应样本。"""

    return BusinessHttpResponse(
        status_code=status_code,
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    )


def request() -> QueryBusinessContextInput:
    """Build the fixed known-equipment request. / 构造固定的已知设备请求。"""

    return QueryBusinessContextInput(device_model="E-200", fault_code="E01")


def successful_responses() -> list[BusinessHttpResponse]:
    """Return the three validated read responses used by a lookup. / 返回一次查询所需的三份已校验读取响应。"""

    return [
        response({"model": "E-200", "firmware_version": "3.1.4"}),
        response({"model": "E-200", "code": "E01", "known": True}),
        response({"model": "E-200", "spare_part_available": True}),
    ]


def make_state(**updates: object) -> SupportState:
    """Build an otherwise valid workflow state. / 构造其他字段均合法的工作流状态。"""

    payload: dict[str, object] = {
        "session_id": "8a7b1e5f-14e8-4bd8-a79b-b3a7f70fe93b",
        "user_message": "设备 E-200 报 E01",
        "device_model": "E-200",
        "fault_code": "E01",
        "trace_id": "trace-c4-business-001",
    }
    payload.update(updates)
    return SupportState.model_validate(payload)


def test_mock_service_exposes_normal_device_fault_inventory_and_ticket_contracts() -> None:
    """The local service is independently callable and validates its ticket body. / 本地服务可独立调用，并校验工单请求体。"""

    with TestClient(create_mock_business_app()) as client:
        device = client.get("/mock/devices/E-200")
        fault = client.get("/mock/devices/E-200/faults/E01")
        inventory = client.get("/mock/inventory/E-200")
        ticket = client.post(
            "/mock/tickets",
            json={"model": "E-200", "fault_code": "E01", "summary": "Synthetic handoff only"},
        )
        invalid_ticket = client.post("/mock/tickets", json={"model": "E-200"})

    assert device.json() == {"model": "E-200", "firmware_version": "3.1.4"}
    assert fault.json() == {"model": "E-200", "code": "E01", "known": True}
    assert inventory.json() == {"model": "E-200", "spare_part_available": True}
    assert ticket.status_code == 201
    assert MockTicketResponse.model_validate(ticket.json()).status == "created"
    assert invalid_ticket.status_code == 422


@pytest.mark.parametrize(
    ("failure_mode", "expected_status"),
    [
        ("not_found", 404),
        ("rate_limited", 429),
        ("service_error", 500),
        ("malformed", 200),
    ],
)
def test_mock_service_injects_documented_failures(failure_mode: str, expected_status: int) -> None:
    """Every immediate test failure is exposed deliberately. / 每种即时测试故障都只能被显式触发。"""

    with TestClient(create_mock_business_app()) as client:
        response_value = client.get(f"/mock/devices/E-200?failure_mode={failure_mode}")

    assert response_value.status_code == expected_status


def test_mock_service_injects_the_two_documented_delays() -> None:
    """Delay switches keep their observable lower bounds. / 延迟开关保持可观察的最小等待时间。"""

    with TestClient(create_mock_business_app()) as client:
        hundred_ms = client.get("/mock/devices/E-200?failure_mode=delay_100ms")
        one_second = client.get("/mock/devices/E-200?failure_mode=delay_1s")

    assert hundred_ms.status_code == 200
    assert one_second.status_code == 200


def test_mock_service_exposes_timeout_mode_for_http_client_testing() -> None:
    """The timeout switch deliberately delays longer than a short client timeout. / 超时开关故意延迟得比短客户端超时更久。"""

    with TestClient(create_mock_business_app()) as client:
        response_value = client.get("/mock/devices/E-200?failure_mode=timeout")

    assert response_value.status_code == 200


def test_client_reads_all_three_validated_facts_and_uses_encoded_paths() -> None:
    """Only three matching, validated responses become workflow facts. / 只有三份身份一致且校验通过的响应才能成为流程事实。"""

    transport = RecordingTransport(successful_responses())
    client = BusinessApiClient(
        base_url="https://business.example.test/", timeout_seconds=1.5, transport=transport
    )

    context = client.get_context(request())

    assert context.firmware_version == "3.1.4"
    assert context.inventory_available is True
    assert transport.calls == [
        ("https://business.example.test/mock/devices/E-200", 1.5),
        ("https://business.example.test/mock/devices/E-200/faults/E01", 1.5),
        ("https://business.example.test/mock/inventory/E-200", 1.5),
    ]


@pytest.mark.parametrize(
    ("response_value", "expected_code"),
    [
        (response({"detail": "missing"}, 404), "BUSINESS_API_NOT_FOUND"),
        (response({"detail": "limit"}, 429), "BUSINESS_API_RATE_LIMITED"),
        (response({"detail": "failure"}, 500), "BUSINESS_API_UNAVAILABLE"),
        (response({"model": 42, "firmware_version": None}), "BUSINESS_API_INVALID_RESPONSE"),
    ],
)
def test_client_maps_status_and_malformed_responses_to_stable_codes(
    response_value: BusinessHttpResponse, expected_code: str
) -> None:
    """HTTP failures and malformed data never become business facts. / HTTP 失败和畸形数据绝不成为业务事实。"""

    client = BusinessApiClient(
        base_url="https://business.example.test",
        timeout_seconds=1.0,
        transport=RecordingTransport([response_value]),
    )

    with pytest.raises(BusinessApiClientError) as exc_info:
        client.get_context(request())

    assert exc_info.value.code == expected_code


def test_timeout_is_hidden_and_node_degrades_without_context() -> None:
    """Timeout diagnostics stay private and a workflow may safely continue without facts. / 超时诊断保持私有，流程可安全地在无事实时继续。"""

    adapter = BusinessApiAdapter(
        BusinessApiClient(
            base_url="https://business.example.test",
            timeout_seconds=0.1,
            transport=TimeoutTransport(),
        )
    )

    with pytest.raises(AdapterError) as exc_info:
        adapter.get_context(request())
    result = query_business_context(make_state(), adapter)

    assert exc_info.value.error_code == "BUSINESS_API_TIMEOUT"
    assert "private timeout detail" not in str(exc_info.value)
    assert isinstance(result, ControlledFailureOutput)
    assert result.error_code == "BUSINESS_API_TIMEOUT"
    assert result.handoff_required is False


def test_validated_business_context_participates_in_answer_generation() -> None:
    """A normal business lookup reaches the answer generator as validated context. / 正常业务查询以已校验上下文进入回答生成器。"""

    transport = RecordingTransport(successful_responses())
    provider = BusinessApiAdapter(
        BusinessApiClient(
            base_url="https://business.example.test", timeout_seconds=1.0, transport=transport
        )
    )
    state = make_state(
        evidence=[
            EvidenceItem(
                document_id="e200-guide",
                source_name="E-200合成维修指南.md",
                section="E01",
                text="先检查电源。",
                content_hash="a" * 64,
            )
        ]
    )
    context_result = query_business_context(state, provider)
    assert isinstance(context_result, QueryBusinessContextOutput)
    state = SupportState.model_validate({**state.model_dump(), **context_result.model_dump()})
    generator = FakeSupportAnswerGenerator(
        result={
            "summary": "先检查电源，备件可用。",
            "steps": [
                {
                    "order": 1,
                    "instruction": "检查主电源。",
                    "reason": "E01 的资料要求先检查电源。",
                    "citation": {"source_name": "E-200合成维修指南.md", "section": "E01"},
                }
            ],
            "confidence": "supported",
            "handoff_required": False,
        }
    )

    answer_result = generate_support_answer(state, generator)

    assert isinstance(answer_result, GenerateSupportAnswerOutput)
    assert generator.requests[0].business_context is not None
    assert generator.requests[0].business_context.firmware_version == "3.1.4"
    assert generator.requests[0].business_context.inventory_available is True
