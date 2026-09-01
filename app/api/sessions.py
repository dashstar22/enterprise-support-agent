"""Session routes backed by application-local memory. / 使用应用本地内存的会话路由。"""

from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock
from uuid import UUID, uuid4

from fastapi import APIRouter, Request, status

from app.agent.workflow import SupportWorkflowExecutor
from app.api.errors import ApiError
from app.api.schemas import (
    CreateSessionRequest,
    ErrorResponse,
    FeedbackCreatedResponse,
    FeedbackRequest,
    MessageHistoryItem,
    MessageResponse,
    RequestId,
    SessionCreatedResponse,
    SessionDetailResponse,
    SubmitMessageRequest,
)
from app.observability.request_context import get_request_context


@dataclass(frozen=True, slots=True)
class StoredExchange:
    """One accepted message and its controlled response. / 一条已接受消息及其受控响应。"""

    request_id: RequestId
    request: SubmitMessageRequest
    response: MessageResponse
    created_at: datetime


class InMemorySessionStore:
    """Keep temporary sessions inside one application instance. / 在单个应用实例内保存临时会话。"""

    def __init__(self) -> None:
        self._sessions: dict[UUID, SessionCreatedResponse] = {}
        self._exchanges: dict[UUID, list[StoredExchange]] = {}
        self._feedback: dict[UUID, FeedbackCreatedResponse] = {}
        self._lock = Lock()

    def create(self, request: CreateSessionRequest) -> SessionCreatedResponse:
        """Create and retain one active session. / 创建并保存一个活动会话。"""

        session = SessionCreatedResponse(
            session_id=uuid4(),
            status="active",
            device_model=request.device_model,
            created_at=datetime.now(UTC),
        )
        with self._lock:
            self._sessions[session.session_id] = session
            self._exchanges[session.session_id] = []
        return session

    def get(self, session_id: UUID) -> SessionCreatedResponse | None:
        """Return a detached session snapshot when it exists. / 会话存在时返回一份独立快照。"""

        with self._lock:
            session = self._sessions.get(session_id)
            return session.model_copy(deep=True) if session is not None else None

    def record_exchange(
        self,
        session_id: UUID,
        request: SubmitMessageRequest,
        response: MessageResponse,
        resolved_device_model: str | None,
        request_id: RequestId,
    ) -> None:
        """Save one valid exchange and its resolved session context. / 保存合法交互及解析后的会话上下文。"""

        exchange = StoredExchange(
            request_id=request_id,
            request=request,
            response=response,
            created_at=datetime.now(UTC),
        )
        with self._lock:
            session = self._sessions[session_id]
            if resolved_device_model != session.device_model:
                self._sessions[session_id] = session.model_copy(
                    update={"device_model": resolved_device_model}
                )
            self._exchanges[session_id].append(exchange)

    def get_detail(self, session_id: UUID) -> SessionDetailResponse | None:
        """Return a detached session and ordered history snapshot. / 返回独立的会话和有序历史快照。"""

        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            messages = [
                MessageHistoryItem(
                    request_id=exchange.request_id,
                    request=exchange.request.model_copy(deep=True),
                    response=exchange.response.model_copy(deep=True),
                    created_at=exchange.created_at,
                )
                for exchange in self._exchanges[session_id]
            ]
            return SessionDetailResponse(
                **session.model_dump(),
                messages=messages,
            )

    def record_feedback(
        self,
        request: FeedbackRequest,
        request_id: str,
        trace_id: str,
    ) -> FeedbackCreatedResponse | None:
        """Record feedback only when its response trace exists. / 仅在响应追踪存在时记录反馈。"""

        with self._lock:
            exchanges = self._exchanges.get(request.session_id)
            if exchanges is None or not any(
                exchange.response.trace_id == request.target_trace_id for exchange in exchanges
            ):
                return None
            feedback = FeedbackCreatedResponse(
                feedback_id=uuid4(),
                status="recorded",
                session_id=request.session_id,
                request_id=request_id,
                trace_id=trace_id,
                target_trace_id=request.target_trace_id,
                rating=request.rating,
                reason=request.reason,
                created_at=datetime.now(UTC),
            )
            self._feedback[feedback.feedback_id] = feedback
            return feedback

    def message_count(self, session_id: UUID) -> int:
        """Return the number of accepted messages in one session. / 返回一个会话已接受的消息数量。"""

        with self._lock:
            return len(self._exchanges.get(session_id, []))

    @property
    def feedback_count(self) -> int:
        """Return the number of accepted feedback records. / 返回已接受反馈记录数量。"""

        with self._lock:
            return len(self._feedback)

    @property
    def count(self) -> int:
        """Return the current number of in-memory sessions. / 返回当前内存会话数量。"""

        with self._lock:
            return len(self._sessions)


def build_sessions_router(
    store: InMemorySessionStore, executor: SupportWorkflowExecutor
) -> APIRouter:
    """Bind session routes to one injected store. / 把会话路由绑定到一个注入的存储。"""

    router = APIRouter(tags=["sessions"])

    @router.post(
        "/sessions",
        response_model=SessionCreatedResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def create_session(request: CreateSessionRequest) -> SessionCreatedResponse:
        return store.create(request)

    @router.get(
        "/sessions/{session_id}",
        response_model=SessionDetailResponse,
    )
    def get_session(session_id: UUID) -> SessionDetailResponse:
        session = store.get_detail(session_id)
        if session is None:
            raise ApiError(
                status_code=status.HTTP_404_NOT_FOUND,
                code="SESSION_NOT_FOUND",
                message="会话不存在。",
            )
        return session

    @router.post(
        "/sessions/{session_id}/messages",
        response_model=MessageResponse,
    )
    def submit_message(
        session_id: UUID,
        payload: SubmitMessageRequest,
        http_request: Request,
    ) -> MessageResponse:
        session = store.get(session_id)
        if session is None:
            raise ApiError(
                status_code=status.HTTP_404_NOT_FOUND,
                code="SESSION_NOT_FOUND",
                message="会话不存在。",
            )

        context = get_request_context(http_request)
        device_model = payload.device_model or session.device_model
        fault_code = payload.fault_code

        response = executor.run(
            session_id=session_id,
            user_message=payload.message,
            device_model=device_model,
            fault_code=fault_code,
            request_id=context.request_id,
            trace_id=context.trace_id,
        )
        if isinstance(response, ErrorResponse):
            raise ApiError(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code=response.error.code,
                message=response.error.message,
                retryable=response.error.retryable,
            )

        store.record_exchange(
            session_id,
            payload,
            response,
            device_model,
            context.request_id,
        )
        return response

    return router
