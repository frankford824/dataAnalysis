"""CLI entry point: python -m verifier path/to/report.json"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from verifier.core import verify


def main() -> None:
    if len(sys.argv) < 2:
        print("用法: python -m verifier <report.json>", file=sys.stderr)
        sys.exit(2)

    report_path = Path(sys.argv[1])
    result = verify(report_path)

    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))

    if not result.passed:
        sys.exit(1)


if __name__ == "__main__":
    main()
