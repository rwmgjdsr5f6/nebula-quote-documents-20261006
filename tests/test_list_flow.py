"""list 列表查询命令行回归测试：JSON 输出顺序与只读边界。

仅依赖 Python 标准库。所有用例在独立的临时目录中通过 README 记载的
公开命令（save / list）操作 quote.py 子进程，使用合成报价数据，
不读取项目内业务数据，也不在项目目录留下 JSON、SQLite 或 HTML 文件。

固定样例（保存顺序与列表顺序无关）：
    保存顺序 Q-LIST-B、Q-LIST-A、Q-LIST-a；
    列表顺序 Q-LIST-A、Q-LIST-B、Q-LIST-a（SQLite 默认 BINARY 排序）；
    合计依次为 0、2500、9223372036854775807 分，均为整数。

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

# ---- 固定合成样例：每张报价只有一条合法明细（数量, 分单位单价）----

NUMBER_B = "Q-LIST-B"
NUMBER_A = "Q-LIST-A"
NUMBER_A_LOWER = "Q-LIST-a"

# 客户原文：中文、首尾空格、换行、<、&、引号均为样例的一部分。
CUSTOMER_B = "  华东客户 <批发> & \"零售\"\n"
CUSTOMER_A = "华南客户（零元样例）"
CUSTOMER_A_LOWER = "  Overseas Customer <x> & 'y' "

# 其中一张报价带说明；list 输出仍只含 number、customer、total 三个字段。
NOTE_FOR_LOWER = "  列表查询不应返回本说明\n第二行 "

# 保存顺序刻意与列表顺序不同：B 先保存、A 次之、a 最后。
SAVE_SAMPLES = [
    {
        "number": NUMBER_B,
        "customer": CUSTOMER_B,
        "quantity": 2,
        "unit_price": 1250,
        "total": 2500,
        "note": None,
    },
    {
        "number": NUMBER_A,
        "customer": CUSTOMER_A,
        "quantity": 1,
        "unit_price": 0,
        "total": 0,
        "note": None,
    },
    {
        "number": NUMBER_A_LOWER,
        "customer": CUSTOMER_A_LOWER,
        "quantity": 1,
        "unit_price": 9223372036854775807,
        "total": 9223372036854775807,
        "note": NOTE_FOR_LOWER,
    },
]

# 预期列表输出：固定常量，不从被测函数或数据库内容生成。
EXPECTED_RECORDS = [
    {"number": NUMBER_A, "customer": CUSTOMER_A, "total": 0},
    {"number": NUMBER_B, "customer": CUSTOMER_B, "total": 2500},
    {"number": NUMBER_A_LOWER, "customer": CUSTOMER_A_LOWER,
     "total": 9223372036854775807},
]
EXPECTED_KEYS = {"number", "customer", "total"}

# 完整现行表结构（含 note 列），用于“建表但无报价”样例。
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

# 旧格式表结构（quotes 无 note 列）。
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

LEGACY_NUMBER = "Q-LEGACY-LIST"
LEGACY_CUSTOMER = " 旧客户 <甲>&乙\"\n"
LEGACY_TOTAL = 2500

# 不是 SQLite 的固定字节内容。
NON_DB_BYTES = b"this is definitely not a sqlite database\n<tag>&\"quote\"\n"


def make_payload(sample):
    payload = {
        "number": sample["number"],
        "customer": sample["customer"],
        "items": [
            {
                "description": "咨询",
                "quantity": sample["quantity"],
                "unit_price": sample["unit_price"],
            }
        ],
    }
    if sample["note"] is not None:
        payload["note"] = sample["note"]
    return payload


class ListFlowTestCase(unittest.TestCase):
    """正常路径：固定样例保存后按编号排序列出。"""

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
            encoding="utf-8",
            cwd=self.workdir,
        )

    def save_sample(self, sample, index):
        input_path = os.path.join(self.workdir, f"input-{index}.json")
        with open(input_path, "w", encoding="utf-8") as f:
            json.dump(make_payload(sample), f, ensure_ascii=False)
        result = self.run_quote(
            "save", "--db", self.db_path, "--input", input_path
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stdout, f"{sample['number']}\n")
        self.assertEqual(result.stderr, "")

    def read_note(self, number):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT note FROM quotes WHERE number = ?", (number,)
            ).fetchone()[0]
        finally:
            conn.close()

    def test_list_returns_fixed_ordered_records(self):
        self.db_path = os.path.join(self.workdir, "quotes.sqlite")

        # 按固定保存顺序先后保存 B、A、a。
        for index, sample in enumerate(SAVE_SAMPLES):
            self.save_sample(sample, index)

        # 带说明的样例确实落库了说明原文（前置条件自检）。
        self.assertEqual(
            self.read_note(NUMBER_A_LOWER), NOTE_FOR_LOWER,
            msg="前置条件：Q-LIST-a 应带说明保存",
        )
        self.assertIsNone(self.read_note(NUMBER_A))
        self.assertIsNone(self.read_note(NUMBER_B))

        listing_before = sorted(os.listdir(self.workdir))
        result = self.run_quote("list", "--db", self.db_path)
        listing_after = sorted(os.listdir(self.workdir))

        # 退出 0、标准错误为空；list 是只读命令，不产生任何新文件。
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "", msg="查询成功时标准错误必须为空")
        self.assertEqual(
            listing_after, listing_before,
            msg="list 不得在工作目录创建任何文件（含 wal/shm 等）",
        )

        # 标准输出是以换行结束的单行 JSON。
        self.assertTrue(
            result.stdout.endswith("\n"),
            msg=f"输出必须以换行结束，实际为 {result.stdout!r}",
        )
        self.assertEqual(
            result.stdout.count("\n"), 1,
            msg="输出必须是单行 JSON，只能在末尾出现一个换行",
        )

        records = json.loads(result.stdout)

        # 顺序固定为 Q-LIST-A、Q-LIST-B、Q-LIST-a，与保存先后无关。
        self.assertEqual(
            [record["number"] for record in records],
            [NUMBER_A, NUMBER_B, NUMBER_A_LOWER],
            msg="列表应按编号 BINARY 排序：Q-LIST-A、Q-LIST-B、Q-LIST-a",
        )

        # 与固定预期整条比较：客户原文逐字符一致，合计为固定整数。
        self.assertEqual(records, EXPECTED_RECORDS)

        for record, expected in zip(records, EXPECTED_RECORDS):
            number = expected["number"]
            with self.subTest(样例=number):
                self.assertEqual(
                    set(record.keys()), EXPECTED_KEYS,
                    msg="每条记录只能含 number、customer、total 三个字段",
                )
                self.assertNotIn(
                    "note", record,
                    msg="即使报价带说明，列表也不得返回 note 字段",
                )
                self.assertIsInstance(record["total"], int)
                self.assertNotIsInstance(record["total"], bool)
                self.assertEqual(record["total"], expected["total"])
                self.assertEqual(record["number"], expected["number"])
                # 客户原文：中文、首尾空格、换行、<、&、引号逐字符一致。
                self.assertEqual(record["customer"], expected["customer"])

        # 三个合计值逐一固定（0、2500、INT64 最大值）。
        self.assertEqual(
            [record["total"] for record in records],
            [0, 2500, 9223372036854775807],
        )


class EmptyAndLegacyListTestCase(unittest.TestCase):
    """空库返回 []；旧格式库（无 note 列）可列出且查询不补列。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="quote-list-empty-")
        self.addCleanup(self._tmp.cleanup)
        self.workdir = self._tmp.name

    def run_quote(self, *cli_args):
        return subprocess.run(
            [sys.executable, QUOTE_PY, *cli_args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=self.workdir,
        )

    def test_full_schema_without_quotes_returns_empty_array(self):
        db_path = os.path.join(self.workdir, "empty.sqlite")
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(FULL_SCHEMA)
            conn.commit()
        finally:
            conn.close()

        listing_before = sorted(os.listdir(self.workdir))
        result = self.run_quote("list", "--db", db_path)
        listing_after = sorted(os.listdir(self.workdir))

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "", msg="空库查询标准错误必须为空")
        self.assertEqual(
            result.stdout, "[]\n",
            msg="完整建表但没有报价时应输出 [] 加换行",
        )
        self.assertEqual(
            listing_after, listing_before,
            msg="list 不得创建任何新文件",
        )

    def test_legacy_database_without_note_column_listed_and_not_altered(self):
        db_path = os.path.join(self.workdir, "legacy.sqlite")
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(LEGACY_SCHEMA)
            conn.execute(
                "INSERT INTO quotes(number, customer, total) VALUES (?, ?, ?)",
                (LEGACY_NUMBER, LEGACY_CUSTOMER, LEGACY_TOTAL),
            )
            conn.execute(
                "INSERT INTO items(quote_number, position, description, "
                "quantity, unit_price, line_amount) VALUES (?, ?, ?, ?, ?, ?)",
                (LEGACY_NUMBER, 0, "咨询", 2, 1250, 2500),
            )
            conn.commit()
        finally:
            conn.close()

        def snapshot():
            conn = sqlite3.connect(db_path)
            try:
                columns = [
                    row[1]
                    for row in conn.execute("PRAGMA table_info(quotes)")
                ]
                schema = conn.execute(
                    "SELECT type, name, sql FROM sqlite_master "
                    "ORDER BY type, name"
                ).fetchall()
                quotes = conn.execute(
                    "SELECT number, customer, total FROM quotes"
                ).fetchall()
                items = conn.execute(
                    "SELECT quote_number, position, description, quantity, "
                    "unit_price, line_amount FROM items"
                ).fetchall()
            finally:
                conn.close()
            return columns, schema, quotes, items

        columns_before, schema_before, quotes_before, items_before = snapshot()
        self.assertEqual(
            columns_before, ["number", "customer", "total"],
            msg="前置条件：旧库 quotes 表没有 note 列",
        )

        result = self.run_quote("list", "--db", db_path)

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "", msg="旧库查询标准错误必须为空")
        self.assertTrue(result.stdout.endswith("\n"))
        self.assertEqual(result.stdout.count("\n"), 1)

        records = json.loads(result.stdout)
        self.assertEqual(
            records,
            [
                {
                    "number": LEGACY_NUMBER,
                    "customer": LEGACY_CUSTOMER,
                    "total": LEGACY_TOTAL,
                }
            ],
            msg="旧库原有报价应正常列出，客户原文逐字符保留",
        )
        self.assertEqual(set(records[0].keys()), EXPECTED_KEYS)
        self.assertNotIn("note", records[0])

        columns_after, schema_after, quotes_after, items_after = snapshot()
        self.assertEqual(
            columns_after, ["number", "customer", "total"],
            msg="只读查询不得为旧库补 note 列",
        )
        self.assertEqual(
            schema_after, schema_before,
            msg="查询前后表结构必须逐字节保持不变",
        )
        self.assertEqual(
            quotes_after, quotes_before,
            msg="查询前后 quotes 记录必须保持不变",
        )
        self.assertEqual(
            items_after, items_before,
            msg="查询前后 items 记录必须保持不变",
        )


