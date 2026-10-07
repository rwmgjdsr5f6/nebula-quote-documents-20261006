"""报价列表（list --output）离线 HTML 的命令行回归测试。

仅依赖 Python 标准库。所有用例在独立的临时目录中通过 README 记载的
公开命令（save / list）操作 quote.py 子进程，使用合成单据，
不读取项目内业务数据，也不在项目目录留下 JSON、SQLite 或 HTML 文件。

运行方式（项目根目录）：
    python3 -m unittest discover -s tests
全部通过时退出码为 0，任一失败时非零退出并指出对应样例与预期。
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUOTE_PY = os.path.join(PROJECT_ROOT, "quote.py")

INT64_MAX = (1 << 63) - 1

# ---- 验收样例：Q-B 合计 2800 分，Q-A 合计 0 分（保存顺序故意为 B、A） ----
NUMBER_A = "Q-A"
NUMBER_B = "Q-B"
CUSTOMER_MIXED = "演示<甲>&乙"
CUSTOMER_YI = "乙"

SAVE_SAMPLES = [
    {
        "number": NUMBER_B,
        "customer": CUSTOMER_MIXED,
        "items": [
            {"description": "咨询", "quantity": 2, "unit_price": 1250},
            {"description": "资料", "quantity": 1, "unit_price": 300},
        ],
    },
    {
        "number": NUMBER_A,
        "customer": CUSTOMER_YI,
        "items": [{"description": "赠", "quantity": 1, "unit_price": 0}],
    },
]

EMPTY_NOTICE = "没有匹配的报价"

FULL_SCHEMA = """
CREATE TABLE quotes (
    number   TEXT PRIMARY KEY,
    customer TEXT NOT NULL,
    total    INTEGER NOT NULL,
    note     TEXT
);
CREATE TABLE items (id INTEGER PRIMARY KEY);
"""

LEGACY_SCHEMA = """
CREATE TABLE quotes (
    number   TEXT PRIMARY KEY,
    customer TEXT NOT NULL,
    total    INTEGER NOT NULL
);
"""


class ListHtmlTestBase(unittest.TestCase):
    """每个用例使用独立临时目录，只通过公开命令行入口驱动 quote.py。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="quote-list-html-test-")
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

    def save_sample(self, payload, index, db_name="quotes.sqlite"):
        db_path = os.path.join(self.workdir, db_name)
        input_path = self.write_input(payload, f"input-{index}.json")
        result = self.run_quote("save", "--db", db_path, "--input", input_path)
        self.assertEqual(
            result.returncode, 0,
            msg=f"样例 {payload['number']} 保存应成功: {result.stderr}",
        )
        self.assertEqual(result.stderr, "")
        return db_path

    def save_all(self, samples):
        db_path = None
        for index, payload in enumerate(samples):
            db_path = self.save_sample(payload, index)
        return db_path

    def list_html(self, db_arg, out_arg="list.html", *extra):
        return self.run_quote(
            "list", "--db", db_arg, "--output", out_arg, *extra
        )

    def read_output(self, out_name="list.html"):
        path = os.path.join(self.workdir, out_name)
        with open(path, "rb") as f:
            raw = f.read()
        # 必须是严格 UTF-8。
        return raw, raw.decode("utf-8")


def extract_body_rows(document):
    """取出 tbody 内的报价行（不含表头），每条为原始 <tr>...</tr> 文本。"""
    match = re.search(r"<tbody>(.*?)</tbody>", document, flags=re.S)
    assert match is not None, "HTML 必须包含 <tbody>"
    return re.findall(r"<tr>.*?</tr>", match.group(1), flags=re.S)


def cells_of(row):
    return re.findall(r"<td[^>]*>(.*?)</td>", row, flags=re.S)


