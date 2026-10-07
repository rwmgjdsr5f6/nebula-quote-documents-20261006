"""list --customer 客户精确筛选专项回归测试。

仅依赖 Python 标准库。所有用例在独立临时目录中通过 README 记载的公开命令
（save / list）以子进程驱动 quote.py，使用固定合成单据，不读取项目内业务
数据，也不在项目目录留下 JSON、SQLite 或 HTML 文件。

覆盖内容：
  - 按 Q-B、Q-A、Q-C、Q-D 顺序保存（客户依次为 Demo、Demo、demo、" Demo "），
    --customer 按编号顺序做逐字符精确匹配，大小写与首尾空格参与匹配；
  - 成功输出契约：退出 0、标准错误为空、单行 JSON 数组（结尾换行），
    每项仅含 number/customer/total，客户原文保留，total 为分单位整数；
  - 中文、引号、换行、尖括号、与号及 %、_ 的客户名与相似客户名对照，
    不发生通配匹配、子串匹配或 HTML 转义；
  - 空字符串及纯空白筛选值在访问数据库前被拒绝（退出 1、固定错误信息），
    不创建数据库文件；
  - 合法筛选访问不存在的数据库：退出 1、标准输出为空、标准错误说明路径与原因；
  - 缺少 note 列的旧库按相同规则筛选成功，且不补列、不改数据；
  - 查询（含命中、未命中）与拒绝后，已有数据库的表结构、报价与明细记录不变。

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
QUOTE_PY = os.path.join(PROJECT_ROOT, "quote.py")

DB_NAME = "demo.sqlite"
DESCRIPTION = "演示明细"

# ---- 固定样例：保存顺序刻意为 B、A、C、D，与列表的编号顺序不同 ----
NUMBER_A = "Q-A"
NUMBER_B = "Q-B"
NUMBER_C = "Q-C"
NUMBER_D = "Q-D"

CUSTOMER_PLAIN = "Demo"
CUSTOMER_LOWER = "demo"
CUSTOMER_PADDED = " Demo "

# (编号, 客户, 分单位单价)：保存顺序 B -> A -> C -> D。
SAVE_PLAN = [
    (NUMBER_B, CUSTOMER_PLAIN, 2500),
    (NUMBER_A, CUSTOMER_PLAIN, 0),
    (NUMBER_C, CUSTOMER_LOWER, 300),
    (NUMBER_D, CUSTOMER_PADDED, 500),
]

# 明确的预期结果：筛选按编号（BINARY）排序，与保存先后无关。
EXPECTED_DEMO = [
    {"number": NUMBER_A, "customer": CUSTOMER_PLAIN, "total": 0},
    {"number": NUMBER_B, "customer": CUSTOMER_PLAIN, "total": 2500},
]
EXPECTED_LOWER = [
    {"number": NUMBER_C, "customer": CUSTOMER_LOWER, "total": 300},
]
EXPECTED_PADDED = [
    {"number": NUMBER_D, "customer": CUSTOMER_PADDED, "total": 500},
]
EXPECTED_ALL = [
    {"number": NUMBER_A, "customer": CUSTOMER_PLAIN, "total": 0},
    {"number": NUMBER_B, "customer": CUSTOMER_PLAIN, "total": 2500},
    {"number": NUMBER_C, "customer": CUSTOMER_LOWER, "total": 300},
    {"number": NUMBER_D, "customer": CUSTOMER_PADDED, "total": 500},
]

BLANK_FILTER_MESSAGE = "错误: 客户筛选值不能为空白\n"
READ_ERROR_PREFIX = "错误: 无法读取数据库 "

# ---- 特殊字符客户样例：中文、双引号、换行、尖括号、与号、%、_ 全覆盖 ----
CUSTOMER_SPECIAL = '客户"甲"\n<乙> & 丙 100% a_b'

# 与 CUSTOMER_SPECIAL 相似但逐字符不同的对照客户（外加一个通配样式客户）。
SPECIAL_LOOKALIKES = [
    ("Q-W2", "前" + CUSTOMER_SPECIAL, 101),     # 只多了前缀
    ("Q-W3", CUSTOMER_SPECIAL + "后", 102),     # 只多了后缀
    ("Q-W4", CUSTOMER_SPECIAL.replace("%", "％"), 103),  # 半角 % 换全角 ％
    ("Q-W5", "%a_b%", 104),                     # 典型 LIKE 通配样式，按普通文本
]

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


class CustomerFilterTestBase(unittest.TestCase):
    """每个用例使用独立临时目录，只通过公开命令行入口驱动 quote.py。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="quote-customer-test-")
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

    def write_input(self, number, customer, unit_price):
        """写一张只有一条“演示明细”（数量 1）的合成单据。"""
        payload = {
            "number": number,
            "customer": customer,
            "items": [
                {
                    "description": DESCRIPTION,
                    "quantity": 1,
                    "unit_price": unit_price,
                }
            ],
        }
        path = os.path.join(self.workdir, f"input-{number}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        return path

    def save_quote(self, number, customer, unit_price):
        input_path = self.write_input(number, customer, unit_price)
        result = self.run_quote("save", "--db", DB_NAME, "--input", input_path)
        self.assertEqual(
            result.returncode, 0,
            msg=f"样例 {number} 保存应成功: {result.stderr}",
        )
        self.assertEqual(result.stderr, "", msg="保存成功时标准错误必须为空")
        self.assertEqual(
            result.stdout, f"{number}\n",
            msg="保存成功时标准输出仅为编号加换行",
        )

    def build_demo_database(self):
        """按 B、A、C、D 的固定顺序建好四张报价所在数据库。"""
        for number, customer, unit_price in SAVE_PLAN:
            self.save_quote(number, customer, unit_price)
        return os.path.join(self.workdir, DB_NAME)

    def list_with_customer(self, customer):
        return self.run_quote(
            "list", "--db", DB_NAME, "--customer", customer
        )

    def list_all(self):
        return self.run_quote("list", "--db", DB_NAME)

    def assert_success_json(self, result, expected_records):
        """成功路径完整契约：退出 0、stderr 空、单行 JSON 数组且内容逐字相等。"""
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "", msg="查询成功时标准错误必须为空")
        stdout = result.stdout
        self.assertTrue(stdout.endswith("\n"), msg="标准输出必须以换行结束")
        self.assertEqual(stdout.count("\n"), 1, msg="标准输出必须是单行 JSON")
        self.assertNotIn("\r", stdout, msg="不得使用 CRLF 换行")
        expected_text = json.dumps(expected_records, ensure_ascii=False) + "\n"
        self.assertEqual(
            stdout, expected_text,
            msg="标准输出必须与固定样例预期逐字符一致",
        )
        records = json.loads(stdout)
        for record, expected in zip(records, expected_records):
            self.assertEqual(
                set(record.keys()), {"number", "customer", "total"},
                msg=f"{expected['number']} 只能含 number、customer、total 三个字段",
            )
            self.assertIsInstance(record["total"], int, msg="total 必须是 JSON 整数")
            self.assertNotIsInstance(record["total"], bool)
            self.assertEqual(
                record["customer"], expected["customer"],
                msg="客户文本必须原样保留（含空格、大小写与特殊符号）",
            )
        return records


