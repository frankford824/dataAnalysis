"""运营归属：哪个商品归谁管。

数据是 `responsibility.csv`，从历史的「运营链接」宽表转出来的长表——
五十万行、五万三千个商品、八十个人、十七个账期。转换规则在
`models/cn-ecommerce/asset-import.yaml` 的 `responsibility_*` 那几条。

为什么不放进 Model
------------------
`load_model()` 每个 HTTP 请求都要跑一次。往 Model 里塞五十万行，等于每次
打开任何一个页面都多花几百毫秒解析 CSV、多占几十兆内存，而其中九成的请求
根本不碰提成。所以这份数据单独放，按需加载，按文件 mtime 缓存。

它是建议，不是配置
------------------
提成实际算钱只认 `commission.csv`。这里出的是「系统猜这个商品归谁」，用来把
配置页从一张空表变成一张填好八成的表。猜错的代价是人改一行，不是算错钱——
所以这里可以大胆猜，但绝不能让它绕过配置直接进计算。

归属是有时效的：同一个商品换过人，`period` 那一列记着是哪个月的归属。取值规则
是「不晚于目标账期的最后一次归属」，也就是沿用最近一次已知的安排。这和提成配置
按下单时间取版是同一个思路，只是这里的粒度是月。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import polars as pl

#: 归属表的文件名，放在模型目录里。
FILENAME = "responsibility.csv"

#: 缓存：模型目录 → (文件指纹, 表)。指纹带 mtime 和大小，换了文件自动重读。
_cache: dict[Path, tuple[tuple[float, int], pl.DataFrame]] = {}


@dataclass(frozen=True, slots=True)
class Owner:
    """一条归属建议。带出处，因为它是猜的，人要能判断该不该信。"""

    person: str
    #: 这条归属记在哪个账期。和目标账期不同就说明是沿用的。
    since: str
    #: 归属表里写的店名。和当前店铺对不上时值得看一眼——可能是同名商品串了店。
    store: str


def table(model_dir: str | Path) -> pl.DataFrame:
    """整张归属表。没有这个文件就返回空表，不报错。

    归属数据缺席是正常状态：新装一套系统、或者换一个模型目录，本来就没有历史
    运营安排。提成配置照样能手填，只是少了自动带出来这一步。
    """
    root = Path(model_dir)
    path = root / FILENAME
    if not path.exists():
        return _empty()

    stat = path.stat()
    fingerprint = (stat.st_mtime, stat.st_size)
    hit = _cache.get(root)
    if hit is not None and hit[0] == fingerprint:
        return hit[1]

    frame = pl.read_csv(
        path,
        schema_overrides={"product_id": pl.Utf8, "period": pl.Utf8,
                          "owner": pl.Utf8, "store": pl.Utf8},
    )
    for col in ("product_id", "period", "owner", "store"):
        if col not in frame.columns:
            return _empty()
    frame = (
        frame.select("product_id", "period", "owner", "store")
        .with_columns(pl.col(c).str.strip_chars().fill_null("") for c in
                      ("product_id", "period", "owner", "store"))
        .filter((pl.col("product_id") != "") & (pl.col("owner") != ""))
    )
    _cache[root] = (fingerprint, frame)
    return frame


def owners_at(model_dir: str | Path, period: str,
              products: list[str] | None = None) -> dict[str, Owner]:
    """这些商品在这个账期归谁。返回 商品 id → 归属建议。

    只认不晚于 `period` 的归属记录，取其中最近的一条。晚于目标账期的记录一概不看：
    那是「后来换的人」，拿它去算之前的月份就是拿今天的安排去改历史的账。

    `products` 给 None 表示要全部。给了列表就先筛——脊柱上通常只有几百个商品，
    先筛能把五十万行砍成几千行，后面的排序和去重都快一个量级。
    """
    frame = table(model_dir)
    if frame.is_empty():
        return {}
    if products is not None:
        wanted = [p for p in {str(x).strip() for x in products} if p]
        if not wanted:
            return {}
        frame = frame.filter(pl.col("product_id").is_in(wanted))
    if period:
        frame = frame.filter(pl.col("period") <= period)
    if frame.is_empty():
        return {}

    # 每个商品留最近的一条。sort + unique(keep=last) 比 group_by + max 快，
    # 而且能一次把 owner 和 store 一起带出来——group_by 还要再 join 回去。
    latest = frame.sort("period").unique(subset=["product_id"], keep="last")
    return {
        r["product_id"]: Owner(person=r["owner"], since=r["period"], store=r["store"])
        for r in latest.iter_rows(named=True)
    }


def coverage(model_dir: str | Path, period: str, products: list[str]) -> dict:
    """这批商品里有多少能查到归属，分别归谁。配置页开头那句话的数据来源。"""
    found = owners_at(model_dir, period, products)
    by_person: dict[str, int] = {}
    for owner in found.values():
        by_person[owner.person] = by_person.get(owner.person, 0) + 1
    unique = {p for p in (str(x).strip() for x in products) if p}
    return {
        "products": len(unique),
        "matched": len(found),
        "people": sorted(
            ({"person": k, "products": v} for k, v in by_person.items()),
            key=lambda d: -d["products"],
        ),
        # 归属数据截止到哪个月。目标账期比它新就说明是沿用的，界面上要说出来，
        # 不然人会以为系统看过这个月的运营安排。
        "latest_period": _latest_period(model_dir),
    }


def _latest_period(model_dir: str | Path) -> str:
    frame = table(model_dir)
    if frame.is_empty():
        return ""
    return frame.get_column("period").max() or ""


def _empty() -> pl.DataFrame:
    return pl.DataFrame(schema={"product_id": pl.Utf8, "period": pl.Utf8,
                                "owner": pl.Utf8, "store": pl.Utf8})


__all__ = ["FILENAME", "Owner", "coverage", "owners_at", "table"]