class ListHtmlAcceptanceTestCase(ListHtmlTestBase):
    """验收：Q-A 的 0.00 在 Q-B 的 28.00 之前；重复执行拒绝且内容不变。"""

    def test_acceptance_q_a_zero_then_q_b_28(self):
        self.save_all(SAVE_SAMPLES)
        result = self.list_html("quotes.sqlite")

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "", msg="成功时标准错误必须为空")
        # 标准输出只含传入路径和一个换行。
        self.assertEqual(result.stdout, "list.html\n")

        raw, document = self.read_output()
        self.assertTrue(raw.startswith(b"<!DOCTYPE html>"))
        # 无外部资源：不引用任何 http/https 链接或外部 src/href，可离线打开。
        self.assertNotRegex(document, r"(?i)\b(?:src|href)\s*=")
        self.assertNotRegex(document, r"https?://")

        # 页面标题与主标题均为“报价列表”。
        self.assertEqual(
            re.findall(r"<title>(.*?)</title>", document, flags=re.S),
            ["报价列表"],
        )
        self.assertEqual(
            re.findall(r"<h1>(.*?)</h1>", document, flags=re.S),
            ["报价列表"],
        )

        rows = extract_body_rows(document)
        self.assertEqual(len(rows), 2, msg="每张报价一行")
        # 编号 BINARY 升序：Q-A 在 Q-B 之前，与保存先后无关。
        self.assertEqual(
            [cells_of(row)[0] for row in rows], [NUMBER_A, NUMBER_B]
        )
        # 客户做 HTML 转义：尖括号与与号显示原文（页面渲染），单元格内为转义文本。
        for row, customer, amount in zip(
            rows,
            (CUSTOMER_YI, "演示&lt;甲&gt;&amp;乙"),
            ("0.00", "28.00"),
        ):
            cells = cells_of(row)
            self.assertEqual(len(cells), 3, msg="每行只含编号、客户、合计")
            self.assertEqual(cells[1], customer)
            self.assertEqual(cells[2], amount, msg="零金额 0.00，2800 分 -> 28.00")

    def test_repeat_run_refused_and_content_unchanged(self):
        self.save_all(SAVE_SAMPLES)
        first = self.list_html("quotes.sqlite")
        self.assertEqual(first.returncode, 0, msg=first.stderr)
        raw_before, _ = self.read_output()

        second = self.list_html("quotes.sqlite")
        self.assertEqual(second.returncode, 1)
        self.assertEqual(second.stdout, "", msg="失败时标准输出必须为空")
        self.assertIn("输出文件已存在", second.stderr)
        self.assertIn("list.html", second.stderr)
        self.assertEqual(second.stderr.count("\n"), 1)

        raw_after, _ = self.read_output()
        self.assertEqual(raw_after, raw_before, msg="重复执行不得改动原页面")

    def test_headers_present_and_only_three_columns(self):
        self.save_all(SAVE_SAMPLES)
        self.list_html("quotes.sqlite")
        _, document = self.read_output()

        head_row = re.search(r"<thead>.*?<tr>(.*?)</tr>.*?</thead>", document, flags=re.S)
        self.assertIsNotNone(head_row)
        self.assertEqual(
            re.findall(r"<th>(.*?)</th>", head_row.group(1), flags=re.S),
            ["报价编号", "客户", "合计（元）"],
        )

    def test_output_uses_saved_total_not_recomputed_items(self):
        """直接篡改库内 total，HTML 仍以已保存合计为准，不重算明细。"""
        self.save_all(SAVE_SAMPLES)
        conn = sqlite3.connect(os.path.join(self.workdir, "quotes.sqlite"))
        try:
            conn.execute(
                "UPDATE quotes SET total = 9999 WHERE number = ?", (NUMBER_B,)
            )
            conn.commit()
        finally:
            conn.close()

        result = self.list_html("quotes.sqlite")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        _, document = self.read_output()
        amounts = [cells_of(row)[2] for row in extract_body_rows(document)]
        self.assertEqual(amounts, ["0.00", "99.99"])


