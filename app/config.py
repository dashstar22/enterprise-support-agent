"""Application settings loaded from environment variables. / 从环境变量读取应用配置。"""

from decimal import Decimal
from functools import lru_cache
from typing import Annotated, Literal, Self

from pydantic import AnyHttpUrl, Field, SecretStr, StringConstraints, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Validate runtime settings and keep secrets out of representations. / 校验运行配置并隐藏密钥。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="ESA_",
        extra="ignore",
        hide_input_in_errors=True,
    )

    environment: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    api_v1_prefix: str = "/api/v1"
    business_api_base_url: AnyHttpUrl = AnyHttpUrl("http://localhost:8001")
    business_api_timeout_seconds: float = Field(default=3.0, gt=0.0, le=60.0)
    database_url: str | None = None
    model_input_price_per_1k: Decimal | None = Field(default=None, ge=Decimal("0"))
    model_output_price_per_1k: Decimal | None = Field(default=None, ge=Decimal("0"))

    ragflow_enabled: bool = False
    ragflow_base_url: AnyHttpUrl | None = None
    ragflow_api_key: SecretStr | None = None
    ragflow_dataset_id: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
        | None
    ) = None
    ragflow_timeout_seconds: float = Field(default=10.0, gt=0.0, le=60.0)
    ragflow_top_k: int = Field(default=5, ge=1, le=20)
    llm_enabled: bool = False
    llm_base_url: AnyHttpUrl | None = None
    llm_model: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
        | None
    ) = None
    llm_timeout_seconds: float = Field(default=30.0, gt=0.0, le=120.0)
    llm_api_key: SecretStr | None = None

    @model_validator(mode="after")
    def require_enabled_ragflow_settings(self) -> Self:
        if not self.ragflow_enabled:
            return self

        missing: list[str] = []
        if self.ragflow_base_url is None:
            missing.append("ESA_RAGFLOW_BASE_URL")
        if self.ragflow_api_key is None or not self.ragflow_api_key.get_secret_value().strip():
            missing.append("ESA_RAGFLOW_API_KEY")
        if self.ragflow_dataset_id is None:
            missing.append("ESA_RAGFLOW_DATASET_ID")

        if missing:
            missing_names = ", ".join(missing)
            raise ValueError(f"已启用 RAGFlow，但缺少必填环境变量: {missing_names}")

        return self

    @model_validator(mode="after")
    def require_complete_model_pricing(self) -> Self:
        """Avoid inventing a partial token-cost calculation. / 防止用不完整价格表虚构 Token 成本。"""

        prices = (self.model_input_price_per_1k, self.model_output_price_per_1k)
        if (prices[0] is None) != (prices[1] is None):
            raise ValueError("模型输入和输出价格必须同时配置，或同时留空")
        if self.database_url is not None and not self.database_url.startswith(
            "postgresql+psycopg://"
        ):
            raise ValueError("ESA_DATABASE_URL 必须使用 postgresql+psycopg:// 连接地址")
        return self

    @model_validator(mode="after")
    def require_enabled_llm_settings(self) -> Self:
        """Require complete provider settings only when real generation is enabled. / 仅启用真实生成时要求完整模型配置。"""

        if not self.llm_enabled:
            return self

        missing: list[str] = []
        if self.llm_base_url is None:
            missing.append("ESA_LLM_BASE_URL")
        if self.llm_model is None:
            missing.append("ESA_LLM_MODEL")
        if self.llm_api_key is None or not self.llm_api_key.get_secret_value().strip():
            missing.append("ESA_LLM_API_KEY")
        if missing:
            raise ValueError(f"已启用真实 LLM，但缺少必填环境变量: {', '.join(missing)}")
        return self


@lru_cache
def get_settings() -> Settings:
    """Return one validated settings instance per process. / 每个进程复用一份已校验配置。"""

    return Settings()
