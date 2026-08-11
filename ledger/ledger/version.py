"""这份账是哪一版代码算出来的。

一个字符串，但没有它，「回滚」这个词是空的。

引擎会一直改——加平台、接新表、改口径，模型自己也会参与改。改错了不一定报错，
更常见的形态是某家店某个月的数字悄悄变了。发现的时候要问三个问题：

    这个数是哪一版算的        没有版本号就只能靠提交时间猜，而快照可以是几周前的
    那一版和现在差在哪         有版本号才 diff 得出来
    回到那一版                 回到一个说不清是什么的状态，不叫回滚

所以每次算账都盖一个印。印的内容是 git 提交号加上工作区脏不脏。

`-dirty` 那个后缀是这里最要紧的一位。工作区脏的时候算出来的结果不可复现——那份代码
不在版本库里，谁也拿不回来。这种结果照样能存、能看，但它不该被当成一个能回滚到的点，
更不该被录成基线。标出来，剩下的交给人判断。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

#: 仓库根。从这个文件往上两层：ledger/ledger/version.py → ledger/ → 仓库根。
_ROOT = Path(__file__).resolve().parents[2]

#: 只看这两个目录脏不脏。文档、笔记、别的项目改了不影响账怎么算。
_WATCHED = ("ledger", "models")

#: 部署时落下的版本印。
#:
#: 线上那台机器上没有 git，代码是打包送过去的，也就不是个 git 仓库——问 git 只会
#: 得到 `unknown`，于是生产环境里每一笔账都说不清是哪一版算的，上面那三个问题
#: 一个都答不了。恰恰是生产环境最需要答。
#:
#: 所以打包的时候把当时的版本号写进这个文件，跟着代码一起走。它不是猜的：
#: 内容就是打包机上 `engine_version()` 当场问出来的那一串，脏就带 `-dirty`。
_STAMP = _ROOT / "VERSION"

#: 问不出来时用这个。空字符串不行——落进数据库之后分不清是「没盖印」还是「盖了个空的」。
UNKNOWN = "unknown"


def engine_version() -> str:
    """引擎的版本标识：git 提交号 + 工作区是否有未提交改动。

    先问 git，问不出来再读部署时留下的 `VERSION`。顺序不能反：开发机上代码随时在动，
    git 说的才是当下这一刻的真话，而 `VERSION` 是打包那一刻的快照，早就过时了。

    两个都问不出来返回 `unknown`。这种情况下不抛异常：算账不该因为版本号取不到就
    停下来，取不到本身也是一条要如实记下的信息。
    """
    try:
        sha = _git("rev-parse", "--short", "HEAD")
        dirty = _git("status", "--porcelain", "--", *_WATCHED)
    except (OSError, subprocess.SubprocessError):
        return _stamped()
    if not sha:
        return _stamped()
    return f"{sha}-dirty" if dirty else sha


def _stamped() -> str:
    try:
        value = _STAMP.read_text(encoding="utf-8").strip()
    except OSError:
        return UNKNOWN
    # 一行、无空白。文件被别的东西写花了就当没有——盖一个错的印比不盖更坏。
    return value if value and len(value.split()) == 1 else UNKNOWN


def reproducible(version: str) -> bool:
    """这个版本能不能原样再算一遍。

    能，才谈得上「回到这一版」。脏工作区和问不出版本号都不能。
    """
    return bool(version) and version != UNKNOWN and not version.endswith("-dirty")


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(_ROOT), *args],
        capture_output=True, text=True, timeout=5, check=True,
    ).stdout.strip()


__all__ = ["UNKNOWN", "engine_version", "reproducible"]

