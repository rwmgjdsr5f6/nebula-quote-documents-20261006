"""预览流程回归测试：用户文本转义与拒绝覆盖已有文件。

仅依赖 Python 标准库。所有用例在独立的临时目录中通过子进程调用
README 描述的 save / preview 公开入口，使用合成客户与演示报价，
结束后不在产品目录留下数据库或 HTML 文件。

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

# 固定样例：编号、客户与说明都含有需要 HTML 转义的字符，
# 客户首尾各带一个空格，说明一中还包含实体样式的原文。
NUMBER = "Q-TEXT<&>"
CUSTOMER = ' 演示客户&"甲"\'乙\' '
DESCRIPTION_1 = "<script>demo()</script>&lt;零件&gt;"
DESCRIPTION_2 = "资料 & 支持"

# 与 quote.py 使用的 html.escape（quote=True）一致的预期转义结果。
ESCAPED_NUMBER = "Q-TEXT&lt;&amp;&gt;"
ESCAPED_CUSTOMER = " 演示客户&amp;&quot;甲&quot;&#x27;乙&#x27; "
ESCAPED_DESCRIPTION_1 = "&lt;script&gt;demo()&lt;/script&gt;&amp;lt;零件&amp;gt;"
ESCAPED_DESCRIPTION_2 = "资料 &amp; 支持"

PAYLOAD = {
    "number": NUMBER,
    "customer": CUSTOMER,
    "items": [
        {"description": DESCRIPTION_1, "quantity": 2, "unit_price": 1250},
        {"description": DESCRIPTION_2, "quantity": 1, "unit_price": 300},
    ],
}


class PreviewFlowTestCase(unittest.TestCase):
    """每个用例使用独立的临时目录，子进程通过公开命令操作 quote.py。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="quote-preview-test-")
        self.addCleanup(self._tmp.cleanup)
        self.workdir = self._tmp.name
        self.db_path = os.path.join(self.workdir, "quotes.sqlite")

    # ---- 基础设施 ----

    def run_quote(self, *cli_args):
        """在临时目录中以独立进程运行 quote.py 的公开命令。"""
        return subprocess.run(
            [sys.executable, QUOTE_PY, *cli_args],
            capture_output=True,
            text=True,
            cwd=self.workdir,
        )

    def save_demo_quote(self):
        """通过 README 的 save 入口保存固定样例，返回 save 结果。"""
        input_path = os.path.join(self.workdir, "input.json")
        with open(input_path, "w", encoding="utf-8") as f:
            json.dump(PAYLOAD, f, ensure_ascii=False)
        return self.run_quote("save", "--db", self.db_path, "--input", input_path)

    def preview(self, number, output_path):
        """通过 README 的 preview 入口生成预览，返回 preview 结果。"""
        return self.run_quote(
            "preview", "--db", self.db_path, "--number", number,
            "--output", output_path,
        )

    def output_path(self, name="preview.html"):
        return os.path.join(self.workdir, name)

    def snapshot_db(self):
        """直接读取 SQLite，返回 (quotes 行列表, items 行列表) 全量快照。"""
        conn = sqlite3.connect(self.db_path)
        try:
            quotes = conn.execute(
                "SELECT number, customer, total FROM quotes ORDER BY number"
            ).fetchall()
            items = conn.execute(
                "SELECT quote_number, position, description, quantity, "
                "unit_price, line_amount FROM items "
                "ORDER BY quote_number, position"
            ).fetchall()
        finally:
            conn.close()
        return quotes, items

    def save_and_assert_ok(self):
        """保存固定样例并断言公开契约：退出码 0、标准输出只有编号、标准错误为空。"""
        result = self.save_demo_quote()
        self.assertEqual(result.returncode, 0, msg=f"保存应成功：{result.stderr!r}")
        self.assertEqual(
            result.stdout, NUMBER + "\n",
            msg=f"保存成功后标准输出应只有编号加换行，实际：{result.stdout!r}",
        )
        self.assertEqual(
            result.stderr, "",
            msg=f"保存成功后标准错误应为空，实际：{result.stderr!r}",
        )
        return result

    # ---- 正常流程：转义与原文保留 ----

    def test_save_and_preview_escape_user_text(self):
        self.save_and_assert_ok()

        path = self.output_path()
        result = self.preview(NUMBER, path)
        self.assertEqual(result.returncode, 0, msg=f"预览应成功：{result.stderr!r}")
        self.assertEqual(
            result.stdout, path + "\n",
            msg=f"预览成功后标准输出应只有目标路径加换行，实际：{result.stdout!r}",
        )
        self.assertEqual(
            result.stderr, "",
            msg=f"预览成功后标准错误应为空，实际：{result.stderr!r}",
        )

        # 生成文件可按 UTF-8 读取。
        with open(path, encoding="utf-8") as f:
            document = f.read()

        # 标题与正文中的编号保留原文（转义后）。
        self.assertIn(
            f"<title>报价单 {ESCAPED_NUMBER}</title>", document,
            msg="标题中的编号应保留原文并完成转义",
        )
        self.assertIn(
            f"<dd>{ESCAPED_NUMBER}</dd>", document,
            msg="正文中的编号应保留原文并完成转义",
        )

        # 客户保留原文，首尾空格不被删除。
        self.assertIn(
            f"<dd>{ESCAPED_CUSTOMER}</dd>", document,
            msg="客户应保留原文（含首尾各一个空格）并完成转义",
        )

        # 两条说明保留原文，顺序与输入一致。
        self.assertIn(
            f"<td>{ESCAPED_DESCRIPTION_1}</td>", document,
            msg="第一条说明应保留原文并完成转义",
        )
        self.assertIn(
            f"<td>{ESCAPED_DESCRIPTION_2}</td>", document,
            msg="第二条说明应保留原文并完成转义",
        )
        positions = [document.index(text)
                     for text in (ESCAPED_DESCRIPTION_1, ESCAPED_DESCRIPTION_2)]
        self.assertEqual(positions, sorted(positions), "明细顺序应与输入一致")

        # 用户输入不能产生 script 元素。
        self.assertNotIn(
            "<script", document.lower(),
            msg="用户输入不得在 HTML 源文中产生 script 元素",
        )

        # 已有实体样式的 &lt;零件&gt; 作为原文保留，不能变成 <零件>。
        self.assertNotIn(
            "<零件>", document,
            msg="实体样式原文 &lt;零件&gt; 不应被还原为 <零件>",
        )
        self.assertIn(
            "&amp;lt;零件&amp;gt;", document,
            msg="实体样式原文 &lt;零件&gt; 应转义为 &amp;lt;零件&amp;gt; 保留",
        )

        # 单价、行金额与总计的显示。
        for unit_price in ("12.50", "3.00"):
            self.assertIn(
                f'<td class="num">{unit_price}</td>', document,
                msg=f"单价应显示 {unit_price} 元",
            )
        for line_amount in ("25.00", "3.00"):
            self.assertIn(
                f'<td class="num">{line_amount}</td>', document,
                msg=f"行金额应显示 {line_amount} 元",
            )
        self.assertIn(
            '<td class="num">28.00</td>', document,
            msg="总计应显示 28.00 元",
        )

    # ---- 同一编号输出到多个新路径 ----

    def test_same_number_two_new_paths_byte_identical(self):
        self.save_and_assert_ok()
        before = self.snapshot_db()

        path_a = self.output_path("a.html")
        path_b = self.output_path("b.html")
        for label, path in (("第一个新路径", path_a), ("第二个新路径", path_b)):
            with self.subTest(路径=label):
                result = self.preview(NUMBER, path)
                self.assertEqual(
                    result.returncode, 0,
                    msg=f"{label}预览应成功：{result.stderr!r}",
                )
                self.assertEqual(result.stdout, path + "\n")
                self.assertEqual(result.stderr, "")

        with open(path_a, "rb") as f:
            bytes_a = f.read()
        with open(path_b, "rb") as f:
            bytes_b = f.read()
        self.assertEqual(bytes_a, bytes_b, "同一编号的两份 HTML 应逐字节一致")

        self.assertEqual(
            self.snapshot_db(), before,
            "预览不得改变已保存的报价及明细",
        )

    # ---- 拒绝覆盖已有文件 ----

    def test_existing_output_is_refused_and_untouched(self):
        self.save_and_assert_ok()
        before = self.snapshot_db()

        path = self.output_path()
        original_bytes = "既有内容 <保持不变>\n".encode("utf-8")
        with open(path, "wb") as f:
            f.write(original_bytes)

        result = self.preview(NUMBER, path)
        self.assertEqual(
            result.returncode, 1,
            msg=f"输出文件已存在应退出码 1，实际 {result.returncode}",
        )
        self.assertEqual(
            result.stdout, "",
            msg=f"拒绝覆盖时标准输出必须为空，实际：{result.stdout!r}",
        )
        self.assertEqual(
            result.stderr,
            f"错误: 输出文件已存在: {path}\n",
            msg=f"标准错误应只有一行覆盖拒绝并使用传入路径原文，实际：{result.stderr!r}",
        )
        with open(path, "rb") as f:
            self.assertEqual(
                f.read(), original_bytes,
                "被拒绝后原文件字节必须保持不变",
            )
        self.assertEqual(
            self.snapshot_db(), before,
            "被拒绝的预览不得改变已保存的报价及明细",
        )

    # ---- 未知编号 ----

    def test_unknown_number_is_refused_and_creates_nothing(self):
        self.save_and_assert_ok()
        before = self.snapshot_db()

        unknown = "Q-TEXT-UNKNOWN"
        path = self.output_path()
        result = self.preview(unknown, path)
        self.assertEqual(
            result.returncode, 1,
            msg=f"未知编号应退出码 1，实际 {result.returncode}",
        )
        self.assertEqual(
            result.stdout, "",
            msg=f"未知编号时标准输出必须为空，实际：{result.stdout!r}",
        )
        self.assertEqual(
            result.stderr,
            f"错误: 报价编号不存在: {unknown}\n",
            msg=f"标准错误应只有一行编号错误并使用编号原文，实际：{result.stderr!r}",
        )
        self.assertFalse(
            os.path.exists(path),
            "未知编号不得创建目标文件",
        )
        self.assertEqual(
            self.snapshot_db(), before,
            "被拒绝的预览不得改变已保存的报价及明细",
        )

    def test_existing_output_error_precedes_unknown_number(self):
        """目标已存在且编号未知时，优先报告输出文件已存在。"""
        self.save_and_assert_ok()
        before = self.snapshot_db()

        path = self.output_path()
        original_bytes = b"pre-existing\n"
        with open(path, "wb") as f:
            f.write(original_bytes)

        result = self.preview("Q-TEXT-UNKNOWN", path)
        self.assertEqual(result.returncode, 1, msg=f"应退出码 1，实际 {result.returncode}")
        self.assertEqual(result.stdout, "")
        self.assertEqual(
            result.stderr,
            f"错误: 输出文件已存在: {path}\n",
            msg=f"目标已存在时应优先报告覆盖拒绝，实际：{result.stderr!r}",
        )
        with open(path, "rb") as f:
            self.assertEqual(f.read(), original_bytes, "原文件字节必须保持不变")
        self.assertEqual(
            self.snapshot_db(), before,
            "被拒绝的预览不得改变已保存的报价及明细",
        )


if __name__ == "__main__":
    unittest.main()
