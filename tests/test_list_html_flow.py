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

# ---- 验收样例：先保存 Q-B（合计 2800 分），再保存 Q-A（合计 0）。
# 列表按编号 BINARY 排序后应先显示 Q-A 的 0.00，再显示 Q-B 的 28.00。
NUMBER_A = "Q-A"
NUMBER_B = "Q-B"
CUSTOMER_A = "乙"
CUSTOMER_B = "演示<甲>&乙"

SAVE_SAMPLES = [
    {
        "number": NUMBER_B,
        "customer": CUSTOMER_B,
        "items": [
            {"description": "咨询", "quantity": 2, "unit_price": 1250},
            {"description": "资料", "quantity": 1, "unit_price": 300},
        ],
    },
    {
        "number": NUMBER_A,
        "customer": CUSTOMER_A,
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

    def save_all(self, samples, db_name="quotes.sqlite"):
        for index, payload in enumerate(samples):
            self.save_sample(payload, index, db_name=db_name)
        return os.path.join(self.workdir, db_name)

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
    """验收：Q-B 先存、Q-A 后存，页面先 Q-A 的 0.00 再 Q-B 的 28.00。"""

    def test_sorted_rows_zero_before_28(self):
        self.save_all(SAVE_SAMPLES)
        result = self.list_html("quotes.sqlite")

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "", msg="成功时标准错误必须为空")
        # 标准输出只含传入路径和一个换行。
        self.assertEqual(result.stdout, "list.html\n")

        raw, document = self.read_output()
        self.assertTrue(raw.startswith(b"<!DOCTYPE html>"))
        # 无外部资源：不引用任何 http/https 链接或外部 src/href。
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
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            [cells_of(row) for row in rows],
            [
                ["Q-A", "乙", "0.00"],
                ["Q-B", "演示&lt;甲&gt;&amp;乙", "28.00"],
            ],
            msg="按编号 BINARY 升序：先 Q-A 的 0.00，再 Q-B 的 28.00；零金额显示 0.00",
        )

    def test_table_has_exactly_three_columns(self):
        self.save_all(SAVE_SAMPLES)
        result = self.list_html("quotes.sqlite")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        _, document = self.read_output()

        head_row = re.search(r"<thead>.*?<tr>(.*?)</tr>.*?</thead>", document, flags=re.S)
        self.assertIsNotNone(head_row)
        self.assertEqual(
            re.findall(r"<th>(.*?)</th>", head_row.group(1), flags=re.S),
            ["报价编号", "客户", "合计（元）"],
        )
        for row in extract_body_rows(document):
            self.assertEqual(len(cells_of(row)), 3)

    def test_second_run_refused_and_file_unchanged(self):
        self.save_all(SAVE_SAMPLES)
        first = self.list_html("quotes.sqlite")
        self.assertEqual(first.returncode, 0, msg=first.stderr)
        with open(os.path.join(self.workdir, "list.html"), "rb") as f:
            original = f.read()

        second = self.list_html("quotes.sqlite")
        self.assertEqual(second.returncode, 1)
        self.assertEqual(second.stdout, "", msg="失败时标准输出必须为空")
        self.assertIn("输出文件已存在", second.stderr)
        self.assertIn("list.html", second.stderr)
        self.assertEqual(second.stderr.count("\n"), 1)

        with open(os.path.join(self.workdir, "list.html"), "rb") as f:
            self.assertEqual(f.read(), original, msg="重复执行不得改变页面内容")

    def test_output_uses_saved_total_not_recomputed_items(self):
        """直接篡改库内 total 为异常值，HTML 仍以已保存合计为准。"""
        db_path = self.save_all(SAVE_SAMPLES)
        conn = sqlite3.connect(db_path)
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
        self.assertIn("99.99", amounts, msg="合计必须读取已保存 total，不重算明细")


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
        self.assertEqual(result.stdout, "list.html\n")
        _, document = self.read_output()

        self.assertIn("<th>报价编号</th>", document, msg="空库仍保留表头")
        self.assertEqual(extract_body_rows(document), [], msg="空库不显示报价行")
        self.assertIn(EMPTY_NOTICE, document)

    def test_no_match_filter_keeps_header_and_shows_notice(self):
        self.save_all(SAVE_SAMPLES)
        result = self.list_html("quotes.sqlite", "nomatch.html",
                                "--customer", "无此客户")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        _, document = self.read_output("nomatch.html")

        self.assertIn("<th>报价编号</th>", document)
        self.assertEqual(extract_body_rows(document), [])
        self.assertIn(EMPTY_NOTICE, document)


