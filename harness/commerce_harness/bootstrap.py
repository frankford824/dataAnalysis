from __future__ import annotations

import calendar
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import PureWindowsPath
from typing import Any, Literal

from .memory.database import DuckDBMemory

# Compatibility exports for modules that have not yet been migrated to
# multi-target lookup. Bootstrap itself no longer uses these fixed identities.
CONTRACT_ID = "contract_taobao_primary_v1"
STORE_ID = "store_primary"
ENTERPRISE_ID = "enterprise_local_design_partner"


@dataclass(frozen=True, slots=True)
class StoreTarget:
    name: str
    period_tokens: list[str]
    platform_code: str | None = None


@dataclass(frozen=True, slots=True)
class SourceRequirementSpec:
    """A finite, business-facing source contract for one platform.

    ``required`` is deliberately limited to sources that prove the core
    order-to-platform-wallet reconciliation. Profit inputs remain visible,
    but an absent optional source is ``not_applicable`` rather than a false
    pass or a blocker for a different platform.
    """

    kind: str
    business_label: str
    required: bool
    purpose: Literal["reconciliation", "profit", "adjustment"]


SOURCE_CONTRACT_VERSION = "platform-source-contract-v2"
_CORE_REQUIREMENTS = (
    SourceRequirementSpec(
        "orders",
        "订单明细",
        True,
        "reconciliation",
    ),
    SourceRequirementSpec(
        "platform_wallet",
        "平台资金明细",
        True,
        "reconciliation",
    ),
)
_PROFIT_REQUIREMENTS = (
    SourceRequirementSpec(
        "advertising",
        "广告费用",
        False,
        "profit",
    ),
    SourceRequirementSpec(
        "product_cost",
        "商品成本",
        False,
        "profit",
    ),
    SourceRequirementSpec(
        "shipping",
        "物流费用",
        False,
        "profit",
    ),
)
PLATFORM_SOURCE_CONTRACTS: dict[str, tuple[SourceRequirementSpec, ...]] = {
    "taobao": (
        *_CORE_REQUIREMENTS,
        SourceRequirementSpec(
            "platform_fee_details",
            "平台费用明细",
            False,
            "adjustment",
        ),
        *_PROFIT_REQUIREMENTS,
    ),
    "pinduoduo": (
        *_CORE_REQUIREMENTS,
        SourceRequirementSpec(
            "platform_adjustments",
            "平台扣款明细",
            False,
            "adjustment",
        ),
        *_PROFIT_REQUIREMENTS,
    ),
    "douyin": (
        *_CORE_REQUIREMENTS,
        SourceRequirementSpec(
            "platform_adjustments",
            "平台保单与扣款明细",
            False,
            "adjustment",
        ),
        *_PROFIT_REQUIREMENTS,
    ),
    "jd": (*_CORE_REQUIREMENTS, *_PROFIT_REQUIREMENTS),
    "1688": (*_CORE_REQUIREMENTS, *_PROFIT_REQUIREMENTS),
}


def source_contract_for(platform_code: str) -> tuple[SourceRequirementSpec, ...]:
    """Return a conservative finite contract; never borrow Taobao-only files."""

    return PLATFORM_SOURCE_CONTRACTS.get(
        platform_code.strip().casefold(),
        (*_CORE_REQUIREMENTS, *_PROFIT_REQUIREMENTS),
    )


def stable_identity(prefix: str, *parts: str) -> str:
    normalized_parts = [unicodedata.normalize("NFKC", part).strip().casefold() for part in parts]
    if not prefix.strip() or any(not part for part in normalized_parts):
        raise ValueError("稳定身份的前缀和组成部分不能为空")
    canonical = "\x1f".join(normalized_parts)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    slug_source = "-".join(normalized_parts)
    slug = re.sub(r"[^a-z0-9]+", "-", slug_source).strip("-")[:36]
    return f"{prefix.strip().casefold()}_{slug or 'scope'}_{digest}"


# Compatibility export for callers that need the complete finite vocabulary.
# Completeness must use ``source_contract_for(platform)`` instead of this union.
CHECKLIST_KINDS = tuple(
    sorted(
        {
            requirement.kind
            for contract in PLATFORM_SOURCE_CONTRACTS.values()
            for requirement in contract
        }
    )
)

