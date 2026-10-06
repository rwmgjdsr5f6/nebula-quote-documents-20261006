"""金额流程回归测试：输入校验 -> SQLite 持久化 -> HTML 预览。

仅依赖 Python 标准库。所有用例在独立的临时目录中运行子进程，
不读取项目内已有业务数据，也不在项目目录留下数据库或 HTML 文件。

运行方式（项目根目录）：
    python3 -m unittest discover -s tests
全部通过时退出码为 0，任一失败时非零退出并指出对应样例与预期。
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUOTE_PY = os.path.join(PROJECT_ROOT, "quote.py")

INT64_MAX = (1 << 63) - 1
# 2 * 4611686018427387904 == 2**63，刚好超出有符号 64 位整数上界。
HALF_OVERFLOW = 1 << 62


def make_payload(items, number="Q-AMOUNT-001", customer="演示客户"):
    return {"number": number, "customer": customer, "items": items}


def make_item(description, quantity, unit_price):
    return {"description": description, "quantity": quantity, "unit_price": unit_price}


class AmountFlowTestCase(unittest.TestCase):
    """每个用例使用独立的临时目录，子进程通过公开命令操作 quote.py。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="quote-amount-test-")
        self.addCleanup(self._tmp.cleanup)
        self.workdir = self._tmp.name

    # ---- 基础设施 ----

    def run_quote(self, *cli_args):
        """在临时目录中以独立进程运行 quote.py 的公开命令。"""
        return subprocess.run(
            [sys.executable, QUOTE_PY, *cli_args],
            capture_output=True,
            text=True,
            cwd=self.workdir,
        )

    def write_input(self, payload, name="input.json"):
        path = os.path.join(self.workdir, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        return path

    def save(self, payload, db_name="quotes.sqlite", input_name="input.json"):
        db_path = os.path.join(self.workdir, db_name)
        input_path = self.write_input(payload, name=input_name)
        result = self.run_quote("save", "--db", db_path, "--input", input_path)
        return result, db_path

    def preview(self, db_path, number, output_name="preview.html"):
        output_path = os.path.join(self.workdir, output_name)
        result = self.run_quote(
            "preview", "--db", db_path, "--number", number, "--output", output_path
        )
        return result, output_path

    def read_rows(self, db_path, number):
        """直接读取 SQLite，返回 (quotes 行, items 行列表)。"""
        conn = sqlite3.connect(db_path)
        try:
            quote_row = conn.execute(
                "SELECT number, customer, total FROM quotes WHERE number = ?",
                (number,),
            ).fetchone()
            item_rows = conn.execute(
                "SELECT description, quantity, unit_price, line_amount "
                "FROM items WHERE quote_number = ? ORDER BY position",
                (number,),
            ).fetchall()
        finally:
            conn.close()
        return quote_row, item_rows

    def assert_save_rejected(self, payload, stderr_keyword, db_name="reject.sqlite"):
        """无效输入：退出码 1、标准输出为空、标准错误以“错误:”开头并含关键字、
        且不创建原本不存在的数据库。"""
        result, db_path = self.save(payload, db_name=db_name)
        self.assertEqual(
            result.returncode, 1,
            msg=f"无效输入应退出码 1，实际 {result.returncode}；stderr={result.stderr!r}",
        )
        self.assertEqual(result.stdout, "", msg="拒绝时标准输出必须为空")
        self.assertTrue(
            result.stderr.startswith("错误:"),
            msg=f"标准错误应以“错误:”开头，实际：{result.stderr!r}",
        )
        self.assertIn(
            stderr_keyword, result.stderr,
            msg=f"标准错误应说明{stderr_keyword}问题，实际：{result.stderr!r}",
        )
        self.assertFalse(
            os.path.exists(db_path),
            msg="校验失败不能创建原本不存在的数据库文件",
        )
        return result

    # ---- 正常流程 ----

    def test_normal_flow_save_then_preview(self):
        payload = make_payload([
            make_item("咨询", 2, 1250),
            make_item("资料", 1, 300),
            make_item("赠品", 4, 0),
        ])

        # 保存：退出码 0，标准输出为编号。
        save_result, db_path = self.save(payload)
        self.assertEqual(save_result.returncode, 0, msg=save_result.stderr)
        self.assertEqual(save_result.stdout.strip(), "Q-AMOUNT-001")

        # 持久化：数量、单价、行金额、合计均为整数，值精确。
        quote_row, item_rows = self.read_rows(db_path, "Q-AMOUNT-001")
        self.assertIsNotNone(quote_row, "保存后数据库中应存在该编号")
        self.assertEqual(quote_row[0], "Q-AMOUNT-001")
        self.assertEqual(quote_row[1], "演示客户")
        self.assertIsInstance(quote_row[2], int)
        self.assertEqual(quote_row[2], 2800, "合计应为 2800 分")

        self.assertEqual(
            item_rows,
            [("咨询", 2, 1250, 2500), ("资料", 1, 300, 300), ("赠品", 4, 0, 0)],
        )
        for row in item_rows:
            for value in row[1:]:
                self.assertIsInstance(value, int, "数量、单价、行金额必须为整数")

        # 预览：退出码 0，标准输出为目标路径。
        preview_result, output_path = self.preview(db_path, "Q-AMOUNT-001")
        self.assertEqual(preview_result.returncode, 0, msg=preview_result.stderr)
        self.assertEqual(preview_result.stdout.strip(), output_path)
        self.assertTrue(os.path.exists(output_path))

        with open(output_path, encoding="utf-8") as f:
            document = f.read()

        # 明细顺序与输入一致。
        positions = [document.index(text) for text in ("咨询", "资料", "赠品")]
        self.assertEqual(positions, sorted(positions), "HTML 明细顺序应与录入一致")

        # 数量、单价、行金额、总计的显示。
        for quantity in (2, 1, 4):
            self.assertIn(f'<td class="num">{quantity}</td>', document)
        for unit_price in ("12.50", "3.00", "0.00"):
            self.assertIn(f'<td class="num">{unit_price}</td>', document)
        for line_amount in ("25.00", "3.00", "0.00"):
            self.assertIn(f'<td class="num">{line_amount}</td>', document)
        self.assertIn('<td class="num">28.00</td>', document, "总计应显示 28.00 元")

    def test_int64_upper_bound_roundtrip(self):
        """单价取有符号 64 位整数上界：保存不丢精度，预览金额精确。"""
        payload = make_payload(
            [make_item("上限服务", 1, INT64_MAX)],
            number="Q-AMOUNT-MAX",
        )
        save_result, db_path = self.save(payload)
        self.assertEqual(save_result.returncode, 0, msg=save_result.stderr)
        self.assertEqual(save_result.stdout.strip(), "Q-AMOUNT-MAX")

        quote_row, item_rows = self.read_rows(db_path, "Q-AMOUNT-MAX")
        self.assertEqual(item_rows, [("上限服务", 1, INT64_MAX, INT64_MAX)])
        self.assertEqual(quote_row[2], INT64_MAX, "合计应精确保留 INT64 上界")

        preview_result, output_path = self.preview(db_path, "Q-AMOUNT-MAX")
        self.assertEqual(preview_result.returncode, 0, msg=preview_result.stderr)
        with open(output_path, encoding="utf-8") as f:
            document = f.read()
        self.assertIn("92233720368547758.07", document, "预览金额应为 92233720368547758.07 元")

    # ---- 拒绝行为 ----

    def test_invalid_inputs_are_rejected(self):
        cases = [
            ("数量为零", [make_item("咨询", 0, 1250)], "数量"),
            ("单价为负数", [make_item("咨询", 1, -1)], "单价"),
            ("数量为布尔值", [make_item("咨询", True, 1250)], "数量"),
            ("单价为布尔值", [make_item("咨询", 1, False)], "单价"),
            ("数量为浮点数", [make_item("咨询", 1.5, 1250)], "数量"),
            ("单价为浮点数", [make_item("咨询", 1, 12.5)], "单价"),
            ("数量超过INT64上界", [make_item("咨询", INT64_MAX + 1, 1)], "数量"),
            ("单价超过INT64上界", [make_item("咨询", 1, INT64_MAX + 1)], "超出"),
            ("单行乘积超界", [make_item("咨询", 2, HALF_OVERFLOW)], "行金额"),
            (
                "合计超界",
                [make_item("咨询", 1, HALF_OVERFLOW), make_item("资料", 1, HALF_OVERFLOW)],
                "合计",
            ),
        ]
        for index, (label, items, keyword) in enumerate(cases):
            with self.subTest(样例=label):
                payload = make_payload(items, number=f"Q-BAD-{index:03d}")
                self.assert_save_rejected(
                    payload, keyword, db_name=f"reject-{index}.sqlite"
                )

    def test_rejected_save_leaves_existing_database_untouched(self):
        """使用已有数据库时，校验失败不留本次报价或明细，已有记录不变。"""
        db_path = os.path.join(self.workdir, "existing.sqlite")

        good_payload = make_payload([make_item("咨询", 2, 1250)], number="Q-KEEP-001")
        good_input = self.write_input(good_payload, name="good.json")
        setup_result = self.run_quote("save", "--db", db_path, "--input", good_input)
        self.assertEqual(setup_result.returncode, 0, msg=setup_result.stderr)

        def snapshot():
            conn = sqlite3.connect(db_path)
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

        before = snapshot()

        invalid_payloads = [
            make_payload([make_item("咨询", 0, 1250)], number="Q-BAD-ZERO"),
            make_payload([make_item("咨询", 1, -5)], number="Q-BAD-NEG"),
            make_payload([make_item("咨询", True, 1250)], number="Q-BAD-BOOL"),
            make_payload([make_item("咨询", 1, 9.9)], number="Q-BAD-FLOAT"),
            make_payload([make_item("咨询", 1, INT64_MAX + 1)], number="Q-BAD-BIG"),
            make_payload([make_item("咨询", 2, HALF_OVERFLOW)], number="Q-BAD-LINE"),
            make_payload(
                [make_item("咨询", 1, HALF_OVERFLOW), make_item("资料", 1, HALF_OVERFLOW)],
                number="Q-BAD-TOTAL",
            ),
        ]
        for index, payload in enumerate(invalid_payloads):
            with self.subTest(编号=payload["number"]):
                input_path = self.write_input(payload, name=f"bad-{index}.json")
                result = self.run_quote("save", "--db", db_path, "--input", input_path)
                self.assertEqual(result.returncode, 1, msg=result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertTrue(result.stderr.startswith("错误:"), msg=result.stderr)

        self.assertEqual(snapshot(), before, "校验失败后已有数据库内容必须保持不变")


class Utf8InputTestCase(unittest.TestCase):
    """非 UTF-8 输入的拒绝路径：可预测报错、无数据库副作用。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="quote-utf8-test-")
        self.addCleanup(self._tmp.cleanup)
        self.workdir = self._tmp.name

    def run_quote(self, *cli_args):
        return subprocess.run(
            [sys.executable, QUOTE_PY, *cli_args],
            capture_output=True,
            text=True,
            cwd=self.workdir,
        )

    def write_bytes(self, data, name="bad.json"):
        path = os.path.join(self.workdir, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def assert_utf8_rejected(self, path, db_path):
        """退出码 1、标准输出为空、标准错误恰为一行编码错误、数据库不创建。"""
        result = self.run_quote("save", "--db", db_path, "--input", path)
        self.assertEqual(
            result.returncode, 1,
            msg=f"非法 UTF-8 应退出码 1，实际 {result.returncode}；stderr={result.stderr!r}",
        )
        self.assertEqual(result.stdout, "", msg="拒绝时标准输出必须为空")
        self.assertEqual(
            result.stderr,
            f"错误: 输入文件不是有效的 UTF-8: {path}\n",
            msg="标准错误应只有一行编码错误并使用 --input 路径原文",
        )
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(
            os.path.exists(db_path),
            msg="编码失败不能创建原本不存在的数据库文件",
        )
        return result

    def test_invalid_utf8_positions_are_rejected(self):
        """文件开头、字符串中间、合法 JSON 末尾的非法字节得到同类结果。"""
        valid_prefix = '{"number": "Q-UTF8-BAD", "customer": "演示客户", "items": [{"description": "'
        valid_suffix = '", "quantity": 2, "unit_price": 1250}]}'
        cases = [
            ("开头FF", b"\xff" + (valid_prefix + "咨询" + valid_suffix).encode("utf-8")),
            ("字符串中间FF", (valid_prefix).encode("utf-8") + b"\xff" + ("咨询" + valid_suffix).encode("utf-8")),
            ("末尾缺末字节E4B8", (valid_prefix + "咨询" + valid_suffix).encode("utf-8") + b"\xe4\xb8"),
        ]
        for index, (label, data) in enumerate(cases):
            with self.subTest(样例=label):
                input_path = self.write_bytes(data, name=f"bad-{index}.json")
                db_path = os.path.join(self.workdir, f"utf8-{index}.sqlite")
                self.assert_utf8_rejected(input_path, db_path)
                with open(input_path, "rb") as f:
                    self.assertEqual(f.read(), data, "输入文件字节内容不得改变")

    def test_path_with_spaces_and_chinese_shown_verbatim(self):
        input_path = self.write_bytes(b"\xff", name="错误 样例.json")
        db_path = os.path.join(self.workdir, "utf8.sqlite")
        self.assert_utf8_rejected(input_path, db_path)

    def test_encoding_error_precedes_database_error(self):
        """--db 指向不存在的父目录时，仍报告编码错误而非数据库错误。"""
        input_path = self.write_bytes(b"\xff")
        db_path = os.path.join(self.workdir, "no-such-dir", "db.sqlite")
        self.assert_utf8_rejected(input_path, db_path)

    def test_rejected_save_leaves_existing_database_untouched(self):
        db_path = os.path.join(self.workdir, "existing.sqlite")
        good_input = os.path.join(self.workdir, "good.json")
        with open(good_input, "w", encoding="utf-8") as f:
            json.dump(make_payload([make_item("咨询", 2, 1250)], number="Q-KEEP-UTF8"),
                      f, ensure_ascii=False)
        setup_result = self.run_quote("save", "--db", db_path, "--input", good_input)
        self.assertEqual(setup_result.returncode, 0, msg=setup_result.stderr)

        bad_input = self.write_bytes(b"\xff")
        result = self.run_quote("save", "--db", db_path, "--input", bad_input)
        self.assertEqual(result.returncode, 1, msg=result.stderr)
        self.assertEqual(result.stdout, "")

        conn = sqlite3.connect(db_path)
        try:
            quotes = conn.execute(
                "SELECT number, customer, total FROM quotes ORDER BY number"
            ).fetchall()
            items = conn.execute(
                "SELECT quote_number, position, description, quantity, "
                "unit_price, line_amount FROM items ORDER BY quote_number, position"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(quotes, [("Q-KEEP-UTF8", "演示客户", 2500)])
        self.assertEqual(items, [("Q-KEEP-UTF8", 0, "咨询", 2, 1250, 2500)])

    def test_valid_utf8_flow_unchanged(self):
        """合法 UTF-8 合成报价：保存与预览行为与既有约定一致。"""
        payload = make_payload(
            [make_item("咨询", 2, 1250)],
            number="Q-UTF8-001",
            customer="演示客户<甲>&乙",
        )
        input_path = os.path.join(self.workdir, "input.json")
        with open(input_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        db_path = os.path.join(self.workdir, "quotes.sqlite")

        save_result = self.run_quote("save", "--db", db_path, "--input", input_path)
        self.assertEqual(save_result.returncode, 0, msg=save_result.stderr)
        self.assertEqual(save_result.stdout.strip(), "Q-UTF8-001")

        output_path = os.path.join(self.workdir, "preview.html")
        preview_result = self.run_quote(
            "preview", "--db", db_path, "--number", "Q-UTF8-001", "--output", output_path
        )
        self.assertEqual(preview_result.returncode, 0, msg=preview_result.stderr)
        with open(output_path, encoding="utf-8") as f:
            document = f.read()
        self.assertIn('<td class="num">12.50</td>', document, "单价应显示 12.50 元")
        self.assertIn('<td class="num">25.00</td>', document, "行金额应显示 25.00 元")
        self.assertIn("演示客户&lt;甲&gt;&amp;乙", document, "客户文字应正确转义")

    def test_valid_utf8_but_invalid_json_keeps_parse_error(self):
        input_path = os.path.join(self.workdir, "broken.json")
        with open(input_path, "w", encoding="utf-8") as f:
            f.write('{"number": "Q-UTF8-002", ')
        db_path = os.path.join(self.workdir, "json.sqlite")
        result = self.run_quote("save", "--db", db_path, "--input", input_path)
        self.assertEqual(result.returncode, 1, msg=result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertTrue(
            result.stderr.startswith("错误: JSON 解析失败:"),
            msg=f"JSON 语法错误应保留解析失败前缀，实际：{result.stderr!r}",
        )
        self.assertFalse(os.path.exists(db_path))


if __name__ == "__main__":
    unittest.main()
