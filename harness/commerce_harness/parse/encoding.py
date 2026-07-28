from __future__ import annotations

from dataclasses import dataclass


class CsvEncodingError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class EncodingDetection:
    encoding: str
    confidence: float
    bom: bool


_BOMS = (
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
)


def detect_csv_encoding(sample: bytes) -> EncodingDetection:
    if not sample:
        return EncodingDetection("utf-8", 1.0, False)
    for marker, encoding in _BOMS:
        if sample.startswith(marker):
            try:
                sample.decode(encoding, errors="strict")
            except UnicodeDecodeError as exc:
                raise CsvEncodingError(f"invalid {encoding} byte stream") from exc
            return EncodingDetection(encoding, 1.0, True)

    if all(byte < 0x80 for byte in sample):
        return EncodingDetection("utf-8", 1.0, False)

    candidates: list[tuple[float, str]] = []
    for encoding in ("utf-8", "gb18030", "big5"):
        try:
            text = sample.decode(encoding, errors="strict")
        except UnicodeDecodeError:
            continue
        candidates.append((_text_quality(text, encoding), encoding))
    if not candidates:
        raise CsvEncodingError("CSV sample is not valid UTF-8, GB18030, or Big5")
    score, encoding = max(candidates, key=lambda candidate: candidate[0])
    return EncodingDetection(encoding, min(max(score, 0.0), 1.0), False)


def _text_quality(text: str, encoding: str) -> float:
    if not text:
        return 1.0
    controls = sum(
        1 for char in text if ord(char) < 32 and char not in {"\r", "\n", "\t"}
    )
    chinese = sum(1 for char in text if "\u3400" <= char <= "\u9fff")
    printable_ratio = (len(text) - controls) / len(text)
    chinese_bonus = min(chinese / max(len(text), 1), 0.2)
    utf8_bonus = 0.03 if encoding == "utf-8" else 0.0
    return printable_ratio * 0.8 + chinese_bonus + utf8_bonus
