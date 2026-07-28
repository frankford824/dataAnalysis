from __future__ import annotations

from datetime import date

import pytest

from commerce_harness.target_plan import (
    PeriodState,
    TargetStatus,
    build_target_plan,
)


def _record(
    source_id: str,
    *,
    platform: str = "天猫",
    store_name: str = "喜必顺旗舰店",
    purpose: str = "orders",
    **extra: object,
) -> dict[str, object]:
    return {
        "source_id": source_id,
        "platform": platform,
        "store_name": store_name,
        "purpose": purpose,
        **extra,
    }


def test_fills_missing_months_and_ignores_dates_in_directory_names() -> None:
    plan = build_target_plan(
        [
            _record(
                "orders-march",
                path=r"D:\天猫\店铺\喜必顺旗舰店\202602\订单.xlsx",
                content_start="2026-03-02",
                content_end="2026-03-31",
            )
        ],
        as_of=date(2026, 5, 18),
    )

    assert [(target.period, target.status) for target in plan.targets] == [
        ("2026-02", TargetStatus.MISSING),
        ("2026-03", TargetStatus.AVAILABLE),
        ("2026-04", TargetStatus.MISSING),
        ("2026-05", TargetStatus.PARTIAL),
    ]
    assert plan.targets[1].source_ids == ("orders-march",)
    assert plan.targets[-1].period_state is PeriodState.PARTIAL


def test_current_month_with_content_is_partial() -> None:
    plan = build_target_plan(
        [_record("orders-current", detected_periods=["2026-07"])],
        as_of=date(2026, 7, 24),
    )

    current = plan.targets[-1]
    assert current.period == "2026-07"
    assert current.status is TargetStatus.PARTIAL
    assert current.period_state is PeriodState.PARTIAL
    assert current.source_ids == ("orders-current",)


def test_same_platform_aliases_merge_to_one_stable_logical_store() -> None:
    aliases = {
        "XBS旗舰店": "喜必顺旗舰店",
        "喜 必 顺旗舰店": "喜必顺旗舰店",
    }
    first = build_target_plan(
        [
            _record(
                "orders-feb",
                store_name="XBS旗舰店",
                content_periods=["2026-02"],
            ),
            _record(
                "orders-mar",
                store_name="喜 必 顺旗舰店",
                content_periods=["2026-03"],
            ),
        ],
        as_of=date(2026, 3, 31),
        aliases=aliases,
    )
    second = build_target_plan(
        [
            _record(
                "orders-mar",
                store_name="喜 必 顺旗舰店",
                content_periods=["2026-03"],
            ),
            _record(
                "orders-feb",
                store_name="XBS旗舰店",
                content_periods=["2026-02"],
            ),
        ],
        as_of=date(2026, 3, 31),
        aliases=aliases,
    )

    assert {target.logical_store for target in first.targets} == {"喜必顺旗舰店"}
    assert len({target.logical_store_key for target in first.targets}) == 1
    assert [target.to_dict() for target in first.targets] == [
        target.to_dict() for target in second.targets
    ]
    assert first.targets[0].source_ids == ("orders-feb",)
    assert first.targets[1].source_ids == ("orders-mar",)


def test_false_candidates_are_reviewed_and_never_become_targets() -> None:
    plan = build_target_plan(
        [
            {
                "source_id": "pbix",
                "path": r"D:\BI\喜必顺旗舰店经营.pbix",
                "purpose": "pbix_asset",
                "extension": ".pbix",
            },
            _record("generic", store_name="店铺数据", content_periods=["2026-02"]),
            _record(
                "not-commerce",
                store_name="会议纪要",
                purpose="documents",
                content_periods=["2026-02"],
            ),
            {
                "source_id": "unknown-platform",
                "store_name": "可疑店铺",
                "purpose": "orders",
                "content_periods": ["2026-02"],
            },
        ],
        as_of=date(2026, 2, 28),
    )

    assert plan.targets == ()
    assert {item.reason for item in plan.review_required} == {
        "generic_directory_name",
        "missing_ecommerce_evidence",
        "pbix_asset_not_store",
        "platform_not_identified",
    }


def test_same_store_name_on_different_platforms_never_merges() -> None:
    plan = build_target_plan(
        [
            _record(
                "tmall-orders",
                platform="天猫",
                content_periods=["2026-02"],
            ),
            _record(
                "jd-orders",
                platform="京东",
                content_periods=["2026-02"],
            ),
        ],
        as_of=date(2026, 2, 28),
    )

    assert [(target.platform, target.logical_store) for target in plan.targets] == [
        ("jd", "喜必顺旗舰店"),
        ("tmall", "喜必顺旗舰店"),
    ]
    assert len({target.logical_store_key for target in plan.targets}) == 2


