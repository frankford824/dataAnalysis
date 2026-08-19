"""改配置。

模型是一目录 YAML，人也能直接编辑，但不该只能这样改——要人去改 YAML 才能
登记一家店，这就不是产品而是脚手架了。所以配置项要能从界面和命令行改，
写回同一份文件。

写回有四条硬要求：

  一、保住注释。stores.yaml 里每条主体都记着它是从哪张表哪一列读出来的，
      那些取证说明比字段本身更值钱。PyYAML 一写回全部冲掉，所以用 ruamel。
  二、不重排没动过的部分。光保住注释还不够：ruamel 回写整个文档会把嵌套序列的
      缩进从 4 格改成 2 格，接一张表产出 600 行改动，改了什么根本审不出来。
      所以新增记录走文本追加，只碰文件末尾。
  三、写完必须能通过模型校验。校验不过就回滚，绝不留下一个加载不了的模型——
      那会让整个系统起不来。
  四、原子写。写临时文件再改名，中途断电也不会剩下半个文件。
"""

from __future__ import annotations

import io
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from .loader import FEE_RULE_COLUMNS, FEE_RULES, ModelError, load_model
from .schema import FeeRule, SourceContract, Store, Template
from .transaction import locked_model

#: 店铺的哪些字段允许改。
#:
#: id 不在里面：它是关联键，改了等于换一家店，历史账会对不上。name 也不在：
#: 认文件靠它，要改名就加 aliases，把旧名留着，否则以前交过的文件立刻认不出。
#:
#: 两个提成字段在里面，是因为口径本来就逐店不同（实测同一家公司三家店两套亏损政策）。
#: 不让改的话，不一致的那几家只能靠人事后手改数，而手改的数没有留痕。填错值不会
#: 落盘：写回后立刻重新加载校验，指向没标基数的行或者一个不认识的政策都会回滚。
EDITABLE = ("entity", "entity_tax_id", "archived", "aliases",
            "commission_base", "commission_on_loss", "note")


def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    # 主体名和说明都比较长，默认 80 列会把中文折得很难读。
    y.width = 4096
    return y


@locked_model
def update_store(model_dir: str | Path, store_id: str, changes: dict[str, Any]) -> Store:
    """改一家店的配置。返回改完之后的这家店。"""
    root = Path(model_dir)
    path = root / "stores.yaml"
    if not path.exists():
        raise ModelError(f"店铺注册表不存在：{path}")

    bad = [k for k in changes if k not in EDITABLE]
    if bad:
        raise ModelError(
            f"这些字段不让改：{'、'.join(bad)}。可改的是 {'、'.join(EDITABLE)}。"
            f"id 是关联键，name 是认文件的依据，改了历史账就对不上——要改名请加别名。"
        )

    y = _yaml()
    with path.open(encoding="utf-8") as fh:
        doc = y.load(fh)
    if not isinstance(doc, list):
        raise ModelError(f"{path} 顶层必须是列表")

    entry = next((e for e in doc if isinstance(e, dict) and e.get("id") == store_id), None)
    if entry is None:
        raise ModelError(f"注册表里没有 {store_id} 这家店")

    for key, value in changes.items():
        if value is None:
            entry.pop(key, None)
        else:
            _put(entry, key, list(value) if key == "aliases" else value)

    _write_back(root, path, doc, y)
    return load_model(root).store(store_id)


def _put(entry: Any, key: str, value: Any) -> None:
    """往一条店铺记录里写字段，新字段插在 note 前面。

    直接赋值会把新字段追加到最后，而 note 往往是个折叠的多行块。追加的结果是
    新字段孤零零地跟在几行缩进文字后面、和下一家店之间只隔一个空行——文件仍然
    合法，但看的人会以为它属于下一家店。配置文件是给人读的，读错了就会改错。
    """
    if key in entry or "note" not in entry:
        entry[key] = value
        return
    entry.insert(list(entry).index("note"), key, value)


