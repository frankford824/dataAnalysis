from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .fingerprint import FormatFingerprint


class SourceKind(StrEnum):
    ORDER = "order"
    ALIPAY = "alipay"
    WECHAT = "wechat"
    COST = "cost"
    ADVERTISING = "advertising"
    FREIGHT = "freight"
    PLATFORM_FEE = "platform_fee"
    HISTORICAL_OUTPUT = "historical_output"
    ALIPAY_CONTROL = "alipay_control"
    WECHAT_CONTROL = "wechat_control"


@dataclass(frozen=True, slots=True)
class HeaderCandidate:
    headers: tuple[str, ...]
    header_row: int
    sheet_name: str | None = None
    sheet_hidden: bool = False
    member_name: str | None = None

    @property
    def locator(self) -> str:
        parts = []
        if self.member_name:
            parts.append(self.member_name)
        if self.sheet_name:
            parts.append(self.sheet_name)
        parts.append(f"row:{self.header_row}")
        return "#".join(parts)


@dataclass(frozen=True, slots=True)
class TabularProfile:
    file_kind: str
    candidates: tuple[HeaderCandidate, ...]
    encoding: str | None = None
    delimiter: str | None = None
    sheet_names: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FileRoute:
    source_kind: SourceKind
    template_id: str
    fields: dict[str, str]
    location: HeaderCandidate
    fingerprint: FormatFingerprint


@dataclass(frozen=True, slots=True)
class ArchiveRoute:
    entries: tuple[FileRoute, ...]
    unmatched_members: tuple[str, ...]
    fingerprint: FormatFingerprint
