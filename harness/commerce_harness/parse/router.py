from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

from .csv_profile import CsvStructureError, profile_csv
from .fingerprint import FormatFingerprint
from .models import ArchiveRoute, FileRoute, HeaderCandidate, TabularProfile
from .templates import DEFAULT_TEMPLATES, TemplateDefinition, normalize_header
from .xlsx import XlsxStructureError, profile_xlsx
from .zip_safe import SafeZipPolicy, inspect_zip, read_safe_member


class TemplateRoutingError(ValueError):
    pass


class NoTemplateMatchError(TemplateRoutingError):
    pass


class AmbiguousTemplateError(TemplateRoutingError):
    pass


class TemplateRouter:
    """Route only when a structural profile has exactly one finite match."""

    def __init__(
        self,
        templates: Iterable[TemplateDefinition] = DEFAULT_TEMPLATES,
        *,
        zip_policy: SafeZipPolicy | None = None,
    ) -> None:
        self.templates = tuple(templates)
        if not self.templates:
            raise ValueError("at least one template is required")
        self.zip_policy = zip_policy or SafeZipPolicy()

    def route_path(self, path: str | os.PathLike[str]) -> FileRoute | ArchiveRoute:
        source = Path(path)
        suffix = source.suffix.lower()
        if suffix == ".csv":
            return self.route_profile(profile_csv(source.read_bytes()))
        if suffix == ".xlsx":
            return self.route_profile(profile_xlsx(source))
        if suffix == ".zip":
            return self.route_archive(source)
        raise NoTemplateMatchError(f"unsupported file type: {suffix or '<none>'}")

    def route_bytes(self, name: str, data: bytes) -> FileRoute | ArchiveRoute:
        suffix = Path(name).suffix.lower()
        if suffix == ".csv":
            return self.route_profile(profile_csv(data, member_name=name))
        if suffix == ".xlsx":
            return self.route_profile(profile_xlsx(data, member_name=name))
        if suffix == ".zip":
            return self.route_archive(data)
        raise NoTemplateMatchError(f"unsupported file type: {suffix or '<none>'}")

    def route_profile(self, profile: TabularProfile) -> FileRoute:
        matches: list[tuple[TemplateDefinition, HeaderCandidate, dict[str, str]]] = []
        for location in profile.candidates:
            if location.sheet_hidden:
                continue
            for template in self.templates:
                field_mapping = template.match(location.headers)
                if field_mapping is not None:
                    matches.append((template, location, field_mapping))

        if not matches:
            raise NoTemplateMatchError("file structure did not match a supported template")
        if len(matches) != 1:
            labels = sorted(
                f"{template.template_id}@{location.locator}"
                for template, location, _ in matches
            )
            raise AmbiguousTemplateError(
                "file structure matched more than one template/header location: "
                + ", ".join(labels)
            )

        template, location, fields = matches[0]
        fingerprint = FormatFingerprint.from_structure(
            {
                "kind": profile.file_kind,
                "template_id": template.template_id,
                "headers": [normalize_header(header) for header in location.headers],
                "header_row": location.header_row,
                "sheet_name": location.sheet_name,
                "sheet_names": list(profile.sheet_names),
                "encoding": profile.encoding,
                "delimiter": profile.delimiter,
            }
        )
        return FileRoute(
            source_kind=template.source_kind,
            template_id=template.template_id,
            fields=fields,
            location=location,
            fingerprint=fingerprint,
        )

    def route_archive(self, source: bytes | str | os.PathLike[str]) -> ArchiveRoute:
        members = inspect_zip(source, policy=self.zip_policy)
        entries: list[FileRoute] = []
        unmatched: list[str] = []
        for member in members:
            suffix = Path(member.name).suffix.lower()
            if suffix not in {".csv", ".xlsx"}:
                unmatched.append(member.name)
                continue
            data = read_safe_member(source, member.name, policy=self.zip_policy)
            try:
                if suffix == ".csv":
                    profile = profile_csv(data, member_name=member.name)
                else:
                    profile = profile_xlsx(data, member_name=member.name)
                entries.append(self.route_profile(profile))
            except (NoTemplateMatchError, CsvStructureError, XlsxStructureError):
                unmatched.append(member.name)

        if not entries:
            raise NoTemplateMatchError("ZIP contained no uniquely routable supported data file")
        fingerprint = FormatFingerprint.from_structure(
            {
                "kind": "zip",
                "members": [
                    {
                        "name": member.name,
                        "compressed_size": member.compressed_size,
                        "uncompressed_size": member.uncompressed_size,
                    }
                    for member in members
                ],
                "routes": [
                    {
                        "member": entry.location.member_name,
                        "template_id": entry.template_id,
                        "fingerprint": entry.fingerprint.digest,
                    }
                    for entry in entries
                ],
                "unmatched": sorted(unmatched),
            }
        )
        return ArchiveRoute(
            entries=tuple(entries),
            unmatched_members=tuple(sorted(unmatched)),
            fingerprint=fingerprint,
        )
