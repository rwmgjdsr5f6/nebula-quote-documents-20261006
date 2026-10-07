"""客户汇总报表合计行（report --output 的 tfoot）命令行回归测试。

专门核对 HTML 报表的合计行：合计只统计本次结果（筛选命中后分组）中的
报价，位于所有客户行之后，与恰好名为“合计”的客户行明确区分；零金额
报价计入张数，多条明细不增加张数，超大合计不溢出、不舍入。省略
--output 的 JSON 形式不增加任何汇总对象。

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

# ---- 固定样例：三张不同编号的报价 ----
# Q-TOTAL-1：客户“演示<甲>&乙”，咨询 数量 2 单价 1250 分（行金额 2500）+
#            资料 数量 1 单价 300 分，合计 2800 分（多条明细只计一张）。
# Q-TOTAL-2：同一客户，赠品 数量 1 单价 0 分，合计 0 分（零金额仍计一张）。
# Q-TOTAL-3：客户原文就是“合计”，服务 数量 1 单价 700 分，合计 700 分；
#            它必须作为 tbody 中的普通客户行，不能被当成汇总行。
CUSTOMER_MIXED = "演示<甲>&乙"
CUSTOMER_TOTAL = "合计"

TOTAL_SAMPLES = [
    {
        "number": "Q-TOTAL-1",
        "customer": CUSTOMER_MIXED,
        "items": [
            {"description": "咨询", "quantity": 2, "unit_price": 1250},
            {"description": "资料", "quantity": 1, "unit_price": 300},
        ],
    },
    {
        "number": "Q-TOTAL-2",
        "customer": CUSTOMER_MIXED,
        "items": [{"description": "赠品", "quantity": 1, "unit_price": 0}],
    },
    {
        "number": "Q-TOTAL-3",
        "customer": CUSTOMER_TOTAL,
        "items": [{"description": "服务", "quantity": 1, "unit_price": 700}],
    },
]

# 未筛选时期望的两个客户行（按客户原文 UTF-8 BINARY 升序：
# “合”E5… 在 “演”E6… 之前），以及唯一的合计行。
EXPECTED_BODY_CELLS = [
    [CUSTOMER_TOTAL, "1", "7.00"],
    ["演示&lt;甲&gt;&amp;乙", "2", "28.00"],
]
EXPECTED_FOOT_CELLS = [CUSTOMER_TOTAL, "3", "35.00"]

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
    quote_number TEXT NOT NULL REFERENCES quotes(number),
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
        self.assertEqual(result.stderr, "", msg="保存成功时标准错误必须为空")
        return db_path

    def save_all(self, samples, db_name="quotes.sqlite"):
        db_path = None
        for index, payload in enumerate(samples):
            db_path = self.save_sample(payload, index, db_name=db_name)
        return db_path

    def report_html(self, db_arg, out_arg="report.html", *extra):
        return self.run_quote(
            "report", "--db", db_arg, "--output", out_arg, *extra
        )

    def read_output(self, out_name="report.html"):
        path = os.path.join(self.workdir, out_name)
        with open(path, "rb") as f:
            raw = f.read()
        # 必须是严格 UTF-8。
        return raw, raw.decode("utf-8")

    def assert_html_success(self, result, out_arg):
        """生成 HTML 成功：退出 0、标准错误为空、标准输出仅为传入路径加换行。"""
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "", msg="成功时标准错误必须为空")
        self.assertEqual(
            result.stdout, f"{out_arg}\n",
            msg="标准输出只能是传入的输出路径加一个换行",
        )

    def create_empty_database(self, db_name="empty.sqlite"):
        db_path = os.path.join(self.workdir, db_name)
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(FULL_SCHEMA)
            conn.commit()
        finally:
            conn.close()
        return db_path

    def snapshot_database(self, db_name="quotes.sqlite"):
        """结构（sqlite_master）与 quotes/items 全部记录的快照，验证只读边界。"""
        db_path = (
            db_name if os.path.isabs(db_name)
            else os.path.join(self.workdir, db_name)
        )
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


def extract_body_rows(document):
    """取出 tbody 内的客户行（不含表头），每条为原始 <tr>...</tr> 文本。"""
    match = re.search(r"<tbody>(.*?)</tbody>", document, flags=re.S)
    assert match is not None, "HTML 必须包含 <tbody>"
    return re.findall(r"<tr>.*?</tr>", match.group(1), flags=re.S)


def extract_foot_cells(document):
    """取出唯一 tfoot 合计行的单元格；结构不符时断言失败。"""
    feet = re.findall(r"<tfoot>.*?</tfoot>", document, flags=re.S)
    assert len(feet) == 1, f"合计行必须唯一，实际找到 {len(feet)} 个 <tfoot>"
    rows = re.findall(r"<tr>.*?</tr>", feet[0], flags=re.S)
    assert len(rows) == 1, f"tfoot 中必须只有一行，实际 {len(rows)} 行"
    return re.findall(r"<td[^>]*>(.*?)</td>", rows[0], flags=re.S)


def cells_of(row):
    return re.findall(r"<td[^>]*>(.*?)</td>", row, flags=re.S)


class ReportTotalFixedSampleTestCase(ReportTotalTestBase):
    """固定三张报价：两个客户行 + 唯一合计行，合计只覆盖本次全部结果。"""

    def test_unfiltered_rows_and_total(self):
        db_path = self.save_all(TOTAL_SAMPLES)
        before = self.snapshot_database(db_path)

        result = self.report_html("quotes.sqlite", "report.html")
        self.assert_html_success(result, "report.html")

        _, document = self.read_output()

        # 两个客户行：名为“合计”的客户是普通客户行（tbody），
        # 演示客户两行报价（含零金额与多明细）合并为一组。
        body_rows = extract_body_rows(document)
        self.assertEqual(
            [cells_of(row) for row in body_rows],
            EXPECTED_BODY_CELLS,
            msg="客户行应为：合计客户 1 张 7.00 元、演示客户 2 张 28.00 元",
        )

        # 唯一合计行：3 张、35.00 元（2800 + 0 + 700 分）。
        self.assertEqual(
            extract_foot_cells(document), EXPECTED_FOOT_CELLS,
            msg="合计行必须是 3 张、35.00 元",
        )

        # 合计行在所有客户行之后；“合计”客户行在 tbody，汇总行在 tfoot。
        self.assertIn("<tbody>", document)
        self.assertIn("<tfoot>", document)
        self.assertLess(
            document.index("</tbody>"), document.index("<tfoot>"),
            msg="合计行（tfoot）必须位于所有客户行（tbody）之后",
        )
        tbody_text = re.search(
            r"<tbody>(.*?)</tbody>", document, flags=re.S
        ).group(1)
        self.assertIn(
            "<tr><td>合计</td>", tbody_text,
            msg="名为“合计”的客户必须仍作为 tbody 中的客户行显示",
        )
        self.assertEqual(
            tbody_text.count("<td>合计</td>"), 1,
            msg="tbody 中只应有一个名为“合计”的客户行",
        )
        self.assertEqual(
            document.count("<tfoot>"), 1, msg="全页只能有一个合计行（tfoot）"
        )

        # 客户特殊字符仍正确转义，原文尖括号与与号不得裸出现在 HTML 中。
        self.assertIn("演示&lt;甲&gt;&amp;乙", document)
        self.assertNotIn("演示<甲>&乙", document)

        # 合计恒等：合计行 = 本次客户行的张数之和与金额之和。
        def to_cents(text):
            yuan, fen = text.split(".")
            return int(yuan) * 100 + int(fen)

        self.assertEqual(
            int(extract_foot_cells(document)[1]),
            sum(int(cells_of(row)[1]) for row in body_rows),
        )
        self.assertEqual(
            to_cents(extract_foot_cells(document)[2]),
            sum(to_cents(cells_of(row)[2]) for row in body_rows),
        )

        # 报表只读：生成前后表结构与报价、明细记录保持一致。
        self.assertEqual(
            self.snapshot_database(db_path), before,
            msg="报表生成前后数据库结构与记录必须一致",
        )

    def test_total_only_covers_filtered_matches(self):
        db_path = self.save_all(TOTAL_SAMPLES)
        before = self.snapshot_database(db_path)

        result = self.report_html(
            "quotes.sqlite", "filtered.html", "--customer", CUSTOMER_MIXED
        )
        self.assert_html_success(result, "filtered.html")

        _, document = self.read_output("filtered.html")

        # 精确筛选后只有演示客户一行：2 张（零金额 Q-TOTAL-2 计入张数，
        # Q-TOTAL-1 两条明细不增加张数）、28.00 元。
        body_rows = extract_body_rows(document)
        self.assertEqual(
            [cells_of(row) for row in body_rows],
            [["演示&lt;甲&gt;&amp;乙", "2", "28.00"]],
            msg="筛选后只显示演示客户 2 张、28.00 元",
        )
        # 合计行同样只统计本次命中的 2 张，不含库中其他客户的报价。
        self.assertEqual(
            extract_foot_cells(document), [CUSTOMER_TOTAL, "2", "28.00"],
            msg="合计行必须随筛选结果收窄为 2 张、28.00 元",
        )
        # 未命中的“合计”客户不得出现在客户行中。
        tbody_text = re.search(
            r"<tbody>(.*?)</tbody>", document, flags=re.S
        ).group(1)
        self.assertNotIn("<td>合计</td>", tbody_text)

        self.assertEqual(
            self.snapshot_database(db_path), before,
            msg="报表生成前后数据库结构与记录必须一致",
        )


class ReportTotalEmptyTestCase(ReportTotalTestBase):
    """空库与筛选不到客户：表头与提示保留、无客户行，合计行为 0 张 0.00 元。"""

    def test_empty_database_total_is_zero(self):
        db_name = "empty.sqlite"
        db_path = self.create_empty_database(db_name)
        before = self.snapshot_database(db_name)

        result = self.report_html(db_name, "empty.html")
        self.assert_html_success(result, "empty.html")

        _, document = self.read_output("empty.html")
        self.assertIn("<th>客户</th>", document, msg="空库仍保留表头")
        self.assertEqual(extract_body_rows(document), [], msg="空库不显示客户行")
        self.assertIn(EMPTY_NOTICE, document)
        self.assertEqual(
            extract_foot_cells(document), [CUSTOMER_TOTAL, "0", "0.00"],
            msg="空库合计行必须为 0 张、0.00 元",
        )
        self.assertEqual(
            self.snapshot_database(db_path), before,
            msg="报表生成前后数据库结构与记录必须一致",
        )

    def test_no_match_filter_total_is_zero(self):
        db_path = self.save_all(TOTAL_SAMPLES)
        before = self.snapshot_database(db_path)

        result = self.report_html(
            "quotes.sqlite", "nomatch.html", "--customer", "无此客户"
        )
        self.assert_html_success(result, "nomatch.html")

        _, document = self.read_output("nomatch.html")
        self.assertIn("<th>客户</th>", document)
        self.assertEqual(extract_body_rows(document), [], msg="无匹配不显示客户行")
        self.assertIn(EMPTY_NOTICE, document)
        self.assertEqual(
            extract_foot_cells(document), [CUSTOMER_TOTAL, "0", "0.00"],
            msg="筛选不到客户时合计行必须为 0 张、0.00 元",
        )
        self.assertEqual(
            self.snapshot_database(db_path), before,
            msg="报表生成前后数据库结构与记录必须一致",
        )


class ReportTotalBigAmountTestCase(ReportTotalTestBase):
    """两个不同客户各一张 INT64_MAX 分报价：合计精确、不溢出、不舍入。"""

    def test_total_beyond_int64_is_exact(self):
        big_samples = [
            {
                "number": "Q-BIG-A",
                "customer": "大额乙",
                "items": [
                    {"description": "服务", "quantity": 1, "unit_price": INT64_MAX}
                ],
            },
            {
                "number": "Q-BIG-B",
                "customer": "大额甲",
                "items": [
                    {"description": "服务", "quantity": 1, "unit_price": INT64_MAX}
                ],
            },
        ]
        db_path = self.save_all(big_samples)
        before = self.snapshot_database(db_path)

        result = self.report_html("quotes.sqlite", "big.html")
        self.assert_html_success(result, "big.html")

        _, document = self.read_output("big.html")

        # 两个客户各一张，单价 9223372036854775807 分 = 92233720368547758.07 元；
        # UTF-8 BINARY 升序：“大额乙”（E4B999）在“大额甲”（E794B2）之前。
        body_rows = extract_body_rows(document)
        body_by_customer = {cells_of(row)[0]: cells_of(row)[1:] for row in body_rows}
        self.assertEqual(
            body_by_customer,
            {
                "大额乙": ["1", "92233720368547758.07"],
                "大额甲": ["1", "92233720368547758.07"],
            },
            msg="两个客户各为 1 张、92233720368547758.07 元",
        )
        # 合计 2 张、184467440737095516.14 元（2 * 2^63-2 分），
        # 超出有符号 64 位仍精确保留两位小数，不经浮点、不舍入。
        self.assertEqual(
            extract_foot_cells(document),
            [CUSTOMER_TOTAL, "2", "184467440737095516.14"],
            msg="合计必须精确为 2 张、184467440737095516.14 元",
        )
        self.assertIn("184467440737095516.14", document)
        self.assertNotRegex(document, r"1\.844674407370955[0-9eE+]*")

        self.assertEqual(
            self.snapshot_database(db_path), before,
            msg="报表生成前后数据库结构与记录必须一致",
        )


class ReportTotalJsonUnchangedTestCase(ReportTotalTestBase):
    """省略 --output 时 JSON 仍只有客户对象，不增加汇总对象。"""

    def test_json_has_only_two_customer_objects(self):
        self.save_all(TOTAL_SAMPLES)
        result = self.run_quote("report", "--db", "quotes.sqlite")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout.count("\n"), 1)
        self.assertTrue(result.stdout.endswith("\n"))

        records = json.loads(result.stdout)
        # 仅两个客户对象，金额为分单位整数 700 与 2800，不含第三个汇总对象。
        self.assertEqual(
            records,
            [
                {"customer": CUSTOMER_TOTAL, "quote_count": 1, "total": 700},
                {"customer": CUSTOMER_MIXED, "quote_count": 2, "total": 2800},
            ],
            msg="JSON 必须只含合计客户（700）与演示客户（2800）两个对象",
        )
        for record in records:
            self.assertEqual(set(record.keys()), {"customer", "quote_count", "total"})
            self.assertIsInstance(record["total"], int)
            self.assertNotIsInstance(record["total"], bool)
        # 客户原文（含 < 与 &）不经 HTML 转义，且不生成任何 HTML 文件。
        self.assertIn("演示<甲>&乙", result.stdout)
        self.assertFalse(
            any(name.endswith(".html") for name in os.listdir(self.workdir)),
            msg="省略 --output 时不得生成 HTML 文件",
        )


# 项目目录清洁性：测试开始前记录项目根内容，全部用例结束后不得新增任何文件
# （所有数据库、输入 JSON、HTML 都必须落在临时目录中，随临时目录清理）。
_PROJECT_ROOT_ENTRIES_AT_START = set(os.listdir(PROJECT_ROOT))


def tearDownModule():
    leftovers = set(os.listdir(PROJECT_ROOT)) - _PROJECT_ROOT_ENTRIES_AT_START
    assert not leftovers, f"测试在项目根目录留下了文件: {sorted(leftovers)}"


if __name__ == "__main__":
    unittest.main()