@locked_model
def add_store(model_dir: str | Path, store: Store) -> Store:
    """登记一家新店。"""
    root = Path(model_dir)
    path = root / "stores.yaml"
    y = _yaml()
    doc: Any = []
    if path.exists():
        with path.open(encoding="utf-8") as fh:
            doc = y.load(fh) or []
    if not isinstance(doc, list):
        raise ModelError(f"{path} 顶层必须是列表")
    if any(isinstance(e, dict) and e.get("id") == store.id for e in doc):
        raise ModelError(f"{store.id} 已经登记过了")
    if any(isinstance(e, dict) and e.get("name") == store.name for e in doc):
        raise ModelError(f"已经有一家店叫 {store.name} 了。同名会让文件认不清归谁。")

    entry: dict[str, Any] = {"id": store.id, "name": store.name, "platform": store.platform}
    for key in EDITABLE:
        value = getattr(store, key)
        if key == "aliases" and value:
            entry[key] = list(value)
        elif key == "archived" and value:
            entry[key] = True
        elif key not in ("aliases", "archived") and value:
            entry[key] = value
    doc.append(entry)

    _write_back(root, path, doc, y)
    return load_model(root).store(store.id)


# --------------------------------------------------------------------------- #
# 接一张新表
# --------------------------------------------------------------------------- #

#: 模板 id 只允许这些字符。它会进快照、进日志、进文件名，留个空格或斜杠迟早出事。
_ID_OK = re.compile(r"^[a-z][a-z0-9_]*$")


@locked_model
def add_template(
    model_dir: str | Path,
    template: Template,
    *,
    source: SourceContract | None = None,
    by: str = "",
) -> Template:
    """登记一个新模板；`source` 不为空就顺带登记新数据源。

    两个一起写是必须的：模板必须指向一个存在的数据源，分两步写的话中间那一刻
    模型是校验不过的，而校验不过就等于整个系统起不来。要么都成，要么都不成。

    这个函数是「以后所有店铺自助接入」真正落地的地方。在它之前，接一张新表要有人
    打开 templates.yaml 手写映射——那意味着接第四家店得等排期。
    """
    root = Path(model_dir)
    if not _ID_OK.match(template.id):
        raise ModelError(
            f"模板 id {template.id!r} 不合规：只能用小写字母、数字和下划线，且以字母开头。"
            f"它会进快照和日志，带空格或斜杠迟早出事。"
        )
    if not template.bindings:
        raise ModelError("模板一列都没映射，接上也取不到任何数据")
    if not template.match_columns:
        raise ModelError("模板没有识别签名（match_columns），认不出哪张表该用它")

    model = load_model(root)
    if any(t.id == template.id for t in model.templates):
        raise ModelError(f"模板 {template.id} 已经有了")
    if clash := next((t for t in model.templates if t.signature == template.signature), None):
        raise ModelError(
            f"这套识别签名和「{clash.id}」一样，两个模板会同时命中同一张表。"
            f"要么复用它，要么让签名里带上能区分的列。"
        )
    if source is None and not any(s.id == template.source for s in model.sources):
        raise ModelError(
            f"模板指向的数据源 {template.source} 不存在。"
            f"要接新数据源就把数据源一起提交，别留个悬空引用。"
        )

    writes: list[tuple[Path, str]] = []

    if source is not None:
        if any(s.id == source.id for s in model.sources):
            raise ModelError(f"数据源 {source.id} 已经有了")
        if source.id != template.source:
            raise ModelError(f"模板挂在 {template.source}，但提交的数据源是 {source.id}")
        writes.append((root / "sources.yaml", _entry_text(_source_entry(source))))

    writes.append((root / "templates.yaml", _entry_text(_template_entry(template, by))))

    _append_all(root, writes)
    return load_model(root).template(template.id)


