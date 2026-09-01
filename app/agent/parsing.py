"""Replaceable request parsing boundary. / 可替换的请求解析边界。"""

from typing import Protocol

from app.agent.state import ParseRequestInput, ParseRequestOutput


class RequestParser(Protocol):
    """Parse one strict input without fixing a model provider. / 解析严格输入但不绑定模型厂商。"""

    def parse(self, request: ParseRequestInput, /) -> ParseRequestOutput:
        """Return structured fields found in one request. / 返回从一条请求中识别出的结构化字段。"""

        ...


class RequestParsingError(Exception):
    """Expected parser failure that may safely enter clarification. / 可安全进入追问的预期解析失败。"""