class ListHtmlFilterTestCase(ListHtmlTestBase):
    """--customer 逐字符精确筛选：大小写、空格、换行参与比较，% 与 _ 为普通字符。"""

    def test_exact_filter_matches_only_equal_customer(self):
        self.save_all(SAVE_SAMPLES)
        result = self.list_html("quotes.sqlite", "yi.html", "--customer", CUSTOMER_A)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        _, document = self.read_output("yi.html")
        rows = extract_body_rows(document)
        self.assertEqual(len(rows), 1)
        self.assertEqual(cells_of(rows[0]), ["Q-A", "乙", "0.00"])

    def test_whitespace_and_newline_text_visible_in_one_row(self):
        number = " Q-WS \n 第二行 "
        customer = ' 甲<乙> & "丙" \n  丁  '
        self.save_sample(
            {
                "number": number,
                "customer": customer,
                "items": [{"description": "x", "quantity": 1, "unit_price": 7}],
            },
            0,
        )
        result = self.list_html("quotes.sqlite", "ws.html", "--customer", customer)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        raw, document = self.read_output("ws.html")

        rows = extract_body_rows(document)
        self.assertEqual(len(rows), 1, msg="多行文字仍属于同一报价行")
        self.assertEqual(
            cells_of(rows[0]),
            [" Q-WS \n 第二行 ", ' 甲&lt;乙&gt; &amp; &quot;丙&quot; \n  丁  ', "0.07"],
        )
        # 原始字节中编号与客户文本（含前导/连续空格与换行）必须逐字符存在。
        self.assertIn(
            "<td> Q-WS \n 第二行 </td>".encode("utf-8"), raw,
            msg="编号首尾空格与换行必须原样落在单元格中",
        )
        self.assertIn(
            '<td> 甲&lt;乙&gt; &amp; &quot;丙&quot; \n  丁  </td>'.encode("utf-8"),
            raw,
            msg="客户首尾、连续空格与换行必须原样落在单元格中",
        )
        # pre-wrap 样式保证空白可见。
        self.assertIn("white-space: pre-wrap", document)

    def test_percent_and_underscore_are_literal(self):
        customer = "100%_特价"
        self.save_sample(
            {
                "number": "Q-PCT",
                "customer": customer,
                "items": [{"description": "x", "quantity": 1, "unit_price": 100}],
            },
            0,
        )
        result = self.list_html("quotes.sqlite", "pct.html", "--customer", customer)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        _, document = self.read_output("pct.html")
        self.assertEqual(len(extract_body_rows(document)), 1)

        miss = self.list_html("quotes.sqlite", "pct-miss.html",
                              "--customer", "100_特价")
        self.assertEqual(miss.returncode, 0, msg=miss.stderr)
        _, miss_doc = self.read_output("pct-miss.html")
        self.assertEqual(extract_body_rows(miss_doc), [])
        self.assertIn(EMPTY_NOTICE, miss_doc)


class ListHtmlBigTotalTestCase(ListHtmlTestBase):
    """有符号 64 位上限的合计仍精确显示两位小数，不使用浮点。"""

    def test_int64_max_total_is_exact(self):
        self.save_sample(
            {
                "number": "Q-MAX",
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
        # INT64_MAX = 9223372036854775807 分 -> 92233720368547758.07 元。
        self.assertEqual(
            cells_of(rows[0])[2],
            f"{INT64_MAX // 100}.{INT64_MAX % 100:02d}",
        )
        self.assertIn("92233720368547758.07", document)


class ListHtmlLegacyTestCase(ListHtmlTestBase):
    """旧库缺 note 列、无 items 表仍可只读生成，不补表补列、不改记录。"""

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
            kept = conn.execute("SELECT * FROM quotes").fetchall()
        finally:
            conn.close()
        self.assertEqual(cols, ["number", "customer", "total"])
        self.assertEqual(tables, ["quotes"])
        self.assertEqual(kept, [("Q-LEGACY", "旧库_客户 %_ <x>&y", 1234)])


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
                self.assertEqual(result.stderr.count("\n"), 1)
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
            conn.execute(
                "CREATE TABLE quotes(number TEXT PRIMARY KEY, customer TEXT NOT NULL)"
            )
            conn.commit()
        finally:
            conn.close()
        result = self.list_html(db_name, "out.html")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("无法读取数据库", result.stderr)
        self.assertIn(db_name, result.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.workdir, "out.html")))

    def test_missing_output_directory_errors(self):
        self.save_all(SAVE_SAMPLES)
        out_name = os.path.join("nodir", "list.html")
        result = self.list_html("quotes.sqlite", out_name)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "", msg="写文件失败时不得有标准输出")
        self.assertIn("无法写入输出文件", result.stderr)
        self.assertIn(out_name, result.stderr)
        self.assertEqual(result.stderr.count("\n"), 1)

    def test_no_script_generated_from_text(self):
        """编号与客户文本中的标签/脚本只转义，不成为真正的元素。"""
        self.save_sample(
            {
                "number": "<script>n</script>",
                "customer": "<script>alert(1)</script><b>x</b>",
                "items": [{"description": "x", "quantity": 1, "unit_price": 1}],
            },
            0,
        )
        result = self.list_html("quotes.sqlite")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        _, document = self.read_output()
        self.assertNotIn("<script>alert(1)</script>", document)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", document)
        # tbody 内除了生成的 <tr>/<td> 外不含文本引入的额外标签名。
        body = re.search(r"<tbody>(.*?)</tbody>", document, flags=re.S).group(1)
        tags = set(re.findall(r"</?([a-zA-Z][a-zA-Z0-9]*)", body))
        self.assertEqual(tags, {"tr", "td"})


class ListJsonUnchangedTestCase(ListHtmlTestBase):
    """省略 --output 时保留既有单行 JSON 行为，且不生成任何文件。"""

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
                {"number": NUMBER_A, "customer": CUSTOMER_A, "total": 0},
                {"number": NUMBER_B, "customer": CUSTOMER_B, "total": 2800},
            ],
        )
        for record in records:
            self.assertEqual(set(record.keys()), {"number", "customer", "total"})
        self.assertFalse(
            any(name.endswith(".html") for name in os.listdir(self.workdir)),
            msg="省略 --output 不得生成任何 HTML",
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


# 项目目录清洁性：测试开始前记录项目根内容，全部用例结束后不得新增任何文件
# （所有数据库、输入 JSON 与 HTML 都必须落在临时目录中）。
_PROJECT_ROOT_ENTRIES_AT_START = set(os.listdir(PROJECT_ROOT))


def tearDownModule():
    leftovers = set(os.listdir(PROJECT_ROOT)) - _PROJECT_ROOT_ENTRIES_AT_START
    assert not leftovers, f"测试在项目根目录留下了文件: {sorted(leftovers)}"


if __name__ == "__main__":
    unittest.main()
