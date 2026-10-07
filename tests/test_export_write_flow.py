"""export --output 在短写与写入中止时的写入回归测试。

仅依赖 Python 标准库。所有用例在独立的临时目录中通过 README 记载的
公开命令（save / export）操作 quote.py 子进程，使用合成固定报价
Q-WRITE-001，不读取项目内业务数据，也不在项目目录留下 JSON、SQLite
或其他文件；临时目录在用例结束或失败时自动清理。

本文件只覆盖 export 的 --output 路径在三种写入情形下的既有行为：
  1. 每次 os.write 只接受少量字节（短写）但最终写完整份文档：退出 0，
     文件与独立写死的固定预期逐字节一致；
  2. 目标创建后首次 os.write 返回 0 字节（写入未取得进展）：退出 1，
     保留空文件，标准错误为一行写入失败原因；
  3. 已写出固定长度前缀后 os.write 抛 OSError（写入中止）：退出 1，
     目标恰好保留此前已写出的前缀，标准错误携带预设异常原因。
故障不依赖真实磁盘损坏或特定平台权限：用例在临时目录生成一个运行器
脚本，它在子进程内加载 quote.py 并修补 os.write，再以相同的公开参数
调用 quote.main。运行器本身不是被测代码，被测的仍是 quote.py 的
export 流程；产品源码、数据库格式与其他公开入口均不改动。

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

# ---- 固定验收样例 Q-WRITE-001：客户与说明覆盖尖括号、与号、首尾空格与换行；
# ---- 两条明细说明相同（同为“服务”），数量与单价为整数 ----

NUMBER = "Q-WRITE-001"
CUSTOMER = " 演示<甲>&乙 "
# JSON 字符串 " 说明\n第二行 "：首尾各一个空格，内部一个换行。
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
    '{"number": "Q-WRITE-001", "customer": " 演示<甲>&乙 ", '
    '"items": [{"description": "服务", "quantity": 2, "unit_price": 1250}, '
    '{"description": "服务", "quantity": 1, "unit_price": 300}], '
    '"note": " 说明\\n第二行 "}\n'
)
EXPECTED_EXPORT_BYTES = EXPECTED_EXPORT_DOCUMENT.encode("utf-8")

# 每次写入只接受少量字节：3 字节，确保 224 字节文档经历数十次短写。
PARTIAL_CHUNK = 3
# 写入中止前固定写出的前缀长度（前 16 字节均为 ASCII，单字节写入即可达成）。
PREFIX_LENGTH = 16
FAIL_REASON = "模拟磁盘写入中止"

WRITE_ERROR_PREFIX = "错误: 无法写入输出文件 "

# 运行器：在子进程内修补 os.write 后调用 quote.main。只拦截写向导出目标
# 的 fd（通过 os.open 记录），对其他 fd（无）保持原样。模式：
#   partial     写向目标时每次最多接受 PARTIAL_CHUNK 字节，最终写完整份
#   zero        写向目标时一律返回 0 字节（写入未取得进展）
#   fail-after  写向目标时先逐字节累计 PREFIX_LENGTH 字节，随后抛 OSError
RUNNER = r'''
import importlib.util
import os
import sys

MODE = sys.argv[1]
QUOTE_PY = sys.argv[2]
OUTPUT_PATH = sys.argv[3]
CLI_ARGS = sys.argv[4:]

real_open = os.open
real_write = os.write
target = {"fd": None}
state = {"written": 0, "calls": 0}


def fake_open(path, flags, *args, **kwargs):
    fd = real_open(path, flags, *args, **kwargs)
    if path == OUTPUT_PATH:
        target["fd"] = fd
    return fd


def fake_write(fd, data):
    if fd != target["fd"]:
        return real_write(fd, data)
    state["calls"] += 1
    if MODE == "partial":
        # 每次只接受少量字节；剩余字节由 write_new_file 的循环继续写。
        return real_write(fd, bytes(data[:%d]))
    if MODE == "zero":
        # 目标创建后的第一次（以及每次）写入都未取得进展。
        return 0
    if MODE == "fail-after":
        # 先逐字节写出固定长度前缀，凑满后于下一次写入中止。
        remaining = %d - state["written"]
        if remaining > 0:
            take = min(remaining, len(data))
            written = real_write(fd, bytes(data[:take]))
            state["written"] += written
            return written
        raise OSError(%r)
    raise SystemExit(f"未知模式: {MODE}")


os.open = fake_open
os.write = fake_write

spec = importlib.util.spec_from_file_location("quote", QUOTE_PY)
quote = importlib.util.module_from_spec(spec)
spec.loader.exec_module(quote)
sys.exit(quote.main(CLI_ARGS))
''' % (PARTIAL_CHUNK, PREFIX_LENGTH, FAIL_REASON)


def make_payload():
    return {
        "number": NUMBER,
        "customer": CUSTOMER,
        "note": NOTE,
        "items": [dict(item) for item in ITEMS],
    }


class ExportWriteTestBase(unittest.TestCase):
    """每个用例使用独立临时目录，只通过公开命令行入口驱动 quote.py。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="quote-export-write-test-")
        self.addCleanup(self._tmp.cleanup)
        self.workdir = self._tmp.name
        self.runner_path = os.path.join(self.workdir, "patched_runner.py")
        with open(self.runner_path, "w", encoding="utf-8") as f:
            f.write(RUNNER)

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

    def run_patched_export(self, mode, db_path, number, output_path):
        """以修补后的 os.write 运行 export 公开命令。"""
        return subprocess.run(
            [
                sys.executable, self.runner_path, mode, QUOTE_PY, output_path,
                "export", "--db", db_path, "--number", number,
                "--output", output_path,
            ],
            capture_output=True,
            text=True,
            cwd=self.workdir,
        )

    def save_demo_quote(self, db_name="demo.sqlite", input_name="quote.json"):
        db_path = os.path.join(self.workdir, db_name)
        input_path = os.path.join(self.workdir, input_name)
        with open(input_path, "w", encoding="utf-8") as f:
            json.dump(make_payload(), f, ensure_ascii=False)
        result = self.run_quote("save", "--db", db_path, "--input", input_path)
        self.assertEqual(
            result.returncode, 0,
            msg=f"样例 {NUMBER} 保存应成功: {result.stderr}",
        )
        self.assertEqual(result.stdout, f"{NUMBER}\n")
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

    def assert_single_line_write_error(self, stderr, output_path, reason_part):
        """断言写入失败的单行错误：固定前缀、含目标路径与非空原因、无堆栈。"""
        self.assertTrue(
            stderr.startswith(WRITE_ERROR_PREFIX),
            msg=f"标准错误应以 {WRITE_ERROR_PREFIX!r} 开头，实际: {stderr!r}",
        )
        self.assertIn(
            output_path, stderr,
            msg=f"标准错误必须包含目标路径 {output_path!r}，实际: {stderr!r}",
        )
        self.assertIn(
            reason_part, stderr,
            msg=f"标准错误必须说明失败原因 {reason_part!r}，实际: {stderr!r}",
        )
        self.assertEqual(stderr.count("\n"), 1, msg="标准错误必须只有一行")
        self.assertTrue(stderr.endswith("\n"), msg="标准错误应以换行结束")
        self.assertNotIn("Traceback", stderr, msg="不得显示异常堆栈")

    def assert_parsed_quote_matches_fixed_input(self, file_bytes):
        """解析导出字节：文本、明细顺序、整数数量/单价与固定输入一致。"""
        quote = json.loads(file_bytes.decode("utf-8"))
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

        self.assertEqual(len(quote["items"]), 2, msg="重复说明仍必须有两行明细")
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

        document = file_bytes.decode("utf-8")
        for forbidden in ("id", "line_amount", "total", "position"):
            self.assertNotIn(forbidden, quote)
            for item in quote["items"]:
                self.assertNotIn(forbidden, item)
            self.assertNotIn(
                f'"{forbidden}"', document,
                msg=f"导出原文不得出现字段 {forbidden!r}",
            )


