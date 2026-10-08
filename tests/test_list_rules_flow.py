"""列表筛选/排序规则的独立验证，及其与公开 list 命令的对照回归测试。

本次重构把 list 的筛选与排序规则从命令行与数据库中抽出为纯函数
arrange_quote_summaries（配套 validate_list_customer_filter、
render_list_json、read_list_rows），公开 list 入口与脱离命令行、数据
库的独立验证调用同一套规则。本文件用同组固定摘要同时验证两侧结果一致。

固定样例（输入顺序刻意为 Q-B、Q-C、Q-A）：
    Q-B 客户 "Demo"   total 2800 分
    Q-C 客户 "demo"   total  300 分
    Q-A 客户 " Demo " total    0 分（两端各一个空格）

无筛选时按编号原文 BINARY 升序得到 Q-A、Q-B、Q-C；筛选 "Demo" 时仅
保留 Q-B（大小写与两端空格参与逐字符精确匹配）。

独立验证一侧只调用纯函数：不访问数据库、不创建文件、不写标准输出。
公开命令一侧在临时目录中以子进程运行 quote.py list（使用仅有
number/customer/total 三列、无 items 表的旧库结构），两侧的成功输出
必须逐字节一致、退出码一致。

运行方式（项目根目录）：
    python3 -m unittest discover -s tests
"""

import contextlib
import importlib.util
import io
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

# 直接按文件路径加载 quote.py：独立验证只依赖模块内纯函数，不经过命令行。
_spec = importlib.util.spec_from_file_location("quote_under_test", QUOTE_PY)
quote = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(quote)

NUMBER_A = "Q-A"
NUMBER_B = "Q-B"
NUMBER_C = "Q-C"

CUSTOMER_DEMO = "Demo"
CUSTOMER_LOWER = "demo"
CUSTOMER_PADDED = " Demo "

# 输入顺序固定为 Q-B、Q-C、Q-A，与任何期望输出顺序都不同。
INPUT_ORDER_SUMMARIES = [
    (NUMBER_B, CUSTOMER_DEMO, 2800),
    (NUMBER_C, CUSTOMER_LOWER, 300),
    (NUMBER_A, CUSTOMER_PADDED, 0),
]

EXPECTED_ALL = [
    (NUMBER_A, CUSTOMER_PADDED, 0),
    (NUMBER_B, CUSTOMER_DEMO, 2800),
    (NUMBER_C, CUSTOMER_LOWER, 300),
]
EXPECTED_FILTER_DEMO = [(NUMBER_B, CUSTOMER_DEMO, 2800)]
EXPECTED_FILTER_LOWER = [(NUMBER_C, CUSTOMER_LOWER, 300)]
EXPECTED_FILTER_PADDED = [(NUMBER_A, CUSTOMER_PADDED, 0)]

EXPECTED_ALL_JSON = (
    '[{"number": "Q-A", "customer": " Demo ", "total": 0}, '
    '{"number": "Q-B", "customer": "Demo", "total": 2800}, '
    '{"number": "Q-C", "customer": "demo", "total": 300}]'
    "\n"
)
EXPECTED_FILTER_DEMO_JSON = (
    '[{"number": "Q-B", "customer": "Demo", "total": 2800}]' "\n"
)

# 旧库结构：quotes 只有 number/customer/total，且没有 items 表。
LEGACY_SCHEMA = """
CREATE TABLE quotes (
    number   TEXT PRIMARY KEY,
    customer TEXT NOT NULL,
    total    INTEGER NOT NULL
);
"""

BLANK_FILTER_ERROR = "错误: 客户筛选值不能为空白\n"
READ_DB_ERROR_PREFIX = "错误: 无法读取数据库 "


