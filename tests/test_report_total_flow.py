"""客户汇总报表合计行（report --output）的命令行回归测试。

核对合计行只统计本次结果中的报价，并与客户行明确区分：
  - 固定三张合成报价：客户原文为“合计”的报价仍是普通客户行，
    合计行固定位于所有客户行之后；
  - 合计张数/金额随 --customer 精确筛选变化，零金额报价计入张数，
    多条明细不增加张数；
  - 空库或无匹配时合计行为 0 张、0.00 元，表头与空态提示保留；
  - 累计超出有符号 64 位上限时不溢出、不舍入；
  - 省略 --output 时 JSON 行为不变，不增加汇总对象；
  - 报表生成前后数据库的结构和记录保持一致。

仅依赖 Python 标准库。所有用例在独立的临时目录中通过 README 记载的
公开命令（save / report）操作 quote.py 子进程，使用合成单据，
不读取项目内业务数据，也不在项目目录留下 JSON、SQLite 或 HTML 文件。

运行方式（项目根目录）：
    python3 -m unittest discover -s tests -p test_report_total_flow.py
全部通过时退出码为 0，任一失败时非零退出并指出对应样例与实际差异。
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

# ---- 固定验收样例：三张不同编号的合成报价 ----
# Q-TOTAL-1 客户“演示<甲>&乙”：咨询 数量 2 单价 1250 分（2500）
#                            + 资料 数量 1 单价 300 分（300），合计 2800 分；
# Q-TOTAL-2 同一客户：赠品 数量 1 单价 0 分，合计 0 分；
# Q-TOTAL-3 客户原文就是“合计”：服务 数量 1 单价 700 分，合计 700 分。
CUSTOMER_SPECIAL = "演示<甲>&乙"
CUSTOMER_TOTAL_WORD = "合计"

FIXED_SAMPLES = [
    {
        "number": "Q-TOTAL-1",
        "customer": CUSTOMER_SPECIAL,
        "items": [
            {"description": "咨询", "quantity": 2, "unit_price": 1250},
            {"description": "资料", "quantity": 1, "unit_price": 300},
        ],
    },
    {
        "number": "Q-TOTAL-2",
        "customer": CUSTOMER_SPECIAL,
        "items": [{"description": "赠品", "quantity": 1, "unit_price": 0}],
    },
    {
        "number": "Q-TOTAL-3",
        "customer": CUSTOMER_TOTAL_WORD,
        "items": [{"description": "服务", "quantity": 1, "unit_price": 700}],
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
CREATE TABLE items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    quote_number TEXT NOT NULL,
    position     INTEGER NOT NULL,
    description  TEXT NOT NULL,
    quantity     INTEGER NOT NULL,
    unit_price   INTEGER NOT NULL,
    line_amount  INTEGER NOT NULL
);
"""


