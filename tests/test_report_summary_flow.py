"""report 重构后的纯规则回归测试 + report 公开入口验收。

重点核对本次重构抽出的纯规则函数——
validate_report_customer_filter / summarize_quote_rows /
summarize_report_grand_total / render_report_json——可以在不打开数据库、
不创建输出文件的情况下独立验证；再通过 README 记载的公开命令（save /
report）以子进程跑同一组固定样例，确认重构后公开行为（命令参数、JSON
与 HTML 数据格式、错误次序与退出码）保持不变。

仅依赖 Python 标准库。CLI 用例全部在独立临时目录中执行，不在项目目录
留下 JSON、SQLite 或 HTML 文件。

运行方式（项目根目录）：
    python3 -m unittest discover -s tests
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUOTE_PY = os.path.join(PROJECT_ROOT, "quote.py")

# 直接按路径加载 quote.py（文件名带模块名保留为 quote）。
_spec = importlib.util.spec_from_file_location("quote", QUOTE_PY)
quote = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(quote)

INT64_MAX = (1 << 63) - 1

# 固定样例：同一客户“演示客户”的两张报价——2800 分（多条明细）与零金额。
CUSTOMER = "演示客户"
ROWS_2800_AND_ZERO = [(CUSTOMER, 2800), (CUSTOMER, 0)]

# 客户原文差异样例：Demo / demo / " Demo " 是三个不同客户。
ROWS_TEXT_DIFFS = [
    ("Demo", 2500),
    ("demo", 300),
    (" Demo ", 500),
    ("Demo", 0),
]

# 保存用的完整报价 JSON：2800 分来自两条明细（2*1250 + 1*300），
# 报表仍应张数 2、累计直接读已保存 total 2800，而不是按明细重算。
SAVE_SAMPLES_2800 = [
    {
        "number": "Q-FIX-1",
        "customer": CUSTOMER,
        "items": [
            {"description": "咨询", "quantity": 2, "unit_price": 1250},
            {"description": "资料", "quantity": 1, "unit_price": 300},
        ],
    },
    {
        "number": "Q-FIX-2",
        "customer": CUSTOMER,
        "items": [{"description": "赠品", "quantity": 1, "unit_price": 0}],
    },
]


class SummarizePureRulesTestCase(unittest.TestCase):
    """纯函数直测：不打开数据库、不创建任何文件。"""

    def test_fixed_sample_2800_and_zero_counts_two(self):
        records = quote.summarize_quote_rows(ROWS_2800_AND_ZERO)
        self.assertEqual(records, [(CUSTOMER, 2, 2800)])

    def test_zero_amount_still_counts(self):
        records = quote.summarize_quote_rows([("甲", 0), ("甲", 0)])
        self.assertEqual(records, [("甲", 2, 0)])

    def test_int64_overflow_sum_is_exact_python_int(self):
        big = INT64_MAX
        records = quote.summarize_quote_rows([("大额客户", big), ("大额客户", big)])
        customer, count, subtotal = records[0]
        self.assertEqual(customer, "大额客户")
        self.assertEqual(count, 2)
        self.assertEqual(subtotal, 18446744073709551614)
        self.assertIsInstance(subtotal, int)
        self.assertGreater(subtotal, INT64_MAX)

    def test_customer_original_text_differences_stay_separate(self):
        records = quote.summarize_quote_rows(ROWS_TEXT_DIFFS)
        # 空格 < 大写 < 小写：按客户原文 UTF-8 BINARY 升序，与读入顺序无关。
        self.assertEqual(
            records,
            [(" Demo ", 1, 500), ("Demo", 2, 2500), ("demo", 1, 300)],
        )

    def test_newline_and_case_participate_in_comparison(self):
        rows = [("A\nb", 1), ("AB", 1), ("a\nb", 1), ("A\rb", 1)]
        records = quote.summarize_quote_rows(rows)
        # UTF-8 字节序：0x0A(\n) < 0x0D(\r) < 0x42(B) < 0x61(a)。
        self.assertEqual(
            [customer for customer, _, _ in records],
            ["A\nb", "A\rb", "AB", "a\nb"],
        )
        # 精确筛选：换行不同即不命中。
        self.assertEqual(
            quote.summarize_quote_rows(rows, "A\nb"), [("A\nb", 1, 1)]
        )

    def test_percent_and_underscore_are_literal_in_filter(self):
        rows = [("100%_特价", 100), ("100_特价", 200)]
        self.assertEqual(
            quote.summarize_quote_rows(rows, "100%_特价"),
            [("100%_特价", 1, 100)],
        )
        # LIKE 风格的通配写法不做模式匹配：筛 "100_特价" 只命中后者，
        # % 与 _ 都是普通字符。
        self.assertEqual(
            quote.summarize_quote_rows(rows, "100_特价"),
            [("100_特价", 1, 200)],
        )

    def test_filter_no_match_returns_empty(self):
        records = quote.summarize_quote_rows(ROWS_TEXT_DIFFS, "无此客户")
        self.assertEqual(records, [])

    def test_filter_preserves_surrounding_whitespace(self):
        # 含有效文字时两端空白原样保留并参与比较。
        self.assertEqual(
            quote.validate_report_customer_filter(" Demo "), " Demo "
        )
        self.assertEqual(
            quote.summarize_quote_rows(ROWS_TEXT_DIFFS, " Demo "),
            [(" Demo ", 1, 500)],
        )

    def test_blank_filter_rejected_without_db_or_file_semantics(self):
        for blank in ("", " ", "\t", " \n\t "):
            with self.subTest(value=repr(blank)):
                with self.assertRaises(ValueError) as ctx:
                    quote.validate_report_customer_filter(blank)
                self.assertEqual(str(ctx.exception), "客户筛选值不能为空白")
        # None 表示未提供筛选，合法。
        self.assertIsNone(quote.validate_report_customer_filter(None))

    def test_empty_input_rows_returns_empty_records(self):
        self.assertEqual(quote.summarize_quote_rows([]), [])
        self.assertEqual(quote.summarize_quote_rows([], "任意"), [])

    def test_grand_total_only_covers_current_records(self):
        records = quote.summarize_quote_rows(ROWS_2800_AND_ZERO)
        self.assertEqual(quote.summarize_report_grand_total(records), (2, 2800))
        # 空结果合计为 0 张、0 分。
        self.assertEqual(quote.summarize_report_grand_total([]), (0, 0))
        # 合计随筛选收窄：未命中客户不计入。
        filtered = quote.summarize_quote_rows(ROWS_TEXT_DIFFS, "demo")
        self.assertEqual(quote.summarize_report_grand_total(filtered), (1, 300))
        # 超 64 位合计仍精确。
        big_records = quote.summarize_quote_rows(
            [("x", INT64_MAX), ("y", INT64_MAX)]
        )
        self.assertEqual(
            quote.summarize_report_grand_total(big_records),
            (2, 18446744073709551614),
        )

    def test_render_json_single_line_trailing_newline_raw_text(self):
        records = quote.summarize_quote_rows(ROWS_2800_AND_ZERO)
        text = quote.render_report_json(records)
        self.assertTrue(text.endswith("\n"))
        self.assertEqual(text.count("\n"), 1)
        self.assertNotIn("\r", text)
        self.assertEqual(
            json.loads(text),
            [{"customer": CUSTOMER, "quote_count": 2, "total": 2800}],
        )
        # 客户原文不做 HTML 转义；大整数完整十进制输出，无科学计数法。
        big_text = quote.render_report_json(
            quote.summarize_quote_rows(
                [("演<甲>&乙", INT64_MAX), ("演<甲>&乙", INT64_MAX)]
            )
        )
        self.assertIn("演<甲>&乙", big_text)
        self.assertIn("18446744073709551614", big_text)
        self.assertNotRegex(big_text, r"[eE]\+")

    def test_render_json_empty_is_empty_array_line(self):
        self.assertEqual(quote.render_report_json([]), "[]\n")

    def test_customer_named_total_is_independent_group(self):
        # 客户原文就是“合计”时仍是普通分组，纯规则层不做任何替换。
        records = quote.summarize_quote_rows(
            [("合计", 700), (CUSTOMER, 2800), (CUSTOMER, 0)]
        )
        self.assertEqual(
            records,
            [("合计", 1, 700), (CUSTOMER, 2, 2800)],
        )
        self.assertEqual(
            quote.summarize_report_grand_total(records), (3, 3500)
        )


class ReportCliAcceptanceTestCase(unittest.TestCase):
    """通过 report 公开入口验证同一组固定样例（save -> report 子进程）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="quote-report-refactor-")
        self.addCleanup(self._tmp.cleanup)
        self.workdir = self._tmp.name

    def run_quote(self, *cli_args):
        return subprocess.run(
            [sys.executable, QUOTE_PY, *cli_args],
            capture_output=True,
            text=True,
            cwd=self.workdir,
        )

    def write_input(self, payload, name):
        path = os.path.join(self.workdir, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        return path

    def save_all(self, samples):
        db_path = os.path.join(self.workdir, "quotes.sqlite")
        for index, payload in enumerate(samples):
            input_path = self.write_input(payload, f"input-{index}.json")
            result = self.run_quote("save", "--db", db_path, "--input", input_path)
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertEqual(result.stderr, "")
        return db_path

    def test_report_json_fixed_sample_two_quotes_2800(self):
        self.save_all(SAVE_SAMPLES_2800)
        result = self.run_quote("report", "--db", "quotes.sqlite")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout.count("\n"), 1)
        self.assertTrue(result.stdout.endswith("\n"))
        self.assertEqual(
            json.loads(result.stdout),
            [{"customer": CUSTOMER, "quote_count": 2, "total": 2800}],
        )

    def test_report_html_fixed_sample_shows_28_yuan(self):
        self.save_all(SAVE_SAMPLES_2800)
        result = self.run_quote(
            "report", "--db", "quotes.sqlite", "--output", "report.html"
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "")
        # 标准输出只回显传入路径。
        self.assertEqual(result.stdout, "report.html\n")
        with open(os.path.join(self.workdir, "report.html"), encoding="utf-8") as f:
            document = f.read()
        # 两张报价（含零金额）-> 2 张、28.00 元；合计行同样为 2 张 28.00。
        self.assertIn('<td class="num">2</td>', document)
        self.assertIn("28.00", document)
        self.assertIn("<td>合计</td>", document)
        self.assertEqual(document.count("28.00"), 2, msg="客户行与合计行各一处")

    def test_report_json_overflow_total_exact_via_cli(self):
        self.save_all(
            [
                {
                    "number": f"Q-BIG-{index}",
                    "customer": "大额客户",
                    "items": [
                        {"description": "上限单价", "quantity": 1,
                         "unit_price": INT64_MAX}
                    ],
                }
                for index in range(2)
            ]
        )
        result = self.run_quote("report", "--db", "quotes.sqlite")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        records = json.loads(result.stdout)
        self.assertEqual(
            records,
            [{"customer": "大额客户", "quote_count": 2,
              "total": 18446744073709551614}],
        )
        self.assertIn("18446744073709551614", result.stdout)

    def test_report_customer_text_differences_via_cli(self):
        self.save_all(
            [
                {"number": "Q-1", "customer": "Demo",
                 "items": [{"description": "x", "quantity": 1, "unit_price": 2500}]},
                {"number": "Q-2", "customer": "demo",
                 "items": [{"description": "x", "quantity": 1, "unit_price": 300}]},
                {"number": "Q-3", "customer": " Demo ",
                 "items": [{"description": "x", "quantity": 1, "unit_price": 500}]},
                {"number": "Q-4", "customer": "Demo",
                 "items": [{"description": "x", "quantity": 1, "unit_price": 0}]},
            ]
        )
        result = self.run_quote("report", "--db", "quotes.sqlite")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            [
                {"customer": " Demo ", "quote_count": 1, "total": 500},
                {"customer": "Demo", "quote_count": 2, "total": 2500},
                {"customer": "demo", "quote_count": 1, "total": 300},
            ],
        )
        # 精确筛选只命中原文字符串完全相同的客户。
        filtered = self.run_quote(
            "report", "--db", "quotes.sqlite", "--customer", "demo"
        )
        self.assertEqual(filtered.returncode, 0, msg=filtered.stderr)
        self.assertEqual(
            json.loads(filtered.stdout),
            [{"customer": "demo", "quote_count": 1, "total": 300}],
        )

    def test_report_no_match_empty_outputs_via_cli(self):
        self.save_all(SAVE_SAMPLES_2800)
        result = self.run_quote(
            "report", "--db", "quotes.sqlite", "--customer", "无此客户"
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stdout, "[]\n")

        html_result = self.run_quote(
            "report", "--db", "quotes.sqlite", "--customer", "无此客户",
            "--output", "empty.html",
        )
        self.assertEqual(html_result.returncode, 0, msg=html_result.stderr)
        self.assertEqual(html_result.stdout, "empty.html\n")
        with open(os.path.join(self.workdir, "empty.html"), encoding="utf-8") as f:
            document = f.read()
        self.assertIn("没有匹配的报价", document)
        self.assertIn("<th>客户</th>", document)
        # 空态合计：0 张、0.00 元。
        self.assertIn("<td>合计</td>", document)
        self.assertIn('<td class="num">0</td>', document)
        self.assertIn("0.00", document)

    def test_report_blank_customer_rejected_before_db_and_output(self):
        db_path = os.path.join(self.workdir, "quotes.sqlite")
        self.assertFalse(os.path.exists(db_path))
        for value in ("", " ", "\t", " \n\t "):
            with self.subTest(value=repr(value)):
                result = self.run_quote(
                    "report", "--db", "quotes.sqlite",
                    "--output", "out.html", "--customer", value,
                )
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertIn("客户筛选值不能为空白", result.stderr)
                self.assertFalse(os.path.exists(db_path))
                self.assertFalse(os.path.exists(os.path.join(self.workdir, "out.html")))

    def test_report_check_order_existing_output_before_db(self):
        # 输出已存在 -> 报告“输出文件已存在”并保留原文件，且不读/建数据库。
        out_path = os.path.join(self.workdir, "out.html")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("MARKER")
        result = self.run_quote(
            "report", "--db", "missing.sqlite", "--output", "out.html"
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("输出文件已存在", result.stderr)
        with open(out_path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "MARKER")
        self.assertFalse(os.path.exists(os.path.join(self.workdir, "missing.sqlite")))

    def test_report_missing_db_no_files_stdout_empty(self):
        result = self.run_quote(
            "report", "--db", "nope.sqlite", "--output", "out.html"
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("无法读取数据库", result.stderr)
        self.assertIn("nope.sqlite", result.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.workdir, "nope.sqlite")))
        self.assertFalse(os.path.exists(os.path.join(self.workdir, "out.html")))

    def test_report_missing_output_directory_reports_write_error(self):
        self.save_all(SAVE_SAMPLES_2800)
        out_arg = os.path.join("nodir", "report.html")
        result = self.run_quote(
            "report", "--db", "quotes.sqlite", "--output", out_arg
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("无法写入输出文件", result.stderr)
        self.assertIn(out_arg, result.stderr)


_PROJECT_ROOT_ENTRIES_AT_START = set(os.listdir(PROJECT_ROOT))


def tearDownModule():
    leftovers = set(os.listdir(PROJECT_ROOT)) - _PROJECT_ROOT_ENTRIES_AT_START
    assert not leftovers, f"测试在项目根目录留下了文件: {sorted(leftovers)}"


if __name__ == "__main__":
    unittest.main()
