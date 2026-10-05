"""金额流程回归测试：输入校验 -> SQLite 持久化 -> HTML 预览。

从项目根目录运行：

    python3 -m unittest discover -s tests

仅依赖 Python 标准库。所有数据库、JSON 输入和 HTML 输出都写在
独立的临时目录中，测试结束后自动清理，不读取已有业务数据，
也不在项目目录留下任何文件。save 与 preview 均通过子进程调用
quote.py 的公开命令完成，模拟真实使用方式。
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
QUOTE_PY = PROJECT_ROOT / "quote.py"

INT64_MAX = (1 << 63) - 1  # 9223372036854775807


def make_payload(number="Q-AMOUNT-001", customer="演示客户", items=None):
    if items is None:
        items = [
            {"description": "咨询", "quantity": 2, "unit_price": 1250},
            {"description": "资料", "quantity": 1, "unit_price": 300},
            {"description": "赠品", "quantity": 4, "unit_price": 0},
        ]
    return {"number": number, "customer": customer, "items": items}


class AmountFlowTestCase(unittest.TestCase):
    """每个测试都在独立的临时目录中运行，互不干扰。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="quote-test-")
        self.workdir = Path(self._tmp.name)
        self.db_path = self.workdir / "test.sqlite"
        self._input_counter = 0

    def tearDown(self):
        self._tmp.cleanup()

    # -- 辅助方法 ---------------------------------------------------------

    def write_input(self, payload):
        """把报价单 JSON 写入临时目录，返回路径。"""
        self._input_counter += 1
        path = self.workdir / f"input-{self._input_counter}.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        return path

    def run_save(self, payload, db_path=None):
        """以子进程执行 quote.py save，返回 CompletedProcess。"""
        input_path = self.write_input(payload)
        return subprocess.run(
            [sys.executable, str(QUOTE_PY), "save",
             "--db", str(db_path or self.db_path),
             "--input", str(input_path)],
            capture_output=True, text=True,
        )

    def run_preview(self, number, output_path, db_path=None):
        """以子进程执行 quote.py preview，返回 CompletedProcess。"""
        return subprocess.run(
            [sys.executable, str(QUOTE_PY), "preview",
             "--db", str(db_path or self.db_path),
             "--number", number,
             "--output", str(output_path)],
            capture_output=True, text=True,
        )

    def fetch_all(self, db_path=None):
        """读出 quotes 与 items 全部行，用于比对数据库内容。"""
        conn = sqlite3.connect(str(db_path or self.db_path))
        try:
            quotes = conn.execute(
                "SELECT number, customer, total FROM quotes ORDER BY number"
            ).fetchall()
            items = conn.execute(
                "SELECT quote_number, position, description, quantity, "
                "unit_price, line_amount FROM items "
                "ORDER BY quote_number, position"
            ).fetchall()
        finally:
            conn.close()
        return quotes, items

    def assert_project_dir_clean(self):
        """项目目录不得因测试新增数据库或 HTML 文件。"""
        leftovers = [
            p for p in PROJECT_ROOT.iterdir()
            if p.suffix in (".sqlite", ".db", ".html") and p.is_file()
        ]
        self.assertEqual(leftovers, [],
                         f"项目目录出现遗留文件: {leftovers}")


class TestNormalFlow(AmountFlowTestCase):
    """正常样例：save 与 preview 分属不同进程，金额全程为整数分。"""

    def test_save_then_preview_amounts(self):
        payload = make_payload()

        save = self.run_save(payload)
        self.assertEqual(save.returncode, 0, f"save 失败: {save.stderr}")
        self.assertEqual(save.stderr, "")
        self.assertEqual(save.stdout.strip(), "Q-AMOUNT-001")

        quotes, items = self.fetch_all()
        self.assertEqual(quotes, [("Q-AMOUNT-001", "演示客户", 2800)])
        self.assertEqual(
            items,
            [
                ("Q-AMOUNT-001", 0, "咨询", 2, 1250, 2500),
                ("Q-AMOUNT-001", 1, "资料", 1, 300, 300),
                ("Q-AMOUNT-001", 2, "赠品", 4, 0, 0),
            ],
        )
        # 数量、单价、行金额、合计在 SQLite 中都必须是整数。
        for row in items:
            for value in row[3:]:
                self.assertIs(type(value), int,
                              f"明细数值不是整数: {row}")
        self.assertIs(type(quotes[0][2]), int, "合计不是整数")

        output_path = self.workdir / "preview.html"
        preview = self.run_preview("Q-AMOUNT-001", output_path)
        self.assertEqual(preview.returncode, 0,
                         f"preview 失败: {preview.stderr}")
        self.assertEqual(preview.stderr, "")
        self.assertEqual(preview.stdout.strip(), str(output_path))

        document = output_path.read_text(encoding="utf-8")
        # 明细顺序与录入一致。
        positions = [document.index(name) for name in ("咨询", "资料", "赠品")]
        self.assertEqual(positions, sorted(positions),
                         "HTML 中明细顺序与录入不一致")
        # 数量保持原样。
        for quantity in (">2<", ">1<", ">4<"):
            self.assertIn(quantity, document)
        # 单价与行金额换算为元、保留两位小数。
        for amount in (">12.50<", ">3.00<", ">0.00<", ">25.00<"):
            self.assertIn(amount, document,
                          f"HTML 缺少金额 {amount}")
        # 行金额 25.00 / 3.00 / 0.00 按明细顺序出现。
        line_positions = [document.index(f">{a}<")
                          for a in ("25.00", "3.00", "0.00")]
        self.assertEqual(line_positions, sorted(line_positions))
        # 总计 2800 分 = 28.00 元。
        self.assertIn(">28.00<", document)

        self.assert_project_dir_clean()

    def test_int64_upper_bound_roundtrip(self):
        """单价取 INT64 上界：保存不丢精度，预览金额精确。"""
        payload = make_payload(
            number="Q-AMOUNT-MAX",
            items=[{"description": "上界", "quantity": 1,
                    "unit_price": INT64_MAX}],
        )
        save = self.run_save(payload)
        self.assertEqual(save.returncode, 0, f"save 失败: {save.stderr}")
        self.assertEqual(save.stdout.strip(), "Q-AMOUNT-MAX")

        quotes, items = self.fetch_all()
        self.assertEqual(quotes, [("Q-AMOUNT-MAX", "演示客户", INT64_MAX)])
        self.assertEqual(items[0][3:], (1, INT64_MAX, INT64_MAX))
        self.assertIs(type(quotes[0][2]), int)
        self.assertIs(type(items[0][5]), int)

        output_path = self.workdir / "max.html"
        preview = self.run_preview("Q-AMOUNT-MAX", output_path)
        self.assertEqual(preview.returncode, 0,
                         f"preview 失败: {preview.stderr}")
        document = output_path.read_text(encoding="utf-8")
        self.assertIn(">92233720368547758.07<", document)

        self.assert_project_dir_clean()


