"""Controlled API errors and exception handlers. / 受控接口错误和异常处理器。"""

from typing import cast

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

from app.api.schemas import ErrorResponse


class ApiError(Exception):
    """A known user-visible API failure. / 已知且可以安全展示给用户的接口失败。"""

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable


def build_error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    retryable: bool,
    trace_id: str,
) -> JSONResponse:
    """Serialize one failure through the public ErrorResponse schema. / 按公开错误结构序列化一次失败。"""

    payload = ErrorResponse.model_validate(
        {
            "error": {
                "code": code,
                "message": message,
                "retryable": retryable,
            },
            "trace_id": trace_id,
        }
    )
    return JSONResponse(status_code=status_code, content=payload.model_dump(mode="json"))


def _trace_id(request: Request) -> str:
    """Read the trace installed before route execution. / 读取路由执行前写入的追踪号。"""

    return cast(str, request.state.trace_id)


def install_exception_handlers(application: FastAPI) -> None:
    """Map framework and application failures to ErrorResponse. / 把框架和应用失败统一映射为错误响应。"""

    @application.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        return build_error_response(
            status_code=exc.status_code,
            code=exc.code,
            message=exc.message,
            retryable=exc.retryable,
            trace_id=_trace_id(request),
        )

    @application.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        del exc
        return build_error_response(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="REQUEST_VALIDATION_ERROR",
            message="请求参数格式无效。",
            retryable=False,
            trace_id=_trace_id(request),
        )

    @application.exception_handler(StarletteHTTPException)
    async def handle_http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == status.HTTP_404_NOT_FOUND:
            return build_error_response(
                status_code=exc.status_code,
                code="ROUTE_NOT_FOUND",
                message="请求的接口不存在。",
                retryable=False,
                trace_id=_trace_id(request),
            )
        return build_error_response(
            status_code=exc.status_code,
            code="HTTP_ERROR",
            message="请求无法处理。",
            retryable=False,
            trace_id=_trace_id(request),
        )
