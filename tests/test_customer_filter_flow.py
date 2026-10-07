"""list --customer 客户精确筛选专项回归测试。

仅依赖 Python 3 标准库。所有用例在独立临时目录中通过 README 记载的公开命令
（save / list）以子进程方式驱动 quote.py，使用固定合成单据，不读取项目内
业务数据，也不在项目目录留下 JSON、SQLite 或 HTML 文件。

固定样例（保存顺序刻意为 Q-B、Q-A、Q-C、Q-D）：
    Q-B 客户 "Demo"   一条“演示明细” 数量 1 单价 2500  -> total 2500
    Q-A 客户 "Demo"   一条“演示明细” 数量 1 单价    0  -> total    0
    Q-C 客户 "demo"   一条“演示明细” 数量 1 单价  300  -> total  300
    Q-D 客户 " Demo " 一条“演示明细” 数量 1 单价  500  -> total  500

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

NUMBER_A = "Q-A"
NUMBER_B = "Q-B"
NUMBER_C = "Q-C"
NUMBER_D = "Q-D"

CUSTOMER_DEMO = "Demo"
CUSTOMER_LOWER = "demo"
CUSTOMER_PADDED = " Demo "
CUSTOMER_UPPER = "DEMO"

# 保存顺序固定为 Q-B、Q-A、Q-C、Q-D：证明列表顺序只由编号排序决定，
# 与保存先后无关。
SAVE_SAMPLES = [
    {
        "number": NUMBER_B,
        "customer": CUSTOMER_DEMO,
        "items": [{"description": DESCRIPTION, "quantity": 1, "unit_price": 2500}],
    },
    {
        "number": NUMBER_A,
        "customer": CUSTOMER_DEMO,
        "items": [{"description": DESCRIPTION, "quantity": 1, "unit_price": 0}],
    },
    {
        "number": NUMBER_C,
        "customer": CUSTOMER_LOWER,
        "items": [{"description": DESCRIPTION, "quantity": 1, "unit_price": 300}],
    },
    {
        "number": NUMBER_D,
        "customer": CUSTOMER_PADDED,
        "items": [{"description": DESCRIPTION, "quantity": 1, "unit_price": 500}],
    },
]

# 各筛选值的期望输出，全部为手写固定常量。
EXPECTED_FILTER_DEMO = [
    {"number": NUMBER_A, "customer": CUSTOMER_DEMO, "total": 0},
    {"number": NUMBER_B, "customer": CUSTOMER_DEMO, "total": 2500},
]
EXPECTED_FILTER_LOWER = [
    {"number": NUMBER_C, "customer": CUSTOMER_LOWER, "total": 300},
]
EXPECTED_FILTER_PADDED = [
    {"number": NUMBER_D, "customer": CUSTOMER_PADDED, "total": 500},
]
EXPECTED_FILTER_UPPER = []
EXPECTED_LIST_ALL = [
    {"number": NUMBER_A, "customer": CUSTOMER_DEMO, "total": 0},
    {"number": NUMBER_B, "customer": CUSTOMER_DEMO, "total": 2500},
    {"number": NUMBER_C, "customer": CUSTOMER_LOWER, "total": 300},
    {"number": NUMBER_D, "customer": CUSTOMER_PADDED, "total": 500},
]

# 空白筛选值的固定报错（单行，以换行结束）。
BLANK_FILTER_ERROR = "错误: 客户筛选值不能为空白\n"
BLANK_FILTER_VALUES = ["", " ", "\t", "\n", " \t \n "]

READ_DB_ERROR_PREFIX = "错误: 无法读取数据库 "

# 缺 note 列的旧库结构。
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

# 特殊字符客户：中文、双引号、换行、尖括号、与号、百分号、下划线一应俱全。
SPECIAL_CUSTOMER = '特殊客户 "引号"\n<尖括号> & 符号%下划线_结尾'
# 逐字符不同的相似客户名：任何一个都不能被 SPECIAL_CUSTOMER 的筛选命中，
# 反过来用它们筛选也不能命中 SPECIAL_CUSTOMER。
SIMILAR_CUSTOMERS = [
    SPECIAL_CUSTOMER + "x",          # 仅尾部多一个字符
    SPECIAL_CUSTOMER[:-1],           # 仅尾部少一个字符
    SPECIAL_CUSTOMER.replace('"', "'"),  # 双引号变单引号
    SPECIAL_CUSTOMER.replace("\n", " "),  # 换行变空格
    SPECIAL_CUSTOMER.replace("%", "％"),  # 半角百分号变全角
    SPECIAL_CUSTOMER.replace("_", "＿"),  # 半角下划线变全角
]
# 通配对抗样例：若误把筛选值当 LIKE 模式，"Demo%" 会命中 Demo100，
# "Demo_" 会命中 DemoX；精确比较下只逐字符命中。
WILDCARD_EXACT = "Demo%"
WILDCARD_OTHERS = ["Demo100", "DemoX", "Demo_"]


class CustomerFilterTestBase(unittest.TestCase):
    """每个用例使用独立临时目录，只通过公开命令行入口驱动 quote.py。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="quote-customer-filter-")
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

    def save_quote(self, payload, index, db_name=DB_NAME):
        input_path = self.write_input(payload, f"input-{index}.json")
        result = self.run_quote("save", "--db", db_name, "--input", input_path)
        self.assertEqual(
            result.returncode, 0,
            msg=f"样例 {payload['number']} 保存应成功: {result.stderr}",
        )
        self.assertEqual(result.stderr, "", msg="保存成功时标准错误必须为空")
        self.assertEqual(
            result.stdout, f"{payload['number']}\n",
            msg="保存成功时标准输出只回显报价编号",
        )
        return db_name

    def save_demo_quotes(self):
        """按 Q-B、Q-A、Q-C、Q-D 的固定顺序保存四张报价，返回数据库相对路径。"""
        for index, payload in enumerate(SAVE_SAMPLES):
            self.save_quote(payload, index)
        return DB_NAME

    def list_with_customer(self, db_name, customer):
        return self.run_quote(
            "list", "--db", db_name, "--customer", customer
        )

    def list_all(self, db_name):
        return self.run_quote("list", "--db", db_name)

    def assert_success_json(self, result, expected_records):
        """成功路径公共断言：退出 0、stderr 为空、stdout 为单行 JSON 数组。"""
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "", msg="查询成功时标准错误必须为空")

        stdout = result.stdout
        self.assertTrue(stdout.endswith("\n"), msg="标准输出必须以换行结束")
        self.assertEqual(stdout.count("\n"), 1, msg="标准输出必须是单行 JSON")
        self.assertNotIn("\r", stdout, msg="不得使用 CRLF 换行")
        # 与手写常量序列化结果逐字节一致（含结尾换行）。
        self.assertEqual(
            stdout,
            json.dumps(expected_records, ensure_ascii=False) + "\n",
            msg="标准输出必须是固定期望的 JSON 数组加单个换行",
        )

        records = json.loads(stdout)
        self.assertEqual(records, expected_records)
        for record in records:
            self.assertEqual(
                set(record.keys()), {"number", "customer", "total"},
                msg="每项只能含 number、customer、total 三个字段",
            )
            self.assertIsInstance(record["total"], int, msg="total 必须是 JSON 整数")
            self.assertNotIsInstance(record["total"], bool)
            self.assertIsInstance(record["customer"], str)
        return records


