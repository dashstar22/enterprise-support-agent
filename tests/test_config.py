"""Configuration safety tests. / 配置安全测试。"""

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.main import create_app


def test_validation_error_does_not_expose_secret() -> None:
    """Validation errors name missing fields without leaking secrets. / 错误指出缺项但不泄露密钥。"""

    sentinel = "must-not-appear-in-validation-error"

    with pytest.raises(ValidationError, match="ESA_RAGFLOW_BASE_URL") as exc_info:
        Settings(
            _env_file=None,
            ragflow_enabled=True,
            ragflow_api_key=sentinel,
        )

    assert sentinel not in str(exc_info.value)


def test_model_pricing_requires_both_input_and_output_values() -> None:
    """Partial prices cannot produce a misleading token-cost estimate. / 不完整价格不能生成误导性的 Token 成本估算。"""

    with pytest.raises(ValidationError, match="模型输入和输出价格必须同时配置"):
        Settings(_env_file=None, model_input_price_per_1k="0.002")

    settings = Settings(
        _env_file=None,
        model_input_price_per_1k="0.002",
        model_output_price_per_1k="0.004",
    )
    assert str(settings.model_input_price_per_1k) == "0.002"


def test_database_configuration_creates_an_audit_repository_without_connecting() -> None:
    """A PostgreSQL URL wires the application audit boundary. / PostgreSQL 地址会接入应用审计边界。"""

    application = create_app(
        Settings(
            _env_file=None,
            environment="test",
            database_url="postgresql+psycopg://esa:password@localhost:5432/enterprise_support",
        )
    )
    assert application.state.audit_repository.__class__.__name__ == "SqlAlchemyAuditRepository"


def test_database_url_rejects_a_non_postgresql_runtime_backend() -> None:
    """Runtime settings do not accidentally claim SQLite is PostgreSQL. / 运行时配置不能把 SQLite 误说成 PostgreSQL。"""

    with pytest.raises(ValidationError, match="postgresql\\+psycopg"):
        Settings(_env_file=None, database_url="sqlite+pysqlite://")
