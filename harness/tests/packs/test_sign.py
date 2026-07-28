"""Tests for pack signing: coverage, tamper detection, and dev-hmac refusal."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from commerce_harness.packs.sign import (
    PackSignatureError,
    get_backend,
    pack_content_sha256,
    pack_files,
    sign_pack,
    verify_pack,
)


def _make_pack(tmp_path: Path) -> Path:
    pack_dir = tmp_path / "pack"
    (pack_dir / "rules").mkdir(parents=True)
    (pack_dir / "pack.json").write_text(
        json.dumps({
            "pack_id": "test", "domain": "test", "version": "0.1.0",
            "publisher": "test", "engine_compat": {"min": "0.1.0"},
        }),
        encoding="utf-8",
    )
    (pack_dir / "rules" / "one.json").write_text(
        json.dumps({"rule_id": "r1", "action": "route"}), encoding="utf-8"
    )
    return pack_dir


class TestSignatureCoverage:
    def test_nested_files_are_covered(self, tmp_path):
        pack_dir = _make_pack(tmp_path)
        names = [
            path.relative_to(pack_dir).as_posix() for path in pack_files(pack_dir)
        ]
        assert names == ["pack.json", "rules/one.json"]

    def test_detached_signature_is_excluded_from_its_own_input(self, tmp_path):
        pack_dir = _make_pack(tmp_path)
        before = pack_content_sha256(pack_dir)
        (pack_dir / "pack.sig").write_text("{}", encoding="utf-8")
        assert pack_content_sha256(pack_dir) == before

    def test_tampering_with_a_nested_rule_breaks_the_signature(self, tmp_path):
        pack_dir = _make_pack(tmp_path)
        key = Ed25519PrivateKey.generate()
        sig = sign_pack(pack_dir, private_key=key)
        assert verify_pack(pack_dir, sig, public_key=key.public_key()) is True

        (pack_dir / "rules" / "one.json").write_text(
            json.dumps({"rule_id": "r1", "action": "route", "participation": "excluded"}),
            encoding="utf-8",
        )
        assert verify_pack(pack_dir, sig, public_key=key.public_key()) is False

    def test_renaming_a_file_breaks_the_signature(self, tmp_path):
        pack_dir = _make_pack(tmp_path)
        key = Ed25519PrivateKey.generate()
        sig = sign_pack(pack_dir, private_key=key)
        (pack_dir / "rules" / "one.json").rename(pack_dir / "rules" / "two.json")
        assert verify_pack(pack_dir, sig, public_key=key.public_key()) is False


class TestBackendSelection:
    def test_ed25519_backend_is_preferred(self):
        assert get_backend() == "ed25519-cryptography"

    def test_signing_without_a_key_is_refused(self, tmp_path):
        pack_dir = _make_pack(tmp_path)
        with pytest.raises(PackSignatureError, match="Ed25519 private key"):
            sign_pack(pack_dir)

    def test_dev_hmac_requires_explicit_opt_in_and_key(self, tmp_path):
        pack_dir = _make_pack(tmp_path)
        with pytest.raises(PackSignatureError, match="explicit hmac_key"):
            sign_pack(pack_dir, allow_dev_hmac=True)

    def test_dev_hmac_signature_is_rejected_by_default_on_verify(self, tmp_path):
        pack_dir = _make_pack(tmp_path)
        sig = sign_pack(pack_dir, allow_dev_hmac=True, hmac_key=b"local-dev-key")
        assert sig["backend"] == "dev-hmac"
        assert verify_pack(pack_dir, sig, hmac_key=b"local-dev-key") is False
        assert verify_pack(
            pack_dir, sig, hmac_key=b"local-dev-key", allow_dev_hmac=True,
        ) is True


class TestVerificationHardening:
    def test_content_hash_alone_does_not_verify(self, tmp_path):
        pack_dir = _make_pack(tmp_path)
        forged = {
            "backend": "ed25519-cryptography",
            "algorithm": "Ed25519",
            "signature": "00" * 64,
            "content_sha256": pack_content_sha256(pack_dir),
        }
        assert verify_pack(pack_dir, forged) is False

    def test_unknown_backend_is_rejected(self, tmp_path):
        pack_dir = _make_pack(tmp_path)
        forged = {
            "backend": "trust-me",
            "signature": "deadbeef",
            "content_sha256": pack_content_sha256(pack_dir),
        }
        assert verify_pack(pack_dir, forged) is False

    def test_wrong_public_key_is_rejected(self, tmp_path):
        pack_dir = _make_pack(tmp_path)
        sig = sign_pack(pack_dir, private_key=Ed25519PrivateKey.generate())
        other = Ed25519PrivateKey.generate().public_key()
        assert verify_pack(pack_dir, sig, public_key=other) is False
