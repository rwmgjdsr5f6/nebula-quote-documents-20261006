"""按编号导出单张报价 JSON（export）命令行回归测试。

仅依赖 Python 标准库。所有用例在独立的临时目录中通过 README 记载的
公开命令（save / export）操作 quote.py 子进程，使用合成单据，
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

# ---- 验收样例 Q-DEMO-001：客户与说明覆盖尖括号、与号、引号、首尾空格与换行 ----

NUMBER = "Q-DEMO-001"
CUSTOMER = "演示客户 <甲>&乙"
NOTE = " 甲\n乙 "
ITEMS = [
    {"description": "咨询", "quantity": 2, "unit_price": 1250},
    {"description": "资料", "quantity": 1, "unit_price": 300},
]
TOTAL_CENTS = 2800

PAYLOAD = {"number": NUMBER, "customer": CUSTOMER, "note": NOTE, "items": ITEMS}

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

DB_ERROR_PREFIX = "错误: 无法读取数据库 "


class QuoteExportTestBase(unittest.TestCase):
    """每个用例使用独立临时目录，只通过公开命令行入口驱动 quote.py。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="quote-export-test-")
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

    def run_quote_bytes(self, *cli_args):
        """以字节方式捕获，便于逐字节断言编码与换行。"""
        return subprocess.run(
            [sys.executable, QUOTE_PY, *cli_args],
            capture_output=True,
            cwd=self.workdir,
        )

    def write_input(self, payload, name="quote.json"):
        path = os.path.join(self.workdir, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        return path

    def save(self, payload, db_name="quotes.sqlite", input_name="quote.json"):
        db_path = os.path.join(self.workdir, db_name)
        input_path = self.write_input(payload, input_name)
        result = self.run_quote("save", "--db", db_path, "--input", input_path)
        self.assertEqual(
            result.returncode, 0,
            msg=f"样例 {payload['number']} 保存应成功: {result.stderr}",
        )
        self.assertEqual(result.stderr, "", msg="保存成功时标准错误必须为空")
        return db_path

    def export(self, db_path, number):
        return self.run_quote("export", "--db", db_path, "--number", number)

    def export_bytes(self, db_path, number):
        return self.run_quote_bytes("export", "--db", db_path, "--number", number)


class ExportFlowTestCase(QuoteExportTestBase):
    """正常导出：字段白名单、原文往返、明细顺序与 JSON 整数。"""

    def test_export_demo_quote_single_line_json_matching_save_structure(self):
        db_path = self.save(PAYLOAD)

        result = self.export_bytes(db_path, NUMBER)
        self.assertEqual(result.returncode, 0, msg=result.stderr.decode())
        self.assertEqual(result.stderr, b"", msg="导出成功时标准错误必须为空")

        raw = result.stdout
        self.assertTrue(raw.endswith(b"\n"), msg="标准输出必须以换行结束")
        self.assertEqual(raw.count(b"\n"), 1, msg="标准输出必须只有末尾一个换行")
        self.assertNotIn(b"\r", raw, msg="不得使用 CRLF 换行")
        raw.decode("utf-8")  # 必须是完整合法的 UTF-8

        quote = json.loads(raw.decode("utf-8"))
        self.assertEqual(
            set(quote.keys()), {"number", "customer", "items", "note"},
            msg="只能包含 save 接受的字段，且带说明时包含 note",
        )
        self.assertEqual(quote["number"], NUMBER)
        self.assertEqual(
            quote["customer"], CUSTOMER,
            msg="客户名解析后必须与库中原文逐字符一致（尖括号、与号不转义）",
        )
        self.assertEqual(
            quote["note"], NOTE,
            msg="说明解析后必须逐字符保留首尾空格与换行",
        )

        self.assertEqual(len(quote["items"]), 2)
        for item in quote["items"]:
            self.assertEqual(
                set(item.keys()), {"description", "quantity", "unit_price"},
                msg="明细不得包含内部标识、行金额或其他字段",
            )
        self.assertEqual(
            [item["description"] for item in quote["items"]],
            ["咨询", "资料"],
            msg="明细必须保持输入顺序",
        )
        self.assertEqual([item["quantity"] for item in quote["items"]], [2, 1])
        self.assertEqual([item["unit_price"] for item in quote["items"]], [1250, 300])
        for item in quote["items"]:
            self.assertIsInstance(item["quantity"], int)
            self.assertNotIsInstance(item["quantity"], bool)
            self.assertIsInstance(item["unit_price"], int)
            self.assertNotIsInstance(item["unit_price"], bool)

        # 原文中尖括号与引号只做 JSON 转义，绝不出现 HTML 实体。
        text = raw.decode("utf-8")
        self.assertIn(CUSTOMER, text)
        for entity in ("&lt;", "&gt;", "&amp;", "&quot;", "&#39;"):
            self.assertNotIn(entity, text, msg=f"导出不得做 HTML 转义: {entity}")
        self.assertNotIn("\\u003c", text)
        self.assertNotIn("\\u0026", text)

    def test_exported_object_round_trips_through_save_with_same_total(self):
        source_db = self.save(PAYLOAD, db_name="source.sqlite")
        exported = self.export_bytes(source_db, NUMBER)
        self.assertEqual(exported.returncode, 0)

        # 将导出对象作为 UTF-8 输入交给 save，保存到另一份新数据库。
        new_input = os.path.join(self.workdir, "exported.json")
        with open(new_input, "wb") as f:
            f.write(exported.stdout)
        new_db = os.path.join(self.workdir, "copy.sqlite")
        save_result = self.run_quote("save", "--db", new_db, "--input", new_input)
        self.assertEqual(save_result.returncode, 0, msg=save_result.stderr)

        conn = sqlite3.connect(new_db)
        try:
            row = conn.execute(
                "SELECT customer, total, note FROM quotes WHERE number = ?",
                (NUMBER,),
            ).fetchone()
            items = conn.execute(
                "SELECT description, quantity, unit_price, line_amount "
                "FROM items ORDER BY position"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(row, (CUSTOMER, TOTAL_CENTS, NOTE))
        self.assertEqual(
            items,
            [("咨询", 2, 1250, 2500), ("资料", 1, 300, 300)],
            msg="重新保存后明细顺序、整数与合计必须一致",
        )
        self.assertEqual(row[1], 2800, msg="合计仍为 2800 分")

        # 原数据库保持不变（字节一致）。
        with open(source_db, "rb") as f:
            before = f.read()
        self.export(source_db, NUMBER)
        with open(source_db, "rb") as f:
            after = f.read()
        self.assertEqual(after, before, msg="导出前后原数据库文件必须逐字节一致")
        for sidecar in ("source.sqlite-wal", "source.sqlite-shm"):
            self.assertFalse(
                os.path.exists(os.path.join(self.workdir, sidecar)),
                msg="只读导出不应留下 WAL/SHM 侧车文件",
            )

    def test_export_is_read_only_on_schema_and_rows(self):
        db_path = self.save(PAYLOAD)
        conn = sqlite3.connect(db_path)
        try:
            quotes_before = conn.execute(
                "SELECT number, customer, total, note FROM quotes"
            ).fetchall()
            items_before = conn.execute(
                "SELECT quote_number, position, description, quantity, "
                "unit_price, line_amount FROM items ORDER BY position"
            ).fetchall()
            master_before = conn.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY name"
            ).fetchall()
        finally:
            conn.close()

        result = self.export(db_path, NUMBER)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

        conn = sqlite3.connect(db_path)
        try:
            self.assertEqual(
                conn.execute("SELECT number, customer, total, note FROM quotes").fetchall(),
                quotes_before,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT quote_number, position, description, quantity, "
                    "unit_price, line_amount FROM items ORDER BY position"
                ).fetchall(),
                items_before,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY name"
                ).fetchall(),
                master_before,
            )
        finally:
            conn.close()

    def test_duplicate_descriptions_kept_as_independent_lines_in_order(self):
        payload = {
            "number": "Q-DUP",
            "customer": "客户",
            "items": [
                {"description": "相同说明", "quantity": 1, "unit_price": 10},
                {"description": "相同说明", "quantity": 3, "unit_price": 0},
                {"description": "相同说明", "quantity": 1, "unit_price": INT64_MAX - 10},
            ],
        }
        db_path = self.save(payload)
        result = self.export(db_path, "Q-DUP")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        quote = json.loads(result.stdout)
        self.assertEqual(len(quote["items"]), 3)
        self.assertEqual(
            [item["description"] for item in quote["items"]],
            ["相同说明", "相同说明", "相同说明"],
            msg="说明相同也必须保留独立行",
        )
        self.assertEqual(
            [(i["quantity"], i["unit_price"]) for i in quote["items"]],
            [(1, 10), (3, 0), (1, INT64_MAX - 10)],
            msg="明细顺序、零单价与 int64 范围大整数必须精确保留",
        )
        for item in quote["items"]:
            self.assertIsInstance(item["unit_price"], int)
            self.assertIsInstance(item["quantity"], int)


