from __future__ import annotations

import csv
import io

from .encoding import detect_csv_encoding
from .models import HeaderCandidate, TabularProfile


class CsvStructureError(ValueError):
    pass


def profile_csv(
    data: bytes,
    *,
    member_name: str | None = None,
    max_header_rows: int = 20,
    max_columns: int = 256,
) -> TabularProfile:
    stream = io.BytesIO(data)
    first_line = stream.readline(16 * 1024)
    detection = detect_csv_encoding(first_line)
    decoded_lines: list[str] = []
    stream.seek(0)
    for _ in range(max_header_rows):
        line = stream.readline(1024 * 1024)
        if not line:
            break
        try:
            decoded_lines.append(line.decode(detection.encoding, errors="strict"))
        except UnicodeDecodeError:
            # Structural profiling only needs the header. Mixed/corrupt later rows
            # remain a normalization gate and are never silently repaired here.
            continue
    text = "".join(decoded_lines)
    delimiter = _detect_header_delimiter(decoded_lines) or _detect_delimiter(
        text[: 128 * 1024]
    )
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    candidates: list[HeaderCandidate] = []
    for row_number, row in enumerate(reader, start=1):
        if row_number > max_header_rows:
            break
        headers = tuple(str(value).strip() for value in row[:max_columns])
        nonempty = [value for value in headers if value]
        if len(nonempty) >= 2:
            candidates.append(
                HeaderCandidate(
                    headers=headers,
                    header_row=row_number,
                    member_name=member_name,
                )
            )
    if not candidates:
        raise CsvStructureError("no plausible header row found in CSV")
    return TabularProfile(
        file_kind="csv",
        candidates=tuple(candidates),
        encoding=detection.encoding,
        delimiter=delimiter,
    )


def _detect_delimiter(text: str) -> str:
    try:
        dialect = csv.Sniffer().sniff(text, delimiters=",\t;|")
    except csv.Error as exc:
        counts = {delimiter: text.count(delimiter) for delimiter in ",\t;|"}
        delimiter, count = max(counts.items(), key=lambda item: item[1])
        if count == 0:
            raise CsvStructureError("unable to detect CSV delimiter") from exc
        return delimiter
    return str(dialect.delimiter)


def _detect_header_delimiter(lines: list[str]) -> str | None:
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        counts = {delimiter: stripped.count(delimiter) for delimiter in ",\t;|"}
        delimiter, count = max(counts.items(), key=lambda item: item[1])
        if count >= 2:
            return delimiter
    return None
