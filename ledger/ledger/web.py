"""界面资源。

界面是 `web/` 下的 Vue 工程，`pnpm build` 的产物落在这里的 `static/`。Python 这边
只负责把 index.html 端出去。

原来这里是手写的原生 JS，理由是「内部工具不值得装一套 npm」。后来页面涨到总览、
单店下钻、交付、店铺、提成、接表向导六个视图，还要加平台/店铺/账期三档筛选和
可翻页的下钻——手写那套里每个视图各自拼字符串、各自记筛选状态，改一处漏三处。
换框架的代价是一次性的，不换的代价按视图数量增长。
"""

from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent / "static"

_ASSET = re.compile(r'(href|src)="(/static/[^"?]+)"')


def version() -> str:
    """静态资源的版本号，取所有资源里最新的修改时间。

    构建产物的文件名自带内容哈希，本来不需要这个。但 index.html 引用的路径要是
    因为什么原因没带上哈希（比如手工塞进去一张图），照样会被缓存住——那时页面看着像
    没更新，或者更糟：新接口配旧脚本，报一堆看不懂的错。
    """
    latest = max((p.stat().st_mtime_ns for p in STATIC.rglob("*") if p.is_file()), default=0)
    return f"{latest:x}"


def built() -> bool:
    """前端构建过没有。

    没构建时 static/ 是空的，`/` 会 500，而错误信息里只有一个 FileNotFoundError——
    对着它没人猜得到要去 web/ 下跑 pnpm build。
    """
    return (STATIC / "index.html").exists()


def page() -> str:
    """首页 HTML。每次读盘，构建完刷新就见效，不用重启服务。"""
    if not built():
        return (
            "<!doctype html><meta charset=utf-8>"
            "<body style='font:15px/1.6 system-ui;padding:48px;max-width:640px'>"
            "<h1>界面还没构建</h1>"
            "<p>在 <code>ledger/web</code> 下跑一次：</p>"
            "<pre style='background:#f7f8fa;padding:12px;border-radius:8px'>"
            "pnpm install\npnpm build</pre>"
            "<p>产物会落到 <code>ledger/ledger/static/</code>，刷新这一页就好了。</p>"
        )
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    tag = version()
    return _ASSET.sub(lambda m: f'{m.group(1)}="{m.group(2)}?v={tag}"', html)