class ListHtmlEscapeTestCase(ListHtmlTestBase):
    """转义与空白：尖括号、与号、引号显示原文，首尾/连续空格与换行可见。"""

    def test_special_chars_escaped_and_whitespace_visible_in_one_row(self):
        number = "  Q-WS \n 号 "
        customer = '  甲 <x> & "y" \n  乙  '
        db_path = os.path.join(self.workdir, "quotes.sqlite")
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(LEGACY_SCHEMA)
            conn.execute(
                "INSERT INTO quotes(number, customer, total) VALUES (?, ?, ?)",
                (number, customer, 5),
            )
            conn.commit()
        finally:
            conn.close()

        result = self.list_html("quotes.sqlite")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        raw, document = self.read_output()

        rows = extract_body_rows(document)
        self.assertEqual(len(rows), 1, msg="多行文字仍属于同一报价行，不拆行")
        cells = cells_of(rows[0])
        self.assertEqual(
            cells[0], "  Q-WS \n 号 ",
            msg="编号首尾空格、连续空格与换行逐字符保留",
        )
        self.assertEqual(
            cells[1],
            '  甲 &lt;x&gt; &amp; &quot;y&quot; \n  乙  ',
            msg="尖括号、与号、双引号转义显示原文，空白与换行保留",
        )
        self.assertEqual(cells[2], "0.05")
        # 原始字节中含换行的客户文本必须落在同一单元格内。
        self.assertIn(
            '<td>  甲 &lt;x&gt; &amp; &quot;y&quot; \n  乙  </td>'.encode("utf-8"),
            raw,
        )
        # pre-wrap 样式保证空白可见。
        self.assertIn("white-space: pre-wrap", document)

    def test_no_script_generated_from_text(self):
        """编号/客户中的标签只转义，不成为真正的元素。"""
        db_path = os.path.join(self.workdir, "quotes.sqlite")
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(LEGACY_SCHEMA)
            conn.execute(
                "INSERT INTO quotes(number, customer, total) VALUES (?, ?, ?)",
                ("<script>x</script>", "<b>q</b>", 1),
            )
            conn.commit()
        finally:
            conn.close()

        result = self.list_html("quotes.sqlite")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        _, document = self.read_output()
        self.assertNotIn("<script>x</script>", document)
        self.assertIn("&lt;script&gt;x&lt;/script&gt;", document)
        body = re.search(r"<tbody>(.*?)</tbody>", document, flags=re.S).group(1)
        tags = set(re.findall(r"</?([a-zA-Z][a-zA-Z0-9]*)", body))
        self.assertEqual(tags, {"tr", "td"})


class ListHtmlFilterTestCase(ListHtmlTestBase):
    """--customer 精确筛选：大小写、空格、换行参与比较，% 与 _ 为普通字符。"""

    def test_exact_filter_matches_only_equal_customer(self):
        self.save_all(SAVE_SAMPLES)
        result = self.list_html("quotes.sqlite", "yi.html",
                                "--customer", CUSTOMER_YI)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        _, document = self.read_output("yi.html")
        rows = extract_body_rows(document)
        self.assertEqual(len(rows), 1)
        self.assertEqual(cells_of(rows[0]), [NUMBER_A, CUSTOMER_YI, "0.00"])

    def test_percent_and_underscore_are_literal(self):
        customer = "100%_特价"
        db_path = os.path.join(self.workdir, "quotes.sqlite")
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(LEGACY_SCHEMA)
            conn.execute(
                "INSERT INTO quotes(number, customer, total) VALUES (?, ?, ?)",
                ("Q-PCT", customer, 100),
            )
            conn.commit()
        finally:
            conn.close()

        hit = self.list_html("quotes.sqlite", "hit.html",
                             "--customer", customer)
        self.assertEqual(hit.returncode, 0, msg=hit.stderr)
        _, hit_doc = self.read_output("hit.html")
        self.assertEqual(len(extract_body_rows(hit_doc)), 1)

        miss = self.list_html("quotes.sqlite", "miss.html",
                              "--customer", "100_特价")
        self.assertEqual(miss.returncode, 0, msg=miss.stderr)
        _, miss_doc = self.read_output("miss.html")
        self.assertEqual(extract_body_rows(miss_doc), [])
        self.assertIn(EMPTY_NOTICE, miss_doc)


class ListHtmlEmptyTestCase(ListHtmlTestBase):
    """空库或无匹配：保留表头、无报价行、显示提示。"""

    def test_empty_database_keeps_header_and_shows_notice(self):
        db_path = os.path.join(self.workdir, "empty.sqlite")
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(FULL_SCHEMA)
            conn.commit()
        finally:
            conn.close()

        result = self.list_html("empty.sqlite")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "")
        _, document = self.read_output()

        self.assertIn("<th>报价编号</th>", document, msg="空库仍保留表头")
        self.assertEqual(extract_body_rows(document), [], msg="空库不显示报价行")
        self.assertIn(EMPTY_NOTICE, document)

    def test_no_match_filter_keeps_header_and_shows_notice(self):
        self.save_all(SAVE_SAMPLES)
        result = self.list_html("quotes.sqlite", "none.html",
                                "--customer", "无此客户")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        _, document = self.read_output("none.html")
        self.assertIn("<th>报价编号</th>", document)
        self.assertEqual(extract_body_rows(document), [])
        self.assertIn(EMPTY_NOTICE, document)


