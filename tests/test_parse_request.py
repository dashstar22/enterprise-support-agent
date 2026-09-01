"""parse_request node tests. / parse_request 节点测试。"""

from dataclasses import dataclass, field

import pytest

from app.agent.nodes import parse_request
from app.agent.parsing import RequestParsingError
from app.agent.state import MAX_SYMPTOMS, ParseRequestInput, ParseRequestOutput, SupportState

SESSION_ID = "8a7b1e5f-14e8-4bd8-a79b-b3a7f70fe93b"


def make_state(**updates: object) -> SupportState:
    """Build one valid workflow state. / 构造一个合法工作流状态。"""

    payload: dict[str, object] = {
        "session_id": SESSION_ID,
        "user_message": "设备 E-200 报 E01，无法启动",
        "trace_id": "trace-c2-parse-001",
    }
    payload.update(updates)
    return SupportState.model_validate(payload)


@dataclass
class FakeParser:
    """Return a fixed result and retain detached call inputs. / 返回固定结果并保存独立的调用输入。"""

    output: ParseRequestOutput
    requests: list[ParseRequestInput] = field(default_factory=list)

    def parse(self, request: ParseRequestInput, /) -> ParseRequestOutput:
        self.requests.append(request.model_copy(deep=True))
        return self.output


class FailingParser:
    """Raise one expected parser failure. / 抛出一个预期解析失败。"""

    def parse(self, request: ParseRequestInput, /) -> ParseRequestOutput:
        del request
        raise RequestParsingError("provider-secret-must-not-leak")


class InvalidOutputParser:
    """Bypass construction checks to simulate a malformed adapter. / 绕过构造检查以模拟畸形适配器。"""

    def parse(self, request: ParseRequestInput, /) -> ParseRequestOutput:
        del request
        return ParseRequestOutput.model_construct(parsed_confidence=2.0)


class CrashingParser:
    """Raise an unexpected programming failure. / 抛出未预期程序错误。"""

    def parse(self, request: ParseRequestInput, /) -> ParseRequestOutput:
        del request
        raise RuntimeError("unexpected parser bug")


def test_parse_request_calls_injected_parser_and_returns_complete_fields() -> None:
    """The node delegates parsing through the injected boundary. / 节点通过注入边界委托解析。"""

    parser = FakeParser(
        ParseRequestOutput(
            device_model="E-200",
            fault_code="E01",
            symptoms=["无法启动"],
            parsed_confidence=0.95,
        )
    )

    output = parse_request(make_state(), parser)

    assert output.device_model == "E-200"
    assert output.fault_code == "E01"
    assert output.symptoms == ["无法启动"]
    assert output.parsed_confidence == 0.95
    assert len(parser.requests) == 1
    assert parser.requests[0].user_message == "设备 E-200 报 E01，无法启动"


def test_explicit_fields_win_and_symptoms_merge_without_mutating_state() -> None:
    """Trusted fields win while symptoms merge in stable order. / 明确字段优先，症状稳定合并。"""

    state = make_state(
        device_model="E-200",
        fault_code="E01",
        symptoms=["无法启动", "面板无显示"],
    )
    before = state.model_dump()
    parser = FakeParser(
        ParseRequestOutput(
            device_model="E-300",
            fault_code="E99",
            symptoms=["面板无显示", "保险丝异常"],
            parsed_confidence=0.8,
        )
    )

    output = parse_request(state, parser)

    assert output.device_model == "E-200"
    assert output.fault_code == "E01"
    assert output.symptoms == ["无法启动", "面板无显示", "保险丝异常"]
    assert state.model_dump() == before
    assert parser.requests[0].device_model == "E-200"
    assert parser.requests[0].fault_code == "E01"


def test_parser_can_fill_only_missing_fields() -> None:
    """A parser supplements context without replacing known data. / 解析器只补齐缺失上下文。"""

    parser = FakeParser(
        ParseRequestOutput(
            fault_code="E01",
            symptoms=[],
            parsed_confidence=0.7,
        )
    )

    output = parse_request(make_state(device_model="E-200"), parser)

    assert output.device_model == "E-200"
    assert output.fault_code == "E01"


def test_symptom_merge_preserves_existing_values_at_the_limit() -> None:
    """Merging cannot exceed the shared state limit. / 合并结果不能超过共享状态上限。"""

    existing = [f"已有症状 {index}" for index in range(MAX_SYMPTOMS)]
    parser = FakeParser(
        ParseRequestOutput(
            symptoms=["解析出的额外症状"],
            parsed_confidence=0.6,
        )
    )

    output = parse_request(make_state(symptoms=existing), parser)

    assert output.symptoms == existing
    assert len(output.symptoms) == MAX_SYMPTOMS


def test_empty_parser_result_remains_a_valid_partial_update() -> None:
    """No extracted fields can continue to required-field validation. / 未解析出字段时仍可进入必填校验。"""

    parser = FakeParser(ParseRequestOutput(parsed_confidence=0.1))

    output = parse_request(make_state(), parser)

    assert output.device_model is None
    assert output.fault_code is None
    assert output.symptoms == []
    assert output.parsed_confidence == 0.1


def test_expected_parser_failure_preserves_context_without_leaking_details() -> None:
    """Expected failures degrade without exposing provider details. / 预期失败降级且不暴露厂商细节。"""

    output = parse_request(
        make_state(device_model="E-200", symptoms=["无法启动"]),
        FailingParser(),
    )

    assert output.device_model == "E-200"
    assert output.fault_code is None
    assert output.symptoms == ["无法启动"]
    assert output.parsed_confidence == 0.0
    assert "provider-secret-must-not-leak" not in str(output)


def test_invalid_parser_output_uses_the_same_controlled_fallback() -> None:
    """Malformed structured output cannot enter shared state. / 畸形结构化结果不能进入共享状态。"""

    output = parse_request(make_state(fault_code="E01"), InvalidOutputParser())

    assert output.device_model is None
    assert output.fault_code == "E01"
    assert output.parsed_confidence == 0.0


def test_unexpected_parser_error_is_not_silently_swallowed() -> None:
    """Programming failures remain visible to the outer error boundary. / 程序错误继续交给外层错误边界。"""

    with pytest.raises(RuntimeError, match="unexpected parser bug"):
        parse_request(make_state(), CrashingParser())
