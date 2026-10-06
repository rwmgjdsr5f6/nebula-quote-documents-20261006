"""列表查询（list）命令行回归测试：保存 -> list JSON 输出 -> 只读边界。

仅依赖 Python 标准库。所有用例在独立的临时目录中通过 README 记载的
公开命令（save / list）操作 quote.py 子进程，使用合成单据，
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

# ---- 固定样例：保存顺序为 B、A、a，列表按编号 BINARY 排序后应为 A、B、a ----

NUMBER_A = "Q-LIST-A"
NUMBER_B = "Q-LIST-B"
NUMBER_LOWER = "Q-LIST-a"

# 客户样例覆盖：中文、首尾空格、换行、<、&、单双引号。
CUSTOMER_A = "中文客户Ａ"
CUSTOMER_B = " 客户<甲> & 乙 \"双引号\" '单引号'\n 尾 "
CUSTOMER_LOWER = "Ｃｌｉｅｎｔ-a"

# Q-LIST-A 带说明：list 输出仍只能包含 number、customer、total 三个字段。
NOTE_A = " 列表查询不应返回本说明\n"

# 三张报价各自只有一条合法明细。
SAVE_SAMPLES = [
    {
        "number": NUMBER_B,
        "customer": CUSTOMER_B,
        "items": [{"description": "咨询", "quantity": 2, "unit_price": 1250}],
    },
    {
        "number": NUMBER_A,
        "customer": CUSTOMER_A,
        "note": NOTE_A,
        "items": [{"description": "资料", "quantity": 1, "unit_price": 0}],
    },
    {
        "number": NUMBER_LOWER,
        "customer": CUSTOMER_LOWER,
        "items": [
            {"description": "上限单价", "quantity": 1, "unit_price": INT64_MAX}
        ],
    },
]

# 期望的列表输出：固定常量，按编号排序，合计依次为 0、2500、INT64_MAX。
EXPECTED_RECORDS = [
    {"number": NUMBER_A, "customer": CUSTOMER_A, "total": 0},
    {"number": NUMBER_B, "customer": CUSTOMER_B, "total": 2500},
    {"number": NUMBER_LOWER, "customer": CUSTOMER_LOWER, "total": INT64_MAX},
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


class QuoteListTestBase(unittest.TestCase):
    """每个用例使用独立临时目录，只通过公开命令行入口驱动 quote.py。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="quote-list-test-")
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

    def list(self, db_path):
        return self.run_quote("list", "--db", db_path)

    def assert_list_error(self, result, db_arg):
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


