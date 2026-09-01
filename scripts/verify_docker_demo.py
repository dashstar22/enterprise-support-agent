"""Verify the documented Compose services without relying on host-only state. / 验证文档中的 Compose 服务，不依赖宿主机隐藏状态。"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any
from urllib.request import Request, urlopen

import psycopg

JsonRequest = Callable[[str, str, dict[str, object] | None], tuple[int, dict[str, Any]]]
DatabaseQuery = Callable[[], set[str]]

API_BASE_URL = os.environ.get("C7_API_BASE_URL", "http://api:8000")
BUSINESS_API_BASE_URL = os.environ.get("C7_BUSINESS_API_BASE_URL", "http://business-api:8001")
EXPECTED_AUDIT_TABLES = {
    "alembic_version",
    "support_sessions",
    "support_messages",
    "workflow_runs",
    "evidence_items",
    "support_steps",
    "external_api_calls",
    "workflow_timings",
    "user_feedback",
}


class VerificationError(RuntimeError):
    """Describe a failed documented Docker check. / 描述文档化 Docker 检查失败。"""


def request_json(
    url: str, method: str = "GET", body: dict[str, object] | None = None
) -> tuple[int, dict[str, Any]]:
    """Perform one small JSON HTTP check. / 执行一次小型 JSON HTTP 检查。"""

    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    with urlopen(request, timeout=10) as response:
        raw_payload = response.read().decode("utf-8")
        return response.status, json.loads(raw_payload)


def query_audit_tables() -> set[str]:
    """Read migration-created table names through the configured database URL. / 通过配置的数据库地址读取迁移创建的表名。"""

    database_url = os.environ.get("ESA_DATABASE_URL")
    if not database_url:
        raise VerificationError("缺少 ESA_DATABASE_URL，无法验证迁移结果")
    driver_url = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(driver_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
            )
            return {str(row[0]) for row in cursor.fetchall()}


def run_verification(
    get_json: JsonRequest,
    get_tables: DatabaseQuery,
    *,
    api_base_url: str = API_BASE_URL,
    business_api_base_url: str = BUSINESS_API_BASE_URL,
) -> None:
    """Check API, mock service, session branch, and migrated audit schema. / 检查 API、模拟服务、会话分支和已迁移审计结构。"""

    api_status, api_health = get_json(f"{api_base_url}/api/v1/health", "GET", None)
    if api_status != 200 or api_health.get("status") != "ok":
        raise VerificationError("主 API 健康检查失败")

    business_status, business_health = get_json(f"{business_api_base_url}/mock/health", "GET", None)
    if business_status != 200 or business_health.get("status") != "ok":
        raise VerificationError("模拟业务 API 健康检查失败")

    device_status, device = get_json(f"{business_api_base_url}/mock/devices/E-200", "GET", None)
    if device_status != 200 or device != {"model": "E-200", "firmware_version": "3.1.4"}:
        raise VerificationError("模拟业务 API 返回的固定设备资料不符合契约")

    session_status, session = get_json(
        f"{api_base_url}/api/v1/sessions", "POST", {"device_model": "E-200"}
    )
    session_id = session.get("session_id")
    if session_status != 201 or not isinstance(session_id, str):
        raise VerificationError("主 API 无法创建演示会话")

    message_status, message = get_json(
        f"{api_base_url}/api/v1/sessions/{session_id}/messages",
        "POST",
        {"message": "E-200 E01 support question", "fault_code": "E01"},
    )
    answer = message.get("answer")
    steps = answer.get("steps") if isinstance(answer, dict) else None
    citation = steps[0].get("citation") if isinstance(steps, list) and steps else None
    if (
        message_status != 200
        or message.get("status") != "completed"
        or not isinstance(answer, dict)
        or answer.get("confidence") != "supported"
        or not isinstance(citation, dict)
        or citation.get("source_name") != "e200-synthetic-maintenance-guide.md"
    ):
        raise VerificationError("主 API 未返回已验证夹具资料支持的引用回答")

    tables = get_tables()
    missing_tables = EXPECTED_AUDIT_TABLES - tables
    if missing_tables:
        missing = ", ".join(sorted(missing_tables))
        raise VerificationError(f"PostgreSQL 迁移缺少审计表: {missing}")


def main() -> None:
    """Run verification and emit a compact result. / 运行验证并输出简明结果。"""

    run_verification(request_json, query_audit_tables)
    print(
        "C7 Docker validation passed: API, mock business API, cited fixture workflow, and audit migration."
    )


if __name__ == "__main__":
    main()