@locked_model
def drop_template(model_dir: str | Path, template_id: str, *, source: str = "") -> None:
    """把一个模板（连同顺带登记的数据源）撤掉。

    这是给人主动撤模板用的。接表落库失败不走这里——那条路是按字节还原原文件
    （见 onboard.land），因为删一条记录必须重写整个文档，而重写会顺手把嵌套缩进
    全改一遍，退回来的文件跟原来差几百行，反而看不出到底退没退。
    """
    root = Path(model_dir)
    y = _yaml()
    writes: list[tuple[Path, Any]] = []
    for path, key in ((root / "templates.yaml", template_id), (root / "sources.yaml", source)):
        if not key or not path.exists():
            continue
        with path.open(encoding="utf-8") as fh:
            doc = y.load(fh) or []
        if not isinstance(doc, list):
            continue
        # 就地删，不能用列表推导重建。重建出来的是普通 list，ruamel 挂在序列上的
        # 注释全丢——实测这一下删掉了 templates.yaml 的文件头（那段说明列名一个字
        # 都不能改的取证），以及「脊柱」这类分节注释。删一个模板，赔掉整份文档。
        hit = next(
            (i for i, e in enumerate(doc) if isinstance(e, dict) and e.get("id") == key), None
        )
        if hit is not None:
            del doc[hit]
            writes.append((path, doc))
    if writes:
        _write_all(root, writes, y)


#: 提成配置的列。顺序就是导出和模板文件里的顺序。
COMMISSION_COLUMNS = (
    "effective_from", "store", "product_id", "product_name",
    "person", "share", "total_rate", "note",
)

#: 中文表头。业务在 Excel 里维护，让他们对着中文填，别背英文列名。
COMMISSION_HEADERS = {
    "effective_from": "生效日期", "store": "店铺", "product_id": "商品ID",
    "product_name": "商品名称", "person": "人员", "share": "子提成率",
    "total_rate": "总提成率", "note": "备注",
}

#: 中文表头反查。上传的表两种表头都认。
_ALIASES = {
    "生效日期": "effective_from", "新增日期": "effective_from", "变更日期": "effective_from",
    "店铺": "store", "店铺id": "store", "归属店铺": "store", "店铺编号": "store",
    "商品id": "product_id", "商品编号": "product_id",
    "商品名称": "product_name", "商品名": "product_name",
    "人员": "person", "人名": "person", "姓名": "person", "负责人": "person",
    "子提成率": "share", "提成比例": "share", "个人提成率": "share",
    "总提成率": "total_rate", "商品总提成率": "total_rate",
    "备注": "note",
}


def commission_column(header: str) -> str:
    """把一个表头翻成配置列名。认不出来返回空串。"""
    key = (header or "").strip().lower().replace(" ", "").replace("　", "")
    if key in COMMISSION_COLUMNS:
        return key
    return _ALIASES.get(key, "")


@locked_model
def replace_commission(model_dir: str | Path, rows: list[dict[str, str]]) -> int:
    """整份换掉提成配置。返回写进去的条数。

    为什么是整份换而不是增量改
    ------------------------
    提成配置的正确性是**整份**的：同一版里几个人的比例加起来要等于总提成率。
    单条改动没法校验这件事——「张三 3% 改 4%」这一条自己看永远合法。所以界面上
    的动作就是「传一份新的」，传上来先整体校验，过了才落盘。

    历史不会因此丢失：生效制下改配置本来就是往表里加行，旧行原样留着，
    所以「整份」里包含全部历史版本。真正被这个函数保护的是「不能存进一份
    自相矛盾的配置」。

    写不成功就还原成原样，一个字节都不留下——校验不过的配置一旦落盘，
    下次加载模型会直接失败，整个系统起不来。

    这里不记「谁改的」。CSV 没有注释语法，落款只能挤进数据行里去污染业务数据；
    而改店铺、接模板同样没记，单给这一处加一套半吊子的留痕，只会让人以为
    配置改动是有审计的。要记就该整套配置变更一起记，那是另一件事。
    """
    root = Path(model_dir)
    path = root / "commission.csv"
    before = {path: (path.read_bytes() if path.exists() else None)}
    text = _commission_text(rows)
    try:
        _atomic_text(path, text)
        model = load_model(root)
    except BaseException:
        _restore(before)
        raise
    return len(model.commission)


def _commission_text(rows: list[dict[str, str]]) -> str:
    out = io.StringIO()
    out.write(",".join(COMMISSION_COLUMNS) + "\n")
    for r in rows:
        out.write(",".join(csv_cell(r.get(c, "")) for c in COMMISSION_COLUMNS) + "\n")
    return out.getvalue()