class ExactCustomerFilterTestCase(CustomerFilterTestBase):
    """核心筛选矩阵：Demo / demo / " Demo " / DEMO / 省略参数。"""

    def test_fixed_filter_matrix(self):
        db_name = self.save_demo_quotes()

        result = self.list_with_customer(db_name, CUSTOMER_DEMO)
        records = self.assert_success_json(result, EXPECTED_FILTER_DEMO)
        self.assertEqual(
            [r["number"] for r in records], [NUMBER_A, NUMBER_B],
            msg="筛选 Demo 只返回 Q-A、Q-B，顺序按编号排列",
        )
        self.assertEqual(
            [r["total"] for r in records], [0, 2500],
            msg="Q-A total 为 0、Q-B total 为 2500（分单位整数）",
        )

        result = self.list_with_customer(db_name, CUSTOMER_LOWER)
        records = self.assert_success_json(result, EXPECTED_FILTER_LOWER)
        self.assertEqual([r["number"] for r in records], [NUMBER_C])

        result = self.list_with_customer(db_name, CUSTOMER_PADDED)
        records = self.assert_success_json(result, EXPECTED_FILTER_PADDED)
        self.assertEqual([r["number"] for r in records], [NUMBER_D])
        # 客户文本原样保留：首尾空格在 JSON 中仍清晰可见。
        self.assertIn('"customer": " Demo "', result.stdout)

        result = self.list_with_customer(db_name, CUSTOMER_UPPER)
        records = self.assert_success_json(result, EXPECTED_FILTER_UPPER)
        self.assertEqual(result.stdout, "[]\n", msg="筛选 DEMO 无匹配，输出空数组")

        result = self.list_all(db_name)
        records = self.assert_success_json(result, EXPECTED_LIST_ALL)
        self.assertEqual(
            [r["number"] for r in records],
            [NUMBER_A, NUMBER_B, NUMBER_C, NUMBER_D],
            msg="省略筛选参数时按编号返回全部四条",
        )
        self.assertEqual(
            [r["total"] for r in records], [0, 2500, 300, 500],
            msg="四条合计依次为 0、2500、300、500",
        )

    def test_no_match_customer_outputs_empty_array_not_error(self):
        db_name = self.save_demo_quotes()
        # 合法但无匹配的筛选值仍是成功查询，不是错误。
        result = self.list_with_customer(db_name, "不存在的客户")
        self.assert_success_json(result, [])


