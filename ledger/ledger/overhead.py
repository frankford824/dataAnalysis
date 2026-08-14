"""公摊费用：一笔全公司的钱，摊到各家店。

目前只有兼职工资一项。业务维护的公共表一共五张，前四张（代发成本、刷单/本金佣金、
小额打款、发货运费）每一行都带订单号或运单号，能落到具体的店、具体的单，走的是
正常的摄取链路。兼职工资是第五张，它一个能落到订单的字段都没有，所以只能摊。

摊法原先是从历史文件反解出来的，2026-08 这批数据把它坐实了：五个平台的订单明细表
（和旁边那份规则说明表）里都新增了一列「兼职费用」，列头批注写的就是

    总公式：总兼职费用 / 总交易收款 * 订单明细表对应行交易收款单元格

也就是业务自己认的口径——按交易收款占比摊，摊的基数是全公司的交易收款。业务是
逐行摊的，本引擎摊到店；两者等价，因为一家店所有行的交易收款加起来就是这家店的
交易收款。落到代码上是：

    某店摊到的兼职 = 全公司当月兼职总额 × 该店当月交易收款 ÷ 全公司当月交易收款
    该店提成利润   = 该店店铺利润 − 该店摊到的兼职

提成正是按提成利润算的，所以这一步不是报表上的装饰，它直接决定发多少钱。

总额还是得单独给。那一列在五家店的订单明细里**一格数都没填**（21,989 / 3,086 /
3,726 / 2,297 / 255 行全空），业务这次加的是规则不是数。所以「兼职费用表」在这一批
里并不存在，月度总额继续从 models/cn-ecommerce/overheads.csv 读。不要因为订单明细
里出现了这一列就去绑角色、去挂订单：这列的值本身就是摊出来的结果，把结果当原始
数据摄进来，等于让摊销依赖它自己的输出，而且一旦哪个月业务真填了数，同一笔钱会
既按行进账、又按月摊一遍。

两处刻意的选择
------------
按交易收款摊，不按利润摊。历史文件里就是这么摊的，而且这样摊有个好处：亏钱的店
照样承担它那份人力成本。按利润摊的话，亏损店的分母是负数，摊出来是负的兼职，
等于亏得越多越倒拿钱。

摊完必须加回原数。四舍五入的分位差如果各自留在各店，加起来就不等于业务那张表上的
总额——那个数是要和工资单对上的。所以最后一家店吸收余额，见 `allocate`。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .money import decimal_amount, money_float


@dataclass(frozen=True)
class Share:
    """一家店摊到多少。"""

    store_id: str
    #: 摊的依据（这家店当月交易收款）。
    basis: float
    #: 摊到的金额，正数（花掉的钱）。
    amount: float


@dataclass(frozen=True)
class Spread:
    """一个账期的摊销结果。"""

    period: str
    #: 全公司总额，正数。没配这个账期的话是 None。
    total: float | None
    shares: tuple[Share, ...] = ()
    #: 摊的依据合计。
    basis_total: float = 0.0
    notes: tuple[str, ...] = field(default_factory=tuple)

    def of(self, store_id: str) -> float:
        for s in self.shares:
            if s.store_id == store_id:
                return s.amount
        return 0.0

    @property
    def settled(self) -> bool:
        """摊出来了没有。没配总额、或者一家店都没有收款，都算没摊出来。"""
        return bool(self.shares)


def allocate(period: str, total: float | None,
             basis: list[tuple[str, float]]) -> Spread:
    """把 `total` 按 `basis` 的占比摊到各店。

    `basis` 是 (店铺 id, 该店当月交易收款) 的清单。收款为零或为负的店不摊——
    负数占比会摊出负的兼职，等于亏得越多越倒拿钱。
    """
    if total is None:
        return Spread(period=period, total=None,
                      notes=("兼职费用表还没交这个账期的总额，提成基数里还没扣它。",))

    usable = [(sid, amount) for sid, amount in basis if amount > 0]
    denominator = sum(decimal_amount(a) for _, a in usable)
    if not usable or denominator <= 0:
        return Spread(period=period, total=total, basis_total=0.0, notes=(
            f"这个账期没有一家店算出交易收款，{total:,.2f} 元兼职费用没有摊的依据，"
            f"先按不摊处理。",
        ))

    # 占比大的排前面，余额落在第一家。落在最后一家（占比最小的那家）的话，
    # 一个店的收款只有几百块时，几分钱的余额相对它自己那份会显得很大。
    usable.sort(key=lambda kv: -kv[1])
    money = decimal_amount(total)
    shares: list[Share] = []
    handed = decimal_amount(0)
    for sid, amount in usable[1:]:
        cut = decimal_amount(money_float(money * decimal_amount(amount) / denominator))
        handed += cut
        shares.append(Share(store_id=sid, basis=amount, amount=money_float(cut)))
    first_id, first_basis = usable[0]
    shares.insert(0, Share(store_id=first_id, basis=first_basis,
                           amount=money_float(money - handed)))

    skipped = [sid for sid, amount in basis if amount <= 0]
    notes = []
    if skipped:
        notes.append(
            f"{len(skipped)} 家店这个月没有交易收款，没有摊到兼职费用："
            f"{'、'.join(skipped)}。"
        )
    return Spread(period=period, total=total, shares=tuple(shares),
                  basis_total=money_float(denominator), notes=tuple(notes))
