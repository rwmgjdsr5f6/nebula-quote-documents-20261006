"""预览流程回归测试：用户文本转义与拒绝覆盖已有输出文件。

仅依赖 Python 标准库。所有用例在独立的临时目录中通过 README 记载的
公开命令（save / preview）操作 quote.py 子进程，合成客户与演示报价，
不读取项目内业务数据，也不在项目目录留下数据库或 HTML 文件。

运行方式（项目根目录）：
    python3 -m unittest discover -s tests
全部通过时退出码为 0，任一失败时非零退出并指出对应样例与预期。
"""

import html
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from html.parser import HTMLParser

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUOTE_PY = os.path.join(PROJECT_ROOT, "quote.py")

# ---- 合成报价（用户原文，首尾空格与特殊字符均为样例的一部分）----

NUMBER = "Q-TEXT<&>"
CUSTOMER = " 演示客户&\"甲\"'乙' "
DESC_SCRIPT = "<script>demo()</script>&lt;零件&gt;"
DESC_PLAIN = "资料 & 支持"

# 期望出现在 HTML 源文中的转义形式（标准库 html.escape 默认转 & < > " '）。
ESCAPED_NUMBER = "Q-TEXT&lt;&amp;&gt;"
ESCAPED_CUSTOMER = " 演示客户&amp;&quot;甲&quot;&#x27;乙&#x27; "
ESCAPED_DESC_SCRIPT = (
    "&lt;script&gt;demo()&lt;/script&gt;&amp;lt;零件&amp;gt;"
)
ESCAPED_DESC_PLAIN = "资料 &amp; 支持"

UNKNOWN_NUMBER = "Q-MISSING-000"

# ---- 空白保留样例：编号、客户与说明含首尾空格、连续空格、换行与连续换行 ----

WS_NUMBER = " Q  WHITE-001 "
WS_CUSTOMER = "  演示  <甲>&乙\n第二行  "
WS_DESC_MULTILINE = "  咨询  &lt;资料&gt;\n补充  "
WS_DESC_BLANK_LINES = "<script>示例</script>\n\n\n末行"

ESCAPED_WS_NUMBER = " Q  WHITE-001 "
ESCAPED_WS_CUSTOMER = "  演示  &lt;甲&gt;&amp;乙\n第二行  "
ESCAPED_WS_DESC_MULTILINE = "  咨询  &amp;lt;资料&amp;gt;\n补充  "
ESCAPED_WS_DESC_BLANK_LINES = "&lt;script&gt;示例&lt;/script&gt;\n\n\n末行"


def make_payload():
    return {
        "number": NUMBER,
        "customer": CUSTOMER,
        "items": [
            {"description": DESC_SCRIPT, "quantity": 2, "unit_price": 1250},
            {"description": DESC_PLAIN, "quantity": 1, "unit_price": 300},
        ],
    }


