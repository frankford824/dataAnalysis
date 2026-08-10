"""建模层：一切皆数据。

建模层是引擎与产品之间的唯一契约。换一家公司只换这批数据。
"""

from .loader import ModelError, load_model
from .schema import (
    Check,
    ColumnBinding,
    DedupRule,
    DictionaryEntry,
    LinkRule,
    Metric,
    Model,
    NodeExpr,
    ParseOptions,
    Platform,
    Predicate,
    SourceContract,
    StatementNode,
    Store,
    Template,
    ValueExpr,
    normalize_header,
    signature_of,
)

__all__ = [
    "Check", "ColumnBinding", "DedupRule", "DictionaryEntry", "LinkRule", "Metric",
    "Model", "ModelError", "NodeExpr", "ParseOptions", "Platform", "Predicate",
    "SourceContract", "StatementNode", "Store", "Template", "ValueExpr", "load_model",
    "normalize_header", "signature_of",
]
