"""Real LLM adapter contracts. / 真实大模型适配器契约测试。"""

import json

import pytest
from pydantic import SecretStr

from app.agent.adapters import AdapterError
from app.agent.llm import OpenAICompatibleSupportAnswerGenerator
from app.agent.state import EvidenceItem, GenerateSupportAnswerInput


class FakeResponse:
    status = 200

    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


def request_input() -> GenerateSupportAnswerInput:
    return GenerateSupportAnswerInput(
        evidence=[
            EvidenceItem(
                document_id="manual-e200-v2",
                source_name="E-200维修手册.pdf",
                page=12,
                section="故障码 E01",
                text="检查主电源和保险丝。",
                content_hash="a" * 64,
            )
        ]
    )


def answer() -> dict[str, object]:
    return {
        "summary": "建议检查主电源和保险丝。",
        "steps": [
            {
                "order": 1,
                "instruction": "检查主电源和保险丝。",
                "reason": "资料将其列为排查步骤。",
                "citation": {
                    "source_name": "E-200维修手册.pdf",
                    "page": 12,
                    "section": "故障码 E01",
                },
            }
        ],
        "confidence": "supported",
        "handoff_required": False,
    }


def generator() -> OpenAICompatibleSupportAnswerGenerator:
    return OpenAICompatibleSupportAnswerGenerator(
        base_url="https://example.test/v1",
        api_key=SecretStr("secret-key"),
        model="demo-model",
    )


def test_generator_posts_grounded_json_and_records_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request: object, *, timeout: float) -> FakeResponse:
        captured["url"] = request.full_url  # type: ignore[attr-defined]
        captured["body"] = json.loads(request.data.decode())  # type: ignore[attr-defined]
        captured["auth"] = request.get_header("Authorization")  # type: ignore[attr-defined]
        captured["timeout"] = timeout
        return FakeResponse(
            {
                "choices": [{"message": {"content": json.dumps(answer(), ensure_ascii=False)}}],
                "usage": {"prompt_tokens": 120, "completion_tokens": 45},
            }
        )

    monkeypatch.setattr("app.agent.llm.urlopen", fake_urlopen)
    model = generator()

    result = model.generate(request_input())

    assert result == answer()
    assert captured["url"] == "https://example.test/v1/chat/completions"
    assert captured["auth"] == "Bearer secret-key"
    assert captured["timeout"] == 30.0
    assert captured["body"]["model"] == "demo-model"  # type: ignore[index]
    assert model.last_usage is not None
    assert model.last_usage.input_tokens == 120
    assert model.last_usage.output_tokens == 45


def test_generator_maps_timeout_to_stable_adapter_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request: object, *, timeout: float) -> None:
        del request, timeout
        raise TimeoutError("private timeout")

    monkeypatch.setattr("app.agent.llm.urlopen", fake_urlopen)

    with pytest.raises(AdapterError) as exc_info:
        generator().generate(request_input())

    assert exc_info.value.error_code == "LLM_TIMEOUT"
    assert "private timeout" not in str(exc_info.value)


def test_generator_rejects_non_json_model_content(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.agent.llm.urlopen",
        lambda request, *, timeout: FakeResponse(
            {"choices": [{"message": {"content": "not-json"}}]}
        ),
    )

    with pytest.raises(AdapterError) as exc_info:
        generator().generate(request_input())

    assert exc_info.value.error_code == "LLM_INVALID_RESPONSE"
