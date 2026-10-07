"""客户汇总报表（report）命令行回归测试：保存 -> report JSON 输出 -> 只读边界。

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

# ---- 固定样例：Q-A、Q-B、Q-C、Q-D，客户分别为 Demo、Demo、demo、
# 两端各带一个空格的 Demo，合计分别为 0、2500、300、500 分；Q-B 含多条明细。
# 保存顺序故意打乱为 D、B、A、C，证明报表顺序与保存先后无关。

NUMBER_A = "Q-A"
NUMBER_B = "Q-B"
NUMBER_C = "Q-C"
NUMBER_D = "Q-D"

CUSTOMER_DEMO = "Demo"
CUSTOMER_LOWER = "demo"
CUSTOMER_PADDED = " Demo "  # 两端各一个空格

SAVE_SAMPLES = [
    {
        "number": NUMBER_D,
        "customer": CUSTOMER_PADDED,
        "items": [{"description": "培训", "quantity": 1, "unit_price": 500}],
    },
    {
        "number": NUMBER_B,
        "customer": CUSTOMER_DEMO,
        "items": [
            {"description": "咨询", "quantity": 2, "unit_price": 1000},
            {"description": "资料", "quantity": 1, "unit_price": 500},
        ],
    },
    {
        "number": NUMBER_A,
        "customer": CUSTOMER_DEMO,
        "items": [{"description": "零金额资料", "quantity": 1, "unit_price": 0}],
    },
    {
        "number": NUMBER_C,
        "customer": CUSTOMER_LOWER,
        "items": [{"description": "资料", "quantity": 1, "unit_price": 300}],
    },
]

# 期望的报表输出：固定常量，按客户原文 BINARY 升序（空格 < 大写 < 小写）。
# Demo 组含 Q-A（0 分）与 Q-B（2500 分）两张，累计 2500 分；零金额同样计张数。
EXPECTED_RECORDS = [
    {"customer": CUSTOMER_PADDED, "quote_count": 1, "total": 500},
    {"customer": CUSTOMER_DEMO, "quote_count": 2, "total": 2500},
    {"customer": CUSTOMER_LOWER, "quote_count": 1, "total": 300},
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

# 旧格式：quotes 缺 note 列，且没有 items 表（报表不读取明细）。
LEGACY_SCHEMA = """
CREATE TABLE quotes (
    number   TEXT PRIMARY KEY,
    customer TEXT NOT NULL,
    total    INTEGER NOT NULL
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

    def save_sample(self, payload, index):
        db_path = os.path.join(self.workdir, "quotes.sqlite")
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
    """固定样例汇总：分组、排序、字段白名单、整数输出与只读边界。"""

    def test_report_groups_customers_with_fixed_expectations(self):
        db_path = None
        for index, payload in enumerate(SAVE_SAMPLES):
            db_path = self.save_sample(payload, index)

        # 前置自检：Q-B 确实含多条明细，证明报表按报价行计数而非按明细计数。
        conn = sqlite3.connect(db_path)
        try:
            item_count = conn.execute(
                "SELECT COUNT(*) FROM items WHERE quote_number = ?", (NUMBER_B,)
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(item_count, 2, msg="前置条件：Q-B 在库中有两条明细")

        schema_before = snapshot_sqlite_master(db_path)
        quotes_before, items_before = snapshot_rows(db_path)
        workdir_entries_before = set(os.listdir(self.workdir))

        result = self.report(db_path)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "", msg="报表成功时标准错误必须为空")

        stdout = result.stdout
        self.assertTrue(stdout.endswith("\n"), msg="标准输出必须以换行结束")
        self.assertEqual(stdout.count("\n"), 1, msg="标准输出必须是单行 JSON")
        self.assertNotIn("\r", stdout, msg="不得使用 CRLF 换行")

        records = json.loads(stdout)
        # 期望数据来自本模块固定常量，不通过被测函数生成。
        self.assertEqual(
            records, EXPECTED_RECORDS,
            msg="报表应依次为带空格客户 1 张 500 分、Demo 2 张 2500 分、demo 1 张 300 分",
        )
        self.assertEqual(
            [r["customer"] for r in records],
            [CUSTOMER_PADDED, CUSTOMER_DEMO, CUSTOMER_LOWER],
            msg="客户应按原文 BINARY 升序（空格 < 大写 < 小写），与保存先后无关",
        )

        for record in records:
            self.assertEqual(
                set(record.keys()), {"customer", "quote_count", "total"},
                msg="每个客户对象只能含 customer、quote_count、total 三个字段",
            )
            for field in ("quote_count", "total"):
                self.assertIsInstance(
                    record[field], int,
                    msg=f"{field} 必须是 JSON 整数，不转为元、浮点数或字符串",
                )
                self.assertNotIsInstance(record[field], bool)

        # Demo 组两张报价（含零金额的 Q-A 与多明细的 Q-B）只各计一次。
        demo_record = records[1]
        self.assertEqual(demo_record["quote_count"], 2, msg="多明细报价只计一张")
        self.assertEqual(demo_record["total"], 2500, msg="累计以已保存合计为准")

        # 只读边界：报表后表结构、报价与明细记录保持一致，且不产生任何新文件。
        self.assertEqual(
            snapshot_sqlite_master(db_path), schema_before,
            msg="报表不得改变表结构",
        )
        self.assertEqual(
            snapshot_rows(db_path), (quotes_before, items_before),
            msg="报表不得改变已有报价与明细记录",
        )
        self.assertEqual(
            set(os.listdir(self.workdir)), workdir_entries_before,
            msg="报表只输出到标准输出，不得生成报表文件",
        )


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
        self.assertEqual(result.stderr, "", msg="报表成功时标准错误必须为空")
        self.assertEqual(
            result.stdout, "[]\n",
            msg="空报价库时标准输出应为空数组加单个换行",
        )
        self.assertEqual(json.loads(result.stdout), [])


class LegacyDatabaseReportTestCase(QuoteReportTestBase):
    """旧格式数据库（quotes 缺 note 列、无 items 表）：可汇总且不补列。"""

    # 客户名覆盖首尾空格、换行、<、&、引号以及 % 与 _（按普通字符处理）。
    LEGACY_NUMBER = "Q-LEGACY"
    LEGACY_CUSTOMER = " 旧库_客户 <%> & 'y'\n"
    LEGACY_TOTAL = 2500

    def build_legacy_database(self, db_path):
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(LEGACY_SCHEMA)
            conn.execute(
                "INSERT INTO quotes(number, customer, total) VALUES (?, ?, ?)",
                (self.LEGACY_NUMBER, self.LEGACY_CUSTOMER, self.LEGACY_TOTAL),
            )
            conn.commit()
        finally:
            conn.close()

    def test_legacy_database_reported_without_schema_changes(self):
        db_path = os.path.join(self.workdir, "legacy.sqlite")
        self.build_legacy_database(db_path)

        schema_before = snapshot_sqlite_master(db_path)
        columns_before = {col[1] for col in query_table_info(db_path)}
        self.assertNotIn("note", columns_before, msg="前置条件：旧库没有 note 列")

        result = self.report(db_path)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertTrue(result.stdout.endswith("\n"))
        self.assertEqual(result.stdout.count("\n"), 1)

        records = json.loads(result.stdout)
        self.assertEqual(
            records,
            [
                {
                    "customer": self.LEGACY_CUSTOMER,
                    "quote_count": 1,
                    "total": self.LEGACY_TOTAL,
                }
            ],
            msg="旧库报价应正常汇总，客户原文（含 % 与 _）逐字符保留",
        )
        self.assertEqual(
            set(records[0].keys()), {"customer", "quote_count", "total"},
            msg="旧库报表同样只能返回三个字段",
        )

        # 只读边界：不补 note 列、不建 items 表，表结构与记录均保持原样。
        self.assertEqual(
            {col[1] for col in query_table_info(db_path)}, columns_before,
            msg="报表不得为旧库补 note 列",
        )
        self.assertEqual(
            snapshot_sqlite_master(db_path), schema_before,
            msg="报表前后 sqlite_master（表结构）必须逐字节一致",
        )
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(
                "SELECT number, customer, total FROM quotes ORDER BY number"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(
            rows,
            [(self.LEGACY_NUMBER, self.LEGACY_CUSTOMER, self.LEGACY_TOTAL)],
            msg="旧库报价记录报表后保持不变",
        )


class BigTotalReportTestCase(QuoteReportTestBase):
    """多张合法报价累计超出有符号 64 位整数上限时仍精确输出。"""

    def test_sum_beyond_int64_is_exact(self):
        customer = "大额客户"
        for index in range(2):
            self.save_sample(
                {
                    "number": f"Q-BIG-{index}",
                    "customer": customer,
                    "items": [
                        {"description": "上限单价", "quantity": 1,
                         "unit_price": INT64_MAX}
                    ],
                },
                index,
            )
        db_path = os.path.join(self.workdir, "quotes.sqlite")

        result = self.report(db_path)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "")

        records = json.loads(result.stdout)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["customer"], customer)
        self.assertEqual(records[0]["quote_count"], 2)
        self.assertEqual(
            records[0]["total"], 2 * INT64_MAX,
            msg="两张 INT64_MAX 报价的累计 18446744073709551614 必须精确输出",
        )
        self.assertIsInstance(records[0]["total"], int)
        # 原始输出文本中应出现完整十进制数字，不经浮点或科学计数法。
        self.assertIn(str(2 * INT64_MAX), result.stdout)