VERIFIED_RULES: tuple[dict[str, Any], ...] = (
    {
        "logical_key": "order_allocation_three_branch",
        "kind": "allocation",
        "title": "订单金额三分支分摊",
        "description": (
            "主订单净收入大于零时按子订单净收入比例；否则按买家实付比例；实付总额为零时平均分配。"
        ),
        "definition": {
            "engine": "commerce_harness.kernel.allocate",
            "algorithm": "three_branch_v1",
            "money_scale": 4,
        },
        "evidence": {
            "status": "verified",
            "replayed_rows": 10530,
            "note": "真实样本逐行一致；不代表其他规则已验证。",
        },
    },
    {
        "logical_key": "wechat_amount_direction",
        "kind": "normalization",
        "title": "微信账单金额方向归一",
        "description": "收入保持正数，支出统一转换为负数。",
        "definition": {
            "engine": "commerce_harness.parse.wechat_pay",
            "algorithm": "expense_negative_v1",
            "money_scale": 4,
        },
        "evidence": {
            "status": "verified",
            "replayed_rows": 2078,
            "note": "真实样本全部命中；不把支付宝映射事实冒充规则。",
        },
    },
)


def _period_dates(token: str) -> tuple[date, date]:
    if len(token) != 4 or not token.isdigit():
        raise ValueError(f"账期必须是 YYMM：{token}")
    year = 2000 + int(token[:2])
    month = int(token[2:])
    if month < 1 or month > 12:
        raise ValueError(f"账期月份无效：{token}")
    return date(year, month, 1), date(
        year,
        month,
        calendar.monthrange(year, month)[1],
    )


def _period_tokens(path: str, configured: list[str]) -> set[str]:
    normalized = path.casefold()
    matched: set[str] = set()
    for token in configured:
        year_month = f"20{token[:2]}{token[2:]}"
        separated_dash = f"20{token[:2]}-{token[2:]}"
        separated_underscore = f"20{token[:2]}_{token[2:]}"
        if any(
            candidate.casefold() in normalized
            for candidate in (
                token,
                year_month,
                separated_dash,
                separated_underscore,
            )
        ):
            matched.add(token)
    if matched:
        return matched
    # Annual workbooks are admitted to each configured month in the same
    # calendar year. Normalization still partitions every row by its business
    # date, so the directory or filename never decides the final period.
    for token in configured:
        year = f"20{token[:2]}"
        if any(
            marker.casefold() in normalized
            for marker in (
                f"\\{year}\\",
                f"{year}年",
                f"{year[2:]}年",
            )
        ):
            matched.add(token)
    return matched


def _observed_kind(
    record: dict[str, Any],
    *,
    platform_code: str,
) -> str | None:
    purpose = str(record["purpose"])
    path = str(record["path"])
    name = PureWindowsPath(path).name.casefold()
    if purpose == "orders":
        return "orders"
    if purpose == "advertising":
        return "advertising"
    if purpose == "product_cost":
        return "product_cost"
    if purpose == "shipping":
        return "shipping"
    if purpose != "settlement":
        return None
    normalized_path = path.casefold()
    if platform_code == "taobao" and "千牛明细" in path:
        return "platform_fee_details"
    if platform_code == "pinduoduo" and "订单扣款" in path:
        return "platform_adjustments"
    if platform_code == "douyin" and any(
        marker in path for marker in ("保单明细", "扣款")
    ):
        return "platform_adjustments"
    # Wallet exports use different names on each platform: 支付宝账务明细,
    # 微信流水, 拼多多月账单, 抖店资金明细 and JD/1688 statements. They are
    # one business requirement and are distinguished later by the parser.
    wallet_markers = (
        "账务明细",
        "资金明细",
        "支付宝收支",
        "微信",
        "bill",
        "账单",
        "收支",
    )
    if any(marker in normalized_path for marker in wallet_markers) or name.startswith(
        "221933"
    ):
        return "platform_wallet"
    # The inventory has already classified this record as settlement and it is
    # uniquely bound to a store. Treat it as a wallet candidate, never as a
    # passed normalized ledger; parsing and quality gates still decide that.
    return "platform_wallet"


def _target_key(target: StoreTarget) -> tuple[str, str]:
    return (
        target.name.strip().casefold(),
        (target.platform_code or "taobao").strip().casefold(),
    )


def _record_target(
    record: dict[str, Any],
    targets: list[StoreTarget],
) -> StoreTarget | None:
    path_parts = {
        unicodedata.normalize("NFKC", part).strip().casefold()
        for part in re.split(r"[\\/]+", str(record["path"]))
        if part.strip()
    }
    matches = [
        target
        for target in targets
        if unicodedata.normalize("NFKC", target.name).strip().casefold()
        in path_parts
    ]
    return matches[0] if len(matches) == 1 else None