# 无效样例：名称、对基础样例的修改、标准错误中应出现的关键字。
def _invalid_cases():
    base = make_payload()

    def with_item(index, **changes):
        payload = make_payload()
        payload["items"][index].update(changes)
        return payload

    overflow_price = (INT64_MAX // 2) + 1  # 4611686018427387904
    total_overflow = make_payload()
    total_overflow["items"] = [
        {"description": "甲", "quantity": 1, "unit_price": overflow_price},
        {"description": "乙", "quantity": 1, "unit_price": overflow_price},
    ]

    return [
        ("quantity_zero",
         with_item(0, quantity=0), ["数量"]),
        ("negative_unit_price",
         with_item(0, unit_price=-1), ["单价"]),
        ("bool_quantity",
         with_item(0, quantity=True), ["数量"]),
        ("bool_unit_price",
         with_item(0, unit_price=False), ["单价"]),
        ("float_quantity",
         with_item(0, quantity=1.5), ["数量"]),
        ("float_unit_price",
         with_item(0, unit_price=12.5), ["单价"]),
        ("quantity_over_int64",
         with_item(0, quantity=INT64_MAX + 1), ["数量", "超出"]),
        ("unit_price_over_int64",
         with_item(0, unit_price=INT64_MAX + 1), ["金额", "超出"]),
        ("line_amount_overflow",
         with_item(0, quantity=2, unit_price=overflow_price),
         ["行金额", "超出"]),
        ("total_overflow",
         total_overflow, ["合计", "超出"]),
    ]


INVALID_CASES = _invalid_cases()


class TestInvalidInputRejected(AmountFlowTestCase):
    """无效输入：退出码 1、stdout 为空、stderr 以“错误:”开头并说明问题。"""

    def test_rejected_without_creating_database(self):
        for name, payload, keywords in INVALID_CASES:
            with self.subTest(case=name):
                self.assertFalse(self.db_path.exists())
                result = self.run_save(payload)
                self.assertEqual(result.returncode, 1,
                                 f"样例 {name} 退出码: {result.returncode}")
                self.assertEqual(result.stdout, "",
                                 f"样例 {name} 标准输出应为空")
                self.assertTrue(result.stderr.startswith("错误:"),
                                f"样例 {name} 标准错误: {result.stderr!r}")
                for keyword in keywords:
                    self.assertIn(keyword, result.stderr,
                                  f"样例 {name} 标准错误未说明问题")
                # 校验失败不得创建原本不存在的数据库。
                self.assertFalse(self.db_path.exists(),
                                 f"样例 {name} 不应创建数据库文件")
        self.assert_project_dir_clean()

    def test_rejected_with_existing_database_unchanged(self):
        # 先用一条合法报价建立已有数据库。
        seed = make_payload(number="Q-SEED-001")
        seed_save = self.run_save(seed)
        self.assertEqual(seed_save.returncode, 0,
                         f"种子数据保存失败: {seed_save.stderr}")
        before = self.fetch_all()

        for name, payload, keywords in INVALID_CASES:
            with self.subTest(case=name):
                payload["number"] = f"Q-BAD-{name}"
                result = self.run_save(payload)
                self.assertEqual(result.returncode, 1,
                                 f"样例 {name} 退出码: {result.returncode}")
                self.assertEqual(result.stdout, "")
                self.assertTrue(result.stderr.startswith("错误:"),
                                f"样例 {name} 标准错误: {result.stderr!r}")
                for keyword in keywords:
                    self.assertIn(keyword, result.stderr)
                # 已有记录保持不变，不留下本次报价或明细。
                self.assertEqual(self.fetch_all(), before,
                                 f"样例 {name} 改变了已有数据")
        self.assert_project_dir_clean()


if __name__ == "__main__":
    unittest.main()
