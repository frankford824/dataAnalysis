"""长活的进度。

交一批表要等十几秒到几分钟：留档、解析几十万行、整店重算、写快照。这段时间里
界面除了一个转圈什么都说不出来，人只能猜它是在干活还是已经死了——猜错的代价是
按下刷新，而刷新之后这次交表的结果就再也看不见了。

做法是最土的一种：交表的时候客户端自己带一个号，服务端把「现在干到哪儿」写进
内存里这个号对应的格子，客户端拿这个号轮询。不用 WebSocket、不用任务队列，因为
这里的活是**同步**的——请求还在等着，进度只是同一件事的旁白，不是另一个任务。

只活在内存里，重启就没了。这是对的：进度是给正在盯着屏幕的那个人看的，服务重启
之后那次上传本来也已经断了。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

#: 超过这个岁数的格子会被清掉。上传断了没人来收尸的格子不该越堆越多。
STALE_SECONDS = 30 * 60


@dataclass
class Lane:
    """一次长活的进度。"""

    phase: str = "正在收表"
    #: 干完几件、总共几件。总数为零表示这一步说不出份数，界面就只显示阶段。
    done: int = 0
    total: int = 0
    #: 已经干完的事，按顺序。界面把它当流水账显示，人能看出卡在哪一步。
    trail: list[str] = field(default_factory=list)
    started: float = field(default_factory=time.time)
    touched: float = field(default_factory=time.time)
    finished: bool = False


_lanes: dict[str, Lane] = {}
_lock = threading.Lock()


def open(token: str) -> None:  # noqa: A001 — 这里的「打开」就是最准的词
    """占一个格子。客户端拿着这个号来问进度。"""
    if not token:
        return
    with _lock:
        _sweep()
        _lanes[token] = Lane()


def step(token: str, phase: str, done: int = 0, total: int = 0) -> None:
    """报一步。同一个阶段反复报只更新数字，不会在流水账里重复出现。"""
    if not token:
        return
    with _lock:
        lane = _lanes.get(token)
        if lane is None or lane.finished:
            return
        if phase != lane.phase:
            if lane.phase:
                lane.trail.append(lane.phase)
            lane.phase = phase
        lane.done = done
        lane.total = total
        lane.touched = time.time()


def close(token: str, phase: str = "算完了") -> None:
    if not token:
        return
    with _lock:
        lane = _lanes.get(token)
        if lane is None:
            return
        if lane.phase and lane.phase != phase:
            lane.trail.append(lane.phase)
        lane.phase = phase
        lane.finished = True
        lane.touched = time.time()


def read(token: str) -> dict | None:
    with _lock:
        lane = _lanes.get(token)
        if lane is None:
            return None
        return {
            "phase": lane.phase,
            "done": lane.done,
            "total": lane.total,
            "trail": list(lane.trail),
            "seconds": round(time.time() - lane.started, 1),
            "finished": lane.finished,
        }


def forget(token: str) -> None:
    with _lock:
        _lanes.pop(token, None)


def _sweep() -> None:
    """调用方持锁。"""
    cutoff = time.time() - STALE_SECONDS
    for token in [t for t, lane in _lanes.items() if lane.touched < cutoff]:
        del _lanes[token]


class Reporter:
    """绑定了号的报进度器，传给干活的那一层。

    干活那层不该知道内存里有个字典、更不该知道有 HTTP：它只会说「我在解析第二份
    表」。所以传下去的是这个东西，不是 token。
    """

    def __init__(self, token: str = "") -> None:
        self.token = token

    def __call__(self, phase: str, done: int = 0, total: int = 0) -> None:
        step(self.token, phase, done, total)


#: 不报进度时用它，省得每一层都写 `if report is not None`。
SILENT = Reporter("")
