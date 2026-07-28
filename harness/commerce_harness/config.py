from __future__ import annotations

import os
import tomllib
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class WorkspaceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root: Path = Path("~/fa-workbench")
    database: str = "ledger.duckdb"
    snapshots: str = "snapshots"
    normalized: str = "normalized"
    reports: str = "reports"
    llm_logs: str = "llm_logs"

    @field_validator("root", mode="before")
    @classmethod
    def expand_root(cls, value: object) -> Path:
        return Path(str(value)).expanduser().resolve()


class SourceScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shop: str = ""
    shops: list[str] = Field(default_factory=list)
    include_all_discovered: bool = False
    periods: list[str] = Field(default_factory=list)
    start_month: str | None = None
    through_current_month: bool = False

    @field_validator("shop")
    @classmethod
    def normalize_legacy_shop(cls, value: str) -> str:
        return value.strip()

    @field_validator("shops")
    @classmethod
    def normalize_shops(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            name = value.strip()
            if not name:
                raise ValueError("shops 不能包含空店铺名称")
            folded = name.casefold()
            if folded not in seen:
                normalized.append(name)
                seen.add(folded)
        return normalized

    @model_validator(mode="after")
    def validate_scope_mode(self) -> SourceScope:
        if self.shop and self.shops:
            raise ValueError("shop 与 shops 不能同时配置")
        if (self.shop or self.shops) and self.include_all_discovered:
            raise ValueError("已明确配置店铺时不能同时启用 include_all_discovered")
        return self

    @field_validator("start_month")
    @classmethod
    def validate_start_month(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if len(normalized) == 4 and normalized.isdigit():
            year = 2000 + int(normalized[:2])
            month = int(normalized[2:])
        elif (
            len(normalized) == 7
            and normalized[4] == "-"
            and normalized[:4].isdigit()
            and normalized[5:].isdigit()
        ):
            year = int(normalized[:4])
            month = int(normalized[5:])
        else:
            raise ValueError("start_month 必须使用 YYYY-MM 或 YYMM")
        if year < 2000 or not 1 <= month <= 12:
            raise ValueError("start_month 不是有效月份")
        return f"{year:04d}-{month:02d}"

    @property
    def bound_shops(self) -> tuple[str, ...]:
        if self.shop:
            return (self.shop,)
        return tuple(self.shops)

    def resolved_periods(self, as_of: date | None = None) -> list[str]:
        """Resolve the configured accounting months as YYMM tokens.

        ``periods`` remains the explicit compatibility mode.  When
        ``start_month`` is configured, the range is generated through either
        the current month or the latest explicit period, so a new month does
        not require editing the deployment configuration.
        """

        if self.start_month is None:
            return list(dict.fromkeys(self.periods))
        start_year, start_month = (int(part) for part in self.start_month.split("-"))
        current = as_of or date.today()
        if self.through_current_month:
            end_year, end_month = current.year, current.month
        elif self.periods:
            normalized = [
                (
                    2000 + int(token[:2]),
                    int(token[2:]),
                )
                for token in self.periods
            ]
            end_year, end_month = max(normalized)
        else:
            end_year, end_month = start_year, start_month
        if (end_year, end_month) < (start_year, start_month):
            raise ValueError("start_month 不能晚于结束月份")
        tokens: list[str] = []
        year, month = start_year, start_month
        while (year, month) <= (end_year, end_month):
            tokens.append(f"{year % 100:02d}{month:02d}")
            if month == 12:
                year, month = year + 1, 1
            else:
                month += 1
        return tokens


class SourceRootConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    purpose: str
    extensions: list[str] = Field(default_factory=list)


class SourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connector: Literal["finance_win_ssh", "local_fixture"] = "finance_win_ssh"
    ssh_alias: str = "finance-win-ro"
    stable_for_seconds: int = Field(default=600, ge=60)
    max_file_bytes: int = Field(default=2_147_483_648, gt=0)
    allow_zip_archives: bool = False
    scope: SourceScope = Field(default_factory=SourceScope)
    roots: list[SourceRootConfig] = Field(default_factory=list)


class LlmConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    base_url_env: str = "FA_GATEWAY_BASE_URL"
    api_key_env: str = "FA_GATEWAY_API_KEY"
    autonomy_level: Literal["L0", "L1", "L2"] = "L0"
    redaction_required: bool = True

    @field_validator("redaction_required")
    @classmethod
    def redaction_cannot_be_disabled(cls, value: bool) -> bool:
        if not value:
            raise ValueError("外部模型脱敏不能关闭")
        return value

    def credentials(self) -> tuple[str | None, str | None]:
        return os.getenv(self.base_url_env), os.getenv(self.api_key_env)


class ReconciliationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["platform_wallet", "bank_three_way"] = "platform_wallet"


class ComputeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    run_on_startup: bool = True
    poll_seconds: int = Field(default=3600, ge=300, le=86400)
    continue_after_scope_failure: bool = True


class HarnessConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    source: SourceConfig = Field(default_factory=SourceConfig)
    reconciliation: ReconciliationConfig = Field(default_factory=ReconciliationConfig)
    compute: ComputeConfig = Field(default_factory=ComputeConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)


def load_config(path: Path | None = None, workspace: Path | None = None) -> HarnessConfig:
    data: dict[str, Any] = {}
    if path is not None:
        with path.expanduser().open("rb") as reader:
            data = tomllib.load(reader)
    if workspace is not None:
        workspace_data = dict(data.get("workspace") or {})
        workspace_data["root"] = workspace
        data["workspace"] = workspace_data
    return HarnessConfig.model_validate(data)