class ExportNumberMatchTestCase(QuoteExportTestBase):
    """编号沿用预览的原文精确匹配：空格与大小写参与匹配。"""

    def setUp(self):
        super().setUp()
        self.db_path = self.save(PAYLOAD)

    def assert_not_found(self, number):
        result = self.export(self.db_path, number)
        self.assertEqual(
            result.returncode, 1,
            msg=f"编号 {number!r} 不应匹配到报价",
        )
        self.assertEqual(result.stdout, "", msg="失败时标准输出必须为空")
        self.assertEqual(
            result.stderr, f"错误: 报价编号不存在: {number}\n",
        )
        self.assertNotIn("Traceback", result.stderr)

    def test_exact_number_matches(self):
        result = self.export(self.db_path, NUMBER)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_case_difference_does_not_match(self):
        self.assert_not_found("q-demo-001")

    def test_leading_and_trailing_space_does_not_match(self):
        self.assert_not_found(f" {NUMBER}")
        self.assert_not_found(f"{NUMBER} ")

    def test_unknown_number_reports_not_found(self):
        self.assert_not_found("Q-DOES-NOT-EXIST")


class ExportNoteTestCase(QuoteExportTestBase):
    """note 输出规则：非空（含纯空白）输出；NULL 或空字符串省略。"""

    def build_database(self, rows):
        db_path = os.path.join(self.workdir, "notes.sqlite")
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(FULL_SCHEMA)
            for index, (number, note) in enumerate(rows):
                conn.execute(
                    "INSERT INTO quotes(number, customer, total, note) "
                    "VALUES (?, ?, ?, ?)",
                    (number, "客户", 0, note),
                )
                conn.execute(
                    "INSERT INTO items(quote_number, position, description, "
                    "quantity, unit_price, line_amount) VALUES (?, 0, ?, 1, 0, 0)",
                    (number, "明细"),
                )
            conn.commit()
        finally:
            conn.close()
        return db_path

    def test_null_and_empty_note_omitted_but_nonblank_and_whitespace_exported(self):
        db_path = self.build_database(
            [
                ("Q-NULL", None),
                ("Q-EMPTY", ""),
                ("Q-WS", "  \n\t "),
                ("Q-TEXT", " 甲\n乙 "),
            ]
        )
        null_obj = json.loads(self.export(db_path, "Q-NULL").stdout)
        empty_obj = json.loads(self.export(db_path, "Q-EMPTY").stdout)
        ws_obj = json.loads(self.export(db_path, "Q-WS").stdout)
        text_obj = json.loads(self.export(db_path, "Q-TEXT").stdout)

        self.assertNotIn("note", null_obj, msg="NULL 说明必须省略 note")
        self.assertNotIn("note", empty_obj, msg="空字符串说明必须省略 note")
        self.assertEqual(ws_obj["note"], "  \n\t ", msg="纯空白说明必须原样输出")
        self.assertEqual(text_obj["note"], " 甲\n乙 ")

        for obj in (null_obj, empty_obj, ws_obj, text_obj):
            self.assertEqual(
                set(obj.keys()) - {"note"}, {"number", "customer", "items"},
            )


