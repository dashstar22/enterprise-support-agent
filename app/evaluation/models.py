"""Versioned C6 evaluation data contracts. / 带版本的 C6 评测数据契约。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EvaluationSchema(BaseModel):
    """Reject drifting evaluation fields. / 拒绝题集字段悄悄漂移。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


ExpectedStatus = Literal["completed", "needs_clarification", "insufficient_evidence", "error"]
QuestionCategory = Literal[
    "success",
    "clarification",
    "ocr",
    "cross_page",
    "model_confusion",
    "business_api",
    "no_evidence",
    "external_failure",
]
BusinessBehavior = Literal["not_called", "success", "failure"]


class CitationTarget(EvaluationSchema):
    """One expected evidence locator. / 一条预期证据定位。"""

    source_name: str = Field(min_length=1, max_length=255)
    page: int | None = Field(default=None, ge=1)
    section: str | None = Field(default=None, min_length=1, max_length=1000)

    @model_validator(mode="after")
    def require_locator(self) -> "CitationTarget":
        if self.page is None and self.section is None:
            raise ValueError("目标引用必须包含 page 或 section")
        return self


class OcrExpectation(EvaluationSchema):
    """One expected OCR field from the fixed panel. / 固定面板的一项 OCR 预期字段。"""

    field: Literal["device_model", "fault_code", "firmware_version"]
    expected_value: str = Field(min_length=1, max_length=100)


class EvaluationQuestion(EvaluationSchema):
    """One fixed C6 evaluation input and oracle. / 一道固定 C6 题及其判定依据。"""

    id: str = Field(pattern=r"^C6-Q[0-9]{2}$")
    category: QuestionCategory
    user_message: str = Field(min_length=1, max_length=4000)
    device_model: str | None = Field(default=None, min_length=1, max_length=100)
    fault_code: str | None = Field(default=None, min_length=1, max_length=50)
    expected_status: ExpectedStatus
    target: CitationTarget | None = None
    expected_evidence_snippet: str | None = Field(default=None, min_length=1, max_length=4000)
    business_api_behavior: BusinessBehavior
    expected_inventory_available: bool | None = None
    ocr_expectations: list[OcrExpectation] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def keep_oracle_consistent(self) -> "EvaluationQuestion":
        if self.expected_status == "completed" and self.target is None:
            raise ValueError("完成题必须声明目标引用")
        if self.expected_status != "completed" and self.target is not None:
            raise ValueError("非完成题不能声明目标引用")
        if (self.target is None) != (self.expected_evidence_snippet is None):
            raise ValueError("目标引用和预期证据原文必须同时存在或同时为空")
        if self.category == "ocr" and not self.ocr_expectations:
            raise ValueError("OCR 题必须声明 OCR 字段预期")
        if self.category != "ocr" and self.ocr_expectations:
            raise ValueError("非 OCR 题不能携带 OCR 字段预期")
        if self.category == "business_api" and self.business_api_behavior == "not_called":
            raise ValueError("业务接口题必须声明成功或失败行为")
        if (
            self.expected_inventory_available is not None
            and self.business_api_behavior != "success"
        ):
            raise ValueError("库存预期只能用于业务接口成功场景")
        return self


class EvaluationQuestionSet(EvaluationSchema):
    """The immutable question-set envelope. / 不可变题集外层结构。"""

    version: str = Field(pattern=r"^C6-v[0-9]+$")
    fixture_manifest_version: int = Field(ge=1)
    questions: list[EvaluationQuestion] = Field(min_length=20)

    @model_validator(mode="after")
    def require_unique_question_ids(self) -> "EvaluationQuestionSet":
        identifiers = [question.id for question in self.questions]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("题目 id 不能重复")
        return self


class SemanticReview(EvaluationSchema):
    """A human decision kept separate from automatic evidence checks. / 与自动证据检查分离的人工结论。"""

    question_id: str = Field(pattern=r"^C6-Q[0-9]{2}$")
    decision: Literal["approved", "rejected", "pending"]
    rationale: str = Field(min_length=1, max_length=1000)


class SemanticReviewSheet(EvaluationSchema):
    """Review sheet supplied after a person has checked each answer. / 人工逐题检查后提供的复核表。"""

    question_set_version: str = Field(pattern=r"^C6-v[0-9]+$")
    reviewer: str = Field(min_length=1, max_length=100)
    reviewer_type: Literal["human"]
    reviews: list[SemanticReview] = Field(min_length=20)
