"""Validated timing and token accounting contracts. / 已校验的耗时和 Token 统计契约。"""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ObservabilitySchema(BaseModel):
    """Reject unreviewed telemetry fields. / 拒绝未经审查的遥测字段。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class ExternalApiCallAudit(ObservabilitySchema):
    """Minimal external-call result without headers or body text. / 不含请求头和正文的最小外部调用结果。"""

    service: Literal["ragflow", "business_api", "ocr"]
    status_code: int | None = Field(default=None, ge=100, le=599)
    latency_ms: float = Field(ge=0.0)
    outcome: Literal["success", "failure"]
    error_code: str | None = Field(default=None, max_length=64)


class ComponentTiming(ObservabilitySchema):
    """One measured workflow component. / 一个已测量的工作流组件。"""

    component: Literal["model", "retrieval", "ocr", "business_api"]
    latency_ms: float = Field(ge=0.0)
    outcome: Literal["success", "failure"]


class TokenUsage(ObservabilitySchema):
    """Token counts reported by a model provider. / 模型服务方报告的 Token 数。"""

    model_name: str = Field(min_length=1, max_length=128)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class TokenPricing(ObservabilitySchema):
    """Configured price per 1,000 tokens; no price means no estimate. / 每 1000 个 Token 的配置价格，未配置就不估算。"""

    input_per_1k: Decimal = Field(ge=Decimal("0"))
    output_per_1k: Decimal = Field(ge=Decimal("0"))


class AuditMetadata(ObservabilitySchema):
    """Extra audited facts that do not belong in business state. / 不属于业务状态的额外审计事实。"""

    request_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    external_api_calls: list[ExternalApiCallAudit] = Field(default_factory=list, max_length=20)
    timings: list[ComponentTiming] = Field(default_factory=list, max_length=4)
    token_usage: TokenUsage | None = None

    @model_validator(mode="after")
    def reject_duplicate_timing_components(self) -> "AuditMetadata":
        components = [item.component for item in self.timings]
        if len(components) != len(set(components)):
            raise ValueError("timings 中的 component 不能重复")
        return self


def estimate_token_cost(usage: TokenUsage, pricing: TokenPricing | None) -> Decimal | None:
    """Estimate cost only from an explicit price table. / 仅依据明确价格表估算成本。"""

    if pricing is None:
        return None
    return (
        Decimal(usage.input_tokens) * pricing.input_per_1k
        + Decimal(usage.output_tokens) * pricing.output_per_1k
    ) / Decimal(1000)
