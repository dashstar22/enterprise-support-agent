"""FastAPI health endpoint tests. / FastAPI 健康检查接口测试。"""

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import SERVICE_NAME, create_app


def test_health_check_returns_stable_service_status() -> None:
    """The endpoint reports only local service state. / 接口只报告本地服务状态。"""

    settings = Settings(_env_file=None, environment="test")

    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": SERVICE_NAME,
        "environment": "test",
    }


def test_health_check_uses_configured_api_prefix() -> None:
    """The application factory honors an injected API prefix. / 应用工厂使用传入的接口前缀。"""

    settings = Settings(
        _env_file=None,
        environment="test",
        api_v1_prefix="/internal/v1",
    )

    with TestClient(create_app(settings)) as client:
        configured_response = client.get("/internal/v1/health")
        default_response = client.get("/api/v1/health")

    assert configured_response.status_code == 200
    assert default_response.status_code == 404
