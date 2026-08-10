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