class ReportTotalTestBase(unittest.TestCase):
    """每个用例使用独立临时目录，只通过公开命令行入口驱动 quote.py。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="quote-report-total-test-")
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
        db_path = None
        for index, payload in enumerate(samples):
            db_path = self.save_sample(payload, index, db_name=db_name)
        return db_path

    def report_html(self, db_name="quotes.sqlite", out_name="report.html", *extra):
        return self.run_quote(
            "report", "--db", db_name, "--output", out_name, *extra
        )

    def assert_html_success(self, result, out_name="report.html"):
        """生成 HTML 成功：退出 0、标准错误为空、标准输出仅为路径加换行。"""
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "", msg="成功时标准错误必须为空")
        self.assertEqual(
            result.stdout, f"{out_name}\n",
            msg="标准输出必须仅为传入路径加一个换行",
        )

    def read_output(self, out_name="report.html"):
        path = os.path.join(self.workdir, out_name)
        with open(path, "rb") as f:
            raw = f.read()
        # 必须是严格 UTF-8。
        return raw, raw.decode("utf-8")


def extract_body_rows(document):
    """取出 tbody 内的客户行（不含表头），每条为原始 <tr>...</tr> 文本。"""
    match = re.search(r"<tbody>(.*?)</tbody>", document, flags=re.S)
    assert match is not None, "HTML 必须包含 <tbody>"
    return re.findall(r"<tr>.*?</tr>", match.group(1), flags=re.S)


def extract_foot_rows(document):
    """取出 tfoot 内的合计行；正常报表应有且仅有一条。"""
    matches = re.findall(r"<tfoot>(.*?)</tfoot>", document, flags=re.S)
    assert len(matches) == 1, f"HTML 必须恰好包含一个 tfoot，实际 {len(matches)} 个"
    return re.findall(r"<tr>.*?</tr>", matches[0], flags=re.S)


def cells_of(row):
    return re.findall(r"<td[^>]*>(.*?)</td>", row, flags=re.S)


def snapshot_database(db_path):
    """记录数据库结构（sqlite_master）与 quotes/items 全部记录，供前后比对。"""
    conn = sqlite3.connect(db_path)
    try:
        master = conn.execute(
            "SELECT type, name, tbl_name, rootpage, sql "
            "FROM sqlite_master ORDER BY type, name"
        ).fetchall()
        quotes = conn.execute(
            "SELECT number, customer, total, note FROM quotes ORDER BY number"
        ).fetchall()
        items = conn.execute(
            "SELECT quote_number, position, description, quantity, "
            "unit_price, line_amount FROM items "
            "ORDER BY quote_number, position"
        ).fetchall()
    finally:
        conn.close()
    return master, quotes, items


class ReportTotalFixedSampleTestCase(ReportTotalTestBase):
    """固定三张报价：两条客户行 + 唯一合计行，合计只统计本次结果。"""

    def test_unfiltered_rows_and_unique_total(self):
        db_path = self.save_all(FIXED_SAMPLES)
        before = snapshot_database(db_path)

        result = self.report_html()
        self.assert_html_success(result)
        _, document = self.read_output()

        # 表头三列保持不变。
        head_row = re.search(r"<thead>.*?<tr>(.*?)</tr>.*?</thead>", document, flags=re.S)
        self.assertIsNotNone(head_row)
        self.assertEqual(
            re.findall(r"<th>(.*?)</th>", head_row.group(1), flags=re.S),
            ["客户", "报价张数", "累计金额（元）"],
        )

        body_rows = extract_body_rows(document)
        # 两条客户行：名为“合计”的客户也是普通客户行，不被当作汇总行。
        self.assertEqual(len(body_rows), 2, msg="必须恰好有两条客户行")
        # BINARY 顺序：“合计”（E59088…）在“演示…”（E6BC94…）之前。
        self.assertEqual(
            [cells_of(row) for row in body_rows],
            [
                ["合计", "1", "7.00"],
                ["演示&lt;甲&gt;&amp;乙", "2", "28.00"],
            ],
            msg="客户行显示各自张数与累计，特殊字符经 HTML 转义",
        )

        # 唯一合计行：3 张、35.00 元。
        foot_rows = extract_foot_rows(document)
        self.assertEqual(len(foot_rows), 1, msg="必须恰好有一条合计行")
        self.assertEqual(
            cells_of(foot_rows[0]),
            ["合计", "3", "35.00"],
            msg="合计行统计本次全部三张报价：700 + 2800 = 3500 分",
        )

        # 合计行必须在所有客户行之后（tfoot 位于 tbody 之后）。
        self.assertLess(
            document.index("</tbody>"), document.index("<tfoot>"),
            msg="合计行必须在所有客户行之后",
        )

        # 名为“合计”的客户行出现在 tbody 而不是 tfoot 中。
        body_text = re.search(r"<tbody>(.*?)</tbody>", document, flags=re.S).group(1)
        foot_text = re.search(r"<tfoot>(.*?)</tfoot>", document, flags=re.S).group(1)
        self.assertIn("<td>合计</td><td", body_text)
        self.assertIn("<td>合计</td><td", foot_text)
        self.assertIn("7.00", body_text)
        self.assertNotIn("35.00", body_text)
        self.assertIn("35.00", foot_text)
        self.assertNotIn("7.00", foot_text)

        # “合计”字样恰好两次：一次客户行、一次合计行；合计行张数 3 唯一。
        self.assertEqual(document.count("<td>合计</td>"), 2)
        self.assertEqual(document.count(">3</td>"), 1)

        # 报表只读取数据库：结构与记录前后完全一致。
        self.assertEqual(
            snapshot_database(db_path), before,
            msg="报表生成前后数据库的结构和记录必须保持一致",
        )

    def test_exact_filter_total_only_covers_matches(self):
        db_path = self.save_all(FIXED_SAMPLES)
        before = snapshot_database(db_path)

        result = self.report_html(
            "quotes.sqlite", "special.html", "--customer", CUSTOMER_SPECIAL
        )
        self.assert_html_success(result, "special.html")
        _, document = self.read_output("special.html")

        body_rows = extract_body_rows(document)
        self.assertEqual(len(body_rows), 1, msg="筛选后只有一个客户行")
        # 两张报价（含零金额赠品）计 2 张；多条明细不增加张数。
        self.assertEqual(
            cells_of(body_rows[0]),
            ["演示&lt;甲&gt;&amp;乙", "2", "28.00"],
        )

        foot_rows = extract_foot_rows(document)
        self.assertEqual(len(foot_rows), 1)
        # 合计只统计本次筛选命中的报价：客户行与合计行一致。
        self.assertEqual(cells_of(foot_rows[0]), ["合计", "2", "28.00"])

        # 未命中的“合计”客户不得出现在任何行中。
        self.assertNotIn("7.00", document)
        self.assertNotIn("Q-TOTAL-3", document)

        self.assertEqual(
            snapshot_database(db_path), before,
            msg="筛选报表生成前后数据库的结构和记录必须保持一致",
        )

    def test_zero_amount_quote_counts_and_items_do_not_inflate(self):
        self.save_all(FIXED_SAMPLES)
        result = self.report_html()
        self.assert_html_success(result)
        _, document = self.read_output()

        # Q-TOTAL-1 两条明细 + Q-TOTAL-2 零金额赠品：客户仍为 2 张、28.00 元。
        special_row = next(
            row for row in extract_body_rows(document) if "甲" in row
        )
        self.assertEqual(
            cells_of(special_row), ["演示&lt;甲&gt;&amp;乙", "2", "28.00"],
            msg="零金额报价计入张数，多条明细不增加张数",
        )


class ReportTotalEmptyTestCase(ReportTotalTestBase):
    """空库或无匹配：保留表头与提示，无客户行，合计行 0 张、0.00 元。"""

    def test_empty_database_total_is_zero(self):
        db_path = os.path.join(self.workdir, "empty.sqlite")
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(FULL_SCHEMA)
            conn.commit()
        finally:
            conn.close()
        before = snapshot_database(db_path)

        result = self.report_html("empty.sqlite")
        self.assert_html_success(result)
        _, document = self.read_output()

        self.assertIn("<th>客户</th>", document, msg="空库仍保留表头")
        self.assertEqual(extract_body_rows(document), [], msg="空库不显示客户行")
        self.assertIn(EMPTY_NOTICE, document)

        foot_rows = extract_foot_rows(document)
        self.assertEqual(len(foot_rows), 1)
        self.assertEqual(
            cells_of(foot_rows[0]), ["合计", "0", "0.00"],
            msg="空库合计行必须为 0 张、0.00 元",
        )

        self.assertEqual(snapshot_database(db_path), before)

    def test_no_match_filter_total_is_zero(self):
        db_path = self.save_all(FIXED_SAMPLES)
        before = snapshot_database(db_path)

        result = self.report_html(
            "quotes.sqlite", "nomatch.html", "--customer", "无此客户"
        )
        self.assert_html_success(result, "nomatch.html")
        _, document = self.read_output("nomatch.html")

        self.assertIn("<th>客户</th>", document, msg="无匹配仍保留表头")
        self.assertEqual(extract_body_rows(document), [], msg="无匹配不显示客户行")
        self.assertIn(EMPTY_NOTICE, document)

        foot_rows = extract_foot_rows(document)
        self.assertEqual(len(foot_rows), 1)
        self.assertEqual(
            cells_of(foot_rows[0]), ["合计", "0", "0.00"],
            msg="无匹配时合计行必须为 0 张、0.00 元",
        )
        # 未命中报价的金额绝不计入合计。
        self.assertNotIn("35.00", document)
        self.assertNotIn("28.00", document)
        self.assertNotIn("7.00", document)

        self.assertEqual(snapshot_database(db_path), before)


class ReportTotalBigAmountTestCase(ReportTotalTestBase):
    """两个不同客户各一张 INT64_MAX 分报价：合计不溢出、不舍入。"""

    def test_total_beyond_int64_is_exact(self):
        samples = [
            {
                "number": "Q-BIG-A",
                "customer": "超大客户甲",
                "items": [
                    {"description": "上限单价", "quantity": 1,
                     "unit_price": INT64_MAX}
                ],
            },
            {
                "number": "Q-BIG-B",
                "customer": "超大客户乙",
                "items": [
                    {"description": "上限单价", "quantity": 1,
                     "unit_price": INT64_MAX}
                ],
            },
        ]
        db_path = self.save_all(samples)
        before = snapshot_database(db_path)

        result = self.report_html("quotes.sqlite", "big.html")
        self.assert_html_success(result, "big.html")
        _, document = self.read_output("big.html")

        body_rows = extract_body_rows(document)
        self.assertEqual(len(body_rows), 2, msg="两个不同客户各一条客户行")
        per_customer = f"{INT64_MAX // 100}.{INT64_MAX % 100:02d}"
        for row in body_rows:
            self.assertEqual(
                cells_of(row)[1:], ["1", per_customer],
                msg="单张报价金额精确显示，不使用浮点",
            )

        foot_rows = extract_foot_rows(document)
        self.assertEqual(len(foot_rows), 1)
        total_cents = 2 * INT64_MAX
        # 18446744073709551614 分 -> 184467440737095516.14 元。
        self.assertEqual(
            cells_of(foot_rows[0]),
            ["合计", "2", f"{total_cents // 100}.{total_cents % 100:02d}"],
        )
        self.assertIn("184467440737095516.14", document)
        # 单元格必须逐字符等于精确值：既不溢出成科学计数法，也不舍入。
        self.assertNotIn("1.8446744073709552e+17", document)
        self.assertNotIn("184467440737095516.15", document)

        self.assertEqual(snapshot_database(db_path), before)


class ReportTotalJsonUnchangedTestCase(ReportTotalTestBase):
    """省略 --output 时 JSON 仍只含客户对象，不增加汇总对象。"""

    def test_without_output_has_only_two_customer_objects(self):
        self.save_all(FIXED_SAMPLES)
        result = self.run_quote("report", "--db", "quotes.sqlite")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout.count("\n"), 1)
        self.assertTrue(result.stdout.endswith("\n"))

        records = json.loads(result.stdout)
        # 仍是两个客户对象（BINARY 顺序：合计在前），金额为分单位整数 700、2800，
        # 不追加任何汇总对象。
        self.assertEqual(
            records,
            [
                {"customer": CUSTOMER_TOTAL_WORD, "quote_count": 1, "total": 700},
                {"customer": CUSTOMER_SPECIAL, "quote_count": 2, "total": 2800},
            ],
        )
        self.assertEqual(len(records), 2)
        for record in records:
            self.assertIsInstance(record["total"], int)
            self.assertIsInstance(record["quote_count"], int)
        self.assertFalse(
            any(name.endswith(".html") for name in os.listdir(self.workdir)),
            msg="省略 --output 不得生成任何 HTML 文件",
        )


_PROJECT_ROOT_ENTRIES_AT_START = set(os.listdir(PROJECT_ROOT))


def tearDownModule():
    leftovers = set(os.listdir(PROJECT_ROOT)) - _PROJECT_ROOT_ENTRIES_AT_START
    assert not leftovers, f"测试在项目根目录留下了文件: {sorted(leftovers)}"


if __name__ == "__main__":
    unittest.main()