class ExportShortWriteSuccessTestCase(ExportWriteTestBase):
    """每次写入只接受少量字节但最终完成：结果与固定预期逐字节一致。"""

    def test_short_writes_complete_and_match_fixed_bytes(self):
        # 固定预期自检：常量确实由样例数据导出，且长度足以覆盖多次短写。
        expected_quote = {
            "number": NUMBER, "customer": CUSTOMER,
            "items": [dict(item) for item in ITEMS], "note": NOTE,
        }
        self.assertEqual(
            EXPECTED_EXPORT_BYTES,
            (json.dumps(expected_quote, ensure_ascii=False) + "\n").encode("utf-8"),
            msg="写死的固定预期必须与由固定输入独立推出的字节一致",
        )
        self.assertGreater(
            len(EXPECTED_EXPORT_BYTES), PARTIAL_CHUNK * 10,
            msg="文档应足够大以经历多次短写",
        )
        self.assertFalse(EXPECTED_EXPORT_BYTES.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(EXPECTED_EXPORT_BYTES.count(b"\n"), 1)

        db_path = self.save_demo_quote()
        before = self.snapshot(db_path)
        output_path = os.path.join(self.workdir, "copy.json")

        # 修补为每次只写少量字节：仍应最终写完整份文档。
        result = self.run_patched_export("partial", db_path, NUMBER, output_path)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "", msg="导出成功时标准错误必须为空")
        self.assertEqual(
            result.stdout, f"{output_path}\n",
            msg="标准输出只能是传入的输出路径 copy.json 加一个换行",
        )

        self.assertTrue(os.path.isfile(output_path), msg="导出必须在新路径创建文件")
        with open(output_path, "rb") as f:
            file_bytes = f.read()

        # 编码与物理形态：无 BOM 的 UTF-8，完整单行 JSON 加恰好一个 LF。
        self.assertFalse(
            file_bytes.startswith(b"\xef\xbb\xbf"),
            msg="导出文件不得带 UTF-8 BOM",
        )
        file_bytes.decode("utf-8")  # 必须是完整合法的 UTF-8
        self.assertTrue(file_bytes.endswith(b"\n"), msg="文件必须以 LF 结束")
        self.assertEqual(file_bytes.count(b"\n"), 1, msg="整篇只能有末尾一个换行")
        self.assertNotIn(b"\r", file_bytes, msg="不得使用 CRLF 换行")
        self.assertEqual(
            file_bytes[:-1].find(b"\n"), -1,
            msg="末尾 LF 之前不得再有换行（必须是完整单行 JSON）",
        )

        # 文件字节逐字节等于写死的固定预期（不是用 quote.py 现算答案，
        # 也不是与另一次实际输出互相比对）。
        self.assertEqual(
            file_bytes, EXPECTED_EXPORT_BYTES,
            msg="经历多次短写后的文件必须与固定预期逐字节一致",
        )

        # 解析后：文本、明细顺序、整数数量/单价与固定输入一致。
        self.assert_parsed_quote_matches_fixed_input(file_bytes)

        # 省略 --output 时仍输出原有 JSON：其标准输出字节独立等于固定预期，
        # 再与文件字节做三方一致（固定预期 == 文件 == 标准输出）。
        stdout_result = self.run_quote_bytes(
            "export", "--db", db_path, "--number", NUMBER
        )
        self.assertEqual(stdout_result.returncode, 0, msg=stdout_result.stderr)
        self.assertEqual(stdout_result.stderr, b"")
        self.assertEqual(
            stdout_result.stdout, EXPECTED_EXPORT_BYTES,
            msg="省略 --output 的标准输出必须逐字节等于固定预期",
        )
        self.assertEqual(
            stdout_result.stdout, file_bytes,
            msg="文件字节必须与省略 --output 时的标准输出字节一致",
        )

        # 成功导出前后，源库的结构与业务记录保持一致，且不留 WAL/SHM。
        self.assert_database_unchanged(db_path, before, "短写完成的文件导出")
        self.assert_no_sidecars(db_path)