def _write_verified_rules(connection: Any, effective_from: date) -> None:
    for spec in VERIFIED_RULES:
        rule_id = f"rule_{spec['logical_key']}"
        definition_json = json.dumps(
            spec["definition"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        checksum = hashlib.sha256(definition_json.encode("utf-8")).hexdigest()
        connection.execute(
            """
            INSERT INTO rule_definition (
                rule_id, logical_key, rule_kind, title, description
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (rule_id) DO NOTHING
            """,
            [
                rule_id,
                spec["logical_key"],
                spec["kind"],
                spec["title"],
                spec["description"],
            ],
        )
        connection.execute(
            """
            INSERT INTO rule_version (
                rule_version_id, rule_id, version, effective_from, status,
                definition_json, checksum_sha256, source_evidence_json,
                approved_by, approved_at
            )
            VALUES (?, ?, 1, ?, 'approved', ?, ?, ?,
                    'verified_replay', current_timestamp)
            ON CONFLICT (rule_version_id) DO NOTHING
            """,
            [
                f"{rule_id}_v1",
                rule_id,
                effective_from,
                definition_json,
                checksum,
                json.dumps(
                    spec["evidence"],
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            ],
        )


def bootstrap_targets(
    database: DuckDBMemory,
    *,
    freeze_run_id: str,
    targets: list[StoreTarget],
    records: list[dict[str, Any]],
    snapshot_by_source_id: dict[str, str],
    reconciliation_mode: Literal["platform_wallet", "bank_three_way"] = "platform_wallet",
    enterprise_key: str = "local-enterprise",
    retire_missing: bool = False,
) -> None:
    if not targets:
        raise ValueError("至少需要一个明确绑定的店铺目标")

    normalized_targets: list[StoreTarget] = []
    seen_targets: set[tuple[str, str]] = set()
    all_period_starts: list[date] = []
    for target in targets:
        name = target.name.strip()
        platform_code = (target.platform_code or "taobao").strip().casefold()
        if not name:
            raise ValueError("店铺名称不能为空")
        if not platform_code:
            raise ValueError(f"店铺 {name} 的平台编码不能为空")
        period_tokens = list(dict.fromkeys(target.period_tokens))
        for token in period_tokens:
            period_start, _ = _period_dates(token)
            all_period_starts.append(period_start)
        normalized = StoreTarget(name, period_tokens, platform_code)
        key = _target_key(normalized)
        if key in seen_targets:
            raise ValueError(f"店铺目标重复：{name} / {platform_code}")
        seen_targets.add(key)
        normalized_targets.append(normalized)

    default_effective_from = date.today().replace(day=1)
    rules_effective_from = min(all_period_starts, default=default_effective_from)
    wallet_mode = reconciliation_mode == "platform_wallet"
    enterprise_id = stable_identity("enterprise", enterprise_key)
    target_contract_ids = {
        stable_identity(
            "contract",
            enterprise_id,
            stable_identity(
                "store",
                enterprise_id,
                target.platform_code or "taobao",
                target.name,
            ),
            target.platform_code or "taobao",
        )
        for target in normalized_targets
    }

    observed: dict[tuple[tuple[str, str], str, str], list[str]] = {}
    for record in records:
        matched_target = _record_target(record, normalized_targets)
        if matched_target is None:
            continue
        observed_kind = _observed_kind(
            record,
            platform_code=matched_target.platform_code or "taobao",
        )
        if observed_kind is None:
            continue
        for token in _period_tokens(
            str(record["path"]),
            matched_target.period_tokens,
        ):
            observed.setdefault(
                (_target_key(matched_target), token, observed_kind),
                [],
            ).append(str(record["source_id"]))

    decisions = (
        (
            "freight_period_attribution",
            "跨店铺运费总表应按哪个业务时间归入月份？",
            "未裁决前，运费不能进入认证成本与利润。",
        ),
        (
            "shared_cost_attribution",
            "公共成本在店铺之间按什么稳定依据分配？",
            "未裁决前，公共成本只能进入待确认差额。",
        ),
        (
            "fund_account_effectivity",
            "各资金账号分别在哪些日期属于该店铺？",
            "未裁决前，资金侧完整性不能只凭文件名确认。",
        ),
    )

    with database.transaction() as connection:
        _write_verified_rules(connection, rules_effective_from)
        profile_by_snapshot = {
            str(snapshot_id): str(status)
            for snapshot_id, status in connection.execute(
                """
                SELECT snapshot_id, status
                FROM (
                    SELECT
                        snapshot_id,
                        status,
                        row_number() OVER (
                            PARTITION BY snapshot_id
                            ORDER BY created_at DESC, profile_id DESC
                        ) AS position
                    FROM source_profile
                )
                WHERE position = 1
                """
            ).fetchall()
        }
        if retire_missing:
            placeholders = ", ".join("?" for _ in target_contract_ids)
            connection.execute(
                f"""
                UPDATE reconciliation_contract
                SET status = 'retired',
                    effective_to = CASE
                        WHEN effective_from > current_date THEN effective_from
                        ELSE current_date
                    END
                WHERE enterprise_id = ?
                  AND status = 'active'
                  AND contract_id NOT IN ({placeholders})
                """,
                [enterprise_id, *sorted(target_contract_ids)],
            )
        for target in normalized_targets:
            platform_code = target.platform_code or "taobao"
            source_contract = source_contract_for(platform_code)
            target_key = _target_key(target)
            store_id = stable_identity(
                "store",
                enterprise_id,
                platform_code,
                target.name,
            )
            contract_id = stable_identity(
                "contract",
                enterprise_id,
                store_id,
                platform_code,
            )
            logical_key = stable_identity(
                "contract-scope",
                enterprise_id,
                store_id,
                platform_code,
            )
            target_periods = [(token, *_period_dates(token)) for token in target.period_tokens]
            effective_from = min(
                (period_start for _, period_start, _ in target_periods),
                default=default_effective_from,
            )
            contract_definition = {
                "name": (
                    f"{platform_code} 订单与平台钱包核对合同 v1"
                    if wallet_mode
                    else f"{platform_code} 三方对账合同 v1"
                ),
                "store_name": target.name,
                "sides": (
                    ["order", "platform_wallet"]
                    if wallet_mode
                    else ["order", "platform", "bank_cash"]
                ),
                "reconciliation_mode": reconciliation_mode,
                "bank_cash_status": ("not_applicable" if wallet_mode else "required"),
                "downstream_view": "pnl_16_columns",
                "pbix_runtime": False,
                "money_engine": "decimal_38_4",
                "source_contract_version": SOURCE_CONTRACT_VERSION,
                "source_requirements": [
                    {
                        "kind": requirement.kind,
                        "business_label": requirement.business_label,
                        "required": requirement.required,
                        "purpose": requirement.purpose,
                    }
                    for requirement in source_contract
                ],
            }
            existing = connection.execute(
                """
                SELECT effective_from
                FROM reconciliation_contract
                WHERE contract_id = ?
                """,
                [contract_id],
            ).fetchone()
            if existing:
                effective_from = min(effective_from, existing[0])
            connection.execute(
                """
                INSERT INTO reconciliation_contract (
                    contract_id, logical_key, enterprise_id, store_id,
                    platform_code, contract_version, effective_from, status,
                    definition_json
                )
                VALUES (?, ?, ?, ?, ?, 1, ?, 'active', ?)
                ON CONFLICT (contract_id) DO UPDATE SET
                    effective_from = excluded.effective_from,
                    effective_to = NULL,
                    status = 'active',
                    definition_json = excluded.definition_json
                """,
                [
                    contract_id,
                    logical_key,
                    enterprise_id,
                    store_id,
                    platform_code,
                    effective_from,
                    json.dumps(
                        contract_definition,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                ],
            )

            period_ids: dict[str, str] = {}
            for token, period_start, period_end in target_periods:
                period_id = stable_identity(
                    "period",
                    contract_id,
                    token,
                )
                period_ids[token] = period_id
                connection.execute(
                    """
                    INSERT INTO accounting_period (
                        period_id, contract_id, store_id, period_start,
                        period_end, status
                    )
                    VALUES (?, ?, ?, ?, ?, 'open')
                    ON CONFLICT (period_id) DO NOTHING
                    """,
                    [
                        period_id,
                        contract_id,
                        store_id,
                        period_start,
                        period_end,
                    ],
                )

            active_kinds = {requirement.kind for requirement in source_contract}
            obsolete_rows = connection.execute(
                """
                SELECT requirement_id, source_kind, effective_from
                FROM checklist_requirement
                WHERE contract_id = ?
                  AND (required = true OR effective_to IS NULL)
                """,
                [contract_id],
            ).fetchall()
            for (
                requirement_id,
                source_kind,
                requirement_effective_from,
            ) in obsolete_rows:
                if str(source_kind) in active_kinds:
                    continue
                retired_on = max(
                    effective_from,
                    requirement_effective_from,
                )
                connection.execute(
                    """
                    UPDATE checklist_requirement
                    SET required = false, effective_to = ?
                    WHERE requirement_id = ?
                    """,
                    [retired_on, requirement_id],
                )

            requirement_ids: dict[str, str] = {}
            for requirement in source_contract:
                kind = requirement.kind
                requirement_id = stable_identity(
                    "requirement",
                    contract_id,
                    kind,
                )
                requirement_ids[kind] = requirement_id
                connection.execute(
                    """
                    INSERT INTO checklist_requirement (
                        requirement_id, contract_id, source_kind, store_scope,
                        required, effective_from, expected_frequency,
                        definition_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, 'monthly', ?)
                    ON CONFLICT (requirement_id) DO UPDATE SET
                        required = excluded.required,
                        effective_from = least(
                            checklist_requirement.effective_from,
                            excluded.effective_from
                        ),
                        effective_to = NULL,
                        definition_json = excluded.definition_json
                    """,
                    [
                        requirement_id,
                        contract_id,
                        kind,
                        store_id,
                        requirement.required,
                        effective_from,
                        json.dumps(
                            {
                                "business_label": requirement.business_label,
                                "purpose": requirement.purpose,
                                "source_contract_version": SOURCE_CONTRACT_VERSION,
                                "automatic_when_unambiguous": True,
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    ],
                )

            for subject, question, impact in decisions:
                connection.execute(
                    """
                    INSERT INTO business_decision (
                        decision_id, contract_id, subject_kind, question,
                        business_impact, status
                    )
                    VALUES (?, ?, ?, ?, ?, 'pending')
                    ON CONFLICT (decision_id) DO NOTHING
                    """,
                    [
                        stable_identity("decision", contract_id, subject),
                        contract_id,
                        subject,
                        question,
                        impact,
                    ],
                )

            for token in target.period_tokens:
                period_id = period_ids[token]
                for requirement in source_contract:
                    kind = requirement.kind
                    source_ids = observed.get(
                        (target_key, token, kind),
                        [],
                    )
                    status = (
                        "present"
                        if source_ids
                        else "missing"
                        if requirement.required
                        else "not_applicable"
                    )
                    snapshot_ids = [
                        snapshot_by_source_id[source_id]
                        for source_id in source_ids
                        if source_id in snapshot_by_source_id
                    ]
                    matched_snapshot_ids = [
                        snapshot_id
                        for snapshot_id in snapshot_ids
                        if profile_by_snapshot.get(snapshot_id) == "matched"
                    ]
                    profile_statuses = {
                        snapshot_id: profile_by_snapshot.get(
                            snapshot_id,
                            "not_profiled",
                        )
                        for snapshot_id in snapshot_ids
                    }
                    if source_ids and not snapshot_ids:
                        status = "failed"
                    elif source_ids and matched_snapshot_ids:
                        status = "present"
                    elif source_ids and any(
                        value != "not_profiled"
                        for value in profile_statuses.values()
                    ):
                        status = "failed"
                    elif source_ids:
                        status = "pending"
                    connection.execute(
                        """
                        INSERT INTO checklist_result (
                            result_id, run_id, period_id, requirement_id,
                            status, observed_json
                        )
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT (result_id) DO NOTHING
                        """,
                        [
                            stable_identity(
                                "check",
                                freeze_run_id,
                                contract_id,
                                token,
                                kind,
                            ),
                            freeze_run_id,
                            period_id,
                            requirement_ids[kind],
                            status,
                            json.dumps(
                                {
                                    "source_ids": source_ids,
                                    "snapshot_ids": snapshot_ids,
                                    "matched_snapshot_ids": matched_snapshot_ids,
                                    "profile_statuses": profile_statuses,
                                    "required": requirement.required,
                                    "purpose": requirement.purpose,
                                    "source_contract_version": SOURCE_CONTRACT_VERSION,
                                },
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                        ],
                    )


def bootstrap_target(
    database: DuckDBMemory,
    *,
    freeze_run_id: str,
    period_tokens: list[str],
    records: list[dict[str, Any]],
    snapshot_by_source_id: dict[str, str],
    store_name: str = "首个设计合作店铺",
    reconciliation_mode: Literal["platform_wallet", "bank_three_way"] = "platform_wallet",
) -> None:
    bootstrap_targets(
        database,
        freeze_run_id=freeze_run_id,
        targets=[StoreTarget(name=store_name, period_tokens=period_tokens)],
        records=records,
        snapshot_by_source_id=snapshot_by_source_id,
        reconciliation_mode=reconciliation_mode,
    )