class IndependentListRulesTestCase(unittest.TestCase):
    """对已读取的合法摘要直接验证纯规则：不访问数据库、不建文件、不写 stdout。"""

    def run_quietly(self, func, *args, **kwargs):
        """调用纯函数并捕获标准输出：规则验证不得产生任何 stdout。"""
        sink = io.StringIO()
        with contextlib.redirect_stdout(sink):
            result = func(*args, **kwargs)
        self.assertEqual(
            sink.getvalue(), "",
            msg="独立规则验证不得写标准输出",
        )
        return result

    def test_no_filter_sorts_binary_regardless_of_input_order(self):
        result = self.run_quietly(
            quote.arrange_quote_summaries, INPUT_ORDER_SUMMARIES
        )
        self.assertEqual(result, EXPECTED_ALL)
        self.assertEqual(
            [number for number, _, _ in result],
            [NUMBER_A, NUMBER_B, NUMBER_C],
            msg="按编号原文 BINARY 升序：Q-A、Q-B、Q-C，与输入顺序无关",
        )

        # 同组摘要反向输入，结果必须完全一致。
        reversed_result = self.run_quietly(
            quote.arrange_quote_summaries, list(reversed(INPUT_ORDER_SUMMARIES))
        )
        self.assertEqual(reversed_result, EXPECTED_ALL)

    def test_filter_is_exact_character_for_character(self):
        self.assertEqual(
            self.run_quietly(
                quote.arrange_quote_summaries, INPUT_ORDER_SUMMARIES, CUSTOMER_DEMO
            ),
            EXPECTED_FILTER_DEMO,
            msg="筛选 Demo 只保留 Q-B：demo 与 ' Demo ' 均不命中",
        )
        self.assertEqual(
            self.run_quietly(
                quote.arrange_quote_summaries, INPUT_ORDER_SUMMARIES, CUSTOMER_LOWER
            ),
            EXPECTED_FILTER_LOWER,
        )
        self.assertEqual(
            self.run_quietly(
                quote.arrange_quote_summaries, INPUT_ORDER_SUMMARIES, CUSTOMER_PADDED
            ),
            EXPECTED_FILTER_PADDED,
            msg="两端空格参与匹配：' Demo ' 只命中 Q-A",
        )
        self.assertEqual(
            self.run_quietly(
                quote.arrange_quote_summaries, INPUT_ORDER_SUMMARIES, "DEMO"
            ),
            [],
            msg="合法但无匹配的筛选返回空列表，不报错",
        )

    def test_percent_and_underscore_are_literal_in_filter(self):
        rows = [
            ("Q-WILD", "Demo%", 100),
            ("Q-X", "DemoX", 200),
            ("Q-U", "Demo_", 300),
        ]
        self.assertEqual(
            self.run_quietly(quote.arrange_quote_summaries, rows, "Demo%"),
            [("Q-WILD", "Demo%", 100)],
            msg="% 是普通字符，不是 LIKE 通配符",
        )
        self.assertEqual(
            self.run_quietly(quote.arrange_quote_summaries, rows, "Demo_"),
            [("Q-U", "Demo_", 300)],
            msg="_ 是普通字符，不匹配单个任意字符",
        )

    def test_filter_keeps_saved_total_without_recomputing(self):
        # 规则层只搬运入参 total：即使数值与明细无关也原样保留，不重算。
        rows = [("Q-T", "Demo", 9223372036854775807)]
        result = self.run_quietly(
            quote.arrange_quote_summaries, rows, "Demo"
        )
        self.assertEqual(result, [("Q-T", "Demo", 9223372036854775807)])

    def test_empty_input_yields_empty_result(self):
        self.assertEqual(
            self.run_quietly(quote.arrange_quote_summaries, []), []
        )
        self.assertEqual(
            self.run_quietly(quote.arrange_quote_summaries, [], "Demo"), []
        )

    def test_rules_do_not_mutate_input(self):
        source = list(INPUT_ORDER_SUMMARIES)
        snapshot = list(source)
        self.run_quietly(quote.arrange_quote_summaries, source, CUSTOMER_DEMO)
        self.assertEqual(source, snapshot, msg="纯规则不得改写入参序列")

    def test_customer_filter_validation(self):
        self.assertIsNone(quote.validate_list_customer_filter(None))
        self.assertEqual(
            quote.validate_list_customer_filter(CUSTOMER_PADDED),
            CUSTOMER_PADDED,
            msg="含有效文字时两端空白原样保留",
        )
        for blank in ("", " ", "\t", "\n", " \t\n "):
            with self.subTest(blank=repr(blank)):
                with self.assertRaises(ValueError) as ctx:
                    quote.validate_list_customer_filter(blank)
                self.assertEqual(str(ctx.exception), "客户筛选值不能为空白")

    def test_render_list_json_fixed_expectations(self):
        text = self.run_quietly(
            quote.render_list_json,
            self.run_quietly(quote.arrange_quote_summaries, INPUT_ORDER_SUMMARIES),
        )
        self.assertEqual(text, EXPECTED_ALL_JSON)
        self.assertEqual(
            json.loads(text),
            [
                {"number": NUMBER_A, "customer": CUSTOMER_PADDED, "total": 0},
                {"number": NUMBER_B, "customer": CUSTOMER_DEMO, "total": 2800},
                {"number": NUMBER_C, "customer": CUSTOMER_LOWER, "total": 300},
            ],
        )
        filtered = self.run_quietly(
            quote.arrange_quote_summaries, INPUT_ORDER_SUMMARIES, CUSTOMER_DEMO
        )
        self.assertEqual(
            self.run_quietly(quote.render_list_json, filtered),
            EXPECTED_FILTER_DEMO_JSON,
        )
        self.assertEqual(self.run_quietly(quote.render_list_json, []), "[]\n")

    def test_render_list_html_amounts_and_empty_notice(self):
        records = self.run_quietly(
            quote.arrange_quote_summaries, INPUT_ORDER_SUMMARIES
        )
        document = self.run_quietly(quote.render_list_html, records)
        # 零金额显示 0.00，2800 分显示 28.00，客户空格在同一单元格内可见。
        self.assertIn("<title>报价列表</title>", document)
        self.assertIn(
            "<tr><th>报价编号</th><th>客户</th><th>合计（元）</th></tr>",
            document,
        )
        self.assertIn(f"<td>{NUMBER_A}</td><td>{CUSTOMER_PADDED}</td>", document)
        self.assertIn('<td class="num">0.00</td>', document)
        self.assertIn('<td class="num">28.00</td>', document)
        self.assertIn('<td class="num">3.00</td>', document)
        self.assertNotIn("没有匹配的报价", document)

        empty_document = self.run_quietly(quote.render_list_html, [])
        self.assertIn("<th>报价编号</th>", empty_document)
        self.assertIn("没有匹配的报价", empty_document)

        # 有符号 64 位上限的分单位整数精确显示为元，不走浮点。
        big_document = self.run_quietly(
            quote.render_list_html, [("Q-MAX", "大额客户", INT64_MAX)]
        )
        self.assertIn("92233720368547758.07", big_document)
        self.assertEqual(
            quote.format_yuan(INT64_MAX), "92233720368547758.07"
        )


