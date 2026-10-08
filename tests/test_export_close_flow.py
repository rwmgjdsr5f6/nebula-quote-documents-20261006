"""export --output 在输出文件关闭阶段失败时的回归测试。

仅依赖 Python 标准库。所有用例在独立的临时目录中通过 README 记载的
公开命令（save / export）操作 quote.py 子进程，使用合成固定报价
Q-CLOSE-001，不读取项目内业务数据，也不在项目目录留下 JSON、SQLite
或其他文件；临时目录在用例结束或失败时自动清理。

本文件只固定 export 的 --output 路径在关闭阶段的既有行为，不新增命令、
不改变产品行为：
  1. 正常对照：导出成功，退出 0，标准输出仅为传入路径加换行，标准错误
     为空，文件为无 BOM 的 UTF-8 单行 JSON 且末尾恰好一个 LF；
  2. 全部内容写完后仅 os.close 报告“模拟关闭失败”：退出 1、标准输出
     为空、标准错误只有一条以“错误: 无法写入输出文件”开头且含路径与
     该原因的单行信息，无异常堆栈；已写完的文件原样保留，存在完整文件
     不能判为成功；
  3. 同一次导出先接受 16 字节、再报告“模拟写入失败”、关闭时又报告
     “模拟关闭失败”：退出 1、标准输出为空，错误只保留最先发生的写入
     失败原因，不被关闭错误覆盖，也不输出第二条错误；文件恰好保留已
     接受的 16 字节，不删除、不补全。

关闭与写入故障通过子进程内的测试引导脚本确定性模拟：脚本先 import 产品
模块 quote.py（不修改其源码、不复制其代码），把 write_new_file 使用的
模块全局 os 替换为仅拦截 open/write/close 的薄包装，再原样调用公开入口
quote.main()。故障只作用于本次导出的输出文件描述符；数据库连接由
sqlite3 模块自行管理，不经过被替换的 quote.os，其打开与关闭不受影响。
不依赖真实磁盘损坏、磁盘满、信号中断或特定平台权限。

运行方式（项目根目录）：
    python3 -m unittest discover -s tests -p test_export_close_flow.py
全部通过时退出码为 0，任一失败时非零退出并指出对应样例与预期。
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUOTE_PY = os.path.join(PROJECT_ROOT, "quote.py")

# ---- 固定验收样例 Q-CLOSE-001：客户含首尾空格、尖括号与与号；说明含换行；
# ---- 两条明细说明相同，数量与单价为整数 ----

NUMBER = "Q-CLOSE-001"
CUSTOMER = " 演示<甲>&乙 "
NOTE = " 说明\n第二行 "
ITEMS = [
    {"description": "服务", "quantity": 2, "unit_price": 1250},
    {"description": "服务", "quantity": 1, "unit_price": 300},
]

PAYLOAD = {"number": NUMBER, "customer": CUSTOMER, "note": NOTE, "items": ITEMS}

DB_NAME = "demo.sqlite"
OUTPUT_NAME = "copy.json"

# ---- 固定预期导出文档：由样例数据按公开行为逐字符独立推出并写死 ----
# json.dumps 默认分隔符为 ", " 与 ": "，ensure_ascii=False 保留原文，
# 键顺序为 number、customer、items、note；note 内换行在 JSON 中是
# 反斜杠加 n 两个字符，整篇为一个物理行，末尾恰好一个 LF。
EXPECTED_EXPORT_DOCUMENT = (
    '{"number": "Q-CLOSE-001", "customer": " 演示<甲>&乙 ", '
    '"items": [{"description": "服务", "quantity": 2, "unit_price": 1250}, '
    '{"description": "服务", "quantity": 1, "unit_price": 300}], '
    '"note": " 说明\\n第二行 "}\n'
)
EXPECTED_EXPORT_BYTES = EXPECTED_EXPORT_DOCUMENT.encode("utf-8")

# 写入+关闭双重失败样例：关闭前恰好接受的字节数。该值肯定不在边界 0，
# 也短于完整文档，使文件保留的是明确的部分前缀。
ACCEPTED_BYTE_LENGTH = 16

# 预设的故障原因（中文，原样进入单行错误消息）。
CLOSE_REASON = "模拟关闭失败"
WRITE_REASON = "模拟写入失败"

WRITE_ERROR_PREFIX = "错误: 无法写入输出文件 "

# ---- 子进程引导脚本 ----
# 仅拦截 quote.write_new_file 经由模块全局 os 使用的 open/write/close：
# open 时记下真实文件描述符并返回一个固定的假 fd；write 按模式全部接受
# 或只接受前 fail_after 字节后抛出 OSError；close 对假 fd 先真正关闭再
# 抛出预设 OSError，对其他 fd 原样透传。os 的其他名字（O_WRONLY 等常量、
# path 等）全部透传给标准 os，产品其余代码与命令行参数解析保持原样。
# 数据库连接由 sqlite3 在 C 层管理，不经过 quote.os，不受故障影响。

_BOOTSTRAP_TEMPLATE = textwrap.dedent("""\
    import os
    import sys

    # 导入产品模块时不写字节码缓存，避免在项目根目录留下 __pycache__。
    sys.dont_write_bytecode = True
    sys.path.insert(0, {project_root!r})
    import quote

    REAL_OS = os

    class _FaultyOs:
        def __init__(self, mode, fail_after, write_reason, close_reason):
            self._mode = mode              # "close" | "write_close"
            self._fail_after = fail_after  # write_close：接受该字节数后抛错
            self._write_reason = write_reason
            self._close_reason = close_reason
            self._real_fd = None
            self._fake_fd = 7
            self.accepted_total = 0

        def __getattr__(self, name):
            # 非拦截名字（常量、path、environ 等）全部透传标准 os。
            return getattr(REAL_OS, name)

        def open(self, path, flags, mode=0o777):
            self._real_fd = REAL_OS.open(path, flags, mode)
            return self._fake_fd

        def write(self, fd, data):
            if self._mode == "write_close":
                # 恰好接受固定前缀：最后一次只接受凑齐前缀所需的少量字节
                # （可以少于本次传入长度），此后下一次写入即抛出预设 OSError。
                remaining = self._fail_after - self.accepted_total
                if remaining <= 0:
                    raise OSError(self._write_reason)
                data = data[:remaining]
            written = REAL_OS.write(self._real_fd, data)
            self.accepted_total += written
            return written

        def close(self, fd):
            if fd == self._fake_fd:
                # 先真正关闭输出文件（已写内容原样落盘），再报告关闭失败。
                REAL_OS.close(self._real_fd)
                raise OSError(self._close_reason)
            return REAL_OS.close(fd)

    quote.os = _FaultyOs({mode!r}, {fail_after!r}, {write_reason!r}, {close_reason!r})
    # python -c 时 sys.argv[0] 为 "-c"，业务参数从 sys.argv[1:] 开始。
    sys.exit(quote.main(sys.argv[1:]))
