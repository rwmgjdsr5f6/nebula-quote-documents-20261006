"""预览写文件完整性回归测试：部分写入、零进展与写入/关闭失败。

仅依赖 Python 标准库。所有用例在独立的临时目录中通过 README 记载的
公开命令（save / preview）操作 quote.py，合成固定演示报价，
不读取项目内业务数据，也不在项目目录留下数据库或 HTML 文件。

为模拟“本地写入一次只接受部分字节 / 返回 0 / 中途抛 OSError / 关闭失败”，
用例在临时目录生成一个运行器脚本：它在子进程内加载 quote.py 并修补
os.write / os.close，再以相同的公开参数调用 quote.main。运行器本身不是
被测代码，被测的仍是 quote.py 的 preview 流程。

运行方式（项目根目录）：
    python3 -m unittest discover -s tests
全部通过时退出码为 0，任一失败时非零退出并指出对应样例与预期。
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUOTE_PY = os.path.join(PROJECT_ROOT, "quote.py")

# ---- 固定合成报价（用户原文，首尾空格与换行均为样例的一部分）----

NUMBER = "Q-WRITE-001"
CUSTOMER = "演示客户<甲>&乙"
NOTE = "  首行说明\n次行说明  "
DESC_CONSULT = "咨询"
DESC_DOC = "资料"

ESCAPED_CUSTOMER = "演示客户&lt;甲&gt;&amp;乙"
ESCAPED_NOTE = "  首行说明\n次行说明  "  # 说明无特殊字符，转义后与原文一致

UNKNOWN_NUMBER = "Q-MISSING-000"

# 运行器：在子进程内修补 os.write / os.close 后调用 quote.main。
# 模式：
#   partial    每次 write 最多接受 7 字节（多次部分写入后应成功）
#   zero       write 一律返回 0 字节（写入未取得进展）
#   fail-after 前 3 次 write 各写 11 字节，之后抛 OSError
#   close-fail 正常写出，但关闭目标 fd 时抛 OSError
RUNNER = r'''
import importlib.util
import os
import sys

MODE = sys.argv[1]
QUOTE_PY = sys.argv[2]
OUTPUT_PATH = sys.argv[3]
CLI_ARGS = sys.argv[4:]

real_write = os.write
calls = {"write": 0}

if MODE == "partial":
    def fake_write(fd, data):
        calls["write"] += 1
        return real_write(fd, bytes(data[:7]))
elif MODE == "zero":
    def fake_write(fd, data):
        calls["write"] += 1
        return 0
elif MODE == "fail-after":
    def fake_write(fd, data):
        calls["write"] += 1
        if calls["write"] <= 3:
            return real_write(fd, bytes(data[:11]))
        raise OSError("模拟磁盘写入失败")
elif MODE == "close-fail":
    fake_write = real_write
else:
    raise SystemExit(f"未知模式: {MODE}")

os.write = fake_write

if MODE == "close-fail":
    real_open = os.open
    real_close = os.close
    target = {"fd": None}

    def fake_open(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if path == OUTPUT_PATH:
            target["fd"] = fd
        return fd

    def fake_close(fd):
        if fd == target["fd"]:
            raise OSError("模拟关闭失败")
        return real_close(fd)

    os.open = fake_open
    os.close = fake_close

spec = importlib.util.spec_from_file_location("quote", QUOTE_PY)
quote = importlib.util.module_from_spec(spec)
spec.loader.exec_module(quote)
sys.exit(quote.main(CLI_ARGS))
'''


def make_payload():
    return {
        "number": NUMBER,
        "customer": CUSTOMER,
        "note": NOTE,
        "items": [
            {"description": DESC_CONSULT, "quantity": 2, "unit_price": 1250},
            {"description": DESC_DOC, "quantity": 1, "unit_price": 300},
        ],
    }


class PreviewWriteFlowTestCase(unittest.TestCase):
    """每个用例使用独立临时目录，只通过公开命令行入口驱动 quote.py。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="quote-write-test-")
        self.addCleanup(self._tmp.cleanup)
        self.workdir = self._tmp.name
        self.runner_path = os.path.join(self.workdir, "patched_runner.py")
        with open(self.runner_path, "w", encoding="utf-8") as f:
            f.write(RUNNER)

    # ---- 基础设施 ----

    def run_quote(self, *cli_args):
        return subprocess.run(
            [sys.executable, QUOTE_PY, *cli_args],
            capture_output=True,
            text=True,
            cwd=self.workdir,
        )

    def run_patched_preview(self, mode, db_path, number, output_path):
        """以修补后的 os.write/os.close 运行 preview 公开命令。"""
        return subprocess.run(
            [
                sys.executable, self.runner_path, mode, QUOTE_PY, output_path,
                "preview", "--db", db_path, "--number", number,
                "--output", output_path,
            ],
            capture_output=True,
            text=True,
            cwd=self.workdir,
        )

    def save_demo_quote(self):
        db_path = os.path.join(self.workdir, "demo.sqlite")
        input_path = os.path.join(self.workdir, "input.json")
        with open(input_path, "w", encoding="utf-8") as f:
            json.dump(make_payload(), f, ensure_ascii=False)
        result = self.run_quote("save", "--db", db_path, "--input", input_path)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stdout, f"{NUMBER}\n")
        self.assertEqual(result.stderr, "")
        return db_path

    def preview_ok(self, db_path, output_name):
        """未修补的正常预览：应成功，返回 (输出路径, 文件字节)。"""
        output_path = os.path.join(self.workdir, output_name)
        result = self.run_quote(
            "preview", "--db", db_path, "--number", NUMBER,
            "--output", output_path,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stdout, f"{output_path}\n")
        self.assertEqual(result.stderr, "")
        with open(output_path, "rb") as f:
            return output_path, f.read()

    # ---- 多次部分写入后成功：与正常写出逐字节一致 ----

    def test_partial_writes_succeed_byte_identical(self):
        db_path = self.save_demo_quote()

        # 基准：正常写出的完整文档。
        _, reference_bytes = self.preview_ok(db_path, "reference.html")
        self.assertGreater(len(reference_bytes), 100, msg="文档应足够大以覆盖多次部分写入")

        # 修补为每次只写 7 字节：仍应成功，且与基准逐字节一致。
        output_path = os.path.join(self.workdir, "partial.html")
        result = self.run_patched_preview("partial", db_path, NUMBER, output_path)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(
            result.stdout, f"{output_path}\n",
            msg="部分写入后成功时标准输出只能是目标路径加换行",
        )
        self.assertEqual(result.stderr, "", msg="成功时标准错误必须为空")
        with open(output_path, "rb") as f:
            partial_bytes = f.read()
        self.assertEqual(
            partial_bytes, reference_bytes,
            msg="多次部分写入的结果必须与正常写出逐字节一致（中文与转义文本不丢失、不重复）",
        )

        # 内容核对：UTF-8 严格解码后，明细顺序、说明位置与空白、金额与转义正确。
        document = partial_bytes.decode("utf-8")
        self.assertIn(f"<dd>{ESCAPED_CUSTOMER}</dd>", document)
        self.assertIn(
            f'<p class="note-text">{ESCAPED_NOTE}</p>', document,
            msg="客户说明应位于客户信息之后、明细表格之前，首尾空格与换行保留",
        )
        self.assertLess(document.index("<dl>"), document.index('<section class="note">'))
        self.assertLess(document.index('<section class="note">'), document.index("<table>"))
        markers = [
            "<td>1</td>",
            f"<td>{DESC_CONSULT}</td>",
            '<td class="num">2</td>',
            '<td class="num">12.50</td>',
            '<td class="num">25.00</td>',
            "<td>2</td>",
            f"<td>{DESC_DOC}</td>",
            '<td class="num">1</td>',
            '<td class="num">3.00</td>',
            '<td class="num">28.00</td>',
        ]
        for marker in markers:
            self.assertIn(marker, document, msg=f"页面缺少预期内容：{marker}")
        positions = [document.index(marker) for marker in markers]
        self.assertEqual(positions, sorted(positions), msg="明细顺序与总计位置应与输入一致")
        self.assertEqual(document.count('<td class="num">3.00</td>'), 2)

    # ---- 写入未取得进展（write 返回 0）：按失败结束 ----

    def test_zero_progress_fails(self):
        db_path = self.save_demo_quote()
        output_path = os.path.join(self.workdir, "zero.html")

        result = self.run_patched_preview("zero", db_path, NUMBER, output_path)
        self.assertEqual(
            result.returncode, 1,
            msg=f"零进展应退出码 1 而不是停留不退出，实际 stdout={result.stdout!r}",
        )
        self.assertEqual(result.stdout, "", msg="失败时标准输出必须为空")
        stderr = result.stderr
        self.assertTrue(
            stderr.startswith("错误: 无法写入输出文件"),
            msg=f"标准错误应以“错误: 无法写入输出文件”开头，实际为 {stderr!r}",
        )
        self.assertIn(output_path, stderr, msg="错误消息应包含目标路径")
        self.assertIn("未取得进展", stderr, msg="错误消息应说明写入未取得进展")
        self.assertEqual(stderr.count("\n"), 1, msg="标准错误应为一行")
        self.assertNotIn("Traceback", stderr, msg="不得显示异常堆栈")
        # 允许目标保留此前已写出的内容；本模式下尚未写出任何字节。
        if os.path.exists(output_path):
            with open(output_path, "rb") as f:
                self.assertEqual(f.read(), b"")

    # ---- 部分写入后抛 OSError：失败结束，允许保留已写出的部分内容 ----

    def test_oserror_after_partial_write_fails(self):
        db_path = self.save_demo_quote()
        _, reference_bytes = self.preview_ok(db_path, "reference.html")

        output_path = os.path.join(self.workdir, "fail-after.html")
        result = self.run_patched_preview("fail-after", db_path, NUMBER, output_path)
        self.assertEqual(result.returncode, 1, msg="写入中途 OSError 应退出码 1")
        self.assertEqual(result.stdout, "", msg="失败时标准输出必须为空")
        stderr = result.stderr
        self.assertTrue(
            stderr.startswith("错误: 无法写入输出文件"),
            msg=f"标准错误应以“错误: 无法写入输出文件”开头，实际为 {stderr!r}",
        )
        self.assertIn(output_path, stderr, msg="错误消息应包含目标路径")
        self.assertIn("模拟磁盘写入失败", stderr, msg="错误消息应附带实际错误原因")
        self.assertEqual(stderr.count("\n"), 1, msg="标准错误应为一行")
        self.assertNotIn("Traceback", stderr, msg="不得显示异常堆栈")

        # 允许目标保留此前已写出的部分内容：非空且是完整文档的前缀。
        self.assertTrue(os.path.exists(output_path), msg="本模式下应保留已写出的部分内容")
        with open(output_path, "rb") as f:
            leftover = f.read()
        self.assertEqual(leftover, reference_bytes[:33], msg="应恰好保留前 3 次写出的 33 字节")
        self.assertTrue(reference_bytes.startswith(leftover))

    # ---- 关闭时抛 OSError：同一错误前缀与输出约定 ----

    def test_close_oserror_fails(self):
        db_path = self.save_demo_quote()
        output_path = os.path.join(self.workdir, "close-fail.html")

        result = self.run_patched_preview("close-fail", db_path, NUMBER, output_path)
        self.assertEqual(result.returncode, 1, msg="关闭失败应退出码 1")
        self.assertEqual(result.stdout, "", msg="失败时标准输出必须为空")
        stderr = result.stderr
        self.assertTrue(
            stderr.startswith("错误: 无法写入输出文件"),
            msg=f"标准错误应以“错误: 无法写入输出文件”开头，实际为 {stderr!r}",
        )
        self.assertIn(output_path, stderr, msg="错误消息应包含目标路径")
        self.assertIn("模拟关闭失败", stderr, msg="错误消息应附带实际错误原因")
        self.assertEqual(stderr.count("\n"), 1, msg="标准错误应为一行")
        self.assertNotIn("Traceback", stderr, msg="不得显示异常堆栈")

    # ---- 既有拒绝行为不变：重复目标、未知编号、不可读取数据库 ----

    def test_refusals_unchanged(self):
        db_path = self.save_demo_quote()
        output_path, reference_bytes = self.preview_ok(db_path, "reference.html")

        # 重复目标：仍优先拒绝，原文件逐字节不变。
        result = self.run_quote(
            "preview", "--db", db_path, "--number", NUMBER,
            "--output", output_path,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, f"错误: 输出文件已存在: {output_path}\n")
        with open(output_path, "rb") as f:
            self.assertEqual(f.read(), reference_bytes, msg="原有目标文件不得改动")

        # 未知编号：退出码 1、标准输出为空、不创建输出。
        missing_path = os.path.join(self.workdir, "missing.html")
        result = self.run_quote(
            "preview", "--db", db_path, "--number", UNKNOWN_NUMBER,
            "--output", missing_path,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, f"错误: 报价编号不存在: {UNKNOWN_NUMBER}\n")
        self.assertFalse(os.path.exists(missing_path), msg="未知编号不得创建输出")

        # 数据库不存在：退出码 1、标准输出为空、不创建输出。
        no_db_out = os.path.join(self.workdir, "no-db.html")
        result = self.run_quote(
            "preview", "--db", os.path.join(self.workdir, "absent.sqlite"),
            "--number", NUMBER, "--output", no_db_out,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertTrue(result.stderr.startswith("错误: 无法读取数据库"))
        self.assertFalse(os.path.exists(no_db_out), msg="数据库不可读时不得创建输出")

        # 数据库不是有效 SQLite：退出码 1、标准输出为空、不创建输出。
        garbage_db = os.path.join(self.workdir, "garbage.sqlite")
        with open(garbage_db, "wb") as f:
            f.write(b"this is not a sqlite database")
        garbage_out = os.path.join(self.workdir, "garbage.html")
        result = self.run_quote(
            "preview", "--db", garbage_db, "--number", NUMBER,
            "--output", garbage_out,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertTrue(result.stderr.startswith("错误: 无法读取数据库"))
        self.assertFalse(os.path.exists(garbage_out), msg="数据库损坏时不得创建输出")


if __name__ == "__main__":
    unittest.main()