class ExportWriteAbortTestCase(ExportWriteTestBase):
    """写入中止的两种确定失败：退出 1、标准输出为空、单行错误、保留现场。"""

    def setUp(self):
        super().setUp()
        self.db_path = self.save_demo_quote()
        self.before = self.snapshot(self.db_path)

    def test_first_write_returning_zero_fails_and_keeps_empty_file(self):
        output_path = os.path.join(self.workdir, "copy.json")
        result = self.run_patched_export("zero", self.db_path, NUMBER, output_path)

        self.assertEqual(
            result.returncode, 1,
            msg=f"首次写入返回 0 字节应退出 1，实际 stdout={result.stdout!r}",
        )
        self.assertEqual(result.stdout, "", msg="失败时标准输出必须为空")
        self.assert_single_line_write_error(
            result.stderr, output_path, "未取得进展"
        )

        # 目标已由 O_EXCL 创建但从未写入：保留空文件（不删除、不覆盖），
        # 沿用既有失败语义。
        self.assertTrue(
            os.path.isfile(output_path), msg="零字节情况应保留已创建的空目标文件"
        )
        with open(output_path, "rb") as f:
            self.assertEqual(f.read(), b"", msg="首次写入返回 0 时目标必须为空文件")

        # 失败前后源库的结构与业务记录保持一致，且不留 WAL/SHM。
        self.assert_database_unchanged(
            self.db_path, self.before, "首次写入零字节的文件导出"
        )
        self.assert_no_sidecars(self.db_path)

    def test_oserror_after_fixed_prefix_fails_and_keeps_exact_prefix(self):
        # 前置自检：固定前缀落在文档的纯 ASCII 区域，单字节写入可精确达成。
        self.assertLessEqual(
            PREFIX_LENGTH, len(EXPECTED_EXPORT_BYTES),
            msg="固定前缀长度不得超过完整文档长度",
        )
        expected_prefix = EXPECTED_EXPORT_BYTES[:PREFIX_LENGTH]
        self.assertTrue(
            all(byte < 0x80 for byte in expected_prefix),
            msg="固定前缀应为纯 ASCII，使逐字节写入与文档字节一一对应",
        )

        output_path = os.path.join(self.workdir, "copy.json")
        result = self.run_patched_export(
            "fail-after", self.db_path, NUMBER, output_path
        )

        self.assertEqual(
            result.returncode, 1,
            msg="固定前缀后 OSError 应退出 1 而不是部分成功",
        )
        self.assertEqual(result.stdout, "", msg="失败时标准输出必须为空")
        self.assert_single_line_write_error(
            result.stderr, output_path, FAIL_REASON
        )

        # 目标恰好保留中止前已写出的固定长度前缀，逐字节等于完整文档的前缀；
        # 不多一个字节也不少一个字节，且不删除/不覆盖。
        self.assertTrue(
            os.path.isfile(output_path), msg="中途异常应保留已写出前缀的目标文件"
        )
        with open(output_path, "rb") as f:
            leftover = f.read()
        self.assertEqual(
            leftover, expected_prefix,
            msg=f"中止后必须恰好保留 {PREFIX_LENGTH} 字节固定前缀",
        )
        self.assertEqual(
            len(leftover), PREFIX_LENGTH,
            msg="保留内容长度必须恰好等于固定前缀长度",
        )
        self.assertTrue(
            EXPECTED_EXPORT_BYTES.startswith(leftover),
            msg="保留的前缀必须是完整预期文档的开头",
        )

        # 失败前后源库的结构与业务记录保持一致，且不留 WAL/SHM。
        self.assert_database_unchanged(
            self.db_path, self.before, "固定前缀后中止的文件导出"
        )
        self.assert_no_sidecars(self.db_path)


# 项目目录清洁性：测试开始前记录项目根内容，全部用例结束后不得新增任何文件。
_PROJECT_ROOT_ENTRIES_AT_START = set(os.listdir(PROJECT_ROOT))


def tearDownModule():
    leftovers = set(os.listdir(PROJECT_ROOT)) - _PROJECT_ROOT_ENTRIES_AT_START
    assert not leftovers, f"测试在项目根目录留下了文件: {sorted(leftovers)}"


if __name__ == "__main__":
    unittest.main()
