"""按客户汇总报表（report）命令行回归测试：保存 -> report JSON 输出 -> 只读边界。

仅依赖 Python 标准库。所有用例在独立的临时目录中通过 README 记载的
公开命令（save / report）操作 quote.py 子进程，使用合成单据，
不读取项目内业务数据，也不在项目目录留下 JSON、SQLite 或 HTML 文件。

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

# ---- 固定验收样例：Q-A、Q-B、Q-C、Q-D ----
# 客户分别为 Demo、Demo、demo、两端各带一个空格的 Demo；
# 合计分别为 0、2500、300、500 分；Q-B 故意包含多条明细。
SAMPLES = [
    {
        "number": "Q-A",
        "customer": "Demo",
        "items": [{"description": "零金额服务", "quantity": 1, "unit_price": 0}],
    },
    {
        "number": "Q-B",
        "customer": "Demo",
        "items": [
            {"description": "咨询", "quantity": 2, "unit_price": 1000},
            {"description": "资料", "quantity": 1, "unit_price": 500},
        ],
    },
    {
        "number": "Q-C",
        "customer": "demo",
        "items": [{"description": "服务", "quantity": 3, "unit_price": 100}],
    },
    {
        "number": "Q-D",
        "customer": " Demo ",
        "items": [{"description": "定制", "quantity": 1, "unit_price": 500}],
    },
]

# 期望输出为固定常量：按客户原文 SQLite BINARY 升序，
# 空格（0x20）开头的客户在最前，大写 Demo 先于小写 demo。
EXPECTED_REPORT = [
    {"customer": " Demo ", "quote_count": 1, "total": 500},
    {"customer": "Demo", "quote_count": 2, "total": 2500},
    {"customer": "demo", "quote_count": 1, "total": 300},
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

LEGACY_SCHEMA = """
CREATE TABLE quotes (
    number   TEXT PRIMARY KEY,
    customer TEXT NOT NULL,
    total    INTEGER NOT NULL
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

ERROR_PREFIX = "错误: 无法读取数据库 "


class QuoteReportTestBase(unittest.TestCase):
    """每个用例使用独立临时目录，只通过公开命令行入口驱动 quote.py。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="quote-report-test-")
        self.addCleanup(self._tmp.cleanup)
        self.workdir = self._tmp.name

    def run_quote(self, *cli_args):
        """在临时目录中以独立进程运行 quote.py 的公开命令。"""
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

    def save_sample(self, payload, index, db_name="demo.sqlite"):
        db_path = os.path.join(self.workdir, db_name)
        input_path = self.write_input(payload, f"input-{index}.json")
        result = self.run_quote("save", "--db", db_path, "--input", input_path)
        self.assertEqual(
            result.returncode, 0,
            msg=f"样例 {payload['number']} 保存应成功: {result.stderr}",
        )
        self.assertEqual(result.stderr, "", msg="保存成功时标准错误必须为空")
        return db_path

    def report(self, db_path):
        return self.run_quote("report", "--db", db_path)

    def assert_report_error(self, result, db_arg):
        """失败路径：退出 1、标准输出为空、标准错误为固定前缀的单行原因。"""
        self.assertEqual(
            result.returncode, 1,
            msg=f"无法读取数据库时应退出 1，实际 {result.returncode}",
        )
        self.assertEqual(result.stdout, "", msg="失败时标准输出必须为空")
        stderr = result.stderr
        self.assertTrue(
            stderr.startswith(ERROR_PREFIX),
            msg=f"标准错误应以 {ERROR_PREFIX!r} 开头，实际: {stderr!r}",
        )
        self.assertIn(
            db_arg, stderr,
            msg=f"标准错误必须包含传入路径 {db_arg!r}，实际: {stderr!r}",
        )
        # 形如“错误: 无法读取数据库 <路径>: <原因>”：路径之后必须给出原因。
        remainder = stderr[len(ERROR_PREFIX):]
        self.assertTrue(
            remainder.startswith(f"{db_arg}: ") and remainder.endswith("\n"),
            msg=f"标准错误应为“路径: 原因”单行格式，实际: {stderr!r}",
        )
        reason = remainder[len(db_arg) + 2:-1]
        self.assertTrue(reason.strip(), msg="失败原因不能为空")
        self.assertNotIn("Traceback", stderr, msg="不得向用户输出异常堆栈")
        self.assertEqual(stderr.count("\n"), 1, msg="标准错误应为单行")


class ReportFlowTestCase(QuoteReportTestBase):
    """固定验收样例：分组、排序、字段白名单、计数与整数累计。"""

    def test_report_matches_fixed_expectations(self):
        db_path = None
        for index, payload in enumerate(SAMPLES):
            db_path = self.save_sample(payload, index)

        # 前置自检：Q-B 确实保存了两条明细，证明每张报价只计一次而不是按明细计数。
        conn = sqlite3.connect(db_path)
        try:
            item_count = conn.execute(
                "SELECT COUNT(*) FROM items WHERE quote_number = ?", ("Q-B",)
            ).fetchone()[0]
            saved_totals = dict(
                conn.execute("SELECT number, total FROM quotes").fetchall()
            )
        finally:
            conn.close()
        self.assertEqual(item_count, 2, msg="前置条件：Q-B 含两条明细")
        self.assertEqual(saved_totals, {"Q-A": 0, "Q-B": 2500, "Q-C": 300, "Q-D": 500})

        schema_before = snapshot_sqlite_master(db_path)

        result = self.report(db_path)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "", msg="报表成功时标准错误必须为空")

        stdout = result.stdout
        self.assertTrue(stdout.endswith("\n"), msg="标准输出必须以换行结束")
        self.assertEqual(stdout.count("\n"), 1, msg="标准输出必须是单行 JSON")
        self.assertNotIn("\r", stdout, msg="不得使用 CRLF 换行")

        report = json.loads(stdout)
        # 期望数据来自本模块固定常量，不通过被测函数或 SQL 分组生成。
        self.assertEqual(report, EXPECTED_REPORT, msg="报表内容必须与固定样例一致")
        self.assertEqual(
            [row["customer"] for row in report],
            [" Demo ", "Demo", "demo"],
            msg="顺序必须为带空格客户、Demo、demo（BINARY 升序）",
        )

        for row, expected in zip(report, EXPECTED_REPORT):
            self.assertEqual(
                set(row.keys()), {"customer", "quote_count", "total"},
                msg=f"{expected['customer']!r} 只能含 customer、quote_count、total 三个字段",
            )
            self.assertIsInstance(row["quote_count"], int)
            self.assertIsInstance(row["total"], int)
            self.assertNotIsInstance(row["quote_count"], bool)
            self.assertNotIsInstance(row["total"], bool)
            self.assertEqual(row["customer"], expected["customer"])
            self.assertEqual(row["quote_count"], expected["quote_count"])
            self.assertEqual(row["total"], expected["total"])

        # 只读边界：报表后表结构（含 note 列）与全部记录保持一致。
        self.assertEqual(
            snapshot_sqlite_master(db_path), schema_before,
            msg="报表查询不得改变表结构",
        )
        conn = sqlite3.connect(db_path)
        try:
            quotes_after = conn.execute(
                "SELECT number, customer, total, note FROM quotes ORDER BY number"
            ).fetchall()
            items_after = conn.execute(
                "SELECT quote_number, position, description, quantity, "
                "unit_price, line_amount FROM items "
                "ORDER BY quote_number, position"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(
            quotes_after,
            sorted(
                [
                    ("Q-A", "Demo", 0, None),
                    ("Q-B", "Demo", 2500, None),
                    ("Q-C", "demo", 300, None),
                    ("Q-D", " Demo ", 500, None),
                ]
            ),
            msg="报表后报价记录必须保持不变",
        )
        self.assertEqual(len(items_after), 5, msg="报表后明细记录必须保持不变（Q-B 两条）")

    def test_report_is_independent_of_save_order_and_single_line_json(self):
        # 逆序保存：报表顺序仍必须是 BINARY 升序，与保存先后无关。
        db_path = None
        for index, payload in enumerate(reversed(SAMPLES)):
            db_path = self.save_sample(payload, index)
        result = self.report(db_path)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(json.loads(result.stdout), EXPECTED_REPORT)
        self.assertEqual(result.stdout.count("\n"), 1)


class EmptyDatabaseReportTestCase(QuoteReportTestBase):
    """完整建表但没有报价：返回 [] 加换行。"""

    def test_empty_database_reports_empty_array(self):
        db_path = os.path.join(self.workdir, "empty.sqlite")
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(FULL_SCHEMA)
            conn.commit()
        finally:
            conn.close()

        result = self.report(db_path)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            result.stdout, "[]\n",
            msg="空报价库标准输出应为空数组加单个换行",
        )
        self.assertEqual(json.loads(result.stdout), [])


class GroupingSemanticsTestCase(QuoteReportTestBase):
    """逐字符分组：换行、连续空格、% 与 _ 都参与区分；零金额照样计数。"""

    def test_whitespace_and_symbol_customers_group_by_exact_bytes(self):
        db_path = os.path.join(self.workdir, "groups.sqlite")
        # 直接构造库：这些客户文本无法体现“合计取自已保存值”与明细的关系，
        # 报表只应读取主表，items 可以缺省；仍建 items 表以保持完整结构。
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(FULL_SCHEMA)
            rows = [
                ("G-1", "a%b", 0),
                ("G-2", "a%b", 0),       # 与 G-1 同组：两张零金额报价
                ("G-3", "a_b", 10),      # 下划线不是通配符，独立成组
                ("G-4", "a  b", 1),      # 连续两个空格
                ("G-5", "a b", 1),       # 单个空格
                ("G-6", "a\nb", 1),      # 换行不是空格
                ("G-7", "X", 7),
                ("G-8", "x", 8),         # 大小写不同
                ("G-9", "客户", 100),
                ("G-10", "客户", 200),
            ]
            conn.executemany(
                "INSERT INTO quotes(number, customer, total) VALUES (?, ?, ?)", rows
            )
            conn.commit()
        finally:
            conn.close()

        result = self.report(db_path)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        report = json.loads(result.stdout)

        # 期望顺序独立用 Python 按 UTF-8 字节排序生成，与 SQLite BINARY 一致。
        customers = sorted({customer for _, customer, _ in rows},
                           key=lambda value: value.encode("utf-8"))
        expected = [
            {
                "customer": customer,
                "quote_count": sum(1 for _, c, _ in rows if c == customer),
                "total": sum(total for _, c, total in rows if c == customer),
            }
            for customer in customers
        ]
        self.assertEqual(report, expected)

        by_customer = {row["customer"]: row for row in report}
        self.assertEqual(by_customer["a%b"]["quote_count"], 2)
        self.assertEqual(by_customer["a%b"]["total"], 0, msg="零金额累计仍为 0")
        self.assertNotIn("ab", by_customer, msg="% 不得当作通配符")
        self.assertIn("a_b", by_customer, msg="_ 按普通字符处理")
        for name in ("a  b", "a b", "a\nb"):
            self.assertEqual(by_customer[name]["quote_count"], 1)
        self.assertEqual(by_customer["客户"]["total"], 300)


class LargeTotalTestCase(QuoteReportTestBase):
    """多个合法报价累计超过有符号 64 位整数上限时仍精确输出。"""

    def test_total_beyond_int64_is_output_exactly(self):
        db_path = os.path.join(self.workdir, "large.sqlite")
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(FULL_SCHEMA)
            # 两张各为 INT64_MAX 的报价：累计 2^64-2，超出有符号 64 位上限。
            conn.execute(
                "INSERT INTO quotes(number, customer, total) VALUES (?, ?, ?)",
                ("L-1", "大客", INT64_MAX),
            )
            conn.execute(
                "INSERT INTO quotes(number, customer, total) VALUES (?, ?, ?)",
                ("L-2", "大客", INT64_MAX),
            )
            conn.commit()
        finally:
            conn.close()

        result = self.report(db_path)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(
            report,
            [{"customer": "大客", "quote_count": 2, "total": 2 * INT64_MAX}],
        )
        self.assertEqual(2 * INT64_MAX, 18446744073709551614)
        self.assertIn("18446744073709551614", result.stdout, msg="大整数必须逐位输出")


class LegacyDatabaseReportTestCase(QuoteReportTestBase):
    """旧格式数据库（quotes 缺 note 列）：可汇总且查询不补列、不改记录。"""

    LEGACY_ROWS = [
        ("R-1", " Demo ", 500),
        ("R-2", "Demo", INT64_MAX),
        ("R-3", "Demo", 5),  # 同组累计超出 int64，仍须精确
    ]

    def build_legacy_database(self, db_path):
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(LEGACY_SCHEMA)
            conn.executemany(
                "INSERT INTO quotes(number, customer, total) VALUES (?, ?, ?)",
                self.LEGACY_ROWS,
            )
            conn.commit()
        finally:
            conn.close()

    def test_legacy_database_reported_without_mutation(self):
        db_path = os.path.join(self.workdir, "legacy.sqlite")
        self.build_legacy_database(db_path)

        schema_before = snapshot_sqlite_master(db_path)
        columns_before = {
            row[1] for row in query_table_info(db_path)
        }
        self.assertNotIn("note", columns_before, msg="前置条件：旧库没有 note 列")

        result = self.report(db_path)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            json.loads(result.stdout),
            [
                {"customer": " Demo ", "quote_count": 1, "total": 500},
                {"customer": "Demo", "quote_count": 2, "total": INT64_MAX + 5},
            ],
        )

        columns_after = {row[1] for row in query_table_info(db_path)}
        self.assertEqual(columns_after, columns_before, msg="报表不得为旧库补 note 列")
        self.assertNotIn("note", columns_after)
        self.assertEqual(
            snapshot_sqlite_master(db_path), schema_before,
            msg="查询前后 sqlite_master（表结构）必须逐字节一致",
        )
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(
                "SELECT number, customer, total FROM quotes ORDER BY number"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(rows, sorted(self.LEGACY_ROWS), msg="旧库记录查询后保持不变")


class ReportFailureTestCase(QuoteReportTestBase):
    """无法读取数据库的各类失败路径，以及不创建文件的只读边界。"""

    def test_missing_database_file_errors_without_creating_file(self):
        db_arg = "does-not-exist.sqlite"
        db_path = os.path.join(self.workdir, db_arg)
        self.assertFalse(os.path.exists(db_path))

        result = self.run_quote("report", "--db", db_arg)
        self.assert_report_error(result, db_arg)
        self.assertFalse(
            os.path.exists(db_path),
            msg="报表不存在的数据库绝不能创建数据库文件",
        )

    def test_non_sqlite_file_errors_and_preserves_bytes(self):
        db_arg = "not-a-database.bin"
        db_path = os.path.join(self.workdir, db_arg)
        original_bytes = b"this is definitely not a sqlite database\n\x00\xff"
        with open(db_path, "wb") as f:
            f.write(original_bytes)

        result = self.run_quote("report", "--db", db_arg)
        self.assert_report_error(result, db_arg)

        with open(db_path, "rb") as f:
            self.assertEqual(f.read(), original_bytes, msg="非数据库文件内容必须保持不变")

    def test_database_without_quotes_table_errors_and_keeps_schema(self):
        db_arg = "no-quotes-table.sqlite"
        db_path = os.path.join(self.workdir, db_arg)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("CREATE TABLE unrelated(id INTEGER PRIMARY KEY, tag TEXT)")
            conn.execute("INSERT INTO unrelated(tag) VALUES (?)", ("保留",))
            conn.commit()
        finally:
            conn.close()

        schema_before = snapshot_sqlite_master(db_path)
        result = self.run_quote("report", "--db", db_arg)
        self.assert_report_error(result, db_arg)

        self.assertEqual(snapshot_sqlite_master(db_path), schema_before)
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute("SELECT id, tag FROM unrelated ORDER BY id").fetchall()
        finally:
            conn.close()
        self.assertEqual(rows, [(1, "保留")], msg="数据库内既有记录必须保持不变")

    def test_missing_each_required_column_errors(self):
        required = {"number", "customer", "total"}
        column_types = {
            "number": "TEXT PRIMARY KEY",
            "customer": "TEXT NOT NULL",
            "total": "INTEGER NOT NULL",
        }
        for missing in sorted(required):
            with self.subTest(missing=missing):
                db_arg = f"miss-{missing}.sqlite"
                db_path = os.path.join(self.workdir, db_arg)
                ddl = "CREATE TABLE quotes (" + ", ".join(
                    f"{name} {column_types[name]}"
                    for name in column_types if name != missing
                ) + ")"
                conn = sqlite3.connect(db_path)
                try:
                    conn.execute(ddl)
                    conn.commit()
                finally:
                    conn.close()

                result = self.run_quote("report", "--db", db_arg)
                self.assert_report_error(result, db_arg)
                self.assertIn(missing, result.stderr, msg="原因应指出缺失的列")

    def test_database_path_that_is_a_directory_errors(self):
        db_arg = "a-directory"
        os.mkdir(os.path.join(self.workdir, db_arg))
        result = self.run_quote("report", "--db", db_arg)
        self.assert_report_error(result, db_arg)


def snapshot_sqlite_master(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT type, name, tbl_name, rootpage, sql "
            "FROM sqlite_master ORDER BY type, name"
        ).fetchall()
    finally:
        conn.close()


def query_table_info(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("PRAGMA table_info(quotes)").fetchall()
    finally:
        conn.close()


# 项目目录清洁性：测试开始前记录项目根内容，全部用例结束后不得新增任何文件
# （所有数据库、输入 JSON 都必须落在临时目录中）。
_PROJECT_ROOT_ENTRIES_AT_START = set(os.listdir(PROJECT_ROOT))


def tearDownModule():
    leftovers = set(os.listdir(PROJECT_ROOT)) - _PROJECT_ROOT_ENTRIES_AT_START
    assert not leftovers, f"测试在项目根目录留下了文件: {sorted(leftovers)}"


if __name__ == "__main__":
    unittest.main()
