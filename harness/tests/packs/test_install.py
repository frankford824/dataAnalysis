"""Tests for pack installation: fail-closed gates and layer precedence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from commerce_harness.packs.install import (
    InstalledPack,
    PackInstallError,
    install_pack,
    merge_packs,
)
from commerce_harness.packs.sign import sign_pack

_MANIFEST = {
    "pack_id": "test.pack",
    "domain": "test",
    "version": "0.1.0",
    "publisher": "test",
    "engine_compat": {"min": "0.1.0"},
}


def _write_pack(root: Path, *, rules: list[dict] | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pack.json").write_text(json.dumps(_MANIFEST), encoding="utf-8")
    if rules:
        rules_dir = root / "rules"
        rules_dir.mkdir(exist_ok=True)
        for index, rule in enumerate(rules):
            (rules_dir / f"rule_{index}.json").write_text(
                json.dumps(rule), encoding="utf-8"
            )
    return root


def _sign(pack_dir: Path) -> Ed25519PrivateKey:
    key = Ed25519PrivateKey.generate()
    signature = sign_pack(pack_dir, private_key=key)
    (pack_dir / "pack.sig").write_text(json.dumps(signature), encoding="utf-8")
    return key


class TestRuleLoading:
    def test_rules_in_subdirectory_are_loaded(self, tmp_path):
        pack_dir = _write_pack(
            tmp_path / "builtin" / "p",
            rules=[{"rule_id": "r1", "action": "route", "participation": "excluded"}],
        )
        key = _sign(pack_dir)
        installed = install_pack(pack_dir, public_key=key.public_key())
        assert [rule["rule_id"] for rule in installed.rules] == ["r1"]

    def test_flat_rules_json_still_works(self, tmp_path):
        pack_dir = _write_pack(tmp_path / "builtin" / "p")
        (pack_dir / "rules.json").write_text(
            json.dumps([{"rule_id": "flat", "action": "route"}]), encoding="utf-8"
        )
        key = _sign(pack_dir)
        installed = install_pack(pack_dir, public_key=key.public_key())
        assert [rule["rule_id"] for rule in installed.rules] == ["flat"]


class TestFailClosedGates:
    def test_missing_signature_is_rejected(self, tmp_path):
        pack_dir = _write_pack(tmp_path / "builtin" / "p")
        with pytest.raises(PackInstallError, match="missing pack.sig"):
            install_pack(pack_dir)

    def test_bad_signature_is_rejected(self, tmp_path):
        pack_dir = _write_pack(tmp_path / "builtin" / "p")
        key = _sign(pack_dir)
        (pack_dir / "extra.json").write_text("{}", encoding="utf-8")
        with pytest.raises(PackInstallError, match="signature verification failed"):
            install_pack(pack_dir, public_key=key.public_key())

    def test_unknown_layer_is_rejected(self, tmp_path):
        pack_dir = _write_pack(tmp_path / "somewhere" / "p")
        with pytest.raises(PackInstallError, match="cannot determine layer"):
            install_pack(pack_dir, require_signature=False)

    def test_redaction_violation_blocks_install(self, tmp_path):
        pack_dir = _write_pack(tmp_path / "enterprise" / "p")
        (pack_dir / "knowledge.json").write_text(
            json.dumps([{"order": "1234567890123456789"}]), encoding="utf-8"
        )
        with pytest.raises(PackInstallError, match="redaction scan rejected"):
            install_pack(pack_dir, require_signature=False)

    def test_layer_is_taken_from_the_deepest_match(self, tmp_path):
        pack_dir = _write_pack(tmp_path / "builtin" / "enterprise" / "p")
        installed = install_pack(pack_dir, require_signature=False)
        assert installed.layer == "enterprise"


class TestMergePrecedence:
    def _pack(self, layer: str, invariants: list[dict]) -> InstalledPack:
        return InstalledPack(
            pack_id=f"{layer}.pack",
            domain="test",
            version="0.1.0",
            layer=layer,
            path=Path("/nonexistent"),
            manifest=dict(_MANIFEST),
            invariants=invariants,
        )

    def test_same_title_does_not_override_a_different_invariant(self):
        builtin = self._pack("builtin", [{"invariant_id": "a", "title": "控制总额"}])
        enterprise = self._pack(
            "enterprise", [{"invariant_id": "b", "title": "控制总额"}]
        )
        invariants, _ = merge_packs([builtin, enterprise])
        assert sorted(item["invariant_id"] for item in invariants) == ["a", "b"]

    def test_same_invariant_id_is_overridden_by_the_upper_layer(self):
        builtin = self._pack("builtin", [{"invariant_id": "a", "note": "builtin"}])
        enterprise = self._pack("enterprise", [{"invariant_id": "a", "note": "custom"}])
        invariants, _ = merge_packs([builtin, enterprise])
        assert invariants == [{"invariant_id": "a", "note": "custom"}]
