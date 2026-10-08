"""列表筛选与排序规则的独立验证，并与公开 list 命令结果对照。

本文件分两部分：

1. 规则独立验证（ListRulesIndependentTestCase）：直接调用
   organize_list_rows / validate_list_customer_filter /
   render_list_json / render_list_html，入参全部是内存中“已读取的合法
   报价摘要”常量 (number, customer, total)——不访问数据库、不创建文件；
   这些纯函数本身也不写标准输出。

2. 公开命令对照（ListRulesCommandTestCase）：把同一组固定样例经公开
   save 命令落库后运行 list，断言子进程的成功输出（JSON 字节 / HTML
   字节）与退出码和“独立规则对同组固定摘要的渲染结果”逐字节一致，
   即公开 list 入口与独立验证使用的是同一套规则。

固定样例（输入顺序刻意为 Q-B、Q-C、Q-A）：
    Q-B 客户 "Demo"   合计 2800 分
    Q-C 客户 "demo"   合计  300 分
    Q-A 客户 " Demo " 合计    0 分（两端各一个空格）

运行方式（项目根目录）：
    python3 -m unittest discover -s tests
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 导入产品模块做独立验证时不得在项目根留下 __pycache__（既有测试有目录
# 清洁断言）；与子进程方式一致，验证本身不创建任何文件。
sys.dont_write_bytecode = True
sys.path.insert(0, PROJECT_ROOT)
QUOTE_PY = os.path.join(PROJECT_ROOT, "quote.py")

import quote

INT64_MAX = (1 << 63) - 1

# ---- 固定样例：内存中的合法报价摘要，顺序刻意为 B、C、A ----
FIXED_ROWS = [
    ("Q-B", "Demo", 2800),
    ("Q-C", "demo", 300),
    ("Q-A", " Demo ", 0),
]

# 同一组数据的 save 合法输入：合计由保存侧计算落库，列表只读取保存值。
SAVE_SAMPLES = [
    {
        "number": "Q-B",
        "customer": "Demo",
        "items": [{"description": "服务", "quantity": 1, "unit_price": 2800}],
    },
    {
        "number": "Q-C",
        "customer": "demo",
        "items": [{"description": "服务", "quantity": 1, "unit_price": 300}],
    },
    {
        "number": "Q-A",
        "customer": " Demo ",
        "items": [{"description": "赠品", "quantity": 1, "unit_price": 0}],
    },
]

# 独立规则的手写期望：无筛选按编号原文 BINARY 升序 -> A、B、C。
EXPECTED_ALL = [
    ("Q-A", " Demo ", 0),
    ("Q-B", "Demo", 2800),
    ("Q-C", "demo", 300),
]
EXPECTED_FILTER_DEMO = [("Q-B", "Demo", 2800)]

# 规则探针：在固定三样例之外补充换行与 %、_ 客户，仍是内存摘要。
PROBE_ROWS = FIXED_ROWS + [
    ("Q-N", "Demo\n", 70),
    ("Q-P", "Demo%", 80),
    ("Q-U", "Demo_", 90),
    ("Q-T", "demo ", 100),
]

FULL_SCHEMA = """
CREATE TABLE quotes (
    number   TEXT PRIMARY KEY,
    customer TEXT NOT NULL,
    total    INTEGER NOT NULL,
    note     TEXT
);
CREATE TABLE items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    quote_number TEXT NOT NULL REFERENCES quotes(number),
    position     INTEGER NOT NULL,
    description  TEXT NOT NULL,
    quantity     INTEGER NOT NULL,
    unit_price   INTEGER NOT NULL,
    line_amount  INTEGER NOT NULL
);
"""


class ListRulesIndependentTestCase(unittest.TestCase):
    """对内存摘要直接套用规则：不访问数据库、不创建文件、不写标准输出。"""

    def test_no_filter_sorts_by_raw_number_binary_regardless_of_input_order(self):
        # 输入顺序 B、C、A，输出必须是 A、B、C。
        self.assertEqual(
            quote.organize_list_rows(FIXED_ROWS, None),
            EXPECTED_ALL,
        )
        self.assertEqual(
            [number for number, _, _ in quote.organize_list_rows(FIXED_ROWS, None)],
            ["Q-A", "Q-B", "Q-C"],
            msg="按编号原文 BINARY 升序，与输入（保存）顺序无关",
        )
        # 打乱输入顺序不改变结果。
        shuffled = [FIXED_ROWS[2], FIXED_ROWS[0], FIXED_ROWS[1]]
        self.assertEqual(quote.organize_list_rows(shuffled, None), EXPECTED_ALL)

    def test_customer_filter_is_exact_character_match(self):
        rows = quote.organize_list_rows(FIXED_ROWS, "Demo")
        self.assertEqual(rows, EXPECTED_FILTER_DEMO, msg="筛选 Demo 只保留 Q-B")

        organize = quote.organize_list_rows
        # 大小写、两端空格各自独立：三个客户互不能被对方的值命中。
        self.assertEqual(organize(FIXED_ROWS, " Demo "), [("Q-A", " Demo ", 0)])
        self.assertEqual(organize(FIXED_ROWS, "demo"), [("Q-C", "demo", 300)])
        self.assertEqual(organize(FIXED_ROWS, "DEMO"), [])
        self.assertEqual(organize(FIXED_ROWS, " Demo"), [])
        self.assertEqual(organize(FIXED_ROWS, "Demo "), [])
        self.assertEqual(organize(FIXED_ROWS, "Demo\n"), [])

    def test_newline_percent_and_underscore_participate_as_literal_characters(self):
        organize = quote.organize_list_rows
        # 换行参与精确匹配。
        self.assertEqual(organize(PROBE_ROWS, "Demo\n"), [("Q-N", "Demo\n", 70)])
        self.assertEqual(organize(PROBE_ROWS, "Demo"), [("Q-B", "Demo", 2800)])
        self.assertEqual(organize(PROBE_ROWS, "Demo\r\n"), [])
        # % 与 _ 是普通字符，绝不是 LIKE 通配：只命中逐字符相同的那一行。
        self.assertEqual(organize(PROBE_ROWS, "Demo%"), [("Q-P", "Demo%", 80)])
        self.assertEqual(organize(PROBE_ROWS, "Demo_"), [("Q-U", "Demo_", 90)])
        # "demo " 末尾空格不能命中 "demo"，反之亦然。
        self.assertEqual(organize(PROBE_ROWS, "demo"), [("Q-C", "demo", 300)])
        self.assertEqual(organize(PROBE_ROWS, "demo "), [("Q-T", "demo ", 100)])

    def test_total_is_passed_through_unchanged(self):
        # 摘要里没有明细：合计只能原样透传，不可能被重算。
        rows = quote.organize_list_rows(FIXED_ROWS, None)
        self.assertEqual(
            [total for _, _, total in rows], [0, 2800, 300],
            msg="合计沿用保存的 total，不重算明细",
        )
        big = ("Q-BIG", "大额客户", INT64_MAX)
        self.assertEqual(quote.organize_list_rows([big], None)[0][2], INT64_MAX)

    def test_blank_filter_validation_rejected_without_side_effects(self):
        validate = quote.validate_list_customer_filter
        self.assertIsNone(validate(None))
        # 含有效文字时保留两端空白，原样参与匹配。
        self.assertEqual(validate(" Demo "), " Demo ")
        for value in ("", " ", "\t", "\n", " \t \n "):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValueError, msg="空白筛选值必须被拒绝"):
                    validate(value)

    def test_render_json_uses_fixed_fields_int_totals_and_trailing_newline(self):
        text = quote.render_list_json(EXPECTED_ALL)
        self.assertEqual(text[-1], "\n")
        self.assertEqual(text.count("\n"), 1)
        self.assertNotIn("\r", text)
        records = json.loads(text)
        self.assertEqual(
            records,
            [
                {"number": "Q-A", "customer": " Demo ", "total": 0},
                {"number": "Q-B", "customer": "Demo", "total": 2800},
                {"number": "Q-C", "customer": "demo", "total": 300},
            ],
        )
        for record in records:
            self.assertEqual(set(record.keys()), {"number", "customer", "total"})
            self.assertIsInstance(record["total"], int)
            self.assertNotIsInstance(record["total"], bool)
        # 空结果为空数组加一个换行；大整数不丢精度、不走浮点。
        self.assertEqual(quote.render_list_json([]), "[]\n")
        big_text = quote.render_list_json([("Q-BIG", "大额客户", INT64_MAX)])
        self.assertIn(str(INT64_MAX), big_text)
        self.assertEqual(json.loads(big_text)[0]["total"], INT64_MAX)

    def test_render_html_amounts_and_empty_notice(self):
        # 零金额 0.00；INT64_MAX 分 -> 92233720368547758.07，全程整数换算。
        self.assertEqual(quote.format_yuan(0), "0.00")
        self.assertEqual(
            quote.format_yuan(INT64_MAX), "92233720368547758.07"
        )
        document = quote.render_list_html(
            [("Q-ZERO", "客户", 0), ("Q-BIG", "大额客户", INT64_MAX)]
        )
        self.assertIn("0.00", document)
        self.assertIn("92233720368547758.07", document)
        # 空结果保留三列表头并显示空态提示。
        empty = quote.render_list_html([])
        self.assertIn("<title>报价列表</title>", empty)
        self.assertIn("<th>报价编号</th><th>客户</th><th>合计（元）</th>", empty)
        self.assertIn("没有匹配的报价", empty)


class ListRulesCommandTestCase(unittest.TestCase):
    """同一组固定数据：公开 list 命令结果必须与独立规则渲染结果一致。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="quote-list-rules-")
        self.addCleanup(self._tmp.cleanup)
        self.workdir = self._tmp.name
        for index, payload in enumerate(SAVE_SAMPLES):
            input_path = os.path.join(self.workdir, f"input-{index}.json")
            with open(input_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            result = self.run_quote(
                "save", "--db", "quotes.sqlite", "--input", f"input-{index}.json"
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)

    def run_quote(self, *cli_args):
        return subprocess.run(
            [sys.executable, QUOTE_PY, *cli_args],
            capture_output=True,
            text=True,
            cwd=self.workdir,
        )

    def test_json_output_matches_independent_rules_for_same_fixed_rows(self):
        # 无筛选：成功输出与退出码同独立规则结果逐字节一致。
        expected = quote.render_list_json(
            quote.organize_list_rows(FIXED_ROWS, None)
        )
        result = self.run_quote("list", "--db", "quotes.sqlite")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout, expected)

        # 筛选 Demo：只保留 Q-B，同样逐字节一致。
        expected_demo = quote.render_list_json(
            quote.organize_list_rows(FIXED_ROWS, "Demo")
        )
        result = self.run_quote(
            "list", "--db", "quotes.sqlite", "--customer", "Demo"
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            result.stdout, expected_demo,
            msg="公开命令的筛选/排序必须与独立规则同源一致",
        )

    def test_html_output_matches_independent_render_for_same_fixed_rows(self):
        expected_bytes = quote.render_list_html(
            quote.organize_list_rows(FIXED_ROWS, None)
        ).encode("utf-8")
        result = self.run_quote(
            "list", "--db", "quotes.sqlite", "--output", "list.html"
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout, "list.html\n")
        with open(os.path.join(self.workdir, "list.html"), "rb") as f:
            self.assertEqual(f.read(), expected_bytes)

        # 筛选 Demo 的 HTML 同样与独立渲染逐字节一致。
        filtered_bytes = quote.render_list_html(
            quote.organize_list_rows(FIXED_ROWS, "Demo")
        ).encode("utf-8")
        result = self.run_quote(
            "list", "--db", "quotes.sqlite",
            "--customer", "Demo", "--output", "demo.html",
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        with open(os.path.join(self.workdir, "demo.html"), "rb") as f:
            self.assertEqual(f.read(), filtered_bytes)

    def test_empty_database_matches_independent_empty_results(self):
        db_path = os.path.join(self.workdir, "empty.sqlite")
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(FULL_SCHEMA)
            conn.commit()
        finally:
            conn.close()

        result = self.run_quote("list", "--db", "empty.sqlite")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(
            result.stdout, quote.render_list_json([]),
        )

        result = self.run_quote(
            "list", "--db", "empty.sqlite", "--output", "empty.html"
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        with open(os.path.join(self.workdir, "empty.html"), "rb") as f:
            self.assertEqual(
                f.read(), quote.render_list_html([]).encode("utf-8")
            )

    def test_failure_order_blank_filter_before_existing_output_and_db(self):
        # 预置输出文件；数据库尚不存在：空白筛选必须最先被拒绝，
        # 原文件不变，数据库不创建。
        out_path = os.path.join(self.workdir, "out.html")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("MARKER")
        result = self.run_quote(
            "list", "--db", "never.sqlite",
            "--customer", "   ", "--output", "out.html",
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "错误: 客户筛选值不能为空白\n")
        with open(out_path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "MARKER")
        self.assertFalse(os.path.exists(os.path.join(self.workdir, "never.sqlite")))

        # 合法筛选时才轮到“输出文件已存在”，仍在读库之前。
        result = self.run_quote(
            "list", "--db", "never.sqlite",
            "--customer", "Demo", "--output", "out.html",
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("输出文件已存在", result.stderr)
        self.assertIn("out.html", result.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.workdir, "never.sqlite")))


_PROJECT_ROOT_ENTRIES_AT_START = set(os.listdir(PROJECT_ROOT))


def tearDownModule():
    leftovers = set(os.listdir(PROJECT_ROOT)) - _PROJECT_ROOT_ENTRIES_AT_START
    assert not leftovers, f"测试在项目根目录留下了文件: {sorted(leftovers)}"


if __name__ == "__main__":
    unittest.main()
