"""export --output 写入新文件的文件导出回归测试。

仅依赖 Python 标准库。所有用例在独立的临时目录中通过 README 记载的
公开命令（save / export）操作 quote.py 子进程，使用合成固定报价
Q-FILE-001，不读取项目内业务数据，也不在项目目录留下 JSON、SQLite
或其他文件；临时目录在用例结束或失败时自动清理。

本文件只覆盖“导出到文件”这一既有行为：
  python3 quote.py export --db demo.sqlite --number Q-FILE-001 --output quote.json
省略 --output 时输出到标准输出的行为由 test_export_flow.py 覆盖，这里仅把
标准输出字节作为与文件字节、固定预期三方比对的一环，不替换导出处理函数，
也不仅以两次实际输出互相比对代替固定预期。

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

# ---- 固定验收样例 Q-FILE-001：客户与说明覆盖尖括号、与号、首尾空格与换行；
# ---- 两条明细说明相同，数量与单价为整数 ----

NUMBER = "Q-FILE-001"
CUSTOMER = " 演示<甲>&乙 "
NOTE = " 说明\n第二行 "
ITEMS = [
    {"description": "服务", "quantity": 2, "unit_price": 1250},
    {"description": "服务", "quantity": 1, "unit_price": 300},
]

PAYLOAD = {"number": NUMBER, "customer": CUSTOMER, "note": NOTE, "items": ITEMS}

# ---- 固定预期导出文档：由样例数据按公开行为逐字符推出并写死 ----
# json.dumps 默认分隔符为 ", " 与 ": "，ensure_ascii=False 保留原文，
# 键顺序为 number、customer、items、note；note 内换行在 JSON 中是
# 反斜杠加 n 两个字符，整篇为一个物理行，末尾恰好一个 LF。
EXPECTED_EXPORT_DOCUMENT = (
    '{"number": "Q-FILE-001", "customer": " 演示<甲>&乙 ", '
    '"items": [{"description": "服务", "quantity": 2, "unit_price": 1250}, '
    '{"description": "服务", "quantity": 1, "unit_price": 300}], '
    '"note": " 说明\\n第二行 "}\n'
)
EXPECTED_EXPORT_BYTES = EXPECTED_EXPORT_DOCUMENT.encode("utf-8")

UNKNOWN_NUMBER = "Q-FILE-UNKNOWN"

EXISTS_ERROR_PREFIX = "错误: 输出文件已存在: "
NOT_FOUND_PREFIX = "错误: 报价编号不存在: "
DB_ERROR_PREFIX = "错误: 无法读取数据库 "
WRITE_ERROR_PREFIX = "错误: 无法写入输出文件 "


class ExportFileTestBase(unittest.TestCase):
    """每个用例使用独立临时目录，只通过公开命令行入口驱动 quote.py。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="quote-export-file-test-")
        self.addCleanup(self._tmp.cleanup)
        self.workdir = self._tmp.name

    # ---- 基础设施 ----

    def run_quote(self, *cli_args):
        """以文本方式捕获，便于断言路径回显与一行错误消息。"""
        return subprocess.run(
            [sys.executable, QUOTE_PY, *cli_args],
            capture_output=True,
            text=True,
            cwd=self.workdir,
        )

    def run_quote_bytes(self, *cli_args):
        """以字节方式捕获，便于逐字节断言编码、换行与标准输出。"""
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

    def save(self, payload=PAYLOAD, db_name="quotes.sqlite", input_name="quote.json"):
        db_path = os.path.join(self.workdir, db_name)
        input_path = self.write_input(payload, input_name)
        result = self.run_quote("save", "--db", db_path, "--input", input_path)
        self.assertEqual(
            result.returncode, 0,
            msg=f"样例 {payload['number']} 保存应成功: {result.stderr}",
        )
        self.assertEqual(result.stderr, "", msg="保存成功时标准错误必须为空")
        return db_path

    def snapshot(self, db_path):
        """直接读取 SQLite 快照：(quotes 全部行, items 全部行, 表结构)。"""
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
            master = conn.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY name"
            ).fetchall()
        finally:
            conn.close()
        return quotes, items, master

    def assert_database_unchanged(self, db_path, before, action_description):
        """断言一次导出（成功或失败）前后源库的结构与业务记录完全一致。"""
        self.assertEqual(
            self.snapshot(db_path), before,
            msg=f"{action_description}前后源库的结构与业务记录必须保持一致",
        )

    def assert_no_sidecars(self, db_path):
        for suffix in ("-wal", "-shm"):
            self.assertFalse(
                os.path.exists(db_path + suffix),
                msg=f"只读导出不应留下侧车文件: {os.path.basename(db_path)}{suffix}",
            )