class ListFlowTestCase(QuoteListTestBase):
    """正常列表输出：排序、字段白名单、整数合计与特殊字符原样往返。"""

    def test_list_returns_sorted_records_with_fixed_expectations(self):
        # 按固定样例先后保存 B、A、a（保存顺序故意不同于期望输出顺序）。
        db_path = None
        for index, payload in enumerate(SAVE_SAMPLES):
            db_path = self.save_sample(payload, index)

        # 前置自检：说明确实已保存，证明 list 是主动不返回 note，而非数据缺失。
        conn = sqlite3.connect(db_path)
        try:
            note_row = conn.execute(
                "SELECT note FROM quotes WHERE number = ?", (NUMBER_A,)
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(
            note_row, (NOTE_A,),
            msg="前置条件：Q-LIST-A 在库中带有说明",
        )

        result = self.list(db_path)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "", msg="查询成功时标准错误必须为空")

        stdout = result.stdout
        self.assertTrue(stdout.endswith("\n"), msg="标准输出必须以换行结束")
        self.assertEqual(stdout.count("\n"), 1, msg="标准输出必须是单行 JSON")
        self.assertNotIn("\r", stdout, msg="不得使用 CRLF 换行")

        records = json.loads(stdout)
        # 期望数据来自本模块固定常量，不通过被测函数生成。
        self.assertEqual(
            records, EXPECTED_RECORDS,
            msg="列表顺序固定为 Q-LIST-A、Q-LIST-B、Q-LIST-a，内容与固定样例一致",
        )
        self.assertEqual(
            [r["number"] for r in records],
            [NUMBER_A, NUMBER_B, NUMBER_LOWER],
            msg="编号应按 BINARY 排序（大写 A/B 在小写 a 之前），与保存先后无关",
        )

        for record, expected in zip(records, EXPECTED_RECORDS):
            self.assertEqual(
                set(record.keys()), {"number", "customer", "total"},
                msg=f"{expected['number']} 只能含 number、customer、total 三个字段",
            )
            self.assertIsInstance(
                record["total"], int,
                msg=f"{expected['number']} 的合计必须是 JSON 整数",
            )
            self.assertNotIsInstance(record["total"], bool)
            self.assertEqual(
                record["customer"], expected["customer"],
                msg=(
                    f"{expected['number']} 客户名应与输入逐字符一致"
                    "（中文、首尾空格、换行、<、&、引号均不得删改）"
                ),
            )
            self.assertEqual(record["total"], expected["total"])

        self.assertEqual(
            [r["total"] for r in records], [0, 2500, INT64_MAX],
            msg="合计依次为 0、2500、9223372036854775807",
        )

        # 中文在标准输出中以原文出现（ensure_ascii=False），且查询后不产生副作用。
        self.assertIn(CUSTOMER_A, stdout)
        _, rows_after = snapshot_database(db_path)
        self.assertEqual(
            rows_after,
            sorted(
                [
                    (NUMBER_A, CUSTOMER_A, 0, NOTE_A),
                    (NUMBER_B, CUSTOMER_B, 2500, None),
                    (NUMBER_LOWER, CUSTOMER_LOWER, INT64_MAX, None),
                ]
            ),
            msg="查询后报价记录必须保持不变",
        )
        self.assertIn(
            "note",
            {col[1] for col in query_table_info(db_path)},
            msg="查询不得破坏既有表结构",
        )


class EmptyDatabaseListTestCase(QuoteListTestBase):
    """完整建表但没有报价：返回 [] 加换行。"""

    def test_empty_database_lists_empty_array(self):
        db_path = os.path.join(self.workdir, "empty.sqlite")
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(FULL_SCHEMA)
            conn.commit()
        finally:
            conn.close()

        result = self.list(db_path)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "", msg="查询成功时标准错误必须为空")
        self.assertEqual(
            result.stdout, "[]\n",
            msg="无报价时标准输出应为空数组加单个换行",
        )
        self.assertEqual(json.loads(result.stdout), [])


class LegacyDatabaseListTestCase(QuoteListTestBase):
    """旧格式数据库（quotes 缺 note 列）：可列出且查询不补列。"""

    LEGACY_NUMBER = "Q-LIST-LEGACY"
    LEGACY_CUSTOMER = " 旧库客户 <x> & 'y'\n"
    LEGACY_TOTAL = 2500

    def build_legacy_database(self, db_path):
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(LEGACY_SCHEMA)
            conn.execute(
                "INSERT INTO quotes(number, customer, total) VALUES (?, ?, ?)",
                (self.LEGACY_NUMBER, self.LEGACY_CUSTOMER, self.LEGACY_TOTAL),
            )
            conn.execute(
                "INSERT INTO items(quote_number, position, description, "
                "quantity, unit_price, line_amount) VALUES (?, ?, ?, ?, ?, ?)",
                (self.LEGACY_NUMBER, 0, "咨询", 2, 1250, 2500),
            )
            conn.commit()
        finally:
            conn.close()

    def test_legacy_database_listed_without_adding_note_column(self):
        db_path = os.path.join(self.workdir, "legacy.sqlite")
        self.build_legacy_database(db_path)

        schema_before = snapshot_sqlite_master(db_path)
        columns_before = {col[1] for col in query_table_info(db_path)}
        self.assertNotIn("note", columns_before, msg="前置条件：旧库没有 note 列")

        result = self.list(db_path)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertTrue(result.stdout.endswith("\n"))
        self.assertEqual(result.stdout.count("\n"), 1)

        records = json.loads(result.stdout)
        self.assertEqual(
            records,
            [
                {
                    "number": self.LEGACY_NUMBER,
                    "customer": self.LEGACY_CUSTOMER,
                    "total": self.LEGACY_TOTAL,
                }
            ],
            msg="旧库原有报价应正常列出，客户原文与整数合计不变",
        )
        self.assertEqual(
            set(records[0].keys()), {"number", "customer", "total"},
            msg="旧库列表同样只能返回三个字段",
        )

        # 只读边界：查询后不补 note 列，表结构与记录均保持原样。
        columns_after = {col[1] for col in query_table_info(db_path)}
        self.assertEqual(
            columns_after, columns_before,
            msg="list 查询不得为旧库补 note 列",
        )
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
        self.assertEqual(
            rows,
            [(self.LEGACY_NUMBER, self.LEGACY_CUSTOMER, self.LEGACY_TOTAL)],
            msg="旧库报价记录查询后保持不变",
        )


