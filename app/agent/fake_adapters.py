"""Deterministic adapters for local workflow tests. / 本地工作流测试用确定性适配器。"""

from copy import deepcopy
from dataclasses import dataclass, field
from uuid import UUID

from app.agent.adapters import AdapterError
from app.agent.state import (
    BusinessContext,
    EvidenceItem,
    GenerateSupportAnswerInput,
    PersistAuditInput,
    QueryBusinessContextInput,
    RetrieveEvidenceInput,
)

DEFAULT_AUDIT_ID = UUID("11111111-1111-4111-8111-111111111111")


@dataclass
class FakeEvidenceRetriever:
    """Return configured evidence without external calls. / 不调用外部服务地返回配置证据。"""

    result: list[EvidenceItem] = field(default_factory=list)
    error: AdapterError | None = None
    requests: list[RetrieveEvidenceInput] = field(default_factory=list)

    def retrieve(self, request: RetrieveEvidenceInput, /) -> list[EvidenceItem]:
        self.requests.append(request.model_copy(deep=True))
        if self.error is not None:
            raise self.error
        return deepcopy(self.result)


@dataclass
class FakeBusinessContextProvider:
    """Return configured business context without external calls. / 不调用外部服务地返回业务上下文。"""

    result: BusinessContext | None = None
    error: AdapterError | None = None
    requests: list[QueryBusinessContextInput] = field(default_factory=list)

    def get_context(self, request: QueryBusinessContextInput, /) -> BusinessContext | None:
        self.requests.append(request.model_copy(deep=True))
        if self.error is not None:
            raise self.error
        return deepcopy(self.result)


@dataclass
class FakeSupportAnswerGenerator:
    """Return configured answer attempts without an LLM call. / 不调用大模型地返回配置回答尝试。"""

    result: object | None = None
    results: list[object] = field(default_factory=list)
    error: AdapterError | None = None
    requests: list[GenerateSupportAnswerInput] = field(default_factory=list)

    def generate(self, request: GenerateSupportAnswerInput, /) -> object:
        self.requests.append(request.model_copy(deep=True))
        if self.error is not None:
            raise self.error
        if self.results:
            return deepcopy(self.results.pop(0))
        return deepcopy(self.result)


@dataclass
class FakeAuditRepository:
    """Return a configured audit identifier without a database. / 不连接数据库地返回配置审计编号。"""

    result: UUID = DEFAULT_AUDIT_ID
    error: AdapterError | None = None
    requests: list[PersistAuditInput] = field(default_factory=list)

    def persist(self, request: PersistAuditInput, /) -> str:
        self.requests.append(request.model_copy(deep=True))
        if self.error is not None:
            raise self.error
        return str(self.result)
