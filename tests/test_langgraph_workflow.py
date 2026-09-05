"""Real LangGraph compilation and execution contracts. / 真实 LangGraph 编译与执行契约。"""

from uuid import uuid4

from langgraph.graph.state import CompiledStateGraph

from app.config import Settings
from app.main import create_app


def test_public_executor_uses_compiled_langgraph_with_expected_nodes() -> None:
    """The public executor must expose a compiled graph, not a manual loop. / 公开执行器必须是已编译状态图而非手写循环。"""

    application = create_app(Settings(_env_file=None, environment="test"))
    graph = application.state.workflow_executor.graph

    assert isinstance(graph, CompiledStateGraph)
    assert {
        "parse_request",
        "validate_required_fields",
        "ask_clarification",
        "build_retrieval_query",
        "retrieve_evidence",
        "check_evidence",
        "query_business_context",
        "generate_support_answer",
        "persist_audit",
        "finish",
    }.issubset(graph.get_graph().nodes)


def test_compiled_graph_runs_clarification_and_supported_paths() -> None:
    """Conditional graph edges preserve both public branch outcomes. / 条件边保持追问和成功两种公开结果。"""

    application = create_app(Settings(_env_file=None, environment="test"))
    executor = application.state.workflow_executor
    session_id = uuid4()

    clarification = executor.run(
        session_id=session_id,
        user_message="设备报警了",
        device_model=None,
        fault_code=None,
        request_id="req-langgraph-clarification",
        trace_id="trace-langgraph-clarification",
    )
    supported = executor.run(
        session_id=session_id,
        user_message="请处理 E01",
        device_model="E-200",
        fault_code="E01",
        request_id="req-langgraph-supported",
        trace_id="trace-langgraph-supported",
    )

    assert clarification.status == "needs_clarification"
    assert supported.status == "completed"
