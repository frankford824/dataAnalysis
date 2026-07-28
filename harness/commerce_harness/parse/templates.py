from __future__ import annotations

import re
import unicodedata
from collections.abc import Collection
from dataclasses import dataclass

from .models import SourceKind


def normalize_header(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).strip().lower()
    return re.sub(r"[\s_\-—/（）()]+", "", text)


@dataclass(frozen=True, slots=True)
class TemplateDefinition:
    template_id: str
    source_kind: SourceKind
    aliases: dict[str, Collection[str]]
    required_groups: tuple[tuple[str, ...], ...]

    def match(self, headers: tuple[str, ...]) -> dict[str, str] | None:
        normalized_to_original: dict[str, str] = {}
        for header in headers:
            normalized = normalize_header(header)
            if normalized and normalized not in normalized_to_original:
                normalized_to_original[normalized] = header

        matched: dict[str, str] = {}
        for semantic_field, aliases in self.aliases.items():
            ordered_aliases = (
                aliases
                if isinstance(aliases, tuple)
                else sorted(aliases, key=normalize_header)
            )
            for alias in ordered_aliases:
                original = normalized_to_original.get(normalize_header(alias))
                if original is not None:
                    matched[semantic_field] = original
                    break
        if not all(any(field in matched for field in group) for group in self.required_groups):
            return None
        return matched


def _aliases(*values: str) -> tuple[str, ...]:
    """Return aliases in explicit business-priority order."""

    return values


