"""export --output 写入新文件的既有行为回归测试。

仅依赖 Python 标准库。所有用例在独立的临时目录中通过 README 记载的
公开命令（save / export）操作 quote.py 子进程，使用固定合成报价，
不替换导出处理函数来模拟成功，也不读取项目内业务数据，不在项目目录
留下 JSON、SQLite 或其他文件；临时目录在用例结束或失败时自动清理。

覆盖范围限定在文件导出：
  - 成功：退出 0、标准错误为空、标准输出仅为目标路径加换行；新文件为
    无 BOM 的 UTF-8、完整单行 JSON 加一个 LF，字节逐字节等于测试内
    写死的固定预期，且与省略 --output 时的标准输出字节一致；
  - 失败：目标已存在（优先于数据库读取）、未知编号、数据库不存在、
    输出父目录不存在，均退出 1、标准输出为空、标准错误带路径与原因，
    不创建数据库/目录/输出文件，源库结构与业务记录保持不变。

运行方式（项目根目录）：
    python3 -m unittest discover -s tests
全部通过时退出码为 0，任一失败时非零退出并指出对应样例与预期。
"""

import codecs
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUOTE_PY = os.path.join(PROJECT_ROOT, "quote.py")

# ---- 固定验收样例 Q-FILE-001：客户覆盖首尾空格、尖括号与与号；
# ---- 说明覆盖首尾空格与换行；两条明细说明相同（必须保留为独立两行）。----

NUMBER = "Q-FILE-001"
CUSTOMER = " 演示<甲>&乙 "
NOTE = " 说明\n第二行 "
ITEMS = [
    {"description": "服务", "quantity": 2, "unit_price": 1250},
    {"description": "服务", "quantity": 1, "unit_price": 300},
]

PAYLOAD = {"number": NUMBER, "customer": CUSTOMER, "note": NOTE, "items": ITEMS}

UNKNOWN_NUMBER = "Q-FILE-NO-SUCH"

# 固定预期：由上面的固定样例逐字符推出、写死在测试中。注意 note 内的
# 换行在 JSON 中必须是转义序列反斜杠+n（源文件中写作 \\n），整份文件
# 唯一的真实 LF 是末尾换行。
EXPECTED_JSON_LINE = (
    '{"number": "Q-FILE-001", '
    '"customer": " 演示<甲>&乙 ", '
    '"items": ['
    '{"description": "服务", "quantity": 2, "unit_price": 1250}, '
    '{"description": "服务", "quantity": 1, "unit_price": 300}'
    '], '
    '"note": " 说明\\n第二行 "}'
)
EXPECTED_FILE_BYTES = (EXPECTED_JSON_LINE + "\n").encode("utf-8")

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

    def run_quote(self, *cli_args):
        """在临时目录中以独立进程运行 quote.py 的公开命令（文本模式）。"""
        return subprocess.run(
            [sys.executable, QUOTE_PY, *cli_args],
            capture_output=True,
            text=True,
            cwd=self.workdir,
        )

    def run_quote_bytes(self, *cli_args):
        """以字节方式捕获，便于逐字节断言标准输出、编码与换行。"""
        return subprocess.run(
            [sys.executable, QUOTE_PY, *cli_args],
            capture_output=True,
            cwd=self.workdir,
        )

    def save_sample(self, db_name="quotes.sqlite", input_name="quote.json"):
        """保存固定样例并断言保存成功，返回数据库绝对路径。"""
        db_path = os.path.join(self.workdir, db_name)
        input_path = os.path.join(self.workdir, input_name)
        with open(input_path, "w", encoding="utf-8") as f:
            json.dump(PAYLOAD, f, ensure_ascii=False)
        result = self.run_quote("save", "--db", db_path, "--input", input_path)
        self.assertEqual(
            result.returncode, 0,
            msg=f"固定样例 {NUMBER} 保存应成功: {result.stderr}",
        )
        self.assertEqual(result.stdout, f"{NUMBER}\n")
        self.assertEqual(result.stderr, "", msg="保存成功时标准错误必须为空")
        return db_path

    def export_to_file(self, db_path, number, output_path):
        return self.run_quote(
            "export", "--db", db_path, "--number", number, "--output", output_path
        )

    def snapshot(self, db_path):
        """直接读取 SQLite 快照：(quotes 行, items 行含内部标识, 表结构)。"""
        conn = sqlite3.connect(db_path)
        try:
            quotes = conn.execute(
                "SELECT number, customer, total, note FROM quotes ORDER BY number"
            ).fetchall()
            items = conn.execute(
                "SELECT id, quote_number, position, description, quantity, "
                "unit_price, line_amount FROM items ORDER BY id"
            ).fetchall()
            master = conn.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY name"
            ).fetchall()
        finally:
            conn.close()
        return quotes, items, master

    def assert_single_error_line(self, stderr):
        self.assertEqual(stderr.count("\n"), 1, msg="标准错误必须只有一行")
        self.assertTrue(stderr.endswith("\n"), msg="标准错误必须以换行结束")
        self.assertNotIn("Traceback", stderr, msg="不得显示异常堆栈")

    def assert_path_reason_error(self, stderr, prefix, path_arg):
        """断言“前缀 路径: 原因”单行格式，且原因非空。"""
        self.assertTrue(
            stderr.startswith(prefix),
            msg=f"标准错误应以 {prefix!r} 开头，实际: {stderr!r}",
        )
        remainder = stderr[len(prefix):]
        self.assertTrue(
            remainder.startswith(f"{path_arg}: ") and remainder.endswith("\n"),
            msg=f"标准错误应为“路径: 原因”单行格式，实际: {stderr!r}",
        )
        reason = remainder[len(path_arg) + 2:-1]
        self.assertTrue(reason.strip(), msg="失败原因不能为空")
        self.assert_single_error_line(stderr)