class SpecialCharactersCustomerFilterTestCase(CustomerFilterTestBase):
    """中文、引号、换行、尖括号、与号及 %、_ 的精确匹配与无转义核对。"""

    def setUp(self):
        super().setUp()
        self.db_name = "special.sqlite"
        all_customers = (
            [(SPECIAL_CUSTOMER, "Q-SPECIAL-1", 1100)]
            + [
                (name, f"Q-SIMILAR-{index:02d}", 1000 + index)
                for index, name in enumerate(SIMILAR_CUSTOMERS)
            ]
            + [(WILDCARD_EXACT, "Q-WILD-EXACT", 700)]
            + [
                (name, f"Q-WILD-OTHER-{index}", 800 + index)
                for index, name in enumerate(WILDCARD_OTHERS)
            ]
        )
        for index, (customer, number, unit_price) in enumerate(all_customers):
            self.save_quote(
                {
                    "number": number,
                    "customer": customer,
                    "items": [
                        {
                            "description": DESCRIPTION,
                            "quantity": 1,
                            "unit_price": unit_price,
                        }
                    ],
                },
                index,
                db_name=self.db_name,
            )

    def test_only_character_identical_customer_matches(self):
        expected = [
            {"number": "Q-SPECIAL-1", "customer": SPECIAL_CUSTOMER, "total": 1100}
        ]
        result = self.list_with_customer(self.db_name, SPECIAL_CUSTOMER)
        records = self.assert_success_json(result, expected)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["customer"], SPECIAL_CUSTOMER)

        # 每个相似客户名在库中各有自己的报价：精确筛选只能命中其自身那条，
        # 绝不顺带命中 Q-SPECIAL-1（任何归一化、子串或通配匹配都会串号）。
        for index, name in enumerate(SIMILAR_CUSTOMERS):
            own_number = f"Q-SIMILAR-{index:02d}"
            with self.subTest(customer=name):
                result = self.list_with_customer(self.db_name, name)
                records = self.assert_success_json(
                    result,
                    [
                        {
                            "number": own_number,
                            "customer": name,
                            "total": 1000 + index,
                        }
                    ],
                )
                self.assertEqual(
                    [r["number"] for r in records], [own_number],
                    msg="相似客户筛选不得命中 SPECIAL_CUSTOMER 的报价",
                )

        # 库里不存在的近似变体必须全部落空：首尾多空格、CRLF、全角与号。
        nonexistent_probes = (
            SPECIAL_CUSTOMER + " ",
            " " + SPECIAL_CUSTOMER,
            SPECIAL_CUSTOMER.replace("\n", "\r\n"),
            SPECIAL_CUSTOMER.replace("&", "＆"),
            SPECIAL_CUSTOMER.replace("符号", "符号 "),
        )
        for probe in nonexistent_probes:
            with self.subTest(probe=probe):
                result = self.list_with_customer(self.db_name, probe)
                self.assert_success_json(result, [])

    def test_percent_and_underscore_are_literal_characters_not_wildcards(self):
        # "Demo%" 作为筛选值：LIKE 语义下会同时命中 Demo100、DemoX、Demo_，
        # 精确语义只命中逐字符相同的 Q-WILD-EXACT 一条。
        result = self.list_with_customer(self.db_name, "Demo%")
        records = self.assert_success_json(
            result,
            [{"number": "Q-WILD-EXACT", "customer": "Demo%", "total": 700}],
        )
        self.assertEqual([r["number"] for r in records], ["Q-WILD-EXACT"])

        # "Demo_" 是库中真实客户：LIKE 语义下还会多命中五位长的 DemoX，
        # 精确语义只命中其自身一条。
        result = self.list_with_customer(self.db_name, "Demo_")
        records = self.assert_success_json(
            result,
            [
                {
                    "number": "Q-WILD-OTHER-2",
                    "customer": "Demo_",
                    "total": 802,
                }
            ],
        )
        self.assertEqual([r["number"] for r in records], ["Q-WILD-OTHER-2"])

    def test_special_characters_are_not_escaped_in_stdout(self):
        result = self.list_with_customer(self.db_name, SPECIAL_CUSTOMER)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        stdout = result.stdout

        # 中文、尖括号、与号、百分号、下划线在 JSON 中原样出现。
        for fragment in ("特殊客户", "<尖括号>", "& 符号", "%", "下划线", "结尾"):
            self.assertIn(fragment, stdout, msg=f"{fragment!r} 必须原样输出")
        # 双引号与换行按 JSON 规则转义，但仅此而已。
        self.assertIn('\\"引号\\"', stdout)
        self.assertIn("\\n", stdout)
        # 不得发生 HTML 转义或 ASCII 转义。
        for escaped in ("&lt;", "&gt;", "&amp;", "&quot;", "&#", "\\u"):
            self.assertNotIn(escaped, stdout, msg=f"输出中不得出现 {escaped!r}")

        # 解析回来后客户名与输入逐字符一致。
        records = json.loads(stdout)
        self.assertEqual(records[0]["customer"], SPECIAL_CUSTOMER)