class ListHtmlBigTotalTestCase(ListHtmlTestBase):
    """合计为有符号 64 位上限时仍精确显示两位小数，不走浮点。"""

    def test_int64_max_total_is_exact(self):
        self.save_sample(
            {
                "number": "Q-BIG",
                "customer": "大额客户",
                "items": [
                    {"description": "上限单价", "quantity": 1,
                     "unit_price": INT64_MAX}
                ],
            },
            0,
        )
        result = self.list_html("quotes.sqlite")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        _, document = self.read_output()
        rows = extract_body_rows(document)
        self.assertEqual(len(rows), 1)
        # 9223372036854775807 分 -> 92233720368547758.07 元，全程整数换算。
        self.assertEqual(
            cells_of(rows[0])[2],
            f"{INT64_MAX // 100}.{INT64_MAX % 100:02d}",
        )
        self.assertIn("92233720368547758.07", document)

    def test_mod100_values_padded_to_two_decimals(self):
        """个位/十分位补零：1 分 -> 0.01，10 分 -> 0.10，0 分 -> 0.00。"""
        db_path = os.path.join(self.workdir, "pad.sqlite")
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(LEGACY_SCHEMA)
            conn.executemany(
                "INSERT INTO quotes(number, customer, total) VALUES (?, ?, ?)",
                [("Q-1", "c", 1), ("Q-10", "c", 10), ("Q-0", "c", 0)],
            )
            conn.commit()
        finally:
            conn.close()

        result = self.list_html("pad.sqlite")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        _, document = self.read_output()
        amounts = [cells_of(row)[2] for row in extract_body_rows(document)]
        self.assertEqual(
            amounts, ["0.00", "0.01", "0.10"],
            msg="BINARY 排序 Q-0、Q-1、Q-10；金额固定两位小数",
        )


