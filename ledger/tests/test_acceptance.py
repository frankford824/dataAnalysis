"""端到端回归：三家店的账，引擎算的要和人工维护的 Excel 对得上。

这是整个仓库最有价值的一条测试，也是唯一一条能证明「引擎算的是对的」的测试。
上面那些单元测试保的是零件，这条保的是结论。

它护着的东西是一次次实测换来的：淘宝七项费用按收入占比分摊、1688 按 COUNTIFS 均摊
且补发并进商品成本、抖音按子订单取结算净额、微信支出取反、合并单元格还原、
派生表识别、跨文件去重。这些改动全在核心路径上，改坏任何一处，某家店的差异
立刻从 0 变成几百几千——而且不看这条测试就发现不了，因为它不会报错，只会算错。

对不上的差异分两类。已解释的差异记在 accept.py 的 explained 里，每条都附着
取证结论（多数是人工表的 SUMIFS 引用了没交过来的外部工作簿）；剩下的算未解释，
必须是 0。这条测试只管未解释的那部分。

需要本机的平台数据，仓库里没有（几百兆的平台导出），没有就跳过。
"""

from __future__ import annotations

import math

import pytest
from conftest import needs_real_data

from tools.accept import CASES, run_case

#: 一分钱以内算对得上。再严就成了浮点数比较游戏，再松就藏得住真问题。
TOLERANCE = 0.005


@needs_real_data
@pytest.mark.slow
@pytest.mark.parametrize("key", sorted(CASES))
def test_store_reconciles_exactly(key: str) -> None:
    case = CASES[key]
    gap = run_case(case)
    assert not math.isnan(gap), (
        f"{case.store} 没算出结果。多半是数据文件没找齐，"
        f"或者模板认不出某张表——看上面的输出。"
    )
    assert gap < TOLERANCE, (
        f"{case.store} 出现 {gap:,.2f} 的未解释差异。"
        f"要么是刚才的改动算错了，要么是发现了新情况需要取证后记进 explained。"
        f"上面的分项对照里标着「未解释」的就是。"
    )


@needs_real_data
@pytest.mark.slow
def test_all_three_stores_covered() -> None:
    """三家店都得在验收里。少一家就等于那家的口径没人看着。"""
    platforms = {c.platform for c in CASES.values()}
    assert platforms == {"taobao", "alibaba1688", "douyin"}


#: 三家店的全部数据都交齐了，账就该结得出来。
ALL_STORES = ["taobao_xibishun", "alibaba1688_xingze", "douyin_qianhuajian"]


def _run_store(store_id: str):
    from conftest import MODELS, PLATFORM_DATA

    from ledger.engine.runtime import ingest, run
    from ledger.model.loader import load_model

    model = load_model(MODELS / "cn-ecommerce")
    store = model.store(store_id)
    found = list(PLATFORM_DATA.rglob("*.xlsx"))
    mine = set(model.files_of(store_id, (p.name for p in found)))
    files = [p for p in found if p.name in mine]
    assert files, f"{store.name} 一个文件都没找到"
    result = run(ingest(files, model, [store.name]), store.platform)
    assert result.slices, f"{store.name} 没算出结果"
    return model, store, result


@needs_real_data
@pytest.mark.slow
@pytest.mark.parametrize("store_id", ALL_STORES)
def test_store_can_close(store_id: str) -> None:
    """数据交齐就能结账。这是产品有没有闭环的唯一判据。

    三家店曾经全部结不出账，拦在两个地方，都不是数据的问题：

      覆盖率的分母算错了  把没发货的订单也要求有出库成本、把全额退款的订单也
                          要求有收款记录。修正分母后三家的商品成本覆盖率是
                          98.1%/98.5%/98.5%，刚好都在 98% 阈值之上——阈值是对的。
      未归类科目没认领    余利宝申购、转出网商银行这类理财与调拨被当成待认领费项，
                          拦着结账。它们是资产间划转，不是损益。

    这条测试盯的是"能结账"这个结论。任何一处改动让某家店重新结不出账，
    这里立刻红——包括那种"把阈值调松让它过去"的改法，因为那会同时让
    test_store_reconciles_exactly 的差异露出来。
    """
    _, store, result = _run_store(store_id)
    for (_, period), sl in result.slices.items():
        blockers = [f for f in sl.audit.findings if f.blocking]
        assert not blockers, (
            f"{store.name} {period} 结不出账，拦截项："
            + "；".join(f"{f.name}（{f.message}）" for f in blockers)
        )
        assert sl.can_close


@needs_real_data
@pytest.mark.slow
@pytest.mark.parametrize("store_id", ALL_STORES)
def test_coverage_denominator_is_scoped(store_id: str) -> None:
    """商品成本的覆盖率只对已发货订单算，而且分母确实比全部订单小。

    要是哪天运单号这一列的绑定失效了，分母会悄悄退回全部订单，覆盖率跟着掉，
    结账被拦——但拦截理由会指向"聚水潭没同步"这个错方向。这里钉住分母被收窄
    这件事本身。
    """
    _, store, result = _run_store(store_id)
    for sl in result.slices.values():
        report = sl.link_reports.get("goods_cost")
        if report is None or not report.spine_keys:
            continue
        assert report.expect_label == "已发货"
        assert report.spine_keys < report.spine_keys_total
        assert report.coverage >= 0.98


@needs_real_data
@pytest.mark.slow
@pytest.mark.parametrize("store_id", ALL_STORES)
def test_spine_source_not_reported_missing(store_id: str) -> None:
    """订单明细交了就不能报它缺。

    脊柱源不产出金额事实，它交付的是订单骨架本身，所以在事实表里永远查不到它。
    早先据此判断完整度，结果三家店都被报「订单明细没有本月数据」——而正是它撑起了
    整张损益表。催人补一份已经交了的文件，是最快让人不信这套提示的办法。
    """
    model, store, result = _run_store(store_id)
    for sl in result.slices.values():
        spine_sources = [s.id for s in model.sources if s.is_spine]
        for sid in spine_sources:
            assert sid not in sl.completeness.missing, (
                f"{store.name} 的 {sid} 交了却被报缺：{sl.completeness.reasons.get(sid)}"
            )