@locked_model
def replace_fee_rules(model_dir: str | Path, rules: list[FeeRule]) -> int:
    """整份换掉界面配的归类规则。返回写进去的条数。

    为什么是整份换
    --------------
    规则链的语义是「第一条命中的生效」，所以正确性在整份的次序上，不在单条上。
    「把第 3 条挪到第 1 条」这种动作没法按增量校验——单看每一条都合法。
    传上来先整体加载校验，过了才落盘。校验不过还原成原样，一个字节都不留下：
    一份加载不了的 fee-rules.csv 会让整个系统起不来。

    条与条之间的相对次序就是文件里的行序，写回时不得按任何一列排序。
    """
    root = Path(model_dir)
    path = root / FEE_RULES
    before = {path: (path.read_bytes() if path.exists() else None)}
    text = _fee_rules_text(rules)
    try:
        _atomic_text(path, text)
        model = load_model(root)
    except BaseException:
        _restore(before)
        raise
    return len(model.fee_rules)


def _fee_rules_text(rules: list[FeeRule]) -> str:
    out = io.StringIO()
    out.write(",".join(FEE_RULE_COLUMNS) + "\n")
    for r in rules:
        cells = []
        for col in FEE_RULE_COLUMNS:
            value = getattr(r, col)
            if isinstance(value, bool):
                cells.append("1" if value else "")
            else:
                cells.append(csv_cell(value))
        out.write(",".join(cells) + "\n")
    return out.getvalue()


def csv_cell(value: Any) -> str:
    s = "" if value is None else str(value).strip()
    if any(ch in s for ch in ',"\n\r'):
        return '"' + s.replace('"', '""') + '"'
    return s


def _source_entry(s: SourceContract) -> CommentedMap:
    m = CommentedMap()
    m["id"] = s.id
    m["name"] = s.name
    m["owner_role"] = s.owner_role
    m["cadence"] = s.cadence
    if s.is_spine:
        m["is_spine"] = True
    if not s.required_for_close:
        m["required_for_close"] = False
    if s.company_wide:
        m["company_wide"] = True
    if s.filename_hints:
        m["filename_hints"] = list(s.filename_hints)
    if s.dedupe_key:
        m["dedupe_key"] = list(s.dedupe_key)
    if s.note:
        m["note"] = s.note
    return m


def _flow(obj: Any) -> Any:
    """一行写完。

    模板动辄十几个绑定，一个绑定摊成 5 行就是 80 行，而手写的那些是一行一个。
    机器写的跟手写的两种排法混在一个文件里，人一眼就觉得这文件被搞乱了，
    然后就不敢再用向导。
    """
    obj.fa.set_flow_style()
    return obj


def _template_entry(t: Template, by: str) -> CommentedMap:
    """把模板写成 YAML。

    落一条来历注释：这个文件里每条手写模板都记着列名是从哪张实测表抄来的，
    机器写进去的那条不留痕，以后没人知道它是谁在什么时候按什么依据加的。
    """
    m = CommentedMap()
    m["id"] = t.id
    m["source"] = t.source
    if t.name:
        m["name"] = t.name
    m["match_columns"] = _flow(CommentedSeq(t.match_columns))
    if t.parse.header_row:
        m["parse"] = _flow(CommentedMap({"header_row": t.parse.header_row}))
    bindings = CommentedSeq()
    for b in t.bindings:
        entry = CommentedMap()
        entry["role"] = b.role
        entry["columns"] = _flow(CommentedSeq(b.columns))
        if b.occurrence:
            entry["occurrence"] = b.occurrence
        if b.kind:
            entry["kind"] = b.kind
        if b.negate:
            entry["negate"] = True
        if not b.required:
            entry["required"] = False
        bindings.append(_flow(entry))
    m["bindings"] = bindings
    if t.time_slots:
        m["time_slots"] = _flow(CommentedMap(t.time_slots))
    if t.total_row_marker:
        m["total_row_marker"] = t.total_row_marker
    if t.note:
        m["note"] = t.note
    m.yaml_set_start_comment(
        f"接表向导登记" + (f"，操作人 {by}" if by else "") + "。列名照实测表头原样抄录。\n"
    )
    return m


