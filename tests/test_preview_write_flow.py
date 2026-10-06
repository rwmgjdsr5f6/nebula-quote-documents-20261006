"""预览写入完整性回归测试：部分写入续写到成功、零进展失败、写入途中报错。

通过 quote.py 的公开命令行（save / preview）驱动独立子进程；为了模拟本地
write 只接受部分字节的情形，在临时目录放置 sitecustomize.py 并把该目录加入
PYTHONPATH：子进程启动时自动加载，按环境变量 QUOTE_FAULT_SCRIPT 指定的
脚本替换 os.write，逐个 fd 按“每次接受 N 字节 / 返回 0 / 抛出 OSError”动作。

仅依赖 Python 标准库，不读取项目内业务数据，所有产物均位于临时目录。

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
import textwrap
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUOTE_PY = os.path.join(PROJECT_ROOT, "quote.py")

# ---- 固定合成报价（用户原文：首尾空格、换行与特殊字符均为样例的一部分）----

NUMBER = "Q-WRITE-001"
CUSTOMER = "演示客户<甲>&乙"
NOTE = "  首行说明\n第二行\n 末行  "
DESC_CONSULT = "咨询"
DESC_MATERIAL = "资料"

ESCAPED_CUSTOMER = "演示客户&lt;甲&gt;&amp;乙"


SITECUSTOMIZE_SOURCE = textwrap.dedent(
    """\
    import os

    _real_write = os.write
    _script = os.environ.get("QUOTE_FAULT_SCRIPT", "")
    if _script:
        # 动作按 fd 独立计数：只对通过 os.open 新建的预览输出文件生效，
        # 不影响 sqlite 内部 fd（其编号早于输出文件，且本替换在脚本解释器
        # 完全启动后才安装——为稳妥起见仍按 fd 分别计数）。
        _counters = {}

        def fake_write(fd, buf):
            index = _counters.get(fd, 0)
            _counters[fd] = index + 1
            actions = [part.strip() for part in _script.split(",")]
            if index >= len(actions) or actions[index] == "PASS":
                return _real_write(fd, buf)
            action = actions[index]
            if action == "ERR":
                raise OSError("模拟写入故障")
            if action == "0":
                return 0
            count = int(action)
            if count >= len(buf):
                return _real_write(fd, buf)
            return _real_write(fd, buf[:count])

        os.write = fake_write
    """
)


def make_payload():
    return {
        "number": NUMBER,
        "customer": CUSTOMER,
        "note": NOTE,
        "items": [
            {"description": DESC_CONSULT, "quantity": 2, "unit_price": 1250},
            {"description": DESC_MATERIAL, "quantity": 1, "unit_price": 300},
        ],
    }


class PreviewWriteFlowTestCase(unittest.TestCase):
    """每个用例使用独立临时目录，只通过公开命令行入口驱动 quote.py。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="quote-write-test-")
        self.addCleanup(self._tmp.cleanup)
        self.workdir = self._tmp.name

        # 故障注入模块：仅在显式设置 QUOTE_FAULT_SCRIPT 时替换 os.write。
        self.inject_dir = os.path.join(self.workdir, "inject")
        os.mkdir(self.inject_dir)
        with open(
            os.path.join(self.inject_dir, "sitecustomize.py"),
            "w",
            encoding="utf-8",
        ) as f:
            f.write(SITECUSTOMIZE_SOURCE)

        self.db_path = os.path.join(self.workdir, "demo.sqlite")
        self.save_quote()
        # 无故障时的参考产物：所有成功路径必须与它逐字节一致。
        self.reference_path = os.path.join(self.workdir, "reference.html")
        result = self.run_preview(self.db_path, NUMBER, self.reference_path)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        with open(self.reference_path, "rb") as f:
            self.reference_bytes = f.read()
        self.assertGreater(len(self.reference_bytes), 64)

    # ---- 基础设施 ----

    def run_quote(self, *cli_args, fault_script=None):
        """在临时目录中以独立进程运行 quote.py，可附带写入故障脚本。"""
        env = None
        if fault_script is not None:
            env = dict(os.environ)
            env["PYTHONPATH"] = self.inject_dir + os.pathsep + env.get("PYTHONPATH", "")
            env["QUOTE_FAULT_SCRIPT"] = fault_script
        return subprocess.run(
            [sys.executable, QUOTE_PY, *cli_args],
            capture_output=True,
            text=True,
            env=env,
            cwd=self.workdir,
        )

    def save_quote(self):
        input_path = os.path.join(self.workdir, "input.json")
        with open(input_path, "w", encoding="utf-8") as f:
            json.dump(make_payload(), f, ensure_ascii=False)
        result = self.run_quote("save", "--db", self.db_path, "--input", input_path)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stdout, f"{NUMBER}\n")
        self.assertEqual(result.stderr, "")

    def run_preview(self, db_path, number, output_path, fault_script=None):
        return self.run_quote(
            "preview",
            "--db",
            db_path,
            "--number",
            number,
            "--output",
            output_path,
            fault_script=fault_script,
        )

    def assert_reference_content(self, output_path):
        """成功文件必须与无故障参考产物逐字节一致且为严格 UTF-8。"""
        with open(output_path, "rb") as f:
            data = f.read()
        self.assertEqual(data, self.reference_bytes, msg="部分写入续写完的文件必须与一次写出逐字节一致")
        document = data.decode("utf-8")  # 非法字节会直接失败
        return document

    def assert_reference_semantics(self, document):
        """固定合成报价的展示要求：顺序、空白、金额、转义。"""
        # 客户原文（含 <甲>&乙）必须转义后出现，且不产生标签。
        self.assertIn(f"<dd>{ESCAPED_CUSTOMER}</dd>", document)
        self.assertNotIn("<甲>", document)

        # 客户说明位于客户之后、表格之前，首尾空格与换行保留，源文为转义文本。
        note_section_at = document.index('<section class="note">')
        customer_at = document.index(ESCAPED_CUSTOMER)
        table_at = document.index("<table>")
        self.assertLess(customer_at, note_section_at)
        self.assertLess(note_section_at, table_at)
        self.assertIn(
            '<p class="note-text">  首行说明\n第二行\n 末行  </p>',
            document,
            msg="说明的首尾空格与换行必须逐字保留",
        )

        # 明细顺序、数量、单价/行金额/总计，以及第二条 3.00 恰好出现两次。
        markers = [
            "<td>1</td>",
            f"<td>{DESC_CONSULT}</td>",
            '<td class="num">2</td>',
            '<td class="num">12.50</td>',
            '<td class="num">25.00</td>',
            "<td>2</td>",
            f"<td>{DESC_MATERIAL}</td>",
            '<td class="num">1</td>',
            '<td class="num">3.00</td>',
            '<td class="num">28.00</td>',
        ]
        positions = [document.index(marker) for marker in markers]
        self.assertEqual(positions, sorted(positions), msg="明细顺序与金额列排列必须保持")
        self.assertEqual(document.count('<td class="num">3.00</td>'), 2)

    def assert_write_failure(self, result, output_path):
        """写入失败的统一约定：退出码 1、无 stdout、stderr 一行约定前缀。"""
        self.assertEqual(
            result.returncode, 1,
            msg=f"写入未完成时必须退出 1，实际 stderr={result.stderr!r}",
        )
        self.assertEqual(result.stdout, "", msg="写入失败时标准输出必须为空")
        lines = result.stderr.splitlines()
        self.assertEqual(len(lines), 1, msg="标准错误必须只有一行，且不得出现异常堆栈")
        self.assertTrue(
            lines[0].startswith("错误: 无法写入输出文件 "),
            msg=f"错误必须以“错误: 无法写入输出文件 ”开头，实际：{lines[0]!r}",
        )
        self.assertIn(output_path, lines[0], msg="错误消息必须包含目标路径")
        self.assertNotIn("Traceback", result.stderr)

    # ---- 参考产物自身先满足固定合成报价的展示要求 ----

    def test_reference_document_contract(self):
        document = self.reference_bytes.decode("utf-8")
        self.assert_reference_semantics(document)

    # ---- 多次部分写入后仍成功，文件逐字节一致 ----

    def test_repeated_partial_writes_complete_byte_identical(self):
        # 文档明显长于 7 字节：连续多次只接受一小段，必须不断续写直到完整。
        tiny_chunks = ",".join(["7"] * 200)
        output_path = os.path.join(self.workdir, "preview-tiny.html")
        result = self.run_preview(
            self.db_path, NUMBER, output_path, fault_script=tiny_chunks
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stdout, f"{output_path}\n")
        self.assertEqual(result.stderr, "")
        document = self.assert_reference_content(output_path)
        self.assert_reference_semantics(document)

    def test_varied_partial_writes_byte_identical(self):
        # 每次接受不同字节数，且切在多字节 UTF-8 字符中间：中文不得损坏。
        varied = ",".join(["1", "3", "2", "5", "1", "4", "2", "6", "3", "1"])
        output_path = os.path.join(self.workdir, "preview-varied.html")
        result = self.run_preview(
            self.db_path, NUMBER, output_path, fault_script=varied
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stdout, f"{output_path}\n")
        self.assertEqual(result.stderr, "")
        document = self.assert_reference_content(output_path)
        # 中文内容逐字保留，不丢失、不重复、不乱码（源文中客户为转义形式）。
        self.assertEqual(document.count(ESCAPED_CUSTOMER), 1)
        self.assertEqual(document.count(NOTE.splitlines()[1]), 1)
        self.assert_reference_semantics(document)

    # ---- 写入未取得进展（返回 0 字节）：失败退出，不停留 ----

    def test_zero_progress_fails_and_keeps_partial_file(self):
        # 先接受 10 字节，随后返回 0：必须以失败结束，而不是重试卡死。
        output_path = os.path.join(self.workdir, "preview-zero.html")
        result = self.run_preview(
            self.db_path, NUMBER, output_path, fault_script="10,0"
        )
        self.assert_write_failure(result, output_path)
        self.assertIn("写入未取得进展", result.stderr, msg="必须说明零进展原因")
        # 目标保留此前已写出的部分内容（恰好 10 字节），而非被清空或伪装成功。
        with open(output_path, "rb") as f:
            partial = f.read()
        self.assertEqual(partial, self.reference_bytes[:10])
        self.assertNotEqual(len(partial), len(self.reference_bytes))

    def test_immediate_zero_progress_fails(self):
        # 首次写入即返回 0：同样退出 1，文件已创建但内容为空。
        output_path = os.path.join(self.workdir, "preview-zero-first.html")
        result = self.run_preview(
            self.db_path, NUMBER, output_path, fault_script="0"
        )
        self.assert_write_failure(result, output_path)
        self.assertIn("写入未取得进展", result.stderr)
        with open(output_path, "rb") as f:
            self.assertEqual(f.read(), b"")

    # ---- 部分写入后报错（OSError）：沿用同一失败约定，保留已写内容 ----

    def test_oserror_after_partial_write_keeps_partial_file(self):
        output_path = os.path.join(self.workdir, "preview-err.html")
        result = self.run_preview(
            self.db_path, NUMBER, output_path, fault_script="13,ERR"
        )
        self.assert_write_failure(result, output_path)
        self.assertIn("模拟写入故障", result.stderr, msg="必须附带实际错误原因")
        with open(output_path, "rb") as f:
            partial = f.read()
        self.assertEqual(partial, self.reference_bytes[:13])
        self.assertNotEqual(len(partial), len(self.reference_bytes))

    # ---- 重复目标仍优先拒绝，且故障注入不触碰原文件 ----

    def test_existing_target_refused_before_write_faults(self):
        sentinel = os.path.join(self.workdir, "sentinel.txt")
        with open(sentinel, "wb") as f:
            f.write(b"DO-NOT-TOUCH")
        # 即便注入“写入即失败”，已存在目标也必须先走拒绝分支，原文件不变。
        result = self.run_preview(
            self.db_path, NUMBER, sentinel, fault_script="0"
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, f"错误: 输出文件已存在: {sentinel}\n")
        with open(sentinel, "rb") as f:
            self.assertEqual(f.read(), b"DO-NOT-TOUCH")

    # ---- 未知编号与不可读取数据库：退出 1、stdout 为空、不创建输出 ----

    def test_unknown_number_creates_nothing(self):
        output_path = os.path.join(self.workdir, "preview-unknown.html")
        self.assertFalse(os.path.exists(output_path))
        result = self.run_preview(self.db_path, "Q-NO-SUCH", output_path)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "错误: 报价编号不存在: Q-NO-SUCH\n")
        self.assertFalse(os.path.exists(output_path))

    def test_unreadable_database_creates_nothing(self):
        bad_db = os.path.join(self.workdir, "broken.sqlite")
        with open(bad_db, "wb") as f:
            f.write(b"this is not a sqlite database\n")
        output_path = os.path.join(self.workdir, "preview-baddb.html")
        self.assertFalse(os.path.exists(output_path))
        result = self.run_preview(bad_db, NUMBER, output_path)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertTrue(
            result.stderr.startswith(f"错误: 无法读取数据库 {bad_db}: "),
            msg=f"实际 stderr={result.stderr!r}",
        )
        self.assertFalse(
            os.path.exists(output_path),
            msg="数据库不可读时不得创建输出文件",
        )

    # ---- 失败后数据库内容不变 ----

    def test_database_unchanged_after_write_failures(self):
        for script, name in [
            ("10,0", "a.html"),
            ("13,ERR", "b.html"),
        ]:
            self.run_preview(
                self.db_path, NUMBER,
                os.path.join(self.workdir, name),
                fault_script=script,
            )
        conn = sqlite3.connect(self.db_path)
        try:
            quote = conn.execute(
                "SELECT customer, total, note FROM quotes WHERE number = ?",
                (NUMBER,),
            ).fetchone()
            items = conn.execute(
                "SELECT description, quantity, unit_price, line_amount "
                "FROM items WHERE quote_number = ? ORDER BY position",
                (NUMBER,),
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(quote, (CUSTOMER, 2800, NOTE))
        self.assertEqual(
            items,
            [(DESC_CONSULT, 2, 1250, 2500), (DESC_MATERIAL, 1, 300, 300)],
        )


if __name__ == "__main__":
    unittest.main()