class CustomerFilterExactTestCase(CustomerFilterTestBase):
    """四张固定报价上的精确筛选、编号排序与省略筛选参数的行为。"""

    def setUp(self):
        super().setUp()
        self.db_path = self.build_demo_database()

    def test_filter_demo_returns_q_a_then_q_b(self):
        # Q-B 先保存、Q-A 后保存：输出仍按编号为 Q-A、Q-B，total 为 0、2500。
        result = self.list_with_customer(CUSTOMER_PLAIN)
        records = self.assert_success_json(result, EXPECTED_DEMO)
        self.assertEqual(
            [r["number"] for r in records], [NUMBER_A, NUMBER_B],
            msg="同客户报价必须按编号排序，与保存先后无关",
        )
        self.assertEqual(
            [r["total"] for r in records], [0, 2500],
            msg="Q-A 单价 0、Q-B 单价 2500，合计依次为 0、2500（分）",
        )

    def test_filter_lowercase_demo_returns_only_q_c(self):
        result = self.list_with_customer(CUSTOMER_LOWER)
        self.assert_success_json(result, EXPECTED_LOWER)

    def test_filter_padded_demo_returns_only_q_d(self):
        # 筛选值首尾各一个空格，作为 argv 原样传入并原样参与匹配。
        result = self.list_with_customer(CUSTOMER_PADDED)
        records = self.assert_success_json(result, EXPECTED_PADDED)
        self.assertEqual(records[0]["customer"], " Demo ")
        self.assertTrue(records[0]["customer"].startswith(" "))
        self.assertTrue(records[0]["customer"].endswith(" "))

    def test_filter_uppercase_demo_returns_empty_array(self):
        result = self.list_with_customer("DEMO")
        self.assert_success_json(result, [])
        self.assertEqual(result.stdout, "[]\n")

    def test_without_filter_returns_all_four_in_number_order(self):
        result = self.list_all()
        records = self.assert_success_json(result, EXPECTED_ALL)
        self.assertEqual(
            [r["number"] for r in records],
            [NUMBER_A, NUMBER_B, NUMBER_C, NUMBER_D],
            msg="省略筛选参数时按编号返回全部四条",
        )


