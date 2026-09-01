"""JSON structured logging with mandatory redaction. / 强制脱敏的 JSON 结构化日志。"""

import json
import logging
from collections.abc import Mapping

from app.observability.redaction import redact_mapping


class StructuredLogger:
    """Emit compact, queryable events without credentials. / 输出可查询且不含凭据的紧凑事件。"""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def emit(
        self,
        event: str,
        *,
        request_id: str,
        trace_id: str,
        session_id: str | None = None,
        workflow_run_id: str | None = None,
        fields: Mapping[str, object] | None = None,
    ) -> None:
        """Log one redacted event with all available correlation identifiers. / 记录一条含全部可用关联编号的脱敏事件。"""

        payload: dict[str, object] = {
            "event": event,
            "request_id": request_id,
            "trace_id": trace_id,
            "session_id": session_id,
            "workflow_run_id": workflow_run_id,
        }
        if fields is not None:
            payload["fields"] = redact_mapping(fields)
        self._logger.info(json.dumps(payload, ensure_ascii=False, sort_keys=True))
