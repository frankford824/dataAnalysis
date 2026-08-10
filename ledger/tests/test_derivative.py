"""认出人工中间产物，同时不误伤真数据。

交上来的文件夹里混着三种东西：平台原始导出、人工筛选出来的副本、人工做的透视汇总。
后两种的数据在源表里已经有一份，摄进去不是多一份证据，是把同一笔钱记两遍——
实测淘宝店 708 行补发订单在两张表里逐行相同，不识别就多记 4,071.43 元。

这批测试一半在测「认出来」，一半在测「别认错」。后一半更重要：误伤的代价是
整张真账单被排除、少记几十万，比漏认一张汇总表严重得多。每条「别认错」的用例
都来自一次真实的误判。
"""

from __future__ import annotations

from ledger.engine.derivative import detect


class TestPivotTables:
    """Excel 数据透视表在列名上留的痕迹。"""

    def test_mostly_pivot_fields_is_derivative(self):
        d = detect(
            ["订单号", "求和项:销售收入", "求和项:服务费", "计数项:万相台充值"],
            [],
        )
        assert d.is_derivative
        assert "透视" in d.reason

    def test_a_few_helper_columns_are_not(self):
        """明细表右边被人多挂一两列汇总辅助列，表本身是真数据。

        实测 1688 收款明细和抖音对账单都是这样：19 列里有 1 列「求和项:已收金额(元)」，
        那是制表人贴在旁边的核对用列，左边 18 列是平台原样导出的明细。
        只要「有没有透视字段」就判派生的话，这两张表会被整张排除。
        """
        headers = [
            "账单编号", "账单创建时间", "应收金额(元)", "已收金额(元)", "待收金额(元)",
            "账单状态", "账单结清时间", "账单类型", "场景类型", "场景明细",
            "归属主体名称", "归属主体税号", "关联业务单号", "关联订单号",
            "包含邮费金额(元)", "是否出口订单", "关联订单号", "求和项:已收金额(元)",
        ]
        assert not detect(headers, []).is_derivative


class TestDuplicatedColumns:
    """把透视结果贴在源数据旁边，会让整组列名出现两遍。"""

    def test_bulk_duplication_is_derivative(self):
        headers = [
            "类型", "订单号", "交易赔付", "交易收款", "交易退款", "软件服务费",
            "主订单id", "交易赔付", "交易收款", "交易退款", "软件服务费",
        ]
        d = detect(headers, [])
        assert d.is_derivative
        assert "重复" in d.reason

    def test_scattered_duplicates_are_not(self):
        """真明细表右边也常挂着人工加的辅助列，零星几个重名不算加工产物。

        实测淘宝的支付宝账务明细就带着 (空白) 和总计这类列，但它是 22 万行真数据。
        靠重名占比区分：汇总表是整块重复，明细表只是零星几列。
        """
        headers = [
            "交易号", "业务基础订单号", "商户订单号", "收入金额（+元）", "支出金额（-元）",
            "账户余额（元）", "交易创建时间", "最近修改时间", "业务描述", "备注",
            "费项", "费项2", "总计",
        ]
        assert not detect(headers, []).is_derivative


class TestSelfDeclaredProvenance:
    """表里自述数据来源。这类句子只会出现在人工加工的表上。"""

    def test_extracted_copy_is_derivative(self):
        """1688 那张「对账」表就是人工把收付款两张明细并排粘出来的。"""
        d = detect(
            ["来源：提取1688收款明细表内“关联订单号”，已收金额（元）对应列", "", ""],
            [],
        )
        assert d.is_derivative
        assert d.upstream == "1688收款明细表"

    def test_platform_export_path_is_not(self):
        """路径写明是从平台后台导出，明确是原始数据，不用再猜。"""
        rows = [["路径：千牛——财务——总览——微信账户——查看明细——选择日期——导出"]]
        assert not detect(["名称：对账"], rows).is_derivative

    def test_note_outside_first_column_is_not(self):
        """表中间的说明注解的是某一块，不是整张表。

        实测微信对账表第 18 列写着「数据来源：前方微信账单提取主订单ID…」，
        说的是右边人工加的几列辅助汇总。全表扫描会把这张 22 万行的真账单
        误判成副本——所以只看第一列。
        """
        headers = [
            "收支类型", "交易时间", "入帐时间", "订单号", "收入金额(元)", "支出金额(元)",
            "账户余额(元)", "业务描述", "备注", "主订单id",
            "数据来源：前方微信账单提取主订单ID，粘贴到这里做核对",
        ]
        assert not detect(headers, []).is_derivative

    def test_unknown_upstream_stays_empty(self):
        """抠不出上游表名就留空，不猜。"""
        d = detect(["来源：从别处筛选而来"], [])
        assert d.is_derivative
        assert d.upstream == ""


class TestPlainTables:
    """干干净净的平台导出，什么都不该触发。"""

    def test_ordinary_export(self):
        headers = ["订单号", "商品", "数量", "金额", "下单时间"]
        d = detect(headers, [["A001", "气球", 2, 10.0, "2026-05-01"]])
        assert not d.is_derivative
        assert d.reason == ""
        assert not d
