"""Public route coverage for the C7 fixture-backed workflow. / C7 夹具工作流的公开路由覆盖。"""

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_public_message_route_runs_clarification_supported_and_no_evidence_branches() -> None:
    """The public route no longer returns the Cycle 1 fixed mock. / 公开路由不再返回 Cycle 1 固定模拟结果。"""

    application = create_app(Settings(_env_file=None, environment="test"))
    with TestClient(application) as client:
        session = client.post("/api/v1/sessions", json={}).json()["session_id"]
        clarification = client.post(
            f"/api/v1/sessions/{session}/messages", json={"message": "设备报警了"}
        )
        supported = client.post(
            f"/api/v1/sessions/{session}/messages",
            json={"message": "请处理 E01", "device_model": "E-200", "fault_code": "E01"},
        )
        no_evidence = client.post(
            f"/api/v1/sessions/{session}/messages",
            json={"message": "请处理 E01", "device_model": "E-201", "fault_code": "E01"},
        )

    assert clarification.status_code == 200
    assert clarification.json()["status"] == "needs_clarification"
    assert clarification.json()["missing_fields"] == ["device_model", "fault_code"]
    assert supported.status_code == 200
    assert supported.json()["answer"]["confidence"] == "supported"
    assert supported.json()["answer"]["steps"][0]["citation"] == {
        "source_name": "e200-synthetic-maintenance-guide.md",
        "page": None,
        "section": "Fault code E01",
    }
    assert "Cycle 1" not in supported.text
    assert no_evidence.status_code == 200
    assert no_evidence.json()["status"] == "insufficient_evidence"
    assert no_evidence.json()["handoff_required"] is True