class ReportFailureTestCase(QuoteReportTestBase):
    """无法读取数据库的失败路径，以及只读/无副作用边界。"""

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
            after_bytes = f.read()
        self.assertEqual(
            after_bytes, original_bytes,
            msg="非数据库文件的内容在报表前后必须逐字节一致",
        )

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

        self.assertEqual(
            snapshot_sqlite_master(db_path), schema_before,
            msg="缺 quotes 表时报表不得改变已有表结构",
        )
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute("SELECT id, tag FROM unrelated ORDER BY id").fetchall()
        finally:
            conn.close()
        self.assertEqual(rows, [(1, "保留")], msg="数据库内既有记录必须保持不变")

    def test_quotes_table_missing_required_column_errors(self):
        """quotes 表缺少 number、customer、total 任一列时退出 1。"""
        for missing in ("number", "customer", "total"):
            with self.subTest(missing=missing):
                db_arg = f"missing-{missing}.sqlite"
                db_path = os.path.join(self.workdir, db_arg)
                kept_columns = [
                    column for column in ("number", "customer", "total")
                    if column != missing
                ]
                conn = sqlite3.connect(db_path)
                try:
                    conn.execute(
                        f"CREATE TABLE quotes({', '.join(kept_columns)})"
                    )
                    conn.commit()
                finally:
                    conn.close()

                schema_before = snapshot_sqlite_master(db_path)

                result = self.run_quote("report", "--db", db_arg)
                self.assert_report_error(result, db_arg)
                self.assertEqual(
                    snapshot_sqlite_master(db_path), schema_before,
                    msg=f"缺 {missing} 列时报表不得改变已有表结构",
                )


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


def snapshot_rows(db_path):
    """返回 (quotes 全字段排序行, items 全字段排序行)，供只读边界断言。"""
    conn = sqlite3.connect(db_path)
    try:
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
    return quotes, items


# 项目目录清洁性：测试开始前记录项目根内容，全部用例结束后不得新增任何文件
# （所有数据库、输入 JSON 都必须落在临时目录中）。
_PROJECT_ROOT_ENTRIES_AT_START = set(os.listdir(PROJECT_ROOT))


def tearDownModule():
    leftovers = set(os.listdir(PROJECT_ROOT)) - _PROJECT_ROOT_ENTRIES_AT_START
    assert not leftovers, f"测试在项目根目录留下了文件: {sorted(leftovers)}"


if __name__ == "__main__":
    unittest.main()
