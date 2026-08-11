"""测试用的最小数据构造。

这批测试盯的是「文件实际长什么样」而不是「文件应该长什么样」——每一条都对应
一个在真实数据上踩过的坑。所以构造出来的样本要保留那些坑的关键特征：
合并单元格、正数表示支出、透视表字段前缀、尾部控制总数。

真实数据不在仓库里（几百兆的平台导出），所以这里用 openpyxl 现场造最小工作簿。
造出来的文件走的是引擎真正的解析路径，不是拿 mock 顶替。
"""

from __future__ import annotations

import functools
import os
import sys
from pathlib import Path

import openpyxl
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: 仓库里的模型目录。
MODELS = ROOT.parent / "models"

#: 真实平台数据。不在仓库里，只有本机有。
PLATFORM_DATA = Path("/home/wsfwk/data/platform")

#: 明确声明这台机器没有语料。
#:
#: 缺语料默认是**失败**而不是跳过，这一点是故意的，而且是这套测试里最重要的一个决定。
#:
#: 端到端验收和回放是唯一能证明「引擎算的是对的」的两条测试，上面那两百多条单元测试
#: 保的都是零件。缺语料时静默跳过，跑出来是一片绿加两行灰字——没人会去看灰字。于是
#: 「测试全过」这句话在有语料和没语料的机器上含义完全不同，而改引擎的人（尤其是模型）
#: 只会看到那个绿。
#:
#: 所以跳过必须是一个显式动作：设 `LEDGER_NO_CORPUS=1`。设了就等于签字承认
#: 「我这次没有验证引擎算得对不对」，这句话应该说出口，不该由缺省行为替人说。
NO_CORPUS = os.environ.get("LEDGER_NO_CORPUS") == "1"


def needs_real_data(fn):
    """标记一条要真实语料的测试。没语料就报失败，除非显式声明放弃。"""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if not PLATFORM_DATA.exists():
            if NO_CORPUS:
                pytest.skip("LEDGER_NO_CORPUS=1，本次显式放弃引擎正确性验证")
            pytest.fail(
                f"找不到历史数据语料 {PLATFORM_DATA}。\n"
                f"这条测试是引擎正确性的依据之一，缺语料不等于通过，所以报失败而不是跳过。\n"
                f"这台机器确实没有语料、也接受「本次不验证引擎算得对不对」，"
                f"就设 LEDGER_NO_CORPUS=1 再跑。"
            )
        return fn(*args, **kwargs)

    return wrapper


def write_xlsx(
    path: Path,
    rows: list[list],
    *,
    sheet: str = "Sheet1",
    merges: list[str] | None = None,
) -> Path:
    """写一个最小 xlsx。rows 从第 1 行开始，merges 是形如 A2:A4 的合并区域。"""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet
    for row in rows:
        ws.append(row)
    for ref in merges or []:
        ws.merge_cells(ref)
    wb.save(path)
    wb.close()
    return path


@pytest.fixture
def tmp_xlsx(tmp_path: Path):
    """返回一个写 xlsx 的函数，文件落在临时目录。"""

    def make(rows: list[list], *, name: str = "t.xlsx", **kw) -> Path:
        return write_xlsx(tmp_path / name, rows, **kw)

    return make


@pytest.fixture
def tmp_csv(tmp_path: Path):
    """返回一个写 csv 的函数。text 原样写入，好构造注释行和控制总数。"""

    def make(text: str, *, name: str = "t.csv", encoding: str = "utf-8") -> Path:
        p = tmp_path / name
        p.write_text(text, encoding=encoding)
        return p

    return make
