"""Ed25519 detached sign/verify for pack integrity.

Signing hierarchy:
1. ``cryptography`` library (preferred)
2. ``nacl`` (PyNaCl)
3. HMAC-SHA256, development only and never selected implicitly

The canonical byte stream covers *every* file in the pack tree, not just the
top-level JSON: rules live in ``rules/`` and would otherwise be modifiable
without breaking the signature.
"""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path
from typing import Any

DEV_HMAC_BACKEND = "dev-hmac"
ED25519_BACKENDS = frozenset({"ed25519-cryptography", "ed25519-nacl"})
SIGNATURE_EXCLUDED_NAMES = frozenset({"pack.sig"})

_BACKEND_CACHE: str | None = None


class PackSignatureError(RuntimeError):
    """Raised when signing or verification cannot be performed safely."""


def _detect_backend() -> str:
    global _BACKEND_CACHE
    if _BACKEND_CACHE is not None:
        return _BACKEND_CACHE
    try:
        import cryptography.hazmat.primitives.asymmetric.ed25519  # noqa: F401

        _BACKEND_CACHE = "ed25519-cryptography"
        return _BACKEND_CACHE
    except ImportError:
        pass
    try:
        import nacl.signing  # noqa: F401

        _BACKEND_CACHE = "ed25519-nacl"
        return _BACKEND_CACHE
    except ImportError:
        pass
    _BACKEND_CACHE = DEV_HMAC_BACKEND
    return _BACKEND_CACHE


def get_backend() -> str:
    """Return the name of the strongest available signing backend."""
    return _detect_backend()


def pack_files(pack_dir: str | Path) -> list[Path]:
    """Every signed file in the pack, in a stable order."""
    pack_path = Path(pack_dir)
    return sorted(
        (
            path
            for path in pack_path.rglob("*")
            if path.is_file() and path.name not in SIGNATURE_EXCLUDED_NAMES
        ),
        key=lambda path: path.relative_to(pack_path).as_posix(),
    )


def _canonical_pack_bytes(pack_dir: str | Path) -> bytes:
    """Produce canonical bytes for signing from the whole pack tree.

    Each entry contributes its POSIX-relative path, its length and its bytes so
    that neither renaming nor concatenation boundaries can be exploited.
    """
    pack_path = Path(pack_dir)
    digest = hashlib.sha256()
    for path in pack_files(pack_path):
        relative = path.relative_to(pack_path).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.digest()


def pack_content_sha256(pack_dir: str | Path) -> str:
    """Content hash over the whole pack tree."""
    return hashlib.sha256(_canonical_pack_bytes(pack_dir)).hexdigest()


def sign_pack(
    pack_dir: str | Path,
    *,
    private_key: Any = None,
    hmac_key: bytes | None = None,
    allow_dev_hmac: bool = False,
) -> dict[str, Any]:
    """Create a detached signature for the pack.

    Returns a dict suitable for writing to ``pack.sig`` (as JSON). Falling back
    to HMAC requires ``allow_dev_hmac=True`` and an explicit key, so a missing
    private key can never silently downgrade a published pack.
    """
    canonical = _canonical_pack_bytes(pack_dir)
    content_sha256 = hashlib.sha256(canonical).hexdigest()

    if private_key is not None:
        backend = _detect_backend()
        if backend == "ed25519-cryptography":
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PrivateKey,
            )

            if not isinstance(private_key, Ed25519PrivateKey):
                raise TypeError("private_key must be an Ed25519PrivateKey")
            return {
                "backend": backend,
                "algorithm": "Ed25519",
                "signature": private_key.sign(canonical).hex(),
                "public_key": private_key.public_key().public_bytes_raw().hex(),
                "content_sha256": content_sha256,
            }
        if backend == "ed25519-nacl":
            import nacl.signing

            if not isinstance(private_key, nacl.signing.SigningKey):
                raise TypeError("private_key must be a nacl.signing.SigningKey")
            return {
                "backend": backend,
                "algorithm": "Ed25519",
                "signature": private_key.sign(canonical).signature.hex(),
                "public_key": private_key.verify_key.encode().hex(),
                "content_sha256": content_sha256,
            }
        raise PackSignatureError(
            "no Ed25519 backend available; install 'cryptography' or 'pynacl'"
        )

    if not allow_dev_hmac:
        raise PackSignatureError(
            "signing requires an Ed25519 private key; "
            "pass allow_dev_hmac=True with an explicit hmac_key for development"
        )
    if not hmac_key:
        raise PackSignatureError("dev-hmac signing requires an explicit hmac_key")
    return {
        "backend": DEV_HMAC_BACKEND,
        "algorithm": "HMAC-SHA256",
        "signature": hmac.new(hmac_key, canonical, hashlib.sha256).hexdigest(),
        "content_sha256": content_sha256,
        "warning": "HMAC-SHA256 is NOT suitable for production; use Ed25519",
    }


def verify_pack(
    pack_dir: str | Path,
    sig_data: dict[str, Any],
    *,
    public_key: Any = None,
    hmac_key: bytes | None = None,
    allow_dev_hmac: bool = False,
) -> bool:
    """Verify a detached signature against the pack contents.

    A matching ``content_sha256`` alone is never sufficient: an unknown backend
    or a missing key returns ``False`` rather than trusting the hash, because a
    hash carried inside the signature file proves nothing about its origin.
    """
    canonical = _canonical_pack_bytes(pack_dir)

    if not hmac.compare_digest(
        hashlib.sha256(canonical).hexdigest(),
        str(sig_data.get("content_sha256", "")),
    ):
        return False

    backend = str(sig_data.get("backend", ""))
    signature_hex = str(sig_data.get("signature", ""))

    if backend == "ed25519-cryptography":
        if public_key is None:
            return False
        try:
            public_key.verify(bytes.fromhex(signature_hex), canonical)
        except Exception:
            return False
        return True

    if backend == "ed25519-nacl":
        if public_key is None:
            return False
        try:
            import nacl.signing

            verify_key = (
                nacl.signing.VerifyKey(public_key)
                if isinstance(public_key, bytes)
                else public_key
            )
            verify_key.verify(canonical, bytes.fromhex(signature_hex))
        except Exception:
            return False
        return True

    if backend == DEV_HMAC_BACKEND:
        if not allow_dev_hmac or not hmac_key:
            return False
        expected = hmac.new(hmac_key, canonical, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature_hex)

    return False
