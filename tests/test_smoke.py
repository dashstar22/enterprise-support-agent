"""Minimal project smoke test. / 项目最小冒烟测试。"""

from pytest import MonkeyPatch

from app.config import Settings


def test_default_settings_load_without_external_services(monkeypatch: MonkeyPatch) -> None:
    """The project starts from safe local defaults. / 项目可从安全的本地默认配置启动。"""

    for field_name in Settings.model_fields:
        monkeypatch.delenv(f"ESA_{field_name.upper()}", raising=False)

    settings = Settings(_env_file=None)

    assert settings.environment == "development"
    assert settings.api_v1_prefix == "/api/v1"
    assert settings.ragflow_enabled is False
    assert settings.ragflow_base_url is None
    assert settings.ragflow_api_key is None
