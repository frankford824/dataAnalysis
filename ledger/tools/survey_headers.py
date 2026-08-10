"""扫一遍真实快照的表头签名，用来写模板。

这个脚本属于建模工具而不是产品的一部分：它把"这家公司实际有哪些表头"变成可读清单，
人（或 AI）据此写模板。它同时是解析原语的一次实战检验——2812 个真实文件里
只要有一个读不出来，就说明解析层还有洞。
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ledger.engine.parse import ParseError, read_headers  # noqa: E402
from ledger.model.schema import normalize_header, signature_of  # noqa: E402

WORKBENCH = Path("/home/wsfwk/fa-workbench")
MANIFESTS = WORKBENCH / "snapshots/manifests/snapshots"


def object_path(sha: str) -> Path:
    return WORKBENCH / "snapshots/objects/sha256" / sha[:2] / sha


def bucket_of(uri: str) -> str:
    """按源路径里的目录分桶。这就是这家公司实际的数据分类。"""
    parts = uri.replace("/", "\\").split("\\")
    for i, p in enumerate(parts):
        if p == "内贸" and i + 1 < len(parts):
            return parts[i + 1]
    return parts[-2] if len(parts) > 1 else "?"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", help="只看某个目录")
    ap.add_argument("--limit", type=int, default=0, help="每桶最多看几个文件")
    ap.add_argument("--out", default="/tmp/header-survey.json")
    ap.add_argument("--show", type=int, default=3, help="每种签名打印几个列名样例")
    args = ap.parse_args()

    manifests = []
    for f in MANIFESTS.glob("*.json"):
        d = json.loads(f.read_text(encoding="utf-8"))
        uri = (d.get("source") or {}).get("uri", "")
        manifests.append(
            {
                "name": d.get("original_name", ""),
                "sha": d.get("content_sha256", ""),
                "size": d.get("byte_size", 0),
                "bucket": bucket_of(uri),
                "uri": uri,
            }
        )

    by_bucket: dict[str, list[dict]] = collections.defaultdict(list)
    for m in manifests:
        by_bucket[m["bucket"]].append(m)

    survey: dict[str, dict] = {}
    failures: list[dict] = []

    buckets = [args.bucket] if args.bucket else sorted(by_bucket, key=lambda b: -len(by_bucket[b]))
    for bucket in buckets:
        items = by_bucket.get(bucket, [])
        if args.limit:
            items = items[: args.limit]
        sigs: dict[str, dict] = {}
        for item in items:
            path = object_path(item["sha"])
            if not path.exists():
                failures.append({**item, "error": "对象文件不在本地"})
                continue
            # 快照没有扩展名，用原始名的后缀建一个软链接名给解析器认格式。
            suffix = Path(item["name"]).suffix.lower()
            probe = path
            if suffix in (".xlsx", ".xlsm", ".csv", ".txt", ".tsv"):
                probe = Path("/tmp/_probe" + suffix)
                if probe.exists() or probe.is_symlink():
                    probe.unlink()
                probe.symlink_to(path)
            try:
                header_sets = read_headers(probe)
            except (ParseError, Exception) as exc:  # noqa: BLE001
                failures.append({**item, "error": f"{type(exc).__name__}: {exc}"[:200]})
                continue
            for headers in header_sets:
                if not headers:
                    continue
                sig = signature_of(headers)
                rec = sigs.setdefault(
                    sig,
                    {"columns": headers, "width": len(headers), "files": 0, "examples": [],
                     "duplicate_columns": _dupes(headers)},
                )
                rec["files"] += 1
                if len(rec["examples"]) < args.show:
                    rec["examples"].append(item["name"])
        survey[bucket] = {
            "files_scanned": len(items),
            "signatures": dict(sorted(sigs.items(), key=lambda kv: -kv[1]["files"])),
        }

    Path(args.out).write_text(json.dumps({"survey": survey, "failures": failures},
                                         ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"扫描 {sum(v['files_scanned'] for v in survey.values())} 个文件，"
          f"失败 {len(failures)} 个\n")
    for bucket, info in survey.items():
        sigs = info["signatures"]
        print(f"{'=' * 78}\n{bucket}：{info['files_scanned']} 个文件，{len(sigs)} 种表头签名")
        for sig, rec in sigs.items():
            dup = f"  重复列名 {rec['duplicate_columns']}" if rec["duplicate_columns"] else ""
            print(f"  {sig}  {rec['width']:>3} 列  {rec['files']:>4} 文件{dup}")
            print(f"      {' | '.join(rec['columns'][:14])}"
                  + (" | …" if len(rec["columns"]) > 14 else ""))
            print(f"      例: {rec['examples'][0]}")
    if failures:
        print(f"\n{'=' * 78}\n读不出来的文件 {len(failures)} 个：")
        for f in failures[:20]:
            print(f"  {f['name'][:50]:<52} {f['error'][:90]}")
    print(f"\n完整结果写到 {args.out}")
    return 0


def _dupes(headers: list[str]) -> list[str]:
    counts = collections.Counter(normalize_header(h) for h in headers if normalize_header(h))
    return [h for h, c in counts.items() if c > 1]


if __name__ == "__main__":
    raise SystemExit(main())
