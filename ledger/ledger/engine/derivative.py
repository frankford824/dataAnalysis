"""认出人工中间产物，不让它们进账。

交上来的文件夹里混着三种东西：

1. 平台原始导出——真数据，该摄
2. 人工筛选出来的副本——补发表就是聚水潭成本表按订单类型筛出来另存的
3. 人工做的透视汇总——淘宝那两张「账单汇总」是 Excel 数据透视表

后两种都是从源表加工来的，数据在源表里已经有一份了。摄进去不是多一份证据，
是把同一笔钱记两遍。实测淘宝店 708 行补发订单在两张表里逐行相同，
不识别出来就会多记 4,071.43 元。

为什么要引擎主动认，而不是让人别交：交这些文件是人家的工作习惯，
汇总表本来就是做给人看的。要求对方改习惯来迁就系统，系统就永远脆弱。
反过来，加工痕迹是有客观特征的——透视表的字段前缀、自述的数据来源——
认出来就行了，谁交什么都不影响结果。

认出来不等于报错。这些表照样能识别、能预览，只是不参与算钱，
并且明确说清楚「这是从哪张表加工来的，钱以那张为准」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Excel 数据透视表在列名上留的痕迹。中英文版本都有。
#:
#: 公开的：接表向导也要用它认单列的加工痕迹（`求和项:花费` 是花费那列汇总出来的，
#: 映射它等于把同一笔钱记两遍）。判据只该有一份，两处各写一套迟早分叉。
PIVOT_PREFIXES = ("求和项:", "求和项：", "计数项:", "计数项：", "平均值项:", "最大值项:",
                  "Sum of ", "Count of ", "Average of ")

#: 透视字段占比达到多少才判定为透视表。明细表挂一两列辅助汇总很常见，
#: 整张表都是透视字段才说明它本身就是汇总产物。
_PIVOT_SHARE = 0.25
_DUP_SHARE = 0.25

#: 列名重名占比达到多少才判定为「汇总贴在源数据旁边」。

#: 表里自述数据来源的说法。这类句子只会出现在人工加工的表上——
#: 平台导出的文件不会解释自己是从哪来的。
_PROVENANCE = re.compile(r"(来源|路径)\s*[：:].{0,40}?(提取|筛选|粘贴|复制|来自|取自)")

#: 路径写的是从平台后台导出/下载，说明是原始数据。有这个声明就不必再猜。
_EXPORTED = re.compile(r"路径\s*[：:][^\n]{0,80}?(导出|下载)")


@dataclass(slots=True)
class Derivative:
    """判定结果。"""

    is_derivative: bool
    #: 判定依据，要能让人一眼看懂为什么被排除。
    reason: str = ""
    #: 从自述里认出来的上游表名，认不出为空。
    upstream: str = ""

    def __bool__(self) -> bool:
        return self.is_derivative


def detect(headers: list[str], first_rows: list[list]) -> Derivative:
    """判断一张表是不是人工中间产物。

    只看表头和头几行——加工痕迹都在这里，不用扫全表。
    """
    filled = [h for h in headers if h.strip()]
    pivot = [h for h in headers if any(h.startswith(p) for p in PIVOT_PREFIXES)]
    # 只看「有没有透视字段」会误伤：明细表右边常被人多挂一两列汇总辅助列，
    # 实测 1688 收款明细和抖音对账单都是这样，表本身是真数据。
    # 真正的透视表是整张表都由透视字段构成，靠占比区分。
    if pivot and filled and len(pivot) / len(filled) >= _PIVOT_SHARE:
        shown = "、".join(pivot[:3])
        return Derivative(
            True,
            f"{len(pivot)}/{len(filled)} 列都是透视表字段（{shown}），"
            f"这是 Excel 数据透视表导出的汇总，不是原始数据。钱以被汇总的那张明细表为准。",
        )

    # 把透视结果贴在源数据旁边，会让整组列名出现两遍。
    # 光看「有没有 (空白) 这种透视标注」不行：真明细表右边也常挂着人工加的辅助列，
    # 实测淘宝支付宝明细就带着 (空白) 和总计，但它是 22 万行真数据。
    # 靠重名占比区分——汇总表是整块重复（19 列里 8 个重名），明细表只是零星几列。
    dup = {h for h in filled if filled.count(h) > 1}
    if filled and len(dup) / len(filled) >= _DUP_SHARE:
        shown = "、".join(sorted(dup)[:4])
        return Derivative(
            True,
            f"{len(dup)}/{len(filled)} 个列名重复出现（{shown}），"
            f"是把汇总结果贴在源数据旁边的做法，整张表算加工产物。",
        )

    # 只看第一列。这批表有一套统一的自我说明约定，写在左上角第一格：
    #
    #     名称：对账
    #     路径：千牛——财务——总览——微信账户——查看明细——选择日期——导出
    #     注：以下字段为原始表单无公式
    #
    # 制表的人已经把「这张表哪来的」写清楚了，读它比猜准得多。
    # 路径指向平台导出就是原始数据，指向另一张内部表（「聚水潭成本表内…筛选…粘贴过来」）
    # 就是副本。
    #
    # 必须限定在第一列：表中间也会有说明，但那是注解某一块的。实测微信对账表第 18 列
    # 写着「数据来源：前方微信账单提取主订单ID…」，说的是右边人工加的几列辅助汇总，
    # 不是整张表——全表扫描会把这张 22 万行的真账单误判成副本。
    texts = [str(headers[0])] if headers else []
    texts += [str(row[0]) for row in first_rows[:3] if row]
    for text in texts:
        if _EXPORTED.search(text):
            # 路径写的是平台导出，明确是原始数据，不用再往下判。
            break
        m = _PROVENANCE.search(text)
        if not m:
            continue
        upstream = _upstream_of(text)
        tail = f"，上游是「{upstream}」" if upstream else ""
        return Derivative(
            True,
            f"表里自述了加工来源{tail}：{text.strip()[:60]}。"
            f"这是从别的表提取出来的副本，钱以上游那张为准。",
            upstream,
        )

    return Derivative(False)


def _upstream_of(text: str) -> str:
    """从自述里抠出上游表名。抠不出来返回空，不猜。"""
    m = re.search(r"(提取|来自|取自)\s*([^内，,。；;]{2,20}?表)", text)
    return m.group(2) if m else ""