def _entry_text(entry: CommentedMap) -> str:
    """把一条新记录渲染成能直接贴进文件的文本。

    渲染成文本而不是回写整个文档，是因为这些文件是人手写的，注释比字段本身值钱：
    templates.yaml 的文件头记着「列名一个字都不能改」以及那次少写一个「编」字导致
    覆盖率掉到 0 的取证。整文档回写即使保住了注释，也会把嵌套序列的缩进从 4 格改成
    2 格——接一张表产出 600 行改动，没人能审。追加只碰文件末尾。

    缩进对齐现有风格（根层横杠在第 0 列，嵌套横杠在第 4 列）。ruamel 的 offset 是
    全局的，会把根层也顶进去 2 格，所以渲染完整体退回来。
    """
    y = _yaml()
    y.indent(mapping=2, sequence=4, offset=2)
    buf = io.StringIO()
    y.dump(CommentedSeq([entry]), buf)
    return "".join(
        line[2:] if line.startswith("  ") else line
        for line in buf.getvalue().splitlines(keepends=True)
    )


def _append_all(root: Path, writes: list[tuple[Path, str]]) -> None:
    """把几条记录追加到各自的文件末尾，校验不过全部还原。

    追加是纯文本操作，所以文件里原有的一个字节都不会动。
    """
    before = {path: (path.read_bytes() if path.exists() else None) for path, _ in writes}
    try:
        for path, text in writes:
            old = path.read_text(encoding="utf-8") if path.exists() else ""
            if old and not old.endswith("\n"):
                old += "\n"
            _atomic_text(path, old + ("\n" if old else "") + text)
        load_model(root)
    except BaseException:
        _restore(before)
        raise


def _restore(before: dict[Path, bytes | None]) -> None:
    for path, raw in before.items():
        if raw is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(raw)


def _atomic_text(path: Path, text: str) -> None:
    tmp = _tempfile(path)
    try:
        with tmp:
            tmp.write(text)
        os.replace(tmp.name, path)
    except BaseException:
        Path(tmp.name).unlink(missing_ok=True)
        raise


def _tempfile(path: Path):
    """写临时文件，写完改名。中途断电也不会剩下半个文件。

    `newline=""` 是关键的一个字：不给它，Windows 上 Python 会把每个 `\\n` 换成
    `\\r\\n`。于是在界面上改一个店铺主体，回写时整份 stores.yaml 的换行符全变了——
    模型照样加载得了，但那份文件从此和仓库里的逐行不同。实测线上就是这样：
    改了一个字段，比对下来「48 行全改过」，真正改的那一行反而看不出来。

    要求写回不重排、保住注释，却在这里把整份文件的字节改一遍，前面的功夫就白做了。
    """
    return tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="", dir=path.parent,
        prefix=f".{path.stem}-", suffix=".yaml", delete=False,
    )


def _write_all(root: Path, writes: list[tuple[Path, Any]], y: YAML) -> None:
    """一组文件一起写，校验不过全部还原。

    分开写会留下中间态：数据源写进去了、模板没写成，模型照样加载不了。
    """
    before = {path: (path.read_bytes() if path.exists() else None) for path, _ in writes}
    try:
        for path, doc in writes:
            _atomic(path, doc, y)
        load_model(root)
    except BaseException:
        _restore(before)
        raise


def _atomic(path: Path, doc: Any, y: YAML) -> None:
    tmp = _tempfile(path)
    try:
        with tmp:
            y.dump(doc, tmp)
        os.replace(tmp.name, path)
    except BaseException:
        Path(tmp.name).unlink(missing_ok=True)
        raise


def _write_back(root: Path, path: Path, doc: Any, y: YAML) -> None:
    """原子写，然后校验；校验不过就还原。

    模型加载不了的话整个系统起不来，所以宁可拒绝这次修改，也不能留个坏文件。
    """
    _write_all(root, [(path, doc)], y)
