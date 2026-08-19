"""规则链按顺序走，顺序本身就是业务判断。

取关联键和归类都不是一次查表能定的事。真实的支付宝账单里，订单号可能在业务基础
订单号列、可能在商户订单号列、可能只在备注里写着「交易单号:1234」、也可能这笔钱
根本不该进店铺账（保证金、提现、账户互转）。这些情况有优先级，链上靠顺序表达。

顺序错了不会报错，只会静默算错——所以这批测试重点测顺序。
"""

from __future__ import annotations

from ledger.engine.rules import (
    EXCLUDED,
    compile_classify_rules,
    compile_key_rules,
    resolve_class,
    resolve_key,
)
from ledger.model.schema import Bridge, ClassifyRule, FieldMatch, KeyRule


class TestKeyChainOrder:
    def test_first_match_wins(self):
        """自带的键优先于回查得来的键。

        实测抖音的运费表自带「子订单」列，而回查运单号只能拿到主订单号。
        要是回查规则排在前面，就会拿主订单号去匹配子订单——挂不上，或者更糟，
        挂到同一主订单下的错误子订单上。抖音的口径正是按子订单匹配的。
        """
        rules = compile_key_rules((
            KeyRule(when=FieldMatch(field="sub_order_id", notnull=True)),
            KeyRule(
                when=FieldMatch(field="tracking_no", notnull=True),
                via=Bridge(source="order_detail", match="tracking_no", take="order_id"),
            ),
        ))
        bridges = {"order_detail": {"SF001": "主订单-9"}}
        row = {"sub_order_id": "子订单-1", "tracking_no": "SF001"}
        assert resolve_key(row, rules, bridges) == "子订单-1"

    def test_falls_through_to_lookup(self):
        """没自带键才回查。"""
        rules = compile_key_rules((
            KeyRule(when=FieldMatch(field="sub_order_id", notnull=True)),
            KeyRule(
                when=FieldMatch(field="tracking_no", notnull=True),
                via=Bridge(source="order_detail", match="tracking_no", take="order_id"),
            ),
        ))
        bridges = {"order_detail": {"SF001": "主订单-9"}}
        assert resolve_key({"sub_order_id": "", "tracking_no": "SF001"}, rules, bridges) == "主订单-9"

    def test_a_lookup_that_returned_zero_is_not_a_key(self):
        """xlookup 查不到时返回的 0 不是空，会把整条链堵死。

        运费表那两列「子订单」「子订单2」是制表人用 xlookup 填出来的，查不到时
        公式的第四个参数给的是 0——29.9 万行里 29.9 万行都写着 0.0。只判非空的话，
        第一条规则对每一行都命中，取到的键是「0.0」：既挂不上任何订单，又因为
        已经命中而不再往下试回查，后面三条规则全部形同虚设。表现不是报错，
        是发货运费整列悄悄归零。所以两条自带键的规则都要求至少有一个非零数字。
        """
        rules = compile_key_rules((
            KeyRule(when=FieldMatch(field="sub_order_id", matches=r"[1-9]")),
            KeyRule(when=FieldMatch(field="sub_order_id_alt", matches=r"[1-9]")),
            KeyRule(
                when=FieldMatch(field="tracking_no", notnull=True),
                via=Bridge(source="order_cost", match="tracking_no", take="original_order_id"),
            ),
        ))
        bridges = {"order_cost": {"SF001": "原始订单-9"}}

        zeros = {"sub_order_id": "0.0", "sub_order_id_alt": "0.0", "tracking_no": "SF001"}
        assert resolve_key(zeros, rules, bridges) == "原始订单-9", "两列都是 0 时必须往下走到回查"

        # 「子订单2 有值、子订单为 0 就补上」是业务的原话，第二条规则守的就是这句。
        alt = {"sub_order_id": "0.0", "sub_order_id_alt": "子订单-2", "tracking_no": "SF001"}
        assert resolve_key(alt, rules, bridges) == "子订单-2"

        real = {"sub_order_id": "子订单-1", "sub_order_id_alt": "0.0", "tracking_no": "SF001"}
        assert resolve_key(real, rules, bridges) == "子订单-1"

    def test_exclusion_must_come_first(self):
        """排除规则要排在最前面，否则会先被别的规则捞走。

        实测支付宝账单里「支付宝证券」「余额宝」这类流水的备注里也带着
        形如数字串的东西，正则一抓就抓出个假订单号。这类钱根本不该进店铺账，
        必须在任何取键尝试之前先排掉。
        """
        rules = compile_key_rules((
            KeyRule(when=FieldMatch(field="business", contains=["保证金", "提现"]), exclude=True),
            KeyRule(when=FieldMatch(field="remark", extract=r"(\d{6,})")),
        ))
        row = {"business": "支付宝提现", "remark": "流水号 998877665544"}
        assert resolve_key(row, rules, {}) is EXCLUDED

    def test_extract_from_free_text(self):
        """订单号只写在备注里的情况，靠正则抠。"""
        rules = compile_key_rules((
            KeyRule(when=FieldMatch(field="remark", extract=r"交易单号[:：]\s*(\d+)")),
        ))
        assert resolve_key({"remark": "服务费 交易单号：4077712345678"}, rules, {}) == "4077712345678"

    def test_no_rule_matches(self):
        rules = compile_key_rules((
            KeyRule(when=FieldMatch(field="order_id", notnull=True)),
        ))
        assert resolve_key({"order_id": ""}, rules, {}) is None


class TestClassifyChain:
    @staticmethod
    def _no_dict(_raw):
        return None

    def test_rule_before_dictionary(self):
        """写在链上的规则优先于科目字典。

        字典是几百条历史积累的通用映射，链上的规则是这张表特有的判断。
        表特有的更具体，该赢。
        """
        rules = compile_classify_rules((
            ClassifyRule(when=FieldMatch(field="subject", contains=["订单售后退款"]),
                         major="trade_receipt_1688"),
            ClassifyRule(dictionary=True),
        ))

        def lookup(raw):
            return ("trade_refund", "退款", False)

        major, _minor, excluded, _ = resolve_class({"subject": "订单售后退款"}, rules, lookup)
        assert major == "trade_receipt_1688"
        assert not excluded

    def test_dictionary_as_fallback(self):
        """规则没覆盖到的科目交给字典。"""
        rules = compile_classify_rules((
            ClassifyRule(when=FieldMatch(field="subject", contains=["订单收入"]),
                         major="trade_receipt_1688"),
            ClassifyRule(dictionary=True),
        ))

        def lookup(raw):
            return ("software_fee", "软件服务费", False) if raw == "软件服务费" else None

        major, minor, _, _ = resolve_class({"subject": "软件服务费"}, rules, lookup)
        assert (major, minor) == ("software_fee", "软件服务费")

    def test_unclassified_stays_unclassified(self):
        """认不出来就是认不出来，不许瞎归。

        归错科目比归不出来危险得多：归不出来会被自检拦住让人来看，
        归错了会安安静静进损益表。
        """
        rules = compile_classify_rules((ClassifyRule(dictionary=True),))
        major, minor, excluded, _ = resolve_class({"subject": "没见过的科目"}, rules, self._no_dict)
        assert major is None and minor is None and not excluded

    def test_explicit_exclusion(self):
        """明确不进账的科目要能排除，且和「认不出来」区分开。"""
        rules = compile_classify_rules((
            ClassifyRule(when=FieldMatch(field="subject", contains=["账户互转"]), exclude=True),
            ClassifyRule(dictionary=True),
        ))
        _major, _minor, excluded, _ = resolve_class({"subject": "账户互转"}, rules, self._no_dict)
        assert excluded