class BlankCustomerFilterTestCase(CustomerFilterTestBase):
    """空字符串与纯空白筛选值：先于数据库访问被拒绝。"""

    def test_blank_values_rejected_without_touching_database(self):
        # 数据库路径不存在：由于拒绝发生在数据库访问之前，文件绝不能被创建。
        missing_db = "never-created.sqlite"
        missing_path = os.path.join(self.workdir, missing_db)
        self.assertFalse(os.path.exists(missing_path))

        for value in BLANK_FILTER_VALUES:
            with self.subTest(value=repr(value)):
                self.assertFalse(os.path.exists(missing_path))
                result = self.list_with_customer(missing_db, value)
                self.assertEqual(
                    result.returncode, 1,
                    msg="空白筛选值必须以退出码 1 拒绝",
                )
                self.assertEqual(result.stdout, "", msg="拒绝时标准输出必须为空")
                self.assertEqual(
                    result.stderr, BLANK_FILTER_ERROR,
                    msg="标准错误必须是固定单行提示并以换行结束",
                )
                self.assertFalse(
                    os.path.exists(missing_path),
                    msg="空白拒绝先于数据库访问，不得创建数据库文件",
                )

    def test_blank_rejection_leaves_existing_database_untouched(self):
        db_name = self.save_demo_quotes()
        db_path = os.path.join(self.workdir, db_name)
        before = snapshot_full_database(db_path)

        for value in BLANK_FILTER_VALUES:
            with self.subTest(value=repr(value)):
                result = self.list_with_customer(db_name, value)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stderr, BLANK_FILTER_ERROR)

        self.assertEqual(
            snapshot_full_database(db_path), before,
            msg="空白筛选拒绝前后，表结构、报价与明细记录必须完全一致",
        )