def test_parser_template_is_deterministic_platform_evidence() -> None:
    plan = build_target_plan(
        [
            {
                "source_id": "taobao-orders",
                "path": r"D:\订单\店铺\喜必顺旗舰店\订单.xlsx",
                "purpose": "orders",
                "template_id": "taobao_order_v1",
            }
        ],
        as_of=date(2026, 2, 28),
    )

    assert len(plan.targets) == 1
    assert plan.targets[0].platform == "taobao"
    assert plan.targets[0].logical_store == "喜必顺旗舰店"


def test_store_directory_prefix_and_export_filename_identify_scope() -> None:
    plan = build_target_plan(
        [
            {
                "source_id": "pdd-advertising-march",
                "path": (
                    r"D:\广告费\店铺\PDD乐趣\2025"
                    r"\商品推广_分天数据_20260301至20260331.xlsx"
                ),
                "purpose": "advertising",
            }
        ],
        as_of=date(2026, 3, 31),
    )

    assert [(target.period, target.status) for target in plan.targets] == [
        ("2026-02", TargetStatus.MISSING),
        ("2026-03", TargetStatus.PARTIAL),
    ]
    assert {target.platform for target in plan.targets} == {"pinduoduo"}
    assert {target.logical_store for target in plan.targets} == {"PDD乐趣"}
    assert plan.targets[1].source_ids == ("pdd-advertising-march",)


def test_configured_store_without_files_remains_visible_as_missing() -> None:
    plan = build_target_plan(
        [],
        as_of=date(2026, 3, 15),
        configured_stores=[
            {
                "platform": "pinduoduo",
                "logical_store": "PDD待接入店铺",
            }
        ],
    )

    assert [
        (target.period, target.status, target.source_ids)
        for target in plan.targets
    ] == [
        ("2026-02", TargetStatus.MISSING, ()),
        ("2026-03", TargetStatus.PARTIAL, ()),
    ]


def test_month_directory_names_never_become_configured_stores() -> None:
    plan = build_target_plan(
        [],
        as_of=date(2026, 3, 15),
        configured_stores=[
            {"platform": "pinduoduo", "logical_store": "2月"},
            {"platform": "pinduoduo", "logical_store": "十二月份"},
            {"platform": "pinduoduo", "logical_store": "PDD真实店铺"},
        ],
    )

    assert {target.logical_store for target in plan.targets} == {"PDD真实店铺"}


@pytest.mark.parametrize(
    ("store_name", "expected_platform"),
    [
        ("拼多多婚庆", "pinduoduo"),
        ("抖店喜品", "douyin"),
        ("京东皇莉诗", "jd"),
        ("淘宝喜气洋洋", "taobao"),
        ("天猫家居", "tmall"),
        ("朗歆1688", "1688"),
    ],
)
def test_known_store_name_affixes_are_finite_platform_evidence(
    store_name: str,
    expected_platform: str,
) -> None:
    plan = build_target_plan(
        [
            {
                "source_id": f"{expected_platform}-feb",
                "path": rf"D:\数据\店铺\{store_name}\订单_2602.csv",
                "purpose": "orders",
            }
        ],
        as_of=date(2026, 2, 28),
    )

    assert len(plan.targets) == 1
    assert plan.targets[0].platform == expected_platform
    assert plan.targets[0].status is TargetStatus.PARTIAL


def test_known_store_roots_bind_wallet_and_cost_paths_to_configured_store() -> None:
    configured = [
        {
            "platform": "pinduoduo",
            "logical_store": "PDDzlvoey",
        }
    ]
    plan = build_target_plan(
        [
            {
                "source_id": "wallet-march",
                "path": r"D:\内贸\支付宝收支\PDDzlvoey\2026\2603PDDzlvoey.xlsx",
                "purpose": "settlement",
                "content_periods": ["2026-03"],
            },
            {
                "source_id": "cost-march",
                "path": r"D:\内贸\聚水潭成本\2026\PDDzlvoey\2603PDDzlvoey.xlsx",
                "purpose": "product_cost",
                "content_periods": ["2026-03"],
            },
        ],
        configured_stores=configured,
        as_of=date(2026, 3, 31),
    )

    march = plan.targets[-1]
    assert march.logical_store == "PDDzlvoey"
    assert march.platform == "pinduoduo"
    assert march.source_ids == ("cost-march", "wallet-march")
    assert not plan.review_required


def test_governance_sources_do_not_pollute_store_review_queue() -> None:
    plan = build_target_plan(
        [
            {
                "source_id": "employees",
                "path": r"D:\内贸\绩效\员工表.xlsx",
                "purpose": "employee_master",
            },
            {
                "source_id": "rules",
                "path": r"D:\内贸\规则\公式.xlsx",
                "purpose": "rule_corpus",
            },
        ],
        as_of=date(2026, 2, 28),
    )

    assert plan.targets == ()
    assert plan.review_required == ()
