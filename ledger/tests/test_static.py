"""界面构建产物。

这条测试是一次真实事故换来的：给接表向导加模型建议时，同一个函数里写了两个
`const mine`。这在 JavaScript 里是解析期错误——整个文件一行都不执行，于是页面是
一片空白：控制台之外没有任何提示。后端 291 条测试全绿，接口用 curl 打过去也一切
正常，因为坏的东西根本不在 Python 这一侧。

换成 Vue 工程之后这类错误由构建挡住：语法不对 `pnpm build` 直接失败。但换来一个
新的失败方式，症状一模一样——改完前端忘了构建，或者构建产物没跟着代码一起提交。
浏览器打开还是旧界面，或者干脆一片空白，而所有测试照样全绿。

所以这里盯的是「端出去的东西是不是完整的一套」：index.html 在不在、它引用的每个
文件在不在、脚本能不能解析。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).parents[1] / "ledger" / "static"
WEB = Path(__file__).parents[1] / "web"

#: 这台机器上没有 node。
#:
#: 和缺语料时一样：跳过必须是显式动作。默认跳过的话，「测试全过」在装了 node 和
#: 没装 node 的机器上含义不同，而看到绿色的人不会去分辨这个区别。
NO_NODE = os.environ.get("LEDGER_NO_NODE") == "1"

_REF = re.compile(r'(?:href|src)="(/static/[^"?]+)"')


def _node() -> str:
    found = shutil.which("node")
    if found:
        return found
    if NO_NODE:
        pytest.skip("LEDGER_NO_NODE=1，本次不检查前端脚本")
    pytest.fail(
        "找不到 node，没法检查前端脚本能不能解析。\n"
        "前端语法错误会让整个界面变成空白页，而后端测试全绿——这是唯一一种\n"
        "「所有测试都过了但产品打不开」的失败，所以缺 node 报失败而不是跳过。\n"
        "这台机器确实没有 node，就设 LEDGER_NO_NODE=1 再跑。"
    )
    raise AssertionError  # pragma: no cover - pytest.fail 不会返回


def _index() -> str:
    page = STATIC / "index.html"
    assert page.exists(), (
        "static/ 下没有 index.html，界面根本端不出去。\n"
        "在 ledger/web 下跑一次 `pnpm install && pnpm build`。"
    )
    return page.read_text(encoding="utf-8")


def test_the_page_is_built():
    assert "<div id=\"app\"></div>" in _index(), "index.html 不是构建产物"


def test_everything_the_page_asks_for_is_there():
    """引用了但没构建出来的文件，浏览器只会静静地 404，页面白屏。"""
    refs = _REF.findall(_index())
    assert refs, "index.html 一个脚本都没引用，这不可能是构建产物"
    missing = [r for r in refs if not (STATIC / r.removeprefix("/static/")).exists()]
    assert not missing, f"index.html 引用了不存在的文件：{missing}。重新构建一次。"


def test_the_scripts_parse():
    """构建产物是 ES 模块，node 要按模块去解析它，否则 import 那一行就报错。"""
    scripts = sorted(STATIC.rglob("*.js"))
    assert scripts, "一个脚本都没有"
    for path in scripts:
        result = subprocess.run(
            [_node(), "--input-type=module", "--check"],
            input=path.read_text(encoding="utf-8"),
            capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, f"{path.name} 解析不了，界面会是空白页：\n{result.stderr}"


def test_the_source_is_in_the_repo():
    """产物是编译结果，改它没有意义。源码必须在版本库里，否则没人改得动界面。"""
    assert (WEB / "package.json").exists()
    assert (WEB / "src" / "main.js").exists()
