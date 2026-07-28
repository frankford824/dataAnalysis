"""受控判断层。

本包刻意不暴露账本写入接口。即使引用核验、critic 和共识全部通过，
公开产物仍然只是等待人工确认的 L0 建议。
"""

from .autonomy import AutonomyAssessment, AutonomyEvaluator, AutonomyPolicy, ReviewedOutcome
from .cite_guard import CiteGuard, EvidenceLedger
from .consensus import ConsensusEngine
from .corrections import CorrectionBook, CorrectionEntry
from .gateway import GatewayConfig, JsonlCallRecorder, OpenAICompatibleGateway, ReplayTransport
from .models import (
    CriticAssessment,
    EvidenceCitation,
    EvidenceRecord,
    GatewayResult,
    GuardResult,
    ReviewRecommendation,
    SuggestionCandidate,
)

__all__ = [
    "AutonomyAssessment",
    "AutonomyEvaluator",
    "AutonomyPolicy",
    "CiteGuard",
    "ConsensusEngine",
    "CorrectionBook",
    "CorrectionEntry",
    "CriticAssessment",
    "EvidenceCitation",
    "EvidenceLedger",
    "EvidenceRecord",
    "GatewayConfig",
    "GatewayResult",
    "GuardResult",
    "JsonlCallRecorder",
    "OpenAICompatibleGateway",
    "ReplayTransport",
    "ReviewRecommendation",
    "ReviewedOutcome",
    "SuggestionCandidate",
]
