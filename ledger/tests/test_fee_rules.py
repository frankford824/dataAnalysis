"""界面配的归类规则：叠在模板链上，写回要整份校验。"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from ledger.engine.classify import COL_MAJOR, COL_MINOR, COL_VIA, classify
from ledger.fees import payload_diff
from ledger.model.config import replace_fee_rules
from ledger.model.loader import ModelError, load_model
from ledger.model.schema import DictionaryEntry, FeeRule, Model, Platform


def _model(**kwargs) -> Model:
    dictionary = kwargs.pop("dictionary", (
        DictionaryEntry(platform="*", raw="已知", minor="已知细项", major="software_fee"),
        DictionaryEntry(platform="*", raw="营销", minor="营销细项", major="marketing_fee"),
    ))
    platforms = kwargs.pop("platforms", (
        Platform(id="taobao", name="淘宝"),
        Platform(id="douyin", name="抖音"),
        Platform(id="jd", name="京东"),
        Platform(id="alibaba1688", name="1688"),
    ))
    return Model(id="t", name="测试", dictionary=dictionary, platforms=platforms, **kwargs)


def _frame(*subjects: str) -> pl.DataFrame:
    return pl.DataFrame({
        "subject": list(subjects),
        "amount": [-1.0] * len(subjects),
        "remark": [""] * len(subjects),
    })


class TestOverlay:
    def test_after_catches_what_the_dictionary_missed(self):
        model = _model(fee_rules=(
            FeeRule(value="新费项", major="software_fee", minor="跨境增值费"),
        ))
        out, report = classify(_frame("已知", "新费项", "仍不认识"), model, "taobao", "amount")
        assert out.get_column(COL_MAJOR).to_list() == ["software_fee", "software_fee", None]
        assert out.get_column(COL_MINOR).to_list()[1] == "跨境增值费"
        assert "仍不认识" in report.unmatched
        assert "新费项" not in report.unmatched
        via = out.get_column(COL_VIA).to_list()[1]
        assert via.startswith("费项规则")

    def test_before_overrides_the_dictionary(self):
        model = _model(fee_rules=(
            FeeRule(value="已知", major="marketing_fee", minor="改判", stage="before"),
        ))
        out, _ = classify(_frame("已知"), model, "taobao", "amount")
        assert out.get_column(COL_MAJOR).to_list() == ["marketing_fee"]
        assert out.get_column(COL_MINOR).to_list() == ["改判"]

    def test_douyin_scene_matches_the_subject_column(self):
        model = _model(fee_rules=(
            FeeRule(platform="douyin", field="douyin_scene", value="评价有礼",
                    major="marketing_fee", minor="营销费用"),
        ))
        out, _ = classify(_frame("评价有礼"), model, "douyin", "amount")
        assert out.get_column(COL_MAJOR).to_list() == ["marketing_fee"]

    def test_1688_scene_detail_prefers_minor_subject(self):
        """收款表场景类型在 subject、场景明细在 minor_subject，必须打中明细那列。"""
        model = _model(fee_rules=(
            FeeRule(platform="alibaba1688", field="scene_detail", how="contains",
                    value="服务费", major="software_fee", minor="服务费"),
        ))
        frame = pl.DataFrame({
            "subject": ["订单收入"],
            "minor_subject": ["技术服务费缴纳"],
            "amount": [-1.0],
            "remark": [""],
        })
        out, _ = classify(frame, model, "alibaba1688", "amount")
        assert out.get_column(COL_MAJOR).to_list() == ["software_fee"]

    def test_1688_scene_type_uses_dedicated_column_when_present(self):
        """付款表 subject 是场景明细。场景类型必须走单独绑的那列，不能误打明细。"""
        model = _model(fee_rules=(
            FeeRule(platform="alibaba1688", field="scene_type", value="订单收入",
                    major="software_fee"),
        ))
        frame = pl.DataFrame({
            "subject": ["服务费"],
            "scene_type": ["订单收入"],
            "amount": [-1.0],
            "remark": [""],
        })
        out, report = classify(frame, model, "alibaba1688", "amount")
        assert out.get_column(COL_MAJOR).to_list() == ["software_fee"]
        assert report.unmatched == {}

    def test_slash_separated_contains_matches_any_keyword(self):
        """合并规则时用 / 隔开多个词，必须当成「含其中任一」，不能当整段去配。"""
        model = _model(
            dictionary=(
                DictionaryEntry(platform="*", raw="已知", minor="已知", major="software_fee"),
                DictionaryEntry(platform="*", raw="上门取件运费", minor="上门取件运费",
                                major="software_fee"),
            ),
            fee_rules=(
                FeeRule(
                    platform="douyin", field="douyin_scene", how="contains",
                    value="上门取件运费/偏远地区物流服务/上门取件-支付快递费",
                    major="software_fee", stage="before",
                ),
            ),
        )
        out, report = classify(
            _frame("上门取件运费", "偏远地区物流服务", "上门取件-支付快递费", "消费者赔付"),
            model, "douyin", "amount",
        )
        assert out.get_column(COL_MAJOR).to_list() == [
            "software_fee", "software_fee", "software_fee", None,
        ]
        assert "消费者赔付" in report.unmatched

    def test_jd_fee_meaning_column(self):
        model = _model(fee_rules=(
            FeeRule(platform="jd", field="jd_fee_meaning", value="佣金",
                    major="marketing_fee", minor="佣金"),
        ))
        frame = pl.DataFrame({
            "subject": ["费用A"],
            "fee_meaning": ["佣金"],
            "amount": [-1.0],
            "remark": [""],
        })
        out, _ = classify(frame, model, "jd", "amount")
        assert out.get_column(COL_MAJOR).to_list() == ["marketing_fee"]

    def test_exact_folds_fullwidth_brackets(self):
        """对照表里常是全角括号，流水里是半角。归一之后必须能配上。"""
        model = _model(fee_rules=(
            FeeRule(value="技术（服务）费", how="exact", major="software_fee", minor="技术服务费"),
        ))
        out, _ = classify(_frame("技术(服务)费"), model, "taobao", "amount")
        assert out.get_column(COL_MAJOR).to_list() == ["software_fee"]

    def test_remark_contains_when_subject_is_empty(self):
        model = _model(fee_rules=(
            FeeRule(field="remark", how="contains", value="淘宝联盟佣金",
                    major="marketing_fee", minor="淘宝客佣金"),
        ))
        frame = pl.DataFrame({
            "subject": [""],
            "remark": ["代扣款 扣款用途：淘宝联盟佣金代扣"],
            "amount": [-12.3],
        })
        out, report = classify(frame, model, "taobao", "amount")
        assert out.get_column(COL_MAJOR).to_list() == ["marketing_fee"]
        assert report.unmatched == {}

    def test_unknown_major_is_rejected(self):
        with pytest.raises((ValueError, Exception), match="口径项不存在"):
            _model(fee_rules=(FeeRule(value="x", major="no_such"),))


class TestWriteBack:
    def _root(self, tmp_path: Path) -> Path:
        root = tmp_path / "m"
        root.mkdir()
        (root / "model.yaml").write_text("id: t\nname: t\n", encoding="utf-8")
        (root / "dictionary.csv").write_text(
            "platform,raw,minor,major,naturally_unlinked\n*,已知,已知,software_fee,\n",
            encoding="utf-8",
        )
        return root

    def test_roundtrip_keeps_order(self, tmp_path: Path):
        root = self._root(tmp_path)
        rules = [
            FeeRule(value="乙", major="software_fee", minor="乙"),
            FeeRule(value="甲", major="software_fee", minor="甲"),
        ]
        assert replace_fee_rules(root, rules) == 2
        loaded = load_model(root).fee_rules
        assert [r.value for r in loaded] == ["乙", "甲"]
        text = (root / "fee-rules.csv").read_text(encoding="utf-8")
        assert text.index("乙") < text.index("甲")

    def test_bad_major_does_not_leave_a_file(self, tmp_path: Path):
        root = self._root(tmp_path)
        with pytest.raises(ModelError):
            replace_fee_rules(root, [FeeRule(value="x", major="no_such")])
        assert not (root / "fee-rules.csv").exists()


class TestPayloadDiff:
    def test_only_changed_rows_appear(self):
        before = {"statement": [
            {"id": "n_software", "name": "平台服务费", "value": -10.0},
            {"id": "n_marketing", "name": "平台营销费用", "value": -3.0},
        ]}
        after = {"statement": [
            {"id": "n_software", "name": "平台服务费", "value": -10.0},
            {"id": "n_marketing", "name": "平台营销费用", "value": -15.3},
        ]}
        diff = payload_diff(before, after)
        assert [d["id"] for d in diff] == ["n_marketing"]
        assert diff[0]["delta"] == pytest.approx(-12.3)


class TestDisplayCopy:
    """界面上不该再出现英文口径号和「接住」这种内部说法。"""

    def test_unmatched_label_hides_engine_keys(self):
        from ledger.fees import pretty_unmatched_label
        raw = "（业务描述为空） biz_type=其它 remark=退回"
        shown = pretty_unmatched_label(raw)
        assert "biz_type" not in shown
        assert "remark=" not in shown
        assert "业务类型：其它" in shown
        assert "备注：退回" in shown

    def test_via_hides_config_marker_and_roles(self):
        from ledger.fees import humanize_via
        shown = humanize_via("[配置] subject 等于 新费项 → software_fee")
        assert shown.startswith("费项规则")
        assert "subject" not in shown
        assert "业务描述" in shown

    def test_cn_ecommerce_majors_are_chinese(self):
        from conftest import MODELS
        from ledger.fees import known_fees, major_options, platform_aliases
        from ledger.model.loader import load_model
        model = load_model(MODELS / "cn-ecommerce")
        names = {row["id"]: row["name"] for row in major_options(model)}
        assert names["ad_topup"] == "广告费用"
        assert names["deposit"] == "保证金"
        assert names["withdrawal"] == "提现"
        assert names["dropship_cost"] == "代购代发"
        assert names["misc_payment"] == "往来款"
        assert names["software_fee"] == "平台服务费"
        assert names["logistics_fee"] == "物流运费"
        assert names["cross_border_fee"] == "跨境服务费"
        for name in names.values():
            assert not name.isascii(), name
        aliases = platform_aliases(model)
        assert aliases["jd_1688"] == "京东（1688）"
        wechat = next(f for f in known_fees(model) if f.origin == "taobao_settlement_wechat_v2")
        assert wechat.origin_name == "淘宝对账-微信账单（天猫八列版）"

    def test_platform_columns_are_listed(self):
        from ledger.fees import FEE_FIELDS
        names = {(i, p): n for i, n, p in FEE_FIELDS}
        assert names["douyin_scene", "douyin"] == "动帐场景"
        assert names["jd_fee_name", "jd"] == "费用名称"
        assert names["jd_fee_meaning", "jd"] == "费用项含义"
        assert names["scene_type", "alibaba1688"] == "场景类型"
        assert names["scene_detail", "alibaba1688"] == "场景明细"
        assert names["bill_type", "alibaba1688"] == "账单类型"

    def test_stage_copy_does_not_look_like_the_rule_is_unsaved(self):
        from ledger.fees import FEE_STAGES
        names = dict(FEE_STAGES)
        assert "尚未归类" not in names["after"]
        assert "未挂上费项" in names["after"]
        assert "覆盖" in names["before"]

    def test_alipay_remark_uses_purpose_not_tradeid(self):
        from ledger.fees import _unmatched_hint
        a = (
            "（业务描述为空） biz_type=转账 remark=代扣款 (扣款用途: 淘宝联盟佣金代扣 "
            "tradeid:5123427735732032406 memberid:3792292908 fee:4.32 "
            "batchno:H_USP_c_d-43626604, 付款方：义乌颂楔科技有限公司)"
        )
        b = a.replace("5123427735732032406", "999")
        hint = _unmatched_hint(a)
        assert hint["field"] == "remark"
        assert hint["how"] == "contains"
        assert hint["value"] == "淘宝联盟佣金代扣"
        assert "tradeid" not in hint["value"]
        assert _unmatched_hint(b) == hint

    def test_same_purpose_collapses_to_one_row(self):
        from types import SimpleNamespace

        from ledger.fees import unmatched_from

        def item(tid: str) -> dict:
            return {
                "label": (
                    "（业务描述为空） biz_type=转账 remark=代扣款 (扣款用途: 淘宝联盟佣金代扣 "
                    f"tradeid:{tid} memberid:1 fee:4.32, 付款方：义乌颂楔科技有限公司)"
                ),
                "count": 1,
                "amount": -4.32,
            }

        st = SimpleNamespace(
            store_id="s1",
            result={"platform": "taobao", "unclassified": [item("111"), item("222")]},
        )
        rows = unmatched_from(SimpleNamespace(overview=lambda: [st]))
        assert len(rows) == 1
        assert rows[0]["count"] == 2
        assert rows[0]["value"] == "淘宝联盟佣金代扣"
        assert "淘宝联盟佣金代扣" in rows[0]["caption"]