class ExportToFileSuccessTestCase(ExportFileTestBase):
    """成功路径：新文件内容逐字节等于固定预期，源库只读不变。"""

    def test_export_to_new_file_matches_fixed_bytes(self):
        db_path = self.save_sample()
        before = self.snapshot(db_path)

        # 固定预期常量自检：它本身是规范的单行 JSON（反序列化再序列化
        # 不变），且解析结果就是固定样例；防止测试常量写错。真正的验收
        # 仍以实际输出逐字节等于该常量为准，而不是两次实际输出互相比对。
        self.assertEqual(
            json.dumps(json.loads(EXPECTED_JSON_LINE), ensure_ascii=False),
            EXPECTED_JSON_LINE,
        )
        self.assertEqual(
            json.loads(EXPECTED_JSON_LINE),
            {"number": NUMBER, "customer": CUSTOMER, "note": NOTE, "items": ITEMS},
        )

        output_path = os.path.join(self.workdir, "exported.json")
        entries_before = set(os.listdir(self.workdir))

        result = self.export_to_file(db_path, NUMBER, output_path)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "", msg="导出成功时标准错误必须为空")
        self.assertEqual(
            result.stdout, f"{output_path}\n",
            msg="标准输出只能包含传入的目标路径和末尾换行",
        )
        self.assertNotIn("{", result.stdout, msg="标准输出不得包含 JSON 内容")

        # 新文件确实被创建，除此之外没有其他多余条目。
        self.assertTrue(os.path.isfile(output_path))
        self.assertEqual(
            set(os.listdir(self.workdir)) - entries_before, {"exported.json"},
        )

        with open(output_path, "rb") as f:
            data = f.read()

        # 逐字节等于固定预期：无 BOM 的 UTF-8、完整单行 JSON 加一个 LF。
        self.assertEqual(data, EXPECTED_FILE_BYTES, msg="文件字节必须等于固定预期")
        self.assertFalse(
            data.startswith(codecs.BOM_UTF8), msg="文件不得带 UTF-8 BOM"
        )
        self.assertTrue(data.endswith(b"\n"), msg="文件必须以 LF 结束")
        self.assertEqual(data.count(b"\n"), 1, msg="整份文件只能有末尾一个 LF")
        self.assertNotIn(b"\r", data, msg="不得使用 CRLF 换行")
        data.decode("utf-8")  # 必须是完整合法的 UTF-8（严格模式抛错即失败）

        quote = json.loads(data.decode("utf-8"))

        # 文本空白与换行逐字符保留。
        self.assertEqual(quote["number"], NUMBER)
        self.assertEqual(
            quote["customer"], CUSTOMER,
            msg="客户名必须保留首尾空格、尖括号与与号原字符",
        )
        self.assertEqual(
            quote["note"], NOTE,
            msg="说明必须逐字符保留首尾空格与内部换行",
        )
        self.assertTrue(quote["customer"].startswith(" "))
        self.assertTrue(quote["customer"].endswith(" "))
        self.assertTrue(quote["note"].startswith(" "))
        self.assertTrue(quote["note"].endswith(" "))
        self.assertIn("\n", quote["note"])

        # 明细顺序、整数类型与重复说明的独立两行。
        self.assertEqual(len(quote["items"]), 2)
        self.assertEqual(
            [item["description"] for item in quote["items"]],
            ["服务", "服务"],
            msg="重复说明必须保留为顺序不变的两条独立明细",
        )
        self.assertEqual(
            [(item["quantity"], item["unit_price"]) for item in quote["items"]],
            [(2, 1250), (1, 300)],
        )
        for item in quote["items"]:
            self.assertIsInstance(item["quantity"], int)
            self.assertNotIsInstance(item["quantity"], bool)
            self.assertIsInstance(item["unit_price"], int)
            self.assertNotIsInstance(item["unit_price"], bool)

        # 字段白名单：不含内部标识、行金额或合计。
        self.assertEqual(
            set(quote.keys()), {"number", "customer", "items", "note"},
        )
        for item in quote["items"]:
            self.assertEqual(
                set(item.keys()), {"description", "quantity", "unit_price"},
                msg="明细不得包含 id、line_amount 等内部字段",
            )

        # 尖括号与与号保留原字符，绝不做 HTML 转义或 Unicode 转义。
        text = data.decode("utf-8")
        self.assertIn(" 演示<甲>&乙 ", text)
        for entity in ("&lt;", "&gt;", "&amp;", "&quot;", "&#39;"):
            self.assertNotIn(entity, text, msg=f"导出不得做 HTML 转义: {entity}")
        self.assertNotIn("\\u003c", text)
        self.assertNotIn("\\u003e", text)
        self.assertNotIn("\\u0026", text)

        # 成功导出只读源库：结构与业务记录保持一致，不留 WAL/SHM 侧车。
        self.assertEqual(self.snapshot(db_path), before)
        for sidecar in ("quotes.sqlite-wal", "quotes.sqlite-shm"):
            self.assertFalse(
                os.path.exists(os.path.join(self.workdir, sidecar)),
                msg="只读导出不应留下 WAL/SHM 侧车文件",
            )

    def test_file_bytes_equal_stdout_bytes_without_output(self):
        db_path = self.save_sample()

        # 同一份报价：一次写入新文件，一次省略 --output 走标准输出，
        # 两者都必须逐字节等于固定预期（固定预期才是验收基准）。
        output_path = os.path.join(self.workdir, "via-file.json")
        file_result = self.export_to_file(db_path, NUMBER, output_path)
        self.assertEqual(file_result.returncode, 0, msg=file_result.stderr)

        entries_before = set(os.listdir(self.workdir))
        stdout_result = self.run_quote_bytes(
            "export", "--db", db_path, "--number", NUMBER
        )
        self.assertEqual(stdout_result.returncode, 0, msg=stdout_result.stderr.decode())
        self.assertEqual(stdout_result.stderr, b"")

        with open(output_path, "rb") as f:
            file_bytes = f.read()
        self.assertEqual(file_bytes, EXPECTED_FILE_BYTES)
        self.assertEqual(stdout_result.stdout, EXPECTED_FILE_BYTES)
        self.assertEqual(
            file_bytes, stdout_result.stdout,
            msg="文件导出字节必须与省略 --output 时的标准输出字节一致",
        )

        # 省略 --output 保留既有行为：只输出到标准输出，不创建任何文件。
        self.assertEqual(
            set(os.listdir(self.workdir)), entries_before,
            msg="省略 --output 不得新建任何文件",
        )