class ExportToFileSuccessTestCase(ExportFileTestBase):
    """导出到新路径：退出码、流内容、文件字节与固定预期完全一致。"""

    def test_export_to_new_file_matches_fixed_bytes_and_stdout(self):
        db_path = self.save()
        before = self.snapshot(db_path)
        output_path = os.path.join(self.workdir, "exported.json")

        # 保存后向新路径导出：退出 0，标准错误为空，标准输出仅含传入路径与
        # 末尾换行（不输出 JSON 本体）。
        result = self.run_quote(
            "export", "--db", db_path, "--number", NUMBER, "--output", output_path
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "", msg="导出成功时标准错误必须为空")
        self.assertEqual(
            result.stdout, f"{output_path}\n",
            msg="标准输出只能是传入的输出路径加一个换行",
        )

        # 文件确实新建在传入路径。
        self.assertTrue(os.path.isfile(output_path), msg="导出必须在新路径创建文件")
        with open(output_path, "rb") as f:
            file_bytes = f.read()

        # 编码与物理形态：无 BOM 的 UTF-8，完整单行 JSON 加恰好一个 LF。
        self.assertFalse(
            file_bytes.startswith(b"\xef\xbb\xbf"),
            msg="导出文件不得带 UTF-8 BOM",
        )
        file_bytes.decode("utf-8")  # 必须是完整合法的 UTF-8，无替换字符空间
        self.assertTrue(file_bytes.endswith(b"\n"), msg="文件必须以 LF 结束")
        self.assertEqual(file_bytes.count(b"\n"), 1, msg="整篇只能有末尾一个换行")
        self.assertNotIn(b"\r", file_bytes, msg="不得使用 CRLF 换行")
        self.assertEqual(
            file_bytes[:-1].find(b"\n"), -1,
            msg="末尾 LF 之前不得再有换行（必须是完整单行 JSON）",
        )

        # 文件字节逐字节等于写死的固定预期（不是用 quote.py 现算答案）。
        self.assertEqual(
            file_bytes, EXPECTED_EXPORT_BYTES,
            msg="文件字节必须与固定预期逐字节一致",
        )

        document = file_bytes.decode("utf-8")
        quote = json.loads(document)

        # 解析后：固定输入中的文本空白与换行逐字符保留。
        self.assertEqual(
            set(quote.keys()), {"number", "customer", "items", "note"},
            msg="只能包含 save 接受的字段，且带说明时包含 note",
        )
        self.assertEqual(quote["number"], NUMBER)
        self.assertEqual(
            quote["customer"], CUSTOMER,
            msg="客户名必须逐字符保留首尾空格、尖括号与与号",
        )
        self.assertEqual(
            quote["note"], NOTE,
            msg="说明必须逐字符保留首尾空格与内部换行",
        )

        # 明细：两行独立记录、顺序固定、整数类型；重复说明不合并。
        self.assertEqual(len(quote["items"]), 2)
        for item in quote["items"]:
            self.assertEqual(
                set(item.keys()), {"description", "quantity", "unit_price"},
                msg="明细不得包含内部标识、行金额或其他字段",
            )
            self.assertIsInstance(item["quantity"], int)
            self.assertNotIsInstance(item["quantity"], bool)
            self.assertIsInstance(item["unit_price"], int)
            self.assertNotIsInstance(item["unit_price"], bool)
        self.assertEqual(
            [item["description"] for item in quote["items"]],
            ["服务", "服务"],
            msg="两条重复说明必须保留为独立的两行",
        )
        self.assertEqual(
            [item["quantity"] for item in quote["items"]], [2, 1],
            msg="明细顺序必须与固定输入一致",
        )
        self.assertEqual(
            [item["unit_price"] for item in quote["items"]], [1250, 300],
        )

        # 不含内部标识、行金额或合计：解析后的对象无这些键，原文中也不出现
        # 对应的 JSON 键记号。
        for forbidden in ("id", "line_amount", "total", "position"):
            self.assertNotIn(forbidden, quote)
            for item in quote["items"]:
                self.assertNotIn(forbidden, item)
            self.assertNotIn(
                f'"{forbidden}"', document,
                msg=f"导出原文不得出现字段 {forbidden!r}",
            )

        # 尖括号与与号保留原字符：原文子串直接出现，且无任何 HTML 实体或
        # JSON 的 反斜杠 u 转义。
        self.assertIn(CUSTOMER, document)
        self.assertIn("<甲>", document)
        self.assertIn("&乙", document)
        for entity in ("&lt;", "&gt;", "&amp;", "&quot;", "&#39;", "&#60;", "&#62;"):
            self.assertNotIn(entity, document, msg=f"导出不得做 HTML 转义: {entity}")
        self.assertNotIn("\\u003c", document, msg="尖括号不得写成 \\u003c")
        self.assertNotIn("\\u0026", document, msg="与号不得写成 \\u0026")
        self.assertIn(
            "演示", document,
            msg="ensure_ascii=False：中文必须以原文 UTF-8 字节写出",
        )

        # 同一报价省略 --output 时的标准输出字节：先独立核对固定预期，
        # 再与文件字节做三方一致（固定预期 == 文件 == 标准输出）。
        stdout_result = self.run_quote_bytes(
            "export", "--db", db_path, "--number", NUMBER
        )
        self.assertEqual(stdout_result.returncode, 0, msg=stdout_result.stderr)
        self.assertEqual(stdout_result.stderr, b"")
        self.assertEqual(
            stdout_result.stdout, EXPECTED_EXPORT_BYTES,
            msg="省略 --output 的标准输出也必须逐字节等于固定预期",
        )
        self.assertEqual(
            stdout_result.stdout, file_bytes,
            msg="文件字节必须与省略 --output 时的标准输出字节一致",
        )

        # 成功导出前后，已有源库的结构与业务记录保持一致，且不留 WAL/SHM。
        self.assert_database_unchanged(db_path, before, "文件导出")
        self.assert_no_sidecars(db_path)