class ListFailureTestCase(unittest.TestCase):
    """不可读数据库的失败路径：退出 1、空 stdout、固定前缀报错、无堆栈。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="quote-list-fail-")
        self.addCleanup(self._tmp.cleanup)
        self.workdir = self._tmp.name

    def run_quote(self, *cli_args):
        return subprocess.run(
            [sys.executable, QUOTE_PY, *cli_args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=self.workdir,
        )

    def assert_read_error(self, result, path, reason):
        self.assertEqual(
            result.returncode, 1,
            msg=f"样例 {path}: 不可读数据库应退出码 1，实际 {result.returncode}",
        )
        self.assertEqual(
            result.stdout, "",
            msg=f"样例 {path}: 失败时标准输出必须为空",
        )
        prefix = f"错误: 无法读取数据库 {path}"
        self.assertTrue(
            result.stderr.startswith(prefix),
            msg=f"样例 {path}: 标准错误应以 {prefix!r} 开头，"
            f"实际 {result.stderr!r}",
        )
        self.assertTrue(
            result.stderr.startswith("错误: 无法读取数据库 "),
            msg="标准错误应以“错误: 无法读取数据库 ”开头",
        )
        self.assertIn(
            reason, result.stderr,
            msg=f"样例 {path}: 标准错误应包含原因 {reason!r}，"
            f"实际 {result.stderr!r}",
        )
        self.assertEqual(
            result.stderr.count("\n"), 1,
            msg=f"样例 {path}: 错误信息只能占一行，实际 {result.stderr!r}",
        )
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("sqlite3.", result.stderr)

    def test_unreadable_databases_fail_cleanly(self):
        missing_path = os.path.join(self.workdir, "does-not-exist.sqlite")
        non_db_path = os.path.join(self.workdir, "not-database.txt")
        no_table_path = os.path.join(self.workdir, "no-quotes-table.sqlite")

        self.assertFalse(os.path.exists(missing_path))
        with open(non_db_path, "wb") as f:
            f.write(NON_DB_BYTES)

        # 合法 SQLite 但缺少 quotes 表。
        conn = sqlite3.connect(no_table_path)
        try:
            conn.execute("CREATE TABLE other(x TEXT)")
            conn.execute("INSERT INTO other(x) VALUES (?) , (?)",
                         ("保留记录一", "保留记录二"))
            conn.commit()
        finally:
            conn.close()

        def db_snapshot(path):
            conn = sqlite3.connect(path)
            try:
                return conn.execute(
                    "SELECT type, name, sql FROM sqlite_master "
                    "ORDER BY type, name"
                ).fetchall(), conn.execute(
                    "SELECT x FROM other ORDER BY x"
                ).fetchall()
            finally:
                conn.close()

        schema_before, rows_before = db_snapshot(no_table_path)

        cases = [
            ("路径不存在", missing_path, "unable to open database file"),
            ("文件不是SQLite", non_db_path, "file is not a database"),
            ("缺少quotes表", no_table_path, "no such table: quotes"),
        ]

        for label, path, reason in cases:
            with self.subTest(样例=label):
                listing_before = sorted(os.listdir(self.workdir))
                result = self.run_quote("list", "--db", path)
                listing_after = sorted(os.listdir(self.workdir))

                self.assert_read_error(result, path, reason)
                self.assertEqual(
                    listing_after, listing_before,
                    msg="失败查询不得在工作目录创建或删除任何文件",
                )

        # 不存在的数据库不能被创建。
        self.assertFalse(
            os.path.exists(missing_path),
            msg="查询不存在的路径后仍不得创建数据库文件",
        )
        # 非数据库文件内容逐字节保持不变。
        with open(non_db_path, "rb") as f:
            self.assertEqual(
                f.read(), NON_DB_BYTES,
                msg="非数据库文件的内容在查询前后必须逐字节一致",
            )
        # 已有数据库的表结构与记录保持不变。
        schema_after, rows_after = db_snapshot(no_table_path)
        self.assertEqual(
            schema_after, schema_before,
            msg="失败查询不得改变已有数据库的表结构",
        )
        self.assertEqual(
            rows_after, rows_before,
            msg="失败查询不得改变已有数据库的记录",
        )


if __name__ == "__main__":
    unittest.main()
