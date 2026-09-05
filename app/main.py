"""FastAPI application factory and process entrypoint. / FastAPI 应用工厂和进程入口。"""

from fastapi import FastAPI

from app.agent.llm import OpenAICompatibleSupportAnswerGenerator
from app.agent.workflow import (
    DEFAULT_FIXTURE_MANIFEST,
    FixtureEvidenceRetriever,
    SupportWorkflowExecutor,
)
from app.api.errors import install_exception_handlers
from app.api.feedback import build_feedback_router
from app.api.sessions import InMemorySessionStore, build_sessions_router
from app.business.adapter import BusinessApiAdapter, BusinessApiClient
from app.config import Settings, get_settings
from app.db.audit import SqlAlchemyAuditRepository
from app.db.session import create_database_engine, create_session_factory
from app.observability.metrics import TokenPricing
from app.observability.request_context import CorrelationIdMiddleware
from app.rag.adapters import RAGFlowRetrieverAdapter
from app.rag.evidence import EvidenceGate, EvidenceRegistry
from app.rag.ragflow_client import RAGFlowClient

SERVICE_NAME = "enterprise-support-agent"


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build an application from explicit or environment settings. / 用显式配置或环境配置创建应用。"""

    app_settings = settings if settings is not None else get_settings()
    application = FastAPI(title="Enterprise Support Agent", version="0.1.0")
    application.add_middleware(CorrelationIdMiddleware)
    install_exception_handlers(application)
    api_prefix = app_settings.api_v1_prefix.rstrip("/")
    session_store = InMemorySessionStore()

    application.state.settings = app_settings
    application.state.session_store = session_store
    registry = EvidenceRegistry.from_manifest(DEFAULT_FIXTURE_MANIFEST)
    gate = EvidenceGate(registry)
    retriever = (
        RAGFlowRetrieverAdapter(RAGFlowClient.from_settings(app_settings), registry)
        if app_settings.ragflow_enabled
        else FixtureEvidenceRetriever(registry)
    )
    audit_repository = None
    if app_settings.database_url is not None:
        token_pricing = None
        if (
            app_settings.model_input_price_per_1k is not None
            and app_settings.model_output_price_per_1k is not None
        ):
            token_pricing = TokenPricing(
                input_per_1k=app_settings.model_input_price_per_1k,
                output_per_1k=app_settings.model_output_price_per_1k,
            )
        engine = create_database_engine(app_settings.database_url)
        application.state.database_engine = engine
        audit_repository = SqlAlchemyAuditRepository(
            create_session_factory(engine), token_pricing=token_pricing
        )
        application.state.audit_repository = audit_repository
    answer_generator = None
    if app_settings.llm_enabled:
        # The validator above guarantees these values when real generation is enabled.
        assert app_settings.llm_base_url is not None
        assert app_settings.llm_api_key is not None
        assert app_settings.llm_model is not None
        answer_generator = OpenAICompatibleSupportAnswerGenerator(
            base_url=str(app_settings.llm_base_url),
            api_key=app_settings.llm_api_key,
            model=app_settings.llm_model,
            timeout_seconds=app_settings.llm_timeout_seconds,
        )

    workflow_executor = SupportWorkflowExecutor(
        retriever=retriever,
        business_provider=BusinessApiAdapter(BusinessApiClient.from_settings(app_settings)),
        audit_repository=audit_repository,
        gate=gate,
        answer_generator=answer_generator,
    )
    application.state.workflow_executor = workflow_executor
    application.include_router(
        build_sessions_router(session_store, workflow_executor), prefix=api_prefix
    )
    application.include_router(build_feedback_router(session_store), prefix=api_prefix)

    health_path = f"{api_prefix}/health"

    @application.get(health_path, tags=["system"])
    def health_check() -> dict[str, str]:
        """Report this process without probing external services. / 只报告本进程，不探测外部服务。"""

        return {
            "status": "ok",
            "service": SERVICE_NAME,
            "environment": app_settings.environment,
        }

    return application


app = create_app()