class RulesVsPublicCommandTestCase(unittest.TestCase):
    """同组固定数据：独立规则结果与公开 list 命令的成功输出逐字节一致。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="quote-list-rules-")
        self.addCleanup(self._tmp.cleanup)
        self.workdir = self._tmp.name

    def build_legacy_database(self, summaries, db_name="demo.sqlite"):
        """直接构造只有三列、无 items 表的旧库；列表只读不应补任何结构。"""
        db_path = os.path.join(self.workdir, db_name)
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(LEGACY_SCHEMA)
            conn.executemany(
                "INSERT INTO quotes(number, customer, total) VALUES (?, ?, ?)",
                summaries,
            )
            conn.commit()
        finally:
            conn.close()
        return db_name

    def run_quote(self, *cli_args):
        return subprocess.run(
            [sys.executable, QUOTE_PY, *cli_args],
            capture_output=True,
            text=True,
            cwd=self.workdir,
        )

    def assert_command_matches_rules(self, db_name, customer_filter):
        """公开 list JSON 与纯规则 JSON 必须逐字节一致，退出码同为 0。"""
        pure_records = quote.arrange_quote_summaries(
            INPUT_ORDER_SUMMARIES, customer_filter
        )
        expected_text = quote.render_list_json(pure_records)

        cli_args = ["list", "--db", db_name]
        if customer_filter is not None:
            cli_args += ["--customer", customer_filter]
        result = self.run_quote(*cli_args)

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            result.stdout, expected_text,
            msg="公开命令 JSON 必须与独立规则结果逐字节一致",
        )
        self.assertEqual(json.loads(result.stdout), json.loads(expected_text))
        return pure_records

    def test_json_success_outputs_match_for_all_and_filtered(self):
        db_name = self.build_legacy_database(INPUT_ORDER_SUMMARIES)
        self.assert_command_matches_rules(db_name, None)
        self.assert_command_matches_rules(db_name, CUSTOMER_DEMO)
        self.assert_command_matches_rules(db_name, CUSTOMER_LOWER)
        self.assert_command_matches_rules(db_name, CUSTOMER_PADDED)
        self.assert_command_matches_rules(db_name, "DEMO")
        self.assert_command_matches_rules(db_name, "无此客户")

    def test_html_success_output_matches_rules_byte_for_byte(self):
        db_name = self.build_legacy_database(INPUT_ORDER_SUMMARIES)
        for out_name, customer_filter in (
            ("all.html", None),
            ("demo.html", CUSTOMER_DEMO),
            ("none.html", "无此客户"),
        ):
            with self.subTest(output=out_name):
                pure_records = quote.arrange_quote_summaries(
                    INPUT_ORDER_SUMMARIES, customer_filter
                )
                expected_bytes = quote.render_list_html(pure_records).encode("utf-8")

                result = self.run_quote(
                    "list", "--db", db_name, "--output", out_name,
                    *(["--customer", customer_filter]
                      if customer_filter is not None else [])
                )
                self.assertEqual(result.returncode, 0, msg=result.stderr)
                self.assertEqual(result.stderr, "")
                self.assertEqual(
                    result.stdout, f"{out_name}\n",
                    msg="HTML 成功时标准输出仅为路径加换行",
                )
                with open(os.path.join(self.workdir, out_name), "rb") as f:
                    actual_bytes = f.read()
                self.assertEqual(
                    actual_bytes, expected_bytes,
                    msg="公开命令 HTML 必须与独立规则渲染结果逐字节一致",
                )

        # 无匹配的 HTML 保留三列表头并显示空态提示。
        with open(os.path.join(self.workdir, "none.html"), encoding="utf-8") as f:
            none_doc = f.read()
        self.assertIn("<th>报价编号</th>", none_doc)
        self.assertIn("没有匹配的报价", none_doc)

    def test_empty_database_matches_empty_rules(self):
        db_name = "empty.sqlite"
        db_path = os.path.join(self.workdir, db_name)
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(LEGACY_SCHEMA)
            conn.commit()
        finally:
            conn.close()

        result = self.run_quote("list", "--db", db_name)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(
            result.stdout, quote.render_list_json([]),
            msg="空库输出必须与空入参的独立规则结果同为 []\\n",
        )
        self.assertEqual(result.stdout, "[]\n")

    def test_int64_max_total_matches_rules(self):
        db_name = self.build_legacy_database(
            [("Q-MAX", "大额客户", INT64_MAX)], db_name="big.sqlite"
        )
        result = self.run_quote("list", "--db", db_name, "--output", "big.html")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        expected = quote.render_list_html(
            quote.arrange_quote_summaries([("Q-MAX", "大额客户", INT64_MAX)])
        ).encode("utf-8")
        with open(os.path.join(self.workdir, "big.html"), "rb") as f:
            self.assertEqual(f.read(), expected)

    def test_legacy_schema_untouched_after_list(self):
        db_name = self.build_legacy_database(INPUT_ORDER_SUMMARIES)
        db_path = os.path.join(self.workdir, db_name)
        conn = sqlite3.connect(db_path)
        try:
            tables_before = [
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            ]
        finally:
            conn.close()

        self.run_quote("list", "--db", db_name)
        self.run_quote("list", "--db", db_name, "--customer", CUSTOMER_DEMO)

        conn = sqlite3.connect(db_path)
        try:
            tables_after = [
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            ]
            columns_after = [
                row[1] for row in conn.execute("PRAGMA table_info(quotes)")
            ]
            rows_after = conn.execute(
                "SELECT number, customer, total FROM quotes"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(tables_after, tables_before, msg="不得补建 items 表")
        self.assertEqual(tables_after, ["quotes"])
        self.assertEqual(columns_after, ["number", "customer", "total"],
                         msg="不得补 note 列")
        self.assertEqual(
            sorted(rows_after),
            sorted(INPUT_ORDER_SUMMARIES),
            msg="旧库记录查询后保持不变",
        )


class ListFailureOrderTestCase(unittest.TestCase):
    """失败顺序与固定提示：空白筛选 -> 输出已存在 -> 读库 -> 写文件。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="quote-list-fail-")
        self.addCleanup(self._tmp.cleanup)
        self.workdir = self._tmp.name

    def run_quote(self, *cli_args):
        return subprocess.run(
            [sys.executable, QUOTE_PY, *cli_args],
            capture_output=True, text=True, cwd=self.workdir,
        )

    def test_blank_customer_rejected_before_db_and_file(self):
        # 数据库与输出文件都不存在：拒绝发生在最前，二者均不得被创建。
        for value in ("", " ", "\t", "\n", " \n\t "):
            with self.subTest(value=repr(value)):
                result = self.run_quote(
                    "list", "--db", "never.sqlite",
                    "--customer", value, "--output", "out.html",
                )
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, BLANK_FILTER_ERROR)
                self.assertFalse(
                    os.path.exists(os.path.join(self.workdir, "never.sqlite"))
                )
                self.assertFalse(
                    os.path.exists(os.path.join(self.workdir, "out.html"))
                )

    def test_existing_output_rejected_before_reading_db(self):
        marker = "MARKER-原内容"
        out_path = os.path.join(self.workdir, "exists.html")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(marker)
        # 数据库不存在：先报“输出文件已存在”，证明文件检查在读库之前。
        result = self.run_quote(
            "list", "--db", "missing.sqlite", "--output", "exists.html"
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(
            result.stderr, "错误: 输出文件已存在: exists.html\n"
        )
        with open(out_path, encoding="utf-8") as f:
            self.assertEqual(f.read(), marker, msg="原内容必须保持不变")

    def test_database_failures_exit_1_with_empty_stdout(self):
        # 数据库不存在：不创建数据库或 HTML。
        result = self.run_quote(
            "list", "--db", "nope.sqlite", "--output", "out.html"
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertTrue(result.stderr.startswith(READ_DB_ERROR_PREFIX))
        self.assertIn("nope.sqlite", result.stderr)
        self.assertEqual(result.stderr.count("\n"), 1)
        self.assertFalse(
            os.path.exists(os.path.join(self.workdir, "nope.sqlite"))
        )
        self.assertFalse(
            os.path.exists(os.path.join(self.workdir, "out.html"))
        )

        # 文件不是有效 SQLite。
        bad_path = os.path.join(self.workdir, "bad.bin")
        with open(bad_path, "wb") as f:
            f.write(b"not a sqlite database\x00\xff")
        result = self.run_quote(
            "list", "--db", "bad.bin", "--output", "bad.html"
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("无法读取数据库", result.stderr)
        with open(bad_path, "rb") as f:
            self.assertEqual(
                f.read(), b"not a sqlite database\x00\xff",
                msg="非数据库文件内容必须逐字节保留",
            )
        self.assertFalse(
            os.path.exists(os.path.join(self.workdir, "bad.html"))
        )

        # 缺 total 列的 quotes 表：按无法读取数据库处理。
        db_path = os.path.join(self.workdir, "no-column.sqlite")
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "CREATE TABLE quotes(number TEXT PRIMARY KEY, customer TEXT)"
            )
            conn.commit()
        finally:
            conn.close()
        result = self.run_quote(
            "list", "--db", "no-column.sqlite", "--output", "nc.html"
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("无法读取数据库", result.stderr)
        self.assertFalse(
            os.path.exists(os.path.join(self.workdir, "nc.html"))
        )

    def test_missing_output_directory_reports_path_and_reason(self):
        # 准备一份可正常读取的旧库。
        db_path = os.path.join(self.workdir, "demo.sqlite")
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(LEGACY_SCHEMA)
            conn.execute(
                "INSERT INTO quotes(number, customer, total) VALUES (?, ?, ?)",
                (NUMBER_B, CUSTOMER_DEMO, 2800),
            )
            conn.commit()
        finally:
            conn.close()

        out_arg = os.path.join("nodir", "list.html")
        result = self.run_quote(
            "list", "--db", "demo.sqlite", "--output", out_arg
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("无法写入输出文件", result.stderr)
        self.assertIn(out_arg, result.stderr, msg="提示必须带输出路径")
        # 路径之后给出具体原因（冒号之后非空）。
        self.assertTrue(
            result.stderr.rstrip("\n").split(out_arg, 1)[1].startswith(": ")
        )
        self.assertTrue(
            result.stderr.rstrip("\n").split(": ", 1)[-1].strip()
        )


# 项目目录清洁性：测试开始前记录项目根内容，全部用例结束后不得新增任何文件。
_PROJECT_ROOT_ENTRIES_AT_START = set(os.listdir(PROJECT_ROOT))


def tearDownModule():
    leftovers = set(os.listdir(PROJECT_ROOT)) - _PROJECT_ROOT_ENTRIES_AT_START
    assert not leftovers, f"测试在项目根目录留下了文件: {sorted(leftovers)}"


if __name__ == "__main__":
    unittest.main()