class ListHtmlLegacyTestCase(ListHtmlTestBase):
    """旧库缺 note 列、无 items 表仍可只读生成，不补表补列。"""

    def test_legacy_database_renders_without_schema_changes(self):
        db_path = os.path.join(self.workdir, "legacy.sqlite")
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(LEGACY_SCHEMA)
            conn.execute(
                "INSERT INTO quotes(number, customer, total) VALUES (?, ?, ?)",
                ("Q-LEGACY", "旧库_客户 %_ <x>&y", 1234),
            )
            conn.commit()
        finally:
            conn.close()

        before = conn_master_snapshot(db_path)

        result = self.list_html("legacy.sqlite")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        _, document = self.read_output()
        rows = extract_body_rows(document)
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            cells_of(rows[0]),
            ["Q-LEGACY", "旧库_客户 %_ &lt;x&gt;&amp;y", "12.34"],
        )

        self.assertEqual(conn_master_snapshot(db_path), before,
                         msg="旧库表结构前后必须一致")
        conn = sqlite3.connect(db_path)
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(quotes)")]
            tables = [r[0] for r in
                      conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        finally:
            conn.close()
        self.assertEqual(cols, ["number", "customer", "total"])
        self.assertEqual(tables, ["quotes"])


class ListHtmlFailureTestCase(ListHtmlTestBase):
    """失败路径：退出 1、无标准输出、不覆盖、不创建数据库或 HTML。"""

    def test_existing_output_refused_and_not_overwritten(self):
        self.save_all(SAVE_SAMPLES)
        out_name = "list.html"
        marker = "<!-- 原有内容，不得被覆盖 -->"
        with open(os.path.join(self.workdir, out_name), "w", encoding="utf-8") as f:
            f.write(marker)

        result = self.list_html("quotes.sqlite", out_name)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "", msg="失败时标准输出必须为空")
        self.assertIn("输出文件已存在", result.stderr)
        self.assertIn(out_name, result.stderr)
        self.assertEqual(result.stderr.count("\n"), 1)

        with open(os.path.join(self.workdir, out_name), encoding="utf-8") as f:
            self.assertEqual(f.read(), marker, msg="绝不能覆盖已有输出")

    def test_blank_customer_rejected_before_db_and_output(self):
        db_path = os.path.join(self.workdir, "quotes.sqlite")
        self.assertFalse(os.path.exists(db_path))
        for value in ("", " ", "\t", " \n\t "):
            with self.subTest(value=repr(value)):
                result = self.list_html("quotes.sqlite", "out.html",
                                        "--customer", value)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertIn("客户筛选值不能为空白", result.stderr)
                self.assertFalse(
                    os.path.exists(os.path.join(self.workdir, "out.html")),
                    msg="拒绝空白客户时不得创建输出文件",
                )
                self.assertFalse(
                    os.path.exists(db_path),
                    msg="拒绝空白客户时不得触碰/创建数据库",
                )

    def test_missing_database_errors_without_creating_files(self):
        result = self.list_html("nope.sqlite", "out.html")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("无法读取数据库", result.stderr)
        self.assertIn("nope.sqlite", result.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.workdir, "nope.sqlite")))
        self.assertFalse(os.path.exists(os.path.join(self.workdir, "out.html")))

    def test_non_sqlite_database_errors_without_html(self):
        db_name = "bad.bin"
        with open(os.path.join(self.workdir, db_name), "wb") as f:
            f.write(b"this is not a sqlite database\x00\xff")
        result = self.list_html(db_name, "out.html")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("无法读取数据库", result.stderr)
        self.assertIn(db_name, result.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.workdir, "out.html")))

    def test_missing_quotes_table_errors(self):
        db_name = "no-table.sqlite"
        conn = sqlite3.connect(os.path.join(self.workdir, db_name))
        try:
            conn.execute("CREATE TABLE unrelated(id INTEGER PRIMARY KEY)")
            conn.commit()
        finally:
            conn.close()
        result = self.list_html(db_name, "out.html")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("无法读取数据库", result.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.workdir, "out.html")))

    def test_missing_required_column_errors(self):
        db_name = "no-column.sqlite"
        conn = sqlite3.connect(os.path.join(self.workdir, db_name))
        try:
            # 缺 total 列。
            conn.execute("CREATE TABLE quotes(number TEXT PRIMARY KEY, customer TEXT)")
            conn.commit()
        finally:
            conn.close()
        result = self.list_html(db_name, "out.html")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("无法读取数据库", result.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.workdir, "out.html")))

    def test_missing_output_directory_errors(self):
        self.save_all(SAVE_SAMPLES)
        out_name = os.path.join("nodir", "list.html")
        result = self.list_html("quotes.sqlite", out_name)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "", msg="写文件失败时不得有标准输出")
        self.assertIn("无法写入输出文件", result.stderr)
        self.assertIn(out_name, result.stderr)


class ListJsonUnchangedTestCase(ListHtmlTestBase):
    """省略 --output 时保留既有单行 JSON 行为，不生成文件。"""

    def test_without_output_prints_single_line_json(self):
        self.save_all(SAVE_SAMPLES)
        result = self.run_quote("list", "--db", "quotes.sqlite")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout.count("\n"), 1)
        self.assertTrue(result.stdout.endswith("\n"))
        self.assertNotIn("\r", result.stdout)
        records = json.loads(result.stdout)
        self.assertEqual(
            records,
            [
                {"number": NUMBER_A, "customer": CUSTOMER_YI, "total": 0},
                {"number": NUMBER_B, "customer": CUSTOMER_MIXED, "total": 2800},
            ],
        )
        for record in records:
            self.assertEqual(set(record.keys()), {"number", "customer", "total"})
        # 省略 --output 不得生成任何 HTML。
        self.assertFalse(
            any(name.endswith(".html") for name in os.listdir(self.workdir))
        )

    def test_empty_database_json_is_empty_array(self):
        db_path = os.path.join(self.workdir, "empty.sqlite")
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(FULL_SCHEMA)
            conn.commit()
        finally:
            conn.close()
        result = self.run_quote("list", "--db", "empty.sqlite")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stdout, "[]\n")


def conn_master_snapshot(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT type, name, tbl_name, rootpage, sql "
            "FROM sqlite_master ORDER BY type, name"
        ).fetchall()
    finally:
        conn.close()


_PROJECT_ROOT_ENTRIES_AT_START = set(os.listdir(PROJECT_ROOT))


def tearDownModule():
    leftovers = set(os.listdir(PROJECT_ROOT)) - _PROJECT_ROOT_ENTRIES_AT_START
    assert not leftovers, f"测试在项目根目录留下了文件: {sorted(leftovers)}"


if __name__ == "__main__":
    unittest.main()
