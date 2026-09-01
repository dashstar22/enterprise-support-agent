"""RAGFlow-to-workflow boundary without premature evidence claims. / 不提前声称证据的 RAGFlow 到工作流边界。"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from app.agent.adapters import AdapterError
from app.agent.state import EvidenceItem, RetrieveEvidenceInput
from app.rag.evidence import EvidenceRegistry
from app.rag.ragflow_client import RAGFlowClientError, RAGFlowRetrievedChunk


class RAGFlowCandidateClient(Protocol):
    """Minimal client contract for raw RAGFlow candidates. / RAGFlow 原始候选的最小客户端约定。"""

    def retrieve(self, question: str) -> list[RAGFlowRetrievedChunk]: ...


class RAGFlowRetrieverAdapter:
    """Forward a validated workflow query to RAGFlow. / 把已校验的工作流检索词转发给 RAGFlow。"""

    def __init__(self, client: RAGFlowCandidateClient, registry: EvidenceRegistry) -> None:
        self._client = client
        self._registry = registry

    @classmethod
    def from_manifest(
        cls, client: RAGFlowCandidateClient, manifest_path: Path
    ) -> RAGFlowRetrieverAdapter:
        return cls(client, EvidenceRegistry.from_manifest(manifest_path))

    def retrieve_candidates(self, request: RetrieveEvidenceInput) -> list[RAGFlowRetrievedChunk]:
        """Return raw candidates; C3-T04 will turn them into EvidenceItem. / 返回原始候选，C3-T04 再转换为 EvidenceItem。"""
        try:
            return self._client.retrieve(request.retrieval_query)
        except RAGFlowClientError as error:
            raise AdapterError(
                _PUBLIC_ERROR_CODES.get(error.code, "RAGFLOW_UNAVAILABLE"),
                handoff_required=True,
            ) from error

    def retrieve(self, request: RetrieveEvidenceInput) -> list[EvidenceItem]:
        """Return only current, registered evidence for the workflow. / 只向工作流返回当前已登记证据。"""
        try:
            return [
                self._registry.to_evidence(candidate)
                for candidate in self.retrieve_candidates(request)
            ]
        except ValueError as error:
            raise AdapterError("RAGFLOW_INVALID_RESPONSE", handoff_required=True) from error


_PUBLIC_ERROR_CODES = {
    "RAGFLOW_TIMEOUT": "RAGFLOW_TIMEOUT",
    "RAGFLOW_REQUEST_REJECTED": "RAGFLOW_REJECTED",
    "RAGFLOW_INVALID_RESPONSE": "RAGFLOW_INVALID_RESPONSE",
    "RAGFLOW_HTTP_ERROR": "RAGFLOW_UNAVAILABLE",
    "RAGFLOW_CONNECTION_ERROR": "RAGFLOW_UNAVAILABLE",
}