class MissingDatabaseWithFilterTestCase(CustomerFilterTestBase):
    """合法筛选 + 数据库不可读：退出 1，stderr 说明路径与原因，不创建文件。"""

    def test_valid_filter_on_missing_database_errors(self):
        db_name = "no-such-demo.sqlite"
        db_path = os.path.join(self.workdir, db_name)
        self.assertFalse(os.path.exists(db_path))

        result = self.list_with_customer(db_name, CUSTOMER_DEMO)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "", msg="失败时标准输出必须为空")

        stderr = result.stderr
        self.assertTrue(
            stderr.startswith(READ_DB_ERROR_PREFIX),
            msg=f"标准错误应以 {READ_DB_ERROR_PREFIX!r} 开头，实际: {stderr!r}",
        )
        self.assertIn(
            db_name, stderr,
            msg=f"标准错误必须包含传入路径 {db_name!r}，实际: {stderr!r}",
        )
        # 形如“错误: 无法读取数据库 <路径>: <原因>”，单行、以换行结束。
        remainder = stderr[len(READ_DB_ERROR_PREFIX):]
        self.assertTrue(
            remainder.startswith(f"{db_name}: ") and remainder.endswith("\n"),
            msg=f"标准错误应为“路径: 原因”单行格式，实际: {stderr!r}",
        )
        reason = remainder[len(db_name) + 2:-1]
        self.assertTrue(reason.strip(), msg="数据库失败原因不能为空")
        self.assertEqual(stderr.count("\n"), 1, msg="标准错误必须为单行")
        self.assertNotIn("Traceback", stderr, msg="不得向用户输出异常堆栈")

        self.assertFalse(
            os.path.exists(db_path),
            msg="只读访问不存在的数据库绝不能创建文件",
        )
        # 临时目录中不得出现任何 SQLite 侧车文件。
        self.assertEqual(
            [name for name in os.listdir(self.workdir) if "no-such-demo" in name],
            [],
        )