class CustomerSpecialCharsTestCase(CustomerFilterTestBase):
    """特殊字符客户名：只有逐字符相同才命中，无通配/子串匹配，无 HTML 转义。"""

    def setUp(self):
        super().setUp()
        self.save_quote("Q-W1", CUSTOMER_SPECIAL, 100)
        for number, customer, unit_price in SPECIAL_LOOKALIKES:
            self.save_quote(number, customer, unit_price)

    def test_only_character_identical_customer_matches(self):
        result = self.list_with_customer(CUSTOMER_SPECIAL)
        records = self.assert_success_json(
            result,
            [{"number": "Q-W1", "customer": CUSTOMER_SPECIAL, "total": 100}],
        )
        self.assertEqual(
            [r["number"] for r in records], ["Q-W1"],
            msg="加前后缀、全角百分号等相似客户都不得命中",
        )

    def test_percent_is_not_a_wildcard(self):
        # 若按 LIKE 处理，"%" 会命中全部、"%a_b%" 会命中含 a_b 的客户；
        # 精确比较下二者都只能逐字匹配。
        self.assert_success_json(self.list_with_customer("%"), [])
        self.assert_success_json(
            self.list_with_customer("%a_b%"),
            [{"number": "Q-W5", "customer": "%a_b%", "total": 104}],
        )

    def test_underscore_is_not_a_single_char_wildcard(self):
        self.assert_success_json(self.list_with_customer("_"), [])

    def test_substring_does_not_match(self):
        self.assert_success_json(self.list_with_customer("客户"), [])
        self.assert_success_json(self.list_with_customer("a_b"), [])

    def test_output_keeps_special_chars_without_html_escaping(self):
        result = self.list_with_customer(CUSTOMER_SPECIAL)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        stdout = result.stdout
        # json.dumps 只做 JSON 必需转义：换行变为 \n 两字符，整行输出因此仍只有一个换行。
        self.assertEqual(stdout.count("\n"), 1)
        self.assertTrue(stdout.endswith("\n"))
        # 尖括号、与号、百分号、下划线与中文原样出现，不做 HTML 或 \\u 转义。
        self.assertIn("<乙>", stdout)
        self.assertIn("&", stdout)
        self.assertIn("100% a_b", stdout)
        self.assertNotIn("&lt;", stdout)
        self.assertNotIn("&gt;", stdout)
        self.assertNotIn("&amp;", stdout)
        self.assertNotIn("\\u003c", stdout)
        self.assertNotIn("\\u003e", stdout)
        self.assertNotIn("\\u0026", stdout)
        # 双引号只按 JSON 规则转义为 \"，解析回来与库中原文逐字符一致。
        records = json.loads(stdout)
        self.assertEqual(records[0]["customer"], CUSTOMER_SPECIAL)


