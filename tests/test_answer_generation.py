"""C2-T06 structured answer generation tests. / C2-T06 结构化回答生成测试。"""

import pytest
from pydantic import ValidationError

from app.agent.adapters import AdapterError
from app.agent.fake_adapters import FakeSupportAnswerGenerator
from app.agent.nodes import generate_support_answer
from app.agent.state import (
    ControlledFailureOutput,
    EvidenceItem,
    GenerateSupportAnswerOutput,
    SupportState,
)
from app.api.schemas import SupportAnswer

SESSION_ID = "8a7b1e5f-14e8-4bd8-a79b-b3a7f70fe93b"


def evidence() -> EvidenceItem:
    """Build one valid candidate. / 构造一条合法候选证据。"""

    return EvidenceItem(
        document_id="manual-e200-v2",
        source_name="E-200维修手册.pdf",
        page=12,
        section="故障码 E01",
        text="检查主电源和保险丝。",
        content_hash="a" * 64,
    )


def answer_payload() -> dict[str, object]:
    """Return one valid cited answer payload. / 返回一个合法的带引用回答数据。"""

    return {
        "summary": "建议先检查主电源连接和保险丝。",
        "steps": [
            {
                "order": 1,
                "instruction": "确认主电源开关处于开启状态。",
                "reason": "手册把电源检查列为第一步。",
                "citation": {
                    "source_name": "E-200维修手册.pdf",
                    "page": 12,
                    "section": "故障码 E01",
                },
            }
        ],
        "confidence": "mock",
        "handoff_required": False,
    }


def make_state(**updates: object) -> SupportState:
    """Build one valid generation state. / 构造一个合法的生成状态。"""

    payload: dict[str, object] = {
        "session_id": SESSION_ID,
        "user_message": "设备无法启动",
        "device_model": "E-200",
        "fault_code": "E01",
        "evidence": [evidence()],
        "trace_id": "trace-c2-generation-001",
    }
    payload.update(updates)
    return SupportState.model_validate(payload)


def test_generation_validates_output_and_keeps_state_unchanged() -> None:
    """Valid output is returned without mutating state. / 合法回答返回且不修改原状态。"""

    state = make_state()
    generator = FakeSupportAnswerGenerator(result=answer_payload())
    before = state.model_dump()

    result = generate_support_answer(state, generator)

    assert isinstance(result, GenerateSupportAnswerOutput)
    assert result.answer == SupportAnswer.model_validate(answer_payload())
    assert result.generation_retry_count == 0
    assert state.model_dump() == before
    assert len(generator.requests) == 1
    assert generator.requests[0].evidence[0].text == "检查主电源和保险丝。"


def test_generation_retries_once_after_invalid_output() -> None:
    """One invalid result gets exactly one retry. / 第一次非法结果只重试一次。"""

    generator = FakeSupportAnswerGenerator(results=[{"summary": "格式不完整"}, answer_payload()])

    result = generate_support_answer(make_state(), generator)

    assert isinstance(result, GenerateSupportAnswerOutput)
    assert result.generation_retry_count == 1
    assert len(generator.requests) == 2


def test_generation_stops_after_two_invalid_outputs() -> None:
    """Two invalid attempts produce a controlled handoff. / 两次非法尝试后返回受控转人工。"""

    generator = FakeSupportAnswerGenerator(
        results=[{"summary": "第一次不完整"}, {"summary": "第二次仍不完整"}]
    )

    result = generate_support_answer(make_state(), generator)

    assert isinstance(result, ControlledFailureOutput)
    assert result.error_code == "ANSWER_GENERATION_INVALID_RESPONSE"
    assert result.handoff_required is True
    assert len(generator.requests) == 2


def test_generation_with_existing_retry_count_does_not_retry_again() -> None:
    """A state already retried gets one final attempt only. / 已重试状态只能再做当前最后一次尝试。"""

    generator = FakeSupportAnswerGenerator(result={"summary": "仍然不完整"})

    result = generate_support_answer(make_state(generation_retry_count=1), generator)

    assert isinstance(result, ControlledFailureOutput)
    assert result.error_code == "ANSWER_GENERATION_INVALID_RESPONSE"
    assert len(generator.requests) == 1


class FailOnceThenSucceed:
    """Raise one expected adapter error, then return a valid answer. / 先抛一次约定错误再返回合法回答。"""

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, request: object, /) -> object:
        del request
        self.calls += 1
        if self.calls == 1:
            raise AdapterError(
                "LLM_TIMEOUT",
                handoff_required=True,
                diagnostic="private model detail",
            )
        return answer_payload()


def test_expected_generation_error_can_retry_without_leaking_diagnostic() -> None:
    """Expected generator errors may retry and expose no diagnostics. / 约定生成错误可重试且不泄露诊断。"""

    generator = FailOnceThenSucceed()
    result = generate_support_answer(make_state(), generator)

    assert isinstance(result, GenerateSupportAnswerOutput)
    assert result.generation_retry_count == 1
    assert generator.calls == 2


class CrashingGenerator:
    """Raise an unexpected programming error. / 抛出未预期的程序错误。"""

    def generate(self, request: object, /) -> object:
        del request
        raise RuntimeError("secret generator bug")


def test_unexpected_generation_errors_are_not_swallowed() -> None:
    """Unknown errors remain visible to the outer boundary. / 未知错误继续交给外层边界。"""

    with pytest.raises(RuntimeError, match="secret generator bug"):
        generate_support_answer(make_state(), CrashingGenerator())


def test_generation_requires_at_least_one_evidence_item_before_call() -> None:
    """No evidence blocks generation before adapter invocation. / 没有证据时在调用生成器前就停止。"""

    generator = FakeSupportAnswerGenerator(result=answer_payload())

    with pytest.raises(ValidationError):
        generate_support_answer(make_state(evidence=[]), generator)
    assert generator.requests == []
