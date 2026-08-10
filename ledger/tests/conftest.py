"""测试用的最小数据构造。

这批测试盯的是「文件实际长什么样」而不是「文件应该长什么样」——每一条都对应
一个在真实数据上踩过的坑。所以构造出来的样本要保留那些坑的关键特征：
合并单元格、正数表示支出、透视表字段前缀、尾部控制总数。

真实数据不在仓库里（几百兆的平台导出），所以这里用 openpyxl 现场造最小工作簿。
造出来的文件走的是引擎真正的解析路径，不是拿 mock 顶替。
"""

from __future__ import annotations

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

#: 没有真实数据时跳过端到端验收。
needs_real_data = pytest.mark.skipif(
    not PLATFORM_DATA.exists(), reason="需要本机的平台数据，仓库里没有"
)


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