class LegacyDatabaseCustomerFilterTestCase(CustomerFilterTestBase):
    """缺 note 列的旧库：客户筛选按相同规则成功，且不补列、不改数据。"""

    LEGACY_ROWS = [
        ("Q-OLD-A", "Demo", 2500),
        ("Q-OLD-B", " Demo ", 500),
        ("Q-OLD-C", "demo", 300),
    ]

    def build_legacy_database(self, db_path):
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(LEGACY_SCHEMA)
            for index, (number, customer, total) in enumerate(self.LEGACY_ROWS):
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

    def test_customer_filter_works_on_legacy_database(self):
        db_name = "legacy.sqlite"
        db_path = os.path.join(self.workdir, db_name)
        self.build_legacy_database(db_path)

        columns_before = {
            row[1]
            for row in sqlite3.connect(db_path).execute("PRAGMA table_info(quotes)")
        }
        self.assertNotIn("note", columns_before, msg="前置条件：旧库没有 note 列")
        master_before = snapshot_sqlite_master(db_path)
        quotes_before = snapshot_legacy_quotes(db_path)

        result = self.list_with_customer(db_name, "Demo")
        self.assert_success_json(
            result,
            [{"number": "Q-OLD-A", "customer": "Demo", "total": 2500}],
        )

        result = self.list_with_customer(db_name, " Demo ")
        self.assert_success_json(
            result,
            [{"number": "Q-OLD-B", "customer": " Demo ", "total": 500}],
        )

        result = self.list_with_customer(db_name, "demo")
        self.assert_success_json(
            result,
            [{"number": "Q-OLD-C", "customer": "demo", "total": 300}],
        )

        result = self.list_with_customer(db_name, "DEMO")
        self.assert_success_json(result, [])

        # 查询后结构与数据保持原样：不补 note 列。
        columns_after = {
            row[1]
            for row in sqlite3.connect(db_path).execute("PRAGMA table_info(quotes)")
        }
        self.assertEqual(columns_after, columns_before)
        self.assertNotIn("note", columns_after)
        self.assertEqual(
            snapshot_sqlite_master(db_path), master_before,
            msg="旧库查询前后 sqlite_master 必须一致",
        )
        self.assertEqual(
            snapshot_legacy_quotes(db_path), quotes_before,
            msg="旧库报价记录查询后保持不变",
        )
        conn = sqlite3.connect(db_path)
        try:
            items_after = conn.execute(
                "SELECT quote_number, position, description, quantity, "
                "unit_price, line_amount FROM items ORDER BY id"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(
            items_after,
            [
                (number, 0, DESCRIPTION, 1, total, total)
                for number, _, total in self.LEGACY_ROWS
            ],
            msg="旧库明细记录查询后保持不变",
        )


class CustomerFilterReadOnlyTestCase(CustomerFilterTestBase):
    """全部查询/拒绝路径结束后，已有数据库的结构与记录与执行前一致。"""

    def test_queries_and_rejections_have_no_side_effects(self):
        db_name = self.save_demo_quotes()
        db_path = os.path.join(self.workdir, db_name)
        before = snapshot_full_database(db_path)

        # 成功路径：全量、命中、零命中。
        self.assertEqual(self.list_all(db_name).returncode, 0)
        self.assertEqual(
            self.list_with_customer(db_name, "Demo").returncode, 0
        )
        self.assertEqual(
            self.list_with_customer(db_name, " Demo ").returncode, 0
        )
        self.assertEqual(
            self.list_with_customer(db_name, "DEMO").returncode, 0
        )
        self.assertEqual(
            self.list_with_customer(db_name, "毫无关系的客户").returncode, 0
        )
        # 拒绝路径：纯空白。
        rejection = self.list_with_customer(db_name, "  \n\t ")
        self.assertEqual(rejection.returncode, 1)

        self.assertEqual(
            snapshot_full_database(db_path), before,
            msg="一系列查询与拒绝后，表结构、报价与明细记录均不得变化",
        )

        # 文件数量与名称也不得变化（无 -wal/-shm/journal 残留）。
        expected_files = {DB_NAME} | {
            f"input-{index}.json" for index, _ in enumerate(SAVE_SAMPLES)
        }
        self.assertEqual(set(os.listdir(self.workdir)), expected_files)


def snapshot_sqlite_master(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT type, name, tbl_name, rootpage, sql "
            "FROM sqlite_master ORDER BY type, name"
        ).fetchall()
    finally:
        conn.close()


def snapshot_full_database(db_path):
    """完整库快照：(sqlite_master, quotes 全字段, items 全字段)。"""
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
            "SELECT id, quote_number, position, description, quantity, "
            "unit_price, line_amount FROM items ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    return master, quotes, items


def snapshot_legacy_quotes(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT number, customer, total FROM quotes ORDER BY number"
        ).fetchall()
    finally:
        conn.close()


# 项目目录清洁性：测试开始前记录项目根内容，全部用例结束后不得新增任何文件。
_PROJECT_ROOT_ENTRIES_AT_START = set(os.listdir(PROJECT_ROOT))


def tearDownModule():
    leftovers = set(os.listdir(PROJECT_ROOT)) - _PROJECT_ROOT_ENTRIES_AT_START
    assert not leftovers, f"测试在项目根目录留下了文件: {sorted(leftovers)}"


if __name__ == "__main__":
    unittest.main()
