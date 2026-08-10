"""引擎层：七类能力原语。

引擎是护城河。它固化的是电商行业的既成事实，可复用到任何一家电商公司。

    识别 recognize  给一个文件，判断它是什么
    解析 parse      把文件变成带行号的原始行
    归一 normalize  抹平各平台的表达差异
    挂钩 link       按声明的键关联，归集到声明的层级
    归类 classify   按字典把原始科目映射到统一科目
    核算 calculate  按公式树求值
    自检 audit      在结账前拦截

引擎里不许出现公司知识。判断标准很简单：换一家公司，引擎代码一行都不用改。
"""

from .audit import AuditResult, audit
from .calculate import NodeValue, evaluate_metric, evaluate_statement, totals_by_metric
from .classify import classify
from .link import Spine, link
from .normalize import normalize, to_date, to_number
from .parse import ParseError, digest, parse, read_headers
from .recognize import infer_period, infer_store, match_headers, recognize
from .runtime import Ingested, Ingestion, RunResult, Slice, ingest, run
from .types import Completeness, FileRef, Finding, LinkReport, RawRow, RawTable, Recognition

__all__ = [
    "AuditResult", "Completeness", "FileRef", "Finding", "Ingested", "Ingestion",
    "LinkReport", "NodeValue", "ParseError", "RawRow", "RawTable", "Recognition",
    "RunResult", "Slice", "Spine", "audit", "classify", "digest", "evaluate_metric",
    "evaluate_statement", "infer_period", "infer_store", "ingest", "link",
    "match_headers", "normalize", "parse", "read_headers", "recognize", "run",
    "to_date", "to_number", "totals_by_metric",
]
