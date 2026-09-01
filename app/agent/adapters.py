"""Replaceable boundaries for external workflow services. / 外部工作流服务的可替换边界。"""

from typing import Protocol

from app.agent.state import (
    BusinessContext,
    EvidenceItem,
    GenerateSupportAnswerInput,
    PersistAuditInput,
    QueryBusinessContextInput,
    RetrieveEvidenceInput,
)


class AdapterError(Exception):
    """Expected adapter failure with a safe public mapping. / 带安全公开映射的预期适配器失败。"""

    def __init__(self, error_code: str, *, handoff_required: bool, diagnostic: str = "") -> None:
        super().__init__(error_code)
        self.error_code = error_code
        self.handoff_required = handoff_required
        self.diagnostic = diagnostic


class EvidenceRetriever(Protocol):
    """Retrieve candidate evidence from a replaceable service. / 从可替换服务检索候选证据。"""

    def retrieve(self, request: RetrieveEvidenceInput, /) -> list[EvidenceItem]:
        """Return validated candidate fragments. / 返回已校验的候选片段。"""

        ...


class BusinessContextProvider(Protocol):
    """Load optional validated business facts. / 加载可选的已校验业务事实。"""

    def get_context(self, request: QueryBusinessContextInput, /) -> BusinessContext | None:
        """Return business context or an explicit empty result. / 返回业务上下文或明确的空结果。"""

        ...


class SupportAnswerGenerator(Protocol):
    """Generate a candidate answer from validated facts. / 根据已校验事实生成候选回答。"""

    def generate(self, request: GenerateSupportAnswerInput, /) -> object:
        """Return raw output that the node validates again. / 返回原始结果，节点会再次校验。"""

        ...


class AuditRepository(Protocol):
    """Persist a complete workflow state. / 保存完整工作流状态。"""

    def persist(self, request: PersistAuditInput, /) -> str:
        """Return the persisted record identifier. / 返回已保存记录的编号。"""

        ...
