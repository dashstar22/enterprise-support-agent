"""Feedback route backed by application-local memory. / 使用应用本地内存的反馈路由。"""

from fastapi import APIRouter, Request, status

from app.api.errors import ApiError
from app.api.schemas import FeedbackCreatedResponse, FeedbackRequest
from app.api.sessions import InMemorySessionStore
from app.observability.request_context import get_request_context


def build_feedback_router(store: InMemorySessionStore) -> APIRouter:
    """Bind the feedback route to one injected store. / 把反馈路由绑定到一个注入的存储。"""

    router = APIRouter(tags=["feedback"])

    @router.post(
        "/feedback",
        response_model=FeedbackCreatedResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def create_feedback(payload: FeedbackRequest, http_request: Request) -> FeedbackCreatedResponse:
        if store.get(payload.session_id) is None:
            raise ApiError(
                status_code=status.HTTP_404_NOT_FOUND,
                code="SESSION_NOT_FOUND",
                message="会话不存在。",
            )

        context = get_request_context(http_request)
        feedback = store.record_feedback(payload, context.request_id, context.trace_id)
        if feedback is None:
            raise ApiError(
                status_code=status.HTTP_404_NOT_FOUND,
                code="FEEDBACK_TARGET_NOT_FOUND",
                message="反馈目标不存在。",
            )
        return feedback

    return router