DEFAULT_TEMPLATES = (
    TemplateDefinition(
        template_id="pdd_order_v1",
        source_kind=SourceKind.ORDER,
        aliases={
            "order_id": _aliases("订单号"),
            "business_date": _aliases("订单成交时间"),
            "paid_amount": _aliases("商家实收金额(元)", "用户实付金额(元)"),
            "sku": _aliases(
                "商品id",
                "样式ID",
                "商家编码-商品维度",
                "商家编码-规格维度",
            ),
            "store_name": _aliases("店铺名称"),
        },
        required_groups=(("order_id",), ("business_date",), ("paid_amount",), ("sku",)),
    ),
    TemplateDefinition(
        template_id="douyin_order_v1",
        source_kind=SourceKind.ORDER,
        aliases={
            "order_id": _aliases("子订单编号", "主订单编号"),
            "business_date": _aliases("支付完成时间", "订单提交时间"),
            "paid_amount": _aliases("子订单收入", "订单应付金额"),
            "sku": _aliases("商品ID"),
            "store_name": _aliases("店铺名称"),
        },
        required_groups=(("order_id",), ("business_date",), ("paid_amount",), ("sku",)),
    ),
    TemplateDefinition(
        template_id="taobao_order_v1",
        source_kind=SourceKind.ORDER,
        aliases={
            "order_id": _aliases("子订单编号", "子订单号", "订单编号", "主订单编号"),
            "business_date": _aliases("订单创建时间", "下单时间", "支付时间"),
            "paid_amount": _aliases(
                "买家实际支付金额",
                "买家实付金额",
                "实付金额",
                "成交金额",
            ),
            "refund_amount": _aliases("退款金额", "售中退款金额", "退款金额（元）"),
            "store_name": _aliases("店铺名称", "所属店铺"),
            "sku": _aliases(
                "宝贝ID",
                "商品ID",
                "商品编码",
                "商家编码",
                "货品编码",
                "SKU编码",
                "SKU ID",
                "规格编码",
            ),
        },
        required_groups=(("order_id",), ("business_date",), ("paid_amount",)),
    ),
    TemplateDefinition(
        template_id="alipay_ledger_v1",
        source_kind=SourceKind.ALIPAY,
        aliases={
            "transaction_id": _aliases("支付宝交易号", "账务流水号", "财务流水号"),
            "accounting_date": _aliases("入账时间", "账务时间", "发生时间"),
            "income_amount": _aliases("收入金额（+元）", "收入金额", "收入（元）"),
            "expense_amount": _aliases("支出金额（-元）", "支出金额", "支出（元）"),
            "business_description": _aliases("业务描述", "业务类型"),
            "merchant_order_id": _aliases("商户订单号", "订单号"),
        },
        required_groups=(
            ("transaction_id",),
            ("accounting_date",),
            ("income_amount",),
            ("expense_amount",),
        ),
    ),
    TemplateDefinition(
        template_id="wechat_income_expense_v1",
        source_kind=SourceKind.WECHAT,
        aliases={
            "transaction_id": _aliases("支付流水号"),
            "accounting_date": _aliases("入帐日期", "发生时间"),
            "income_amount": _aliases(
                "收入金额（+元）",
                "收入金额",
                "收入金额(元)",
            ),
            "expense_amount": _aliases(
                "支出金额（-元）",
                "支出金额",
                "支出金额(元)",
            ),
            "business_description": _aliases("业务描述", "入帐类型"),
            "merchant_order_id": _aliases("业务基础订单号", "子订单id"),
        },
        required_groups=(
            ("transaction_id",),
            ("accounting_date",),
            ("income_amount",),
            ("expense_amount",),
        ),
    ),
    TemplateDefinition(
        template_id="alipay_control_total_v1",
        source_kind=SourceKind.ALIPAY_CONTROL,
        aliases={
            "category": _aliases("类型"),
            "income_count": _aliases("收入笔数"),
            "income_amount": _aliases("收入金额（+元）"),
            "expense_count": _aliases("支出笔数"),
            "expense_amount": _aliases("支出金额（-元）"),
            "net_amount": _aliases("总金额（元）"),
        },
        required_groups=(
            ("category",),
            ("income_amount",),
            ("expense_amount",),
            ("net_amount",),
        ),
    ),
    TemplateDefinition(
        template_id="wechat_control_total_v1",
        source_kind=SourceKind.WECHAT_CONTROL,
        aliases={
            "period": _aliases("日期"),
            "detail_count": _aliases("明细笔数"),
            "income_amount": _aliases("收入金额（元）"),
            "income_count": _aliases("收入笔数"),
            "expense_amount": _aliases("支出金额（元）"),
            "expense_count": _aliases("支出笔数"),
            "ending_balance": _aliases("期末余额（元）"),
        },
        required_groups=(
            ("period",),
            ("detail_count",),
            ("income_amount",),
            ("expense_amount",),
            ("ending_balance",),
        ),
    ),
    TemplateDefinition(
        template_id="wechat_ledger_v1",
        source_kind=SourceKind.WECHAT,
        aliases={
            "transaction_id": _aliases("微信支付业务单号", "微信业务单号"),
            "accounting_date": _aliases("记账时间", "微信交易时间"),
            "amount": _aliases("收支金额", "金额（元）"),
            "direction": _aliases("收支类型", "资金变动类型"),
            "business_description": _aliases("业务描述", "业务类型"),
            "merchant_order_id": _aliases("商户单号", "商户订单号"),
        },
        required_groups=(
            ("transaction_id",),
            ("accounting_date",),
            ("amount",),
            ("direction",),
        ),
    ),
    TemplateDefinition(
        template_id="jushuitan_cost_v1",
        source_kind=SourceKind.COST,
        aliases={
            "order_id": _aliases("内部订单号", "线上子订单号", "订单号"),
            "sku": _aliases("商品编码", "款式编码", "SKU编码", "商家编码"),
            "quantity": _aliases("数量", "实发数量", "商品数量"),
            "unit_cost": _aliases("成本价", "单位成本", "商品成本"),
            "store_name": _aliases("店铺名称", "店铺"),
            "business_date": _aliases("订单日期", "发货日期"),
        },
        required_groups=(("order_id",), ("sku",), ("quantity",), ("unit_cost",)),
    ),
    TemplateDefinition(
        template_id="advertising_spend_v1",
        source_kind=SourceKind.ADVERTISING,
        aliases={
            "business_date": _aliases("日期", "数据日期", "统计日期"),
            "spend_amount": _aliases("消耗", "花费", "广告费", "现金消耗"),
            "campaign": _aliases(
                "计划名称",
                "广告计划",
                "推广计划名称",
                "账户名称",
                "主体名称",
            ),
            "store_name": _aliases("店铺名称", "所属店铺"),
            "sku": _aliases(
                "主体ID",
                "宝贝ID",
                "商品ID",
                "商品编码",
                "商家编码",
                "SKU编码",
            ),
            "entity_type": _aliases("主体类型"),
        },
        required_groups=(("business_date",), ("spend_amount",), ("campaign",)),
    ),
    TemplateDefinition(
        template_id="store_order_freight_v1",
        source_kind=SourceKind.FREIGHT,
        aliases={
            "order_id": _aliases("订单号", "订单编号"),
            "business_date": _aliases("发货日期", "结算日期", "日期"),
            "freight_amount": _aliases("金额", "运费", "物流费用"),
            "store_name": _aliases("店铺名称", "店铺"),
        },
        required_groups=(("order_id",), ("business_date",), ("freight_amount",)),
    ),
    TemplateDefinition(
        template_id="freight_statement_v1",
        source_kind=SourceKind.FREIGHT,
        aliases={
            "tracking_number": _aliases("运单号", "物流单号", "快递单号"),
            "business_date": _aliases("发货日期", "结算日期", "日期"),
            "freight_amount": _aliases("运费", "结算金额", "应付金额", "物流费用"),
            "carrier": _aliases("快递公司", "物流公司", "承运商"),
            "store_name": _aliases("店铺名称", "店铺"),
            "order_id": _aliases("订单编号", "订单号", "线上订单号", "子订单号"),
            "sku": _aliases(
                "商品编码",
                "商家编码",
                "货品编码",
                "SKU编码",
                "SKU ID",
            ),
        },
        required_groups=(("tracking_number",), ("business_date",), ("freight_amount",)),
    ),
    TemplateDefinition(
        template_id="taobao_platform_fee_v1",
        source_kind=SourceKind.PLATFORM_FEE,
        aliases={
            "billing_period": _aliases("账期"),
            "direction": _aliases("资金方向"),
            "category": _aliases("业务小类", "支出项目", "业务大类"),
            "business_date": _aliases(
                "扣费日期",
                "订单日期",
                "确认收货日期",
                "回款时间",
            ),
            "fee_amount": _aliases(
                "扣费金额",
                "扣费金额(元)",
                "本月付款",
                "本月服务费",
            ),
            "main_order_id": _aliases("交易主订单号", "交易主单号"),
            "sub_order_id": _aliases("交易子订单号"),
            "merchant_order_id": _aliases(
                "支付宝商户订单号",
                "支付渠道商户订单号",
            ),
            "sku": _aliases(
                "宝贝ID",
                "商品ID",
                "商品编码",
                "商家编码",
                "货品编码",
                "SKU编码",
            ),
        },
        required_groups=(
            ("billing_period",),
            ("category",),
            ("fee_amount",),
        ),
    ),
    TemplateDefinition(
        template_id="historical_pnl_16_v1",
        source_kind=SourceKind.HISTORICAL_OUTPUT,
        aliases={
            "sku": _aliases("宝贝编码"),
            "sales": _aliases("交易收款"),
            "refund": _aliases("交易退款"),
            "platform_fee": _aliases("软件服务费"),
            "freight": _aliases("发货运费"),
            "cost": _aliases("订单成本"),
            "advertising": _aliases("广告费"),
            "profit": _aliases("店铺利润"),
        },
        required_groups=(
            ("sku",),
            ("sales",),
            ("refund",),
            ("profit",),
        ),
    ),
)