class ExportToFileFailureTestCase(ExportFileTestBase):
    """文件导出的确定失败结果：退出 1、标准输出为空、不改动/不创建文件。"""

    def setUp(self):
        super().setUp()
        self.db_path = self.save()
        self.before = self.snapshot(self.db_path)

    def assert_single_line_error(self, stderr, prefix, target):
        """断言“前缀 + 目标路径 + ': ' + 非空原因 + LF”的单行错误格式。"""
        self.assertTrue(
            stderr.startswith(prefix),
            msg=f"标准错误应以 {prefix!r} 开头，实际: {stderr!r}",
        )
        remainder = stderr[len(prefix):]
        self.assertTrue(
            remainder.startswith(f"{target}: ") and remainder.endswith("\n"),
            msg=f"标准错误应为“{target}: 原因”的单行格式，实际: {stderr!r}",
        )
        self.assertTrue(remainder[len(target) + 2:-1].strip(), msg="必须附带失败原因")
        self.assertEqual(stderr.count("\n"), 1, msg="标准错误必须只有一行")
        self.assertNotIn("Traceback", stderr, msg="不得显示异常堆栈")

    def test_existing_target_rejected_and_bytes_unchanged(self):
        output_path = os.path.join(self.workdir, "already.json")
        sentinel = b"preexisting \xe5\x8e\x9f\xe6\x9c\x89 bytes\n"
        with open(output_path, "wb") as f:
            f.write(sentinel)

        result = self.run_quote(
            "export", "--db", self.db_path, "--number", NUMBER,
            "--output", output_path,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "", msg="失败时标准输出必须为空")
        self.assertEqual(
            result.stderr, f"{EXISTS_ERROR_PREFIX}{output_path}\n",
            msg="标准错误应说明输出文件已存在并带上传入路径",
        )
        with open(output_path, "rb") as f:
            self.assertEqual(f.read(), sentinel, msg="原文件字节必须保持不变")

        # 拒绝发生在读库之前：源库结构与业务记录同样保持一致。
        self.assert_database_unchanged(self.db_path, self.before, "目标已存在的导出")
        self.assert_no_sidecars(self.db_path)

    def test_existing_target_reported_before_missing_database(self):
        """数据库不存在时，只要目标已存在仍先报告目标已存在。"""
        missing_db = os.path.join(self.workdir, "no-such-database.sqlite")
        self.assertFalse(os.path.exists(missing_db))
        output_path = os.path.join(self.workdir, "already.json")
        sentinel = b"do not touch\n"
        with open(output_path, "wb") as f:
            f.write(sentinel)

        result = self.run_quote(
            "export", "--db", missing_db, "--number", NUMBER,
            "--output", output_path,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "", msg="失败时标准输出必须为空")
        self.assertEqual(
            result.stderr, f"{EXISTS_ERROR_PREFIX}{output_path}\n",
            msg="目标已存在的检查必须先于数据库读取，报告目标已存在",
        )
        self.assertNotIn("数据库", result.stderr)
        with open(output_path, "rb") as f:
            self.assertEqual(f.read(), sentinel, msg="原文件字节必须保持不变")
        self.assertFalse(
            os.path.exists(missing_db), msg="不得因失败路径创建数据库文件"
        )

    def test_unknown_number_reports_not_found_and_creates_nothing(self):
        output_name = "unknown.json"
        output_path = os.path.join(self.workdir, output_name)
        result = self.run_quote(
            "export", "--db", self.db_path, "--number", UNKNOWN_NUMBER,
            "--output", output_path,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "", msg="失败时标准输出必须为空")
        self.assertEqual(
            result.stderr, f"{NOT_FOUND_PREFIX}{UNKNOWN_NUMBER}\n",
            msg="标准错误应说明报价编号不存在并带编号",
        )
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(
            os.path.exists(output_path), msg="未知编号不得创建输出文件"
        )
        self.assert_database_unchanged(self.db_path, self.before, "未知编号的导出")
        self.assert_no_sidecars(self.db_path)

    def test_missing_database_reports_read_error_and_creates_nothing(self):
        db_name = "absent.sqlite"
        db_path = os.path.join(self.workdir, db_name)
        output_name = "should-not-exist.json"
        output_path = os.path.join(self.workdir, output_name)
        self.assertFalse(os.path.exists(db_path))

        result = self.run_quote(
            "export", "--db", db_name, "--number", NUMBER,
            "--output", output_name,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "", msg="失败时标准输出必须为空")
        self.assert_single_line_error(result.stderr, DB_ERROR_PREFIX, db_name)

        self.assertFalse(os.path.exists(db_path), msg="不得创建数据库文件")
        self.assertFalse(os.path.exists(output_path), msg="不得创建输出文件")

    def test_missing_parent_directory_reports_write_error_and_creates_nothing(self):
        missing_dir = os.path.join(self.workdir, "missing-dir")
        output_name = os.path.join("missing-dir", "exported.json")
        output_path = os.path.join(missing_dir, "exported.json")
        self.assertFalse(os.path.exists(missing_dir))

        result = self.run_quote(
            "export", "--db", self.db_path, "--number", NUMBER,
            "--output", output_name,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "", msg="失败时标准输出必须为空")
        self.assert_single_line_error(result.stderr, WRITE_ERROR_PREFIX, output_name)

        self.assertFalse(
            os.path.exists(missing_dir), msg="不得创建缺失的输出父目录"
        )
        self.assertFalse(os.path.exists(output_path), msg="不得创建输出文件")

        # 数据库读取发生在写入之前且为只读：源库结构与业务记录保持一致。
        self.assert_database_unchanged(self.db_path, self.before, "父目录缺失的导出")
        self.assert_no_sidecars(self.db_path)


# 项目目录清洁性：测试开始前记录项目根内容，全部用例结束后不得新增任何文件。
_PROJECT_ROOT_ENTRIES_AT_START = set(os.listdir(PROJECT_ROOT))


def tearDownModule():
    leftovers = set(os.listdir(PROJECT_ROOT)) - _PROJECT_ROOT_ENTRIES_AT_START
    assert not leftovers, f"测试在项目根目录留下了文件: {sorted(leftovers)}"


if __name__ == "__main__":
    unittest.main()
