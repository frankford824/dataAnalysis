"""窄屏。

店长在手机上看这套界面的次数比想象中多。手机上最容易出的是同一个毛病：某处宽度
写死，整个页面能横向拖动，于是每一屏都少半截，人以为数据缺了。

盯的是构建后的样式表而不是源文件——中间隔着一次打包，源码里写对了、打包后被丢掉
（比如某个文件根本没被引用）照样是白搭。
"""

from pathlib import Path

STATIC = Path(__file__).parents[1] / "ledger" / "static"


def _css() -> str:
    """打包后的样式表。空格被压掉了，所以比对前统一去掉空白。"""
    sheets = list(STATIC.rglob("*.css"))
    assert sheets, "构建产物里一张样式表都没有，在 ledger/web 下跑一次 pnpm build"
    return "".join(p.read_text(encoding="utf-8") for p in sheets).replace(" ", "")


def test_the_side_nav_stops_eating_the_screen_on_a_phone():
    """208px 的侧栏在 375px 宽的手机上占掉一半还多。窄屏必须让它变成横条。"""
    css = _css()
    assert "@media(max-width:900px)" in css, "没有窄屏断点"
    phone = css.split("@media(max-width:900px)", 1)[1]
    assert "grid-template-columns:minmax(0,1fr)" in phone


def test_wide_tables_scroll_inside_themselves():
    """明细表列多，缩不下去。让表格自己横向滚，别让整个页面横向滚。"""
    assert "overflow-x:auto" in _css()