""")


def build_bootstrap(mode, fail_after=0):
    return _BOOTSTRAP_TEMPLATE.format(
        project_root=PROJECT_ROOT,
        mode=mode,
        fail_after=fail_after,
        write_reason=WRITE_REASON,
        close_reason=CLOSE_REASON,
    )


class ExportCloseTestBase(unittest.TestCase):
    """每个用例使用独立临时目录，只通过公开命令行入口驱动 quote.py。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="quote-export-close-test-")
        self.addCleanup(self._tmp.cleanup)
        self.workdir = self._tmp.name

    # ---- 基础设施 ----

    def _run(self, argv, bootstrap=None):
        """运行 quote.py 子进程。

        bootstrap 为 None 时直接执行产品脚本；否则通过 -c 引导脚本先注入
        确定性关闭/写入故障再调用公开入口 quote.main()。两种方式都以字节
        捕获，便于逐字节断言编码、换行与错误文本。
        """
        if bootstrap is None:
            cmd = [sys.executable, QUOTE_PY, *argv]
        else:
            cmd = [sys.executable, "-c", bootstrap, *argv]
        return subprocess.run(cmd, capture_output=True, cwd=self.workdir)

    def run_quote(self, *cli_args):
        return self._run(cli_args)

    def run_quote_with_fault(self, mode, *cli_args, **fault_kwargs):
        bootstrap = build_bootstrap(mode, **fault_kwargs)
        return self._run(cli_args, bootstrap=bootstrap)

    def write_input(self, payload, name="quote.json"):
        path = os.path.join(self.workdir, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        return path

    def save(self, db_name=DB_NAME, input_name="quote.json"):
        db_path = os.path.join(self.workdir, db_name)
        input_path = self.write_input(PAYLOAD, input_name)
        result = self.run_quote("save", "--db", db_path, "--input", input_path)
        self.assertEqual(
            result.returncode, 0,
            msg=f"样例 {NUMBER} 保存应成功: {result.stderr.decode('utf-8', 'replace')}",
        )
        self.assertEqual(result.stderr, b"", msg="保存成功时标准错误必须为空")
        return db_path

    def export_with_output(self, **fault):
        """对固定样例执行 export --output；fault 为空时走正常路径。"""
        cli = ("export", "--db", DB_NAME, "--number", NUMBER,
               "--output", OUTPUT_NAME)
        if fault:
            return self.run_quote_with_fault(fault.pop("mode"), *cli, **fault)
        return self.run_quote(*cli)

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

    def assert_export_content_matches_fixed_input(self, file_bytes):
        """解析导出字节并按固定输入逐项核对（文本、顺序、整数类型）。"""
        document = file_bytes.decode("utf-8")
        quote = json.loads(document)

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
            msg="明细顺序与数量必须与固定输入一致",
        )
        self.assertEqual(
            [item["unit_price"] for item in quote["items"]], [1250, 300],
            msg="明细单价必须与固定输入一致",
        )

        # 不输出行金额、合计或内部标识：解析后的对象无这些键，原文也不出现。
        for forbidden in ("id", "line_amount", "total", "position"):
            self.assertNotIn(forbidden, quote)
            for item in quote["items"]:
                self.assertNotIn(forbidden, item)
            self.assertNotIn(
                f'"{forbidden}"', document,
                msg=f"导出原文不得出现字段 {forbidden!r}",
            )

    def assert_write_error_envelope(self, result, expected_hint):
        """退出 1、标准输出为空；标准错误为单行且以固定前缀开头、带目标路径。"""
        self.assertEqual(result.returncode, 1, msg="关闭/写入失败时应退出 1")
        self.assertEqual(result.stdout, b"", msg="失败时标准输出必须为空")

        stderr = result.stderr.decode("utf-8")
        self.assertTrue(
            stderr.startswith(WRITE_ERROR_PREFIX),
            msg=f"标准错误应以 {WRITE_ERROR_PREFIX!r} 开头，实际: {stderr!r}",
        )
        self.assertIn(
            OUTPUT_NAME, stderr,
            msg="单行错误必须包含目标输出路径 copy.json",
        )
        self.assertEqual(
            stderr.count("\n"), 1,
            msg="标准错误必须只有一行（末尾一个换行）",
        )
        self.assertTrue(stderr.endswith("\n"), msg="单行错误应以换行结束")
        self.assertIn(expected_hint, stderr, msg=f"错误必须说明失败原因: {stderr!r}")
        self.assertNotIn("Traceback", stderr, msg="不得显示异常堆栈")
        return stderr


