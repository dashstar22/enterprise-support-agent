"""Request and trace identifier middleware. / 请求编号与追踪编号中间件。"""

import re
from dataclasses import dataclass
from typing import cast
from uuid import uuid4

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.api.errors import build_error_response

REQUEST_ID_HEADER = "X-Request-ID"
TRACE_ID_HEADER = "X-Trace-ID"
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Safe identifiers for one HTTP request. / 一次 HTTP 请求的安全关联编号。"""

    request_id: str
    trace_id: str


def new_request_id() -> str:
    """Generate a server-owned request identifier. / 生成服务端请求编号。"""

    return f"req-{uuid4()}"


def new_trace_id() -> str:
    """Generate a server-owned trace identifier. / 生成服务端追踪编号。"""

    return f"trace-{uuid4()}"


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Validate, generate, and echo correlation headers. / 校验、生成并回传关联编号响应头。"""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        supplied_request_id = request.headers.get(REQUEST_ID_HEADER)
        request_id = new_request_id()
        trace_id = new_trace_id()

        request_id_is_invalid = (
            supplied_request_id is not None
            and not REQUEST_ID_PATTERN.fullmatch(supplied_request_id)
        )
        if supplied_request_id is not None and not request_id_is_invalid:
            request_id = supplied_request_id

        request.state.request_id = request_id
        request.state.trace_id = trace_id

        if request_id_is_invalid:
            response: Response = build_error_response(
                status_code=400,
                code="INVALID_REQUEST_ID",
                message="X-Request-ID 格式无效。",
                retryable=False,
                trace_id=trace_id,
            )
        else:
            try:
                response = await call_next(request)
            except Exception:
                response = build_error_response(
                    status_code=500,
                    code="INTERNAL_SERVER_ERROR",
                    message="服务暂时无法处理请求，请稍后重试。",
                    retryable=True,
                    trace_id=trace_id,
                )

        response.headers[REQUEST_ID_HEADER] = request_id
        response.headers[TRACE_ID_HEADER] = trace_id
        return response


def get_request_context(request: Request) -> RequestContext:
    """Read identifiers installed by the middleware. / 读取中间件写入的关联编号。"""

    return RequestContext(
        request_id=cast(str, request.state.request_id),
        trace_id=cast(str, request.state.trace_id),
    )
