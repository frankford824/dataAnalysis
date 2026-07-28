"""Safe structural profiling and finite-template routing."""

from .encoding import EncodingDetection, detect_csv_encoding
from .fingerprint import FormatFingerprint
from .models import ArchiveRoute, FileRoute, HeaderCandidate, SourceKind, TabularProfile
from .router import AmbiguousTemplateError, NoTemplateMatchError, TemplateRouter
from .templates import DEFAULT_TEMPLATES, TemplateDefinition
from .zip_safe import SafeZipPolicy, UnsafeZipError

__all__ = [
    "AmbiguousTemplateError",
    "ArchiveRoute",
    "DEFAULT_TEMPLATES",
    "EncodingDetection",
    "FileRoute",
    "FormatFingerprint",
    "HeaderCandidate",
    "NoTemplateMatchError",
    "SafeZipPolicy",
    "SourceKind",
    "TabularProfile",
    "TemplateDefinition",
    "TemplateRouter",
    "UnsafeZipError",
    "detect_csv_encoding",
]