class ListFailureTestCase(QuoteListTestBase):
    """无法读取数据库的三类失败路径，以及只读/无副作用边界。"""

    def test_missing_database_file_errors_without_creating_file(self):
        db_arg = "does-not-exist.sqlite"
        db_path = os.path.join(self.workdir, db_arg)
        self.assertFalse(os.path.exists(db_path))

        result = self.run_quote("list", "--db", db_arg)
        self.assert_list_error(result, db_arg)
        self.assertFalse(
            os.path.exists(db_path),
            msg="查询不存在的数据库绝不能创建数据库文件",
        )

    def test_non_sqlite_file_errors_and_preserves_bytes(self):
        db_arg = "not-a-database.bin"
        db_path = os.path.join(self.workdir, db_arg)
        original_bytes = b"this is definitely not a sqlite database\n\x00\xff"
        with open(db_path, "wb") as f:
            f.write(original_bytes)

        result = self.run_quote("list", "--db", db_arg)
        self.assert_list_error(result, db_arg)

        with open(db_path, "rb") as f:
            after_bytes = f.read()
        self.assertEqual(
            after_bytes, original_bytes,
            msg="非数据库文件的内容在查询前后必须逐字节一致",
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

        result = self.run_quote("list", "--db", db_arg)
        self.assert_list_error(result, db_arg)

        self.assertEqual(
            snapshot_sqlite_master(db_path), schema_before,
            msg="缺 quotes 表时查询不得改变已有表结构",
        )
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute("SELECT id, tag FROM unrelated ORDER BY id").fetchall()
        finally:
            conn.close()
        self.assertEqual(rows, [(1, "保留")], msg="数据库内既有记录必须保持不变")

    def test_successful_list_is_read_only(self):
        """正常查询前后：已有数据库的记录与表结构保持一致。"""
        db_path = self.save_sample(SAVE_SAMPLES[1], 1)  # Q-LIST-A（带 note）

        schema_before = snapshot_sqlite_master(db_path)
        conn = sqlite3.connect(db_path)
        try:
            quotes_before = conn.execute(
                "SELECT number, customer, total, note FROM quotes ORDER BY number"
            ).fetchall()
            items_before = conn.execute(
                "SELECT quote_number, position, description, quantity, "
                "unit_price, line_amount FROM items "
                "ORDER BY quote_number, position"
            ).fetchall()
        finally:
            conn.close()

        result = self.list(db_path)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

        self.assertEqual(
            snapshot_sqlite_master(db_path), schema_before,
            msg="list 查询不得改变表结构",
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
        self.assertEqual(quotes_after, quotes_before, msg="报价记录查询后保持不变")
        self.assertEqual(items_after, items_before, msg="明细记录查询后保持不变")


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


def snapshot_database(db_path):
    """返回 (sqlite_master 快照, quotes 全字段排序行)，供只读边界断言。"""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT number, customer, total, note FROM quotes ORDER BY number"
        ).fetchall()
    finally:
        conn.close()
    return snapshot_sqlite_master(db_path), rows


# 项目目录清洁性：测试开始前记录项目根内容，全部用例结束后不得新增任何文件
# （所有数据库、输入 JSON 都必须落在临时目录中）。
_PROJECT_ROOT_ENTRIES_AT_START = set(os.listdir(PROJECT_ROOT))


def tearDownModule():
    leftovers = set(os.listdir(PROJECT_ROOT)) - _PROJECT_ROOT_ENTRIES_AT_START
    assert not leftovers, f"测试在项目根目录留下了文件: {sorted(leftovers)}"


if __name__ == "__main__":
    unittest.main()