class CustomerBlankRejectionTestCase(CustomerFilterTestBase):
    """空字符串与纯空白筛选值：访问数据库前拒绝，固定错误信息。"""

    BLANK_VALUES = ["", " ", "\t", "\n", " \t\n "]

    def test_blank_values_rejected_against_existing_database(self):
        self.build_demo_database()
        for value in self.BLANK_VALUES:
            with self.subTest(value=value):
                result = self.list_with_customer(value)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "", msg="拒绝时标准输出必须为空")
                self.assertEqual(
                    result.stderr, BLANK_FILTER_MESSAGE,
                    msg="标准错误必须是固定文案并以换行结束",
                )

    def test_blank_rejection_precedes_database_access(self):
        missing_db = "never-opened.sqlite"
        missing_path = os.path.join(self.workdir, missing_db)
        for value in self.BLANK_VALUES:
            with self.subTest(value=value):
                self.assertFalse(os.path.exists(missing_path))
                result = self.run_quote(
                    "list", "--db", missing_db, "--customer", value
                )
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, BLANK_FILTER_MESSAGE)
                self.assertFalse(
                    os.path.exists(missing_path),
                    msg="空白筛选被拒绝前不得访问或创建数据库",
                )


class CustomerFilterMissingDatabaseTestCase(CustomerFilterTestBase):
    """合法筛选值访问不存在的数据库：失败信息含路径与原因，不创建文件。"""

    def test_legal_filter_on_missing_database_fails_without_creating_file(self):
        db_arg = "absent.sqlite"
        db_path = os.path.join(self.workdir, db_arg)
        self.assertFalse(os.path.exists(db_path))

        result = self.run_quote(
            "list", "--db", db_arg, "--customer", CUSTOMER_PLAIN
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "", msg="失败时标准输出必须为空")

        stderr = result.stderr
        self.assertTrue(
            stderr.startswith(READ_ERROR_PREFIX),
            msg=f"标准错误应以 {READ_ERROR_PREFIX!r} 开头，实际: {stderr!r}",
        )
        remainder = stderr[len(READ_ERROR_PREFIX):]
        self.assertTrue(
            remainder.startswith(f"{db_arg}: ") and remainder.endswith("\n"),
            msg=f"标准错误应为“路径: 原因”单行格式，实际: {stderr!r}",
        )
        reason = remainder[len(db_arg) + 2:-1]
        self.assertTrue(reason.strip(), msg="必须说明具体原因")
        self.assertNotIn("Traceback", stderr, msg="不得向用户输出异常堆栈")
        self.assertEqual(stderr.count("\n"), 1)
        self.assertFalse(
            os.path.exists(db_path),
            msg="数据库不存在时查询绝不能创建文件",
        )


