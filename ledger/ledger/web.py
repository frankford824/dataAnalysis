"""界面资源。

页面在 `static/` 下，不是拼在 Python 字符串里。以前那份是单文件、单动作
（拖表进来看结果），塞进一个字符串还算合理；现在有总览矩阵、单店下钻、交付看板、
店铺设置四个视图，继续拼字符串就没人改得动了，也没有语法高亮和格式化。

不上构建链、不引前端框架。这是内部工具，装一套 npm 只是给以后添维护负担；
真到这一页装不下的时候再换，那时也该知道到底需要什么了。
"""

from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent / "static"

_ASSET = re.compile(r'(href|src)="(/static/[^"?]+)"')


def version() -> str:
    """静态资源的版本号，取所有资源里最新的修改时间。

    不带版本号的话，改完前端部署上去，用户手里还是浏览器缓存的旧脚本——页面看着像
    没更新，或者更糟：新接口配旧脚本，报一堆看不懂的错。这一版按文件时间戳走，
    改一个字就换一个号，不用记得手动升级。
    """
    latest = max((p.stat().st_mtime_ns for p in STATIC.rglob("*") if p.is_file()), default=0)
    return f"{latest:x}"


def page() -> str:
    """首页 HTML。每次读盘，改完刷新就见效，开发时不用重启。

    文件不算大，读盘开销远小于后面那一趟算账。真嫌慢的话在前面挂个反向代理，
    比在这儿做缓存靠谱——缓存会让人以为改了没生效。
    """
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    tag = version()
    return _ASSET.sub(lambda m: f'{m.group(1)}="{m.group(2)}?v={tag}"', html)
