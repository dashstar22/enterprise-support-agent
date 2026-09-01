"""Required-field and clarification node tests. / 必填字段与追问节点测试。"""

import pytest
from pydantic import ValidationError

from app.agent.nodes import ask_clarification, validate_required_fields
from app.agent.state import AskClarificationOutput, SupportState
from app.api.schemas import MissingField

SESSION_ID = "8a7b1e5f-14e8-4bd8-a79b-b3a7f70fe93b"


def make_state(**updates: object) -> SupportState:
    """Build one valid workflow state. / 构造一个合法工作流状态。"""

    payload: dict[str, object] = {
        "session_id": SESSION_ID,
        "user_message": "设备无法启动",
        "trace_id": "trace-c2-clarify-001",
    }
    payload.update(updates)
    return SupportState.model_validate(payload)


@pytest.mark.parametrize(
    ("device_model", "fault_code", "expected"),
    [
        ("E-200", "E01", []),
        (None, "E01", ["device_model"]),
        ("E-200", None, ["fault_code"]),
        (None, None, ["device_model", "fault_code"]),
    ],
)
def test_validate_required_fields_covers_all_field_combinations(
    device_model: str | None,
    fault_code: str | None,
    expected: list[MissingField],
) -> None:
    """Every field combination yields one stable missing list. / 每种字段组合都得到稳定缺失列表。"""

    state = make_state(device_model=device_model, fault_code=fault_code)
    before = state.model_dump()

    output = validate_required_fields(state)

    assert output.missing_fields == expected
    assert state.model_dump() == before


@pytest.mark.parametrize(
    ("missing_fields", "expected_question"),
    [
        (["device_model"], "请补充设备型号。"),
        (["fault_code"], "请补充故障码。"),
        (["device_model", "fault_code"], "请补充设备型号和故障码。"),
        (["fault_code", "device_model"], "请补充设备型号和故障码。"),
    ],
)
def test_ask_clarification_matches_and_normalizes_missing_fields(
    missing_fields: list[MissingField],
    expected_question: str,
) -> None:
    """Questions match missing fields in canonical order. / 追问按固定顺序匹配缺失字段。"""

    state = make_state(missing_fields=missing_fields)
    before = state.model_dump()

    output = ask_clarification(state)

    assert output.clarification_question == expected_question
    assert state.model_dump() == before


def test_empty_missing_fields_cannot_enter_clarification() -> None:
    """Complete context cannot produce an empty clarification. / 字段完整时不能生成空泛追问。"""

    with pytest.raises(ValidationError):
        ask_clarification(make_state(missing_fields=[]))


def test_validation_output_can_feed_clarification_without_external_calls() -> None:
    """The two pure nodes form the local clarification path. / 两个纯节点组成本地追问路径。"""

    state = make_state(device_model="E-200")
    validation = validate_required_fields(state)
    clarified_state = SupportState.model_validate(
        {
            **state.model_dump(),
            **validation.model_dump(),
        }
    )

    output = ask_clarification(clarified_state)

    assert output.clarification_question == "请补充故障码。"


def test_clarification_output_rejects_fields_owned_by_later_nodes() -> None:
    """Clarification cannot smuggle retrieval or answer data. / 追问不能夹带检索或回答数据。"""

    with pytest.raises(ValidationError, match="retrieval_query"):
        AskClarificationOutput.model_validate(
            {
                "clarification_question": "请补充设备型号。",
                "retrieval_query": "不允许提前检索",
            }
        )