class LegacyDatabaseCustomerFilterTestCase(CustomerFilterTestBase):
    """缺少 note 列的旧库：筛选规则相同，且不补列、不改任何记录。"""

    LEGACY_ROWS = [
        ("Q-L1", CUSTOMER_PLAIN, 0),
        ("Q-L2", CUSTOMER_LOWER, 2500),
        ("Q-L3", CUSTOMER_PADDED, 500),
    ]

    def build_legacy_database(self, db_path):
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(LEGACY_SCHEMA)
            for number, customer, total in self.LEGACY_ROWS:
                conn.execute(
                    "INSERT INTO quotes(number, customer, total) VALUES (?, ?, ?)",
                    (number, customer, total),
                )
                conn.execute(
                    "INSERT INTO items(quote_number, position, description, "
                    "quantity, unit_price, line_amount) VALUES (?, ?, ?, ?, ?, ?)",
                    (number, 0, DESCRIPTION, 1, total, total),
                )
            conn.commit()
        finally:
            conn.close()

    def test_legacy_database_filter_follows_same_rules(self):
        db_arg = "legacy.sqlite"
        db_path = os.path.join(self.workdir, db_arg)
        self.build_legacy_database(db_path)

        columns_before = {
            row[1]
            for row in sqlite3.connect(db_path).execute("PRAGMA table_info(quotes)")
        }
        self.assertNotIn("note", columns_before, msg="前置条件：旧库没有 note 列")
        schema_before = snapshot_sqlite_master(db_path)
        quotes_before = snapshot_quotes(db_path, legacy=True)
        items_before = snapshot_items(db_path)

        cases = [
            (CUSTOMER_PLAIN, [("Q-L1", CUSTOMER_PLAIN, 0)]),
            (CUSTOMER_LOWER, [("Q-L2", CUSTOMER_LOWER, 2500)]),
            (CUSTOMER_PADDED, [("Q-L3", CUSTOMER_PADDED, 500)]),
            ("DEMO", []),
        ]
        for customer, expected in cases:
            with self.subTest(customer=customer):
                result = self.run_quote(
                    "list", "--db", db_arg, "--customer", customer
                )
                expected_records = [
                    {"number": number, "customer": cust, "total": total}
                    for number, cust, total in expected
                ]
                self.assert_success_json(result, expected_records)

        # 旧库在筛选后仍不得被补列，表结构与全部记录逐字节保持。
        columns_after = {
            row[1]
            for row in sqlite3.connect(db_path).execute("PRAGMA table_info(quotes)")
        }
        self.assertEqual(columns_after, columns_before)
        self.assertNotIn("note", columns_after)
        self.assertEqual(snapshot_sqlite_master(db_path), schema_before)
        self.assertEqual(snapshot_quotes(db_path, legacy=True), quotes_before)
        self.assertEqual(snapshot_items(db_path), items_before)


class CustomerFilterReadOnlyTestCase(CustomerFilterTestBase):
    """查询（命中/未命中/全部）与空白拒绝后，已有数据库内容完全不变。"""

    def test_queries_and_rejection_leave_database_untouched(self):
        db_path = self.build_demo_database()

        schema_before = snapshot_sqlite_master(db_path)
        quotes_before = snapshot_quotes(db_path)
        items_before = snapshot_items(db_path)

        hit = self.list_with_customer(CUSTOMER_PLAIN)
        miss = self.list_with_customer("DEMO")
        all_rows = self.list_all()
        rejected = self.list_with_customer("   ")
        self.assertEqual(hit.returncode, 0)
        self.assertEqual(miss.returncode, 0)
        self.assertEqual(miss.stdout, "[]\n")
        self.assertEqual(all_rows.returncode, 0)
        self.assertEqual(rejected.returncode, 1)

        self.assertEqual(snapshot_sqlite_master(db_path), schema_before)
        self.assertEqual(snapshot_quotes(db_path), quotes_before)
        self.assertEqual(snapshot_items(db_path), items_before)

        # 每条报价的明细仍是一条“演示明细”，数量 1、单价即合计，未被改动。
        conn = sqlite3.connect(db_path)
        try:
            item_rows = conn.execute(
                "SELECT quote_number, position, description, quantity, "
                "unit_price, line_amount FROM items "
                "ORDER BY quote_number, position"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(
            item_rows,
            [
                (NUMBER_A, 0, DESCRIPTION, 1, 0, 0),
                (NUMBER_B, 0, DESCRIPTION, 1, 2500, 2500),
                (NUMBER_C, 0, DESCRIPTION, 1, 300, 300),
                (NUMBER_D, 0, DESCRIPTION, 1, 500, 500),
            ],
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


def snapshot_quotes(db_path, legacy=False):
    columns = "number, customer, total" if legacy else "number, customer, total, note"
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            f"SELECT {columns} FROM quotes ORDER BY number"
        ).fetchall()
    finally:
        conn.close()


def snapshot_items(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT id, quote_number, position, description, quantity, "
            "unit_price, line_amount FROM items ORDER BY id"
        ).fetchall()
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