class LegacyDatabaseExportTestCase(QuoteExportTestBase):
    """旧库缺少 note 列：按无说明导出，不补列、不改动数据。"""

    LEGACY_NUMBER = "Q-EXPORT-LEGACY"
    LEGACY_CUSTOMER = " 旧库客户 <x> & 'y' \"z\"\n"

    def build_legacy_database(self, db_path):
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(LEGACY_SCHEMA)
            conn.execute(
                "INSERT INTO quotes(number, customer, total) VALUES (?, ?, ?)",
                (self.LEGACY_NUMBER, self.LEGACY_CUSTOMER, 2500),
            )
            conn.execute(
                "INSERT INTO items(quote_number, position, description, "
                "quantity, unit_price, line_amount) VALUES (?, ?, ?, ?, ?, ?)",
                (self.LEGACY_NUMBER, 0, "咨询", 2, 1250, 2500),
            )
            conn.commit()
        finally:
            conn.close()

    def test_legacy_database_exported_without_note_or_schema_change(self):
        db_path = os.path.join(self.workdir, "legacy.sqlite")
        self.build_legacy_database(db_path)

        conn = sqlite3.connect(db_path)
        try:
            master_before = conn.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY name"
            ).fetchall()
            columns_before = {
                row[1] for row in conn.execute("PRAGMA table_info(quotes)")
            }
        finally:
            conn.close()
        self.assertNotIn("note", columns_before)

        result = self.export(db_path, self.LEGACY_NUMBER)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        quote = json.loads(result.stdout)
        self.assertEqual(
            quote,
            {
                "number": self.LEGACY_NUMBER,
                "customer": self.LEGACY_CUSTOMER,
                "items": [
                    {"description": "咨询", "quantity": 2, "unit_price": 1250}
                ],
            },
            msg="旧库导出不含 note，客户原文与整数单价保持一致",
        )
        self.assertNotIn("note", quote)

        conn = sqlite3.connect(db_path)
        try:
            columns_after = {
                row[1] for row in conn.execute("PRAGMA table_info(quotes)")
            }
            master_after = conn.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY name"
            ).fetchall()
            rows = conn.execute(
                "SELECT number, customer, total FROM quotes"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(columns_after, columns_before, msg="导出不得补 note 列")
        self.assertNotIn("note", columns_after)
        self.assertEqual(master_after, master_before, msg="表结构必须保持不变")
        self.assertEqual(rows, [(self.LEGACY_NUMBER, self.LEGACY_CUSTOMER, 2500)])


class ExportFailureTestCase(QuoteExportTestBase):
    """数据库无法读取的各类失败路径：退出 1、无部分 JSON、不创建数据库。"""

    def assert_db_error(self, result, db_arg):
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "", msg="失败时标准输出必须为空")
        stderr = result.stderr
        self.assertTrue(
            stderr.startswith(DB_ERROR_PREFIX),
            msg=f"标准错误应以 {DB_ERROR_PREFIX!r} 开头，实际: {stderr!r}",
        )
        remainder = stderr[len(DB_ERROR_PREFIX):]
        self.assertTrue(
            remainder.startswith(f"{db_arg}: ") and remainder.endswith("\n"),
            msg=f"标准错误应为“路径: 原因”单行格式，实际: {stderr!r}",
        )
        self.assertTrue(remainder[len(db_arg) + 2:-1].strip())
        self.assertEqual(stderr.count("\n"), 1)
        self.assertNotIn("Traceback", stderr)

    def test_missing_database_file_errors_without_creating_file(self):
        db_arg = "does-not-exist.sqlite"
        db_path = os.path.join(self.workdir, db_arg)
        self.assertFalse(os.path.exists(db_path))
        result = self.export(db_arg, "Q-ANY")
        self.assert_db_error(result, db_arg)
        self.assertFalse(
            os.path.exists(db_path),
            msg="导出不存在的数据库绝不能创建数据库文件",
        )

    def test_non_sqlite_file_errors_and_preserves_bytes(self):
        db_arg = "not-a-database.bin"
        db_path = os.path.join(self.workdir, db_arg)
        original = b"this is definitely not a sqlite database\n\x00\xff"
        with open(db_path, "wb") as f:
            f.write(original)
        result = self.export(db_arg, "Q-ANY")
        self.assert_db_error(result, db_arg)
        with open(db_path, "rb") as f:
            self.assertEqual(f.read(), original, msg="非数据库文件内容必须原样保留")

    def test_database_without_quotes_table_errors(self):
        db_arg = "no-quotes-table.sqlite"
        db_path = os.path.join(self.workdir, db_arg)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("CREATE TABLE unrelated(id INTEGER PRIMARY KEY, tag TEXT)")
            conn.execute("INSERT INTO unrelated(tag) VALUES (?)", ("保留",))
            conn.commit()
        finally:
            conn.close()
        result = self.export(db_arg, "Q-ANY")
        self.assert_db_error(result, db_arg)
        conn = sqlite3.connect(db_path)
        try:
            self.assertEqual(
                conn.execute("SELECT tag FROM unrelated").fetchall(), [("保留",)]
            )
        finally:
            conn.close()

    def test_database_without_items_table_errors(self):
        db_arg = "no-items-table.sqlite"
        db_path = os.path.join(self.workdir, db_arg)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "CREATE TABLE quotes(number TEXT PRIMARY KEY, customer TEXT, "
                "total INTEGER, note TEXT)"
            )
            conn.execute(
                "INSERT INTO quotes VALUES ('Q-ANY', '客户', 0, NULL)"
            )
            conn.commit()
        finally:
            conn.close()
        result = self.export(db_arg, "Q-ANY")
        self.assert_db_error(result, db_arg)

    def test_quotes_table_missing_required_column_errors(self):
        db_arg = "missing-customer.sqlite"
        db_path = os.path.join(self.workdir, db_arg)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("CREATE TABLE quotes(number TEXT PRIMARY KEY, total INTEGER)")
            conn.execute(
                "CREATE TABLE items(id INTEGER PRIMARY KEY, quote_number TEXT, "
                "position INTEGER, description TEXT, quantity INTEGER, "
                "unit_price INTEGER, line_amount INTEGER)"
            )
            conn.commit()
        finally:
            conn.close()
        result = self.export(db_arg, "Q-ANY")
        self.assert_db_error(result, db_arg)

    def test_items_table_missing_required_column_errors(self):
        db_arg = "missing-unit-price.sqlite"
        db_path = os.path.join(self.workdir, db_arg)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "CREATE TABLE quotes(number TEXT PRIMARY KEY, customer TEXT, "
                "total INTEGER, note TEXT)"
            )
            conn.execute(
                "CREATE TABLE items(id INTEGER PRIMARY KEY, quote_number TEXT, "
                "position INTEGER, description TEXT, quantity INTEGER, "
                "line_amount INTEGER)"
            )
            conn.commit()
        finally:
            conn.close()
        result = self.export(db_arg, "Q-ANY")
        self.assert_db_error(result, db_arg)

    def test_unreadable_file_errors_without_partial_output(self):
        if os.geteuid() == 0:
            self.skipTest("root 绕过文件权限，无法构造不可读文件")
        db_arg = "unreadable.sqlite"
        db_path = os.path.join(self.workdir, db_arg)
        with open(db_path, "wb") as f:
            f.write(b"x")
        os.chmod(db_path, 0o000)
        try:
            result = self.export(db_arg, "Q-ANY")
        finally:
            os.chmod(db_path, 0o644)
        self.assert_db_error(result, db_arg)


# 项目目录清洁性：测试开始前记录项目根内容，全部用例结束后不得新增任何文件。
_PROJECT_ROOT_ENTRIES_AT_START = set(os.listdir(PROJECT_ROOT))


def tearDownModule():
    leftovers = set(os.listdir(PROJECT_ROOT)) - _PROJECT_ROOT_ENTRIES_AT_START
    assert not leftovers, f"测试在项目根目录留下了文件: {sorted(leftovers)}"


if __name__ == "__main__":
    unittest.main()