class ExportToFileFailureTestCase(ExportFileTestBase):
    """确定的失败结果：退出 1、标准输出为空、不改动文件系统与源库。"""

    def setUp(self):
        super().setUp()
        self.db_path = self.save_sample()
        self.before = self.snapshot(self.db_path)

    def assert_source_db_unchanged(self):
        self.assertEqual(
            self.snapshot(self.db_path), self.before,
            msg="失败导出前后源库的结构与业务记录必须保持一致",
        )

    def test_existing_target_rejected_and_preserved(self):
        output_path = os.path.join(self.workdir, "already.json")
        sentinel = b"preexisting \xe5\x86\x85\xe5\xae\xb9 \x00\nbytes"
        with open(output_path, "wb") as f:
            f.write(sentinel)

        result = self.export_to_file(self.db_path, NUMBER, output_path)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "", msg="失败时标准输出必须为空")
        self.assertEqual(
            result.stderr, f"{EXISTS_ERROR_PREFIX}{output_path}\n",
            msg="标准错误应说明输出文件已存在并带路径",
        )
        with open(output_path, "rb") as f:
            self.assertEqual(f.read(), sentinel, msg="原有目标文件字节不得改动")
        self.assert_source_db_unchanged()

    def test_existing_target_reported_before_missing_database(self):
        # 目标已存在且数据库也不存在：仍必须先报告目标已存在，
        # 既不读取数据库，也不改动已存在的目标。
        output_path = os.path.join(self.workdir, "already.json")
        sentinel = b"keep me\n"
        with open(output_path, "wb") as f:
            f.write(sentinel)
        missing_db = os.path.join(self.workdir, "absent.sqlite")
        self.assertFalse(os.path.exists(missing_db))

        result = self.export_to_file(missing_db, NUMBER, output_path)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "", msg="失败时标准输出必须为空")
        self.assertEqual(
            result.stderr, f"{EXISTS_ERROR_PREFIX}{output_path}\n",
            msg="目标已存在的检查必须先于数据库读取",
        )
        self.assertFalse(
            os.path.exists(missing_db), msg="此路径下不得创建数据库文件"
        )
        with open(output_path, "rb") as f:
            self.assertEqual(f.read(), sentinel, msg="原有目标文件字节不得改动")

    def test_unknown_number_does_not_create_output(self):
        output_path = os.path.join(self.workdir, "missing-number.json")
        entries_before = set(os.listdir(self.workdir))

        result = self.export_to_file(self.db_path, UNKNOWN_NUMBER, output_path)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "", msg="失败时标准输出必须为空")
        self.assertEqual(
            result.stderr, f"{NOT_FOUND_PREFIX}{UNKNOWN_NUMBER}\n",
            msg="标准错误应说明报价编号不存在",
        )
        self.assertFalse(
            os.path.exists(output_path), msg="未知编号不得创建输出文件"
        )
        self.assertEqual(
            set(os.listdir(self.workdir)), entries_before,
            msg="未知编号不得在工作目录留下任何新条目",
        )
        self.assert_source_db_unchanged()

    def test_missing_database_creates_nothing(self):
        db_arg = "absent.sqlite"
        missing_db = os.path.join(self.workdir, db_arg)
        output_path = os.path.join(self.workdir, "should-not-exist.json")
        self.assertFalse(os.path.exists(missing_db))
        self.assertFalse(os.path.exists(output_path))
        entries_before = set(os.listdir(self.workdir))

        result = self.export_to_file(db_arg, NUMBER, output_path)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "", msg="失败时标准输出必须为空")
        self.assert_path_reason_error(result.stderr, DB_ERROR_PREFIX, db_arg)
        self.assertIn(
            "无法读取数据库", result.stderr,
            msg="标准错误应说明无法读取数据库",
        )

        self.assertFalse(
            os.path.exists(missing_db), msg="数据库不存在时不得创建数据库文件"
        )
        self.assertFalse(
            os.path.exists(output_path), msg="数据库不可读时不得创建输出文件"
        )
        for sidecar in ("absent.sqlite-wal", "absent.sqlite-shm"):
            self.assertFalse(os.path.exists(os.path.join(self.workdir, sidecar)))
        self.assertEqual(
            set(os.listdir(self.workdir)), entries_before,
            msg="数据库不存在时不得留下任何文件系统条目",
        )

    def test_missing_parent_directory_creates_nothing(self):
        missing_dir = os.path.join(self.workdir, "no-such-dir")
        output_path = os.path.join(missing_dir, "exported.json")
        self.assertFalse(os.path.exists(missing_dir))
        entries_before = set(os.listdir(self.workdir))

        result = self.export_to_file(self.db_path, NUMBER, output_path)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "", msg="失败时标准输出必须为空")
        self.assert_path_reason_error(
            result.stderr, WRITE_ERROR_PREFIX, output_path
        )
        self.assertIn(
            "无法写入输出文件", result.stderr,
            msg="标准错误应说明无法写入输出文件",
        )

        self.assertFalse(
            os.path.exists(missing_dir), msg="不得自动创建缺失的输出父目录"
        )
        self.assertFalse(
            os.path.exists(output_path), msg="父目录缺失时不得创建输出文件"
        )
        self.assertEqual(
            set(os.listdir(self.workdir)), entries_before,
            msg="父目录缺失时不得留下任何文件系统条目",
        )
        self.assert_source_db_unchanged()


# 项目目录清洁性：测试开始前记录项目根内容，全部用例结束后不得新增任何文件。
_PROJECT_ROOT_ENTRIES_AT_START = set(os.listdir(PROJECT_ROOT))


def tearDownModule():
    leftovers = set(os.listdir(PROJECT_ROOT)) - _PROJECT_ROOT_ENTRIES_AT_START
    assert not leftovers, f"测试在项目根目录留下了文件: {sorted(leftovers)}"


if __name__ == "__main__":
    unittest.main()