class NormalExportControlTestCase(ExportCloseTestBase):
    """正常导出对照：退出 0、标准输出为路径加换行、文件逐字节符合预期。"""

    def test_normal_export_writes_byte_exact_file(self):
        db_path = self.save()
        before = self.snapshot(db_path)
        output_path = os.path.join(self.workdir, OUTPUT_NAME)
        self.assertFalse(os.path.exists(output_path))

        result = self.export_with_output()

        # 退出 0、标准错误为空、标准输出仅为 copy.json 加换行。
        self.assertEqual(
            result.returncode, 0,
            msg="正常导出应退出 0: " + result.stderr.decode("utf-8", "replace"),
        )
        self.assertEqual(result.stderr, b"", msg="导出成功时标准错误必须为空")
        self.assertEqual(
            result.stdout, f"{OUTPUT_NAME}\n".encode("utf-8"),
            msg="标准输出只能是 copy.json 加一个换行",
        )

        self.assertTrue(os.path.isfile(output_path), msg="导出必须在新路径创建文件")
        with open(output_path, "rb") as f:
            file_bytes = f.read()

        # 无 BOM 的 UTF-8、单行 JSON、末尾恰好一个 LF（无 CR、无多余换行）。
        self.assertFalse(file_bytes.startswith(b"\xef\xbb\xbf"), msg="不得带 BOM")
        file_bytes.decode("utf-8")  # 必须是完整合法的 UTF-8
        self.assertTrue(file_bytes.endswith(b"\n"), msg="文件必须以 LF 结束")
        self.assertEqual(file_bytes.count(b"\n"), 1, msg="整篇只能有末尾一个换行")
        self.assertNotIn(b"\r", file_bytes, msg="不得使用 CRLF 换行")
        self.assertEqual(
            file_bytes[:-1].find(b"\n"), -1,
            msg="末尾 LF 之前不得再有换行（必须是完整单行 JSON）",
        )

        # 完整文件与独立确定的固定预期逐字节核对（不是两次实际输出互相比）。
        self.assertEqual(
            file_bytes, EXPECTED_EXPORT_BYTES,
            msg="正常导出的文件必须与固定预期逐字节一致",
        )

        # 解析后文本、明细顺序与整数数量/单价与固定输入一致。
        self.assert_export_content_matches_fixed_input(file_bytes)

        # 正常导出前后源库记录与表结构一致，不留侧车文件；临时目录由夹具清理。
        self.assert_database_unchanged(db_path, before, "正常文件导出")
        self.assert_no_sidecars(db_path)


