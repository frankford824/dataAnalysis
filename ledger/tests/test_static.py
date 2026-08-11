"""前端脚本至少要能被解析。

这条测试是一次真实事故换来的：给接表向导加模型建议时，同一个函数里写了两个
`const mine`。这在 JavaScript 里是解析期错误——整个文件一行都不执行，于是页面
是一片空白：导航栏还在（那是 HTML 里的），主体区什么都没有，控制台之外没有任何
提示。后端 291 条测试全绿，接口用 curl 打过去也一切正常，因为坏的东西根本不在
Python 这一侧。

这类错误的代价和发现它的难度完全不成比例：写错一个变量名，整个产品打不开；
而要发现它，得有人真的用浏览器打开一次页面。所以这里花几十毫秒把它挡住。

这条测试只保证「能解析」，不保证「跑得对」。这是它力所能及的全部，但恰好覆盖了
最贵的那一类失败——语法错误是唯一一种会让整个界面同时消失的错误。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).parents[1] / "ledger" / "static"

#: 这台机器上没有 node。
#:
#: 和缺语料时一样：跳过必须是显式动作。默认跳过的话，「测试全过」在装了 node 和
#: 没装 node 的机器上含义不同，而看到绿色的人不会去分辨这个区别。
NO_NODE = os.environ.get("LEDGER_NO_NODE") == "1"


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


@pytest.mark.parametrize("name", ["app.js"])
def test_the_script_parses(name):
    result = subprocess.run(
        [_node(), "--check", str(STATIC / name)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"{name} 解析不了，界面会是空白页：\n{result.stderr}"


def test_every_script_the_page_loads_is_checked():
    """漏检一个文件，这条测试就退化成摆设。

    以后拆出第二个脚本时，上面那个清单必须跟着加——这里盯着它。
    """
    shipped = {p.name for p in STATIC.glob("*.js")}
    assert shipped == {"app.js"}, (
        f"static 目录下的脚本变成了 {sorted(shipped)}，"
        "把新增的加进 test_the_script_parses 的清单里"
    )
