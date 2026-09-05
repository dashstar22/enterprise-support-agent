"""OpenAI-compatible answer generation adapter. / OpenAI 兼容回答生成适配器。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from json import JSONDecodeError
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import SecretStr

from app.agent.adapters import AdapterError
from app.agent.state import GenerateSupportAnswerInput
from app.observability.metrics import TokenUsage


@dataclass
class OpenAICompatibleSupportAnswerGenerator:
    """Call a chat-completions compatible endpoint and return decoded JSON. / 调用兼容聊天接口并返回 JSON。"""

    base_url: str
    api_key: SecretStr
    model: str
    timeout_seconds: float = 30.0
    last_usage: TokenUsage | None = None
    last_status_code: int | None = None
    call_count: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    is_external: bool = True

    def generate(self, request: GenerateSupportAnswerInput, /) -> object:
        """Generate from validated evidence; provider output is validated by the node. / 使用已校验证据生成，节点还会二次校验。"""

        self.last_usage = None
        self.last_status_code = None
        payload = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": _user_prompt(request)},
            ],
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        endpoint = self.base_url.rstrip("/") + "/chat/completions"
        http_request = Request(
            endpoint,
            data=body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key.get_secret_value()}",
            },
        )
        self.call_count += 1

        try:
            with urlopen(http_request, timeout=self.timeout_seconds) as response:
                self.last_status_code = response.status
                raw_response = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            self.last_status_code = exc.code
            code = {
                401: "LLM_AUTHENTICATION_FAILED",
                403: "LLM_AUTHENTICATION_FAILED",
                429: "LLM_RATE_LIMITED",
            }.get(exc.code, "LLM_UNAVAILABLE" if exc.code >= 500 else "LLM_REQUEST_FAILED")
            raise AdapterError(code, handoff_required=True, diagnostic=f"HTTP {exc.code}") from exc
        except TimeoutError as exc:
            raise AdapterError(
                "LLM_TIMEOUT", handoff_required=True, diagnostic="request timed out"
            ) from exc
        except URLError as exc:
            raise AdapterError(
                "LLM_UNAVAILABLE", handoff_required=True, diagnostic=str(exc.reason)
            ) from exc
        except OSError as exc:
            raise AdapterError(
                "LLM_UNAVAILABLE", handoff_required=True, diagnostic="network error"
            ) from exc
        except (UnicodeDecodeError, JSONDecodeError) as exc:
            raise AdapterError(
                "LLM_INVALID_RESPONSE", handoff_required=True, diagnostic="response is not JSON"
            ) from exc

        try:
            content = _content_from_response(raw_response)
            answer = json.loads(_strip_json_fence(content))
        except (KeyError, IndexError, TypeError, JSONDecodeError) as exc:
            raise AdapterError(
                "LLM_INVALID_RESPONSE", handoff_required=True, diagnostic="missing JSON content"
            ) from exc

        usage = raw_response.get("usage") if isinstance(raw_response, dict) else None
        if isinstance(usage, dict):
            prompt_tokens = usage.get("prompt_tokens")
            completion_tokens = usage.get("completion_tokens")
            if isinstance(prompt_tokens, int) and isinstance(completion_tokens, int):
                self.last_usage = TokenUsage(
                    model_name=self.model,
                    input_tokens=prompt_tokens,
                    output_tokens=completion_tokens,
                )
                self.total_input_tokens += prompt_tokens
                self.total_output_tokens += completion_tokens
        return answer


_SYSTEM_PROMPT = """你是企业设备售后助手。只能根据用户提供的已验证证据和已验证业务上下文组织回答。
必须只输出一个 JSON 对象，不要 Markdown 代码围栏，不要额外解释。JSON 必须包含:
summary (字符串), steps (数组), confidence (只能是 supported), handoff_required (必须是 false)。
每个 steps 项必须包含 order、instruction、reason 和 citation; citation 必须逐字使用输入证据中的 source_name、page、section。
证据不足时不要猜测; 但本适配器只会在证据门禁通过后被调用。"""


def _user_prompt(request: GenerateSupportAnswerInput) -> str:
    evidence = [item.model_dump(mode="json") for item in request.evidence]
    context: Any = (
        request.business_context.model_dump(mode="json") if request.business_context else None
    )
    citation_options = [
        {
            "source_name": item["source_name"],
            "page": item["page"],
            "section": item["section"],
        }
        for item in evidence
    ]
    instructions = (
        "只生成一个最小必要排障步骤。instruction 必须逐字摘录 evidence.text 中的一句，"
        "不要补充证据之外的动作。每个 step 的 citation 必须从 citation_options 原样复制，"
        "不得改写 source_name、page 或 section，也不得填写 null 以外的新定位。"
    )
    return json.dumps(
        {
            "evidence": evidence,
            "citation_options": citation_options,
            "business_context": context,
            "output_requirements": instructions,
        },
        ensure_ascii=False,
    )


def _content_from_response(response: object) -> str:
    choices = response["choices"]  # type: ignore[index]
    message = choices[0]["message"]
    content = message["content"]
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    raise TypeError("message content is not text")


def _strip_json_fence(content: str) -> str:
    stripped = content.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        lines = stripped.splitlines()
        return "\n".join(lines[1:-1]).strip()
    return stripped