class CloseFailureTestCase(ExportCloseTestBase):
    """全部内容写完后仅关闭报告“模拟关闭失败”：退出 1，完整文件原样保留。"""

    def test_close_failure_after_full_write(self):
        db_path = self.save()
        before = self.snapshot(db_path)
        output_path = os.path.join(self.workdir, OUTPUT_NAME)
        self.assertFalse(os.path.exists(output_path))

        result = self.export_with_output(mode="close")

        # 退出 1、标准输出为空、单行错误含路径与关闭失败原因、无堆栈。
        self.assert_write_error_envelope(result, CLOSE_REASON)

        # 已写完的完整文件原样保留：存在完整文件不能判为成功。
        self.assertTrue(
            os.path.isfile(output_path),
            msg="关闭失败后应保留目标文件（既有语义不删除）",
        )
        with open(output_path, "rb") as f:
            on_disk = f.read()
        self.assertEqual(
            on_disk, EXPECTED_EXPORT_BYTES,
            msg="关闭失败前全部内容已写完，文件必须与固定预期逐字节一致",
        )
        self.assert_export_content_matches_fixed_input(on_disk)

        # 故障只作用于导出文件：源库记录与表结构不变，数据库连接不受影响。
        self.assert_database_unchanged(db_path, before, "关闭失败的文件导出")
        self.assert_no_sidecars(db_path)


class WriteThenCloseFailureTestCase(ExportCloseTestBase):
    """先接受 16 字节再写入失败、关闭又失败：错误只保留最先的写入原因。"""

    def test_write_failure_not_masked_by_close_failure(self):
        db_path = self.save()
        before = self.snapshot(db_path)
        output_path = os.path.join(self.workdir, OUTPUT_NAME)
        self.assertFalse(os.path.exists(output_path))

        self.assertGreater(ACCEPTED_BYTE_LENGTH, 0, msg="固定前缀必须为正数")
        self.assertLess(
            ACCEPTED_BYTE_LENGTH, len(EXPECTED_EXPORT_BYTES),
            msg="固定前缀必须短于完整文档，中止后才是部分内容",
        )
        expected_prefix = EXPECTED_EXPORT_BYTES[:ACCEPTED_BYTE_LENGTH]

        result = self.export_with_output(
            mode="write_close", fail_after=ACCEPTED_BYTE_LENGTH,
        )

        # 退出 1、标准输出为空、单行错误含路径与最先发生的写入失败原因。
        stderr = self.assert_write_error_envelope(result, WRITE_REASON)
        self.assertNotIn(
            CLOSE_REASON, stderr,
            msg="先发生的写入失败原因不得被其后的关闭失败覆盖，"
            "也不得以第二条错误输出",
        )

        # 文件恰好保留已接受的 16 字节，不删除、不补全。
        self.assertTrue(
            os.path.isfile(output_path),
            msg="写入中止后应保留目标文件（既有语义不删除）",
        )
        with open(output_path, "rb") as f:
            on_disk = f.read()
        self.assertEqual(
            on_disk, expected_prefix,
            msg="文件必须恰好保留固定预期中已接受的前 16 字节",
        )
        self.assertEqual(
            len(on_disk), ACCEPTED_BYTE_LENGTH,
            msg="保留前缀长度必须等于预设的 16 字节",
        )

        # 故障只作用于导出文件：源库记录与表结构不变，数据库连接不受影响。
        self.assert_database_unchanged(db_path, before, "写入并关闭双重失败的文件导出")
        self.assert_no_sidecars(db_path)


# 项目目录清洁性：测试开始前记录项目根内容，全部用例结束后不得新增任何文件。
_PROJECT_ROOT_ENTRIES_AT_START = set(os.listdir(PROJECT_ROOT))


def tearDownModule():
    leftovers = set(os.listdir(PROJECT_ROOT)) - _PROJECT_ROOT_ENTRIES_AT_START
    assert not leftovers, f"测试在项目根目录留下了文件: {sorted(leftovers)}"


if __name__ == "__main__":
    unittest.main()