class TextAndTagCollector(HTMLParser):
    """收集文本节点与起始标签：核对页面文本仍为原文、且不产生 script 元素。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.chunks = []
        self.start_tags = []

    def handle_starttag(self, tag, attrs):
        self.start_tags.append(tag)

    def handle_data(self, data):
        self.chunks.append(data)


class PreviewFlowTestCase(unittest.TestCase):
    """每个用例使用独立临时目录，只通过公开命令行入口驱动 quote.py。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="quote-preview-test-")
        self.addCleanup(self._tmp.cleanup)
        self.workdir = self._tmp.name

    # ---- 基础设施 ----

    def run_quote(self, *cli_args):
        """在临时目录中以独立进程运行 quote.py 的公开命令。"""
        return subprocess.run(
            [sys.executable, QUOTE_PY, *cli_args],
            capture_output=True,
            text=True,
            cwd=self.workdir,
        )

    def save(self, payload, db_name="quotes.sqlite", input_name="input.json"):
        db_path = os.path.join(self.workdir, db_name)
        input_path = os.path.join(self.workdir, input_name)
        with open(input_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        result = self.run_quote("save", "--db", db_path, "--input", input_path)
        return result, db_path

    def run_preview(self, db_path, number, output_path):
        return self.run_quote(
            "preview", "--db", db_path, "--number", number, "--output", output_path
        )

    def read_rows(self, db_path):
        """直接读取 SQLite 快照：(quotes 全部行, items 全部行)。"""
        conn = sqlite3.connect(db_path)
        try:
            quote_rows = conn.execute(
                "SELECT number, customer, total FROM quotes ORDER BY number"
            ).fetchall()
            item_rows = conn.execute(
                "SELECT quote_number, position, description, quantity, "
                "unit_price, line_amount FROM items "
                "ORDER BY quote_number, position"
            ).fetchall()
        finally:
            conn.close()
        return quote_rows, item_rows

    def expected_rows(self):
        return (
            [(NUMBER, CUSTOMER, 2800)],
            [
                (NUMBER, 0, DESC_SCRIPT, 2, 1250, 2500),
                (NUMBER, 1, DESC_PLAIN, 1, 300, 300),
            ],
        )

    # ---- 正常样例：save -> preview，文本转义 ----

    def test_save_and_preview_escape_user_text(self):
        # 期望常量自检：它们确实是标准库对用户原文的转义结果，防止测试常量写错。
        self.assertEqual(html.escape(NUMBER), ESCAPED_NUMBER)
        self.assertEqual(html.escape(CUSTOMER), ESCAPED_CUSTOMER)
        self.assertEqual(html.escape(DESC_SCRIPT), ESCAPED_DESC_SCRIPT)
        self.assertEqual(html.escape(DESC_PLAIN), ESCAPED_DESC_PLAIN)

        # 保存：退出码 0，标准输出只有编号，标准错误为空。
        save_result, db_path = self.save(make_payload())
        self.assertEqual(save_result.returncode, 0, msg=save_result.stderr)
        self.assertEqual(
            save_result.stdout, f"{NUMBER}\n",
            msg="保存成功后标准输出只能是编号加换行",
        )
        self.assertEqual(save_result.stderr, "", msg="保存成功时标准错误必须为空")

        # 落库内容：合法文本原样保留（客户首尾空格不删除），金额为整数分。
        self.assertEqual(self.read_rows(db_path), self.expected_rows())

        # 预览：退出码 0，标准输出只有目标路径，标准错误为空。
        output_path = os.path.join(self.workdir, "preview-a.html")
        preview_result = self.run_preview(db_path, NUMBER, output_path)
        self.assertEqual(preview_result.returncode, 0, msg=preview_result.stderr)
        self.assertEqual(
            preview_result.stdout, f"{output_path}\n",
            msg="预览成功后标准输出只能是目标路径加换行",
        )
        self.assertEqual(preview_result.stderr, "", msg="预览成功时标准错误必须为空")

        # 生成文件可按 UTF-8 严格解码（非法字节会直接让本用例失败）。
        with open(output_path, "rb") as f:
            raw_bytes = f.read()
        document = raw_bytes.decode("utf-8")

        # 标题与正文中的编号、客户、两条说明：源文中为转义形式。
        self.assertIn(f"<title>报价单 {ESCAPED_NUMBER}</title>", document)
        self.assertEqual(
            document.count(ESCAPED_NUMBER), 2,
            msg="编号应只在标题与正文各出现一次",
        )
        self.assertIn(f"<dd>{ESCAPED_NUMBER}</dd>", document)
        self.assertIn(
            f"<dd>{ESCAPED_CUSTOMER}</dd>", document,
            msg="客户在源文中必须转义，且首尾空格保留在 <dd> 文本内",
        )
        self.assertIn(f"<td>{ESCAPED_DESC_SCRIPT}</td>", document)
        self.assertIn(f"<td>{ESCAPED_DESC_PLAIN}</td>", document)

        # 用户输入不能产生 script 元素：源文无 <script 起始标签。
        self.assertNotIn("<script", document.lower())
        self.assertNotIn("</script", document.lower())

        # 已有实体样式的 &lt;零件&gt; 是用户原文，必须再转义一层原样保留，
        # 既不能被当成标签变成 <零件>，也不能被解码成裸实体。
        self.assertIn("&amp;lt;零件&amp;gt;", document)
        self.assertNotIn("&lt;零件&gt;", document)
        self.assertNotIn("<零件>", document)

        # 解析后的可见文本：编号、客户（含首尾空格）、两条说明均为原文。
        collector = TextAndTagCollector()
        collector.feed(document)
        self.assertNotIn("script", collector.start_tags, msg="页面不得出现 script 元素")
        visible_text = "".join(collector.chunks)
        self.assertIn(f"报价单 {NUMBER}", visible_text, msg="标题文本应保留编号原文")
        self.assertIn(NUMBER, visible_text)
        self.assertIn(CUSTOMER, visible_text, msg="客户首尾空格不得被删除")
        self.assertIn(DESC_SCRIPT, visible_text)
        self.assertIn("&lt;零件&gt;", visible_text, msg="实体样式文本应作为原文显示")
        self.assertNotIn("<零件>", visible_text)
        self.assertIn(DESC_PLAIN, visible_text)

        # 明细顺序与输入一致：行号、说明、数量、单价、行金额依次出现。
        markers = [
            "<td>1</td>",
            ESCAPED_DESC_SCRIPT,
            '<td class="num">2</td>',
            '<td class="num">12.50</td>',
            '<td class="num">25.00</td>',
            "<td>2</td>",
            ESCAPED_DESC_PLAIN,
            '<td class="num">1</td>',
            '<td class="num">3.00</td>',
            '<td class="num">28.00</td>',
        ]
        for marker in markers:
            self.assertIn(marker, document, msg=f"页面缺少预期内容：{marker}")
        positions = [document.index(marker) for marker in markers]
        self.assertEqual(
            positions, sorted(positions),
            msg="明细顺序与各列排列应与输入一致：行1(说明1, 2, 12.50, 25.00) "
                "-> 行2(说明2, 1, 3.00, 3.00) -> 总计 28.00",
        )
        # 第二条明细的单价与行金额都是 3.00，应恰好出现两次。
        self.assertEqual(document.count('<td class="num">3.00</td>'), 2)
        self.assertIn('<td class="num">28.00</td>', document, msg="总计应显示 28.00")

    # ---- 空白保留：编号、客户、说明中的空格与换行按原样呈现 ----

    def test_preview_preserves_whitespace_and_newlines(self):
        # 期望常量自检：它们确实是标准库对用户原文的转义结果，防止测试常量写错。
        self.assertEqual(html.escape(WS_NUMBER), ESCAPED_WS_NUMBER)
        self.assertEqual(html.escape(WS_CUSTOMER), ESCAPED_WS_CUSTOMER)
        self.assertEqual(html.escape(WS_DESC_MULTILINE), ESCAPED_WS_DESC_MULTILINE)
        self.assertEqual(html.escape(WS_DESC_BLANK_LINES), ESCAPED_WS_DESC_BLANK_LINES)

        payload = {
            "number": WS_NUMBER,
            "customer": WS_CUSTOMER,
            "items": [
                {"description": WS_DESC_MULTILINE, "quantity": 2, "unit_price": 1250},
                {"description": WS_DESC_BLANK_LINES, "quantity": 1, "unit_price": 300},
            ],
        }
        save_result, db_path = self.save(payload)
        self.assertEqual(save_result.returncode, 0, msg=save_result.stderr)
        self.assertEqual(save_result.stdout, f"{WS_NUMBER}\n")
        self.assertEqual(save_result.stderr, "")

        output_path = os.path.join(self.workdir, "preview-ws.html")
        preview_result = self.run_preview(db_path, WS_NUMBER, output_path)
        self.assertEqual(preview_result.returncode, 0, msg=preview_result.stderr)
        self.assertEqual(preview_result.stdout, f"{output_path}\n")
        self.assertEqual(preview_result.stderr, "")

        with open(output_path, "rb") as f:
            document = f.read().decode("utf-8")

        # 正文样式：编号/客户（dl dd）与明细说明（tbody 第二列）均以
        # pre-wrap 展示，首尾空格、连续空格、换行与连续换行（空行）按原样呈现。
        self.assertIn("dl dd { white-space: pre-wrap; }", document)
        self.assertIn("tbody td:nth-child(2) { white-space: pre-wrap; }", document)

        # 源文中编号与客户（含多行）原样转义保留在各自 <dd> 内。
        self.assertIn(f"<dd>{ESCAPED_WS_NUMBER}</dd>", document)
        self.assertIn(
            f"<dd>{ESCAPED_WS_CUSTOMER}</dd>", document,
            msg="多行客户应完整保留在客户信息区域的同一个 <dd> 内",
        )

        # 多行说明（含连续换行）原样转义保留在同一个 <td> 内，不拆行。
        self.assertIn(f"<td>{ESCAPED_WS_DESC_MULTILINE}</td>", document)
        self.assertIn(
            f"<td>{ESCAPED_WS_DESC_BLANK_LINES}</td>", document,
            msg="连续换行形成的空行应保留在同一条明细的单元格内",
        )
        tbody = document[document.index("<tbody>"):document.index("</tbody>")]
        self.assertEqual(
            tbody.count("<tr>"), 2,
            msg="两条明细仍各是一行表格行，多行说明不得拆出新的表格行",
        )

        # 金额与顺序：行金额 25.00、3.00，总计 28.00，两条明细按输入顺序。
        markers = [
            ESCAPED_WS_DESC_MULTILINE,
            '<td class="num">25.00</td>',
            ESCAPED_WS_DESC_BLANK_LINES,
            '<td class="num">3.00</td>',
            '<td class="num">28.00</td>',
        ]
        positions = [document.index(marker) for marker in markers]
        self.assertEqual(positions, sorted(positions))

        # 用户输入不产生 script 元素；实体样式文本再转义一层显示原文。
        self.assertNotIn("<script", document.lower())
        self.assertIn("&amp;lt;资料&amp;gt;", document)
        self.assertNotIn("&lt;资料&gt;", document)

        # 解析后的可见文本：编号、客户、说明均逐字符等于原文（含空行）。
        collector = TextAndTagCollector()
        collector.feed(document)
        self.assertNotIn("script", collector.start_tags)
        visible_text = "".join(collector.chunks)
        self.assertIn(WS_NUMBER, visible_text)
        self.assertIn(WS_CUSTOMER, visible_text)
        self.assertIn(WS_DESC_MULTILINE, visible_text)
        self.assertIn(WS_DESC_BLANK_LINES, visible_text)
        self.assertIn("&lt;资料&gt;", visible_text)

    # ---- 同一编号多次预览：新路径成功且逐字节一致；拒绝覆盖；未知编号 ----

    def test_new_paths_identical_and_existing_or_unknown_refused(self):
        save_result, db_path = self.save(make_payload())
        self.assertEqual(save_result.returncode, 0, msg=save_result.stderr)
        self.assertEqual(save_result.stdout, f"{NUMBER}\n")
        self.assertEqual(save_result.stderr, "")

        before_snapshot = self.read_rows(db_path)
        self.assertEqual(before_snapshot, self.expected_rows())

        # 同一编号输出到两个不同的新路径：都成功，输出各自路径。
        path_a = os.path.join(self.workdir, "preview-a.html")
        path_b = os.path.join(self.workdir, "preview-b.html")
        result_a = self.run_preview(db_path, NUMBER, path_a)
        self.assertEqual(result_a.returncode, 0, msg=result_a.stderr)
        self.assertEqual(result_a.stdout, f"{path_a}\n")
        self.assertEqual(result_a.stderr, "")

        result_b = self.run_preview(db_path, NUMBER, path_b)
        self.assertEqual(result_b.returncode, 0, msg=result_b.stderr)
        self.assertEqual(result_b.stdout, f"{path_b}\n")
        self.assertEqual(result_b.stderr, "")

        # 两份 HTML 逐字节一致。
        with open(path_a, "rb") as f:
            bytes_a = f.read()
        with open(path_b, "rb") as f:
            bytes_b = f.read()
        self.assertEqual(bytes_a, bytes_b, msg="同一报价两次预览应逐字节一致")

        # 再次输出到已有路径：退出码 1、标准输出为空、标准错误恰为约定的一行。
        result_overwrite = self.run_preview(db_path, NUMBER, path_a)
        self.assertEqual(
            result_overwrite.returncode, 1,
            msg=f"覆盖已有文件应退出码 1，实际 stderr={result_overwrite.stderr!r}",
        )
        self.assertEqual(result_overwrite.stdout, "", msg="拒绝时标准输出必须为空")
        self.assertEqual(
            result_overwrite.stderr,
            f"错误: 输出文件已存在: {path_a}\n",
            msg="标准错误只能是“错误: 输出文件已存在: ”加传入路径和换行",
        )
        with open(path_a, "rb") as f:
            self.assertEqual(f.read(), bytes_a, msg="原文件字节必须保持不变")

        # 未知编号 + 新路径：退出码 1、标准输出为空、标准错误恰为编号不存在，
        # 目标文件不创建。
        missing_path = os.path.join(self.workdir, "preview-missing.html")
        self.assertFalse(os.path.exists(missing_path))
        result_missing = self.run_preview(db_path, UNKNOWN_NUMBER, missing_path)
        self.assertEqual(
            result_missing.returncode, 1,
            msg=f"未知编号应退出码 1，实际 stderr={result_missing.stderr!r}",
        )
        self.assertEqual(result_missing.stdout, "", msg="拒绝时标准输出必须为空")
        self.assertEqual(
            result_missing.stderr,
            f"错误: 报价编号不存在: {UNKNOWN_NUMBER}\n",
            msg="标准错误只能是“错误: 报价编号不存在: ”加编号和换行",
        )
        self.assertFalse(
            os.path.exists(missing_path),
            msg="编号不存在时不得创建目标文件",
        )

        # 未知编号 + 已存在路径：仍优先得到“输出文件已存在”，原文件不变。
        result_priority = self.run_preview(db_path, UNKNOWN_NUMBER, path_a)
        self.assertEqual(result_priority.returncode, 1)
        self.assertEqual(result_priority.stdout, "")
        self.assertEqual(
            result_priority.stderr,
            f"错误: 输出文件已存在: {path_a}\n",
            msg="目标已存在时应优先报输出文件已存在，而不是编号不存在",
        )
        self.assertNotIn("报价编号不存在", result_priority.stderr)
        with open(path_a, "rb") as f:
            self.assertEqual(f.read(), bytes_a, msg="原文件字节必须保持不变")

        # 所有预览调用（含成功与被拒绝的）都不改变已保存的报价及明细。
        self.assertEqual(
            self.read_rows(db_path), before_snapshot,
            msg="预览只读取数据库，报价与明细必须保持不变",
        )


if __name__ == "__main__":
    unittest.main()
