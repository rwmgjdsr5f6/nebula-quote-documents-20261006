"""导出 -> 重新保存 -> 预览 的往返回归测试：最终 HTML 文档逐字节一致且符合固定预期。

仅依赖 Python 标准库。所有用例在独立的临时目录中通过 README 记载的
公开命令（save / export / preview）操作 quote.py 子进程：把源库导出的
原始 UTF-8 字节原样写入文件，交给另一份新数据库保存，再分别生成两个
新路径的预览。预期 HTML 为测试内写死的固定样例文档，不调用 quote.py
内部的渲染函数生成答案。测试不读取项目内业务数据，也不在项目目录留下
JSON、SQLite 或 HTML 文件；临时目录在用例结束或失败时自动清理。

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

INT64_MAX = (1 << 63) - 1

# ---- 主样例 Q-COPY-001：客户与说明覆盖尖括号、与号、script 文本、
# ---- 已有实体串、首尾空格与换行；两条明细说明相同 ----

NUMBER = "Q-COPY-001"
CUSTOMER = " 演示客户 <甲>&乙 "
NOTE = "  <script>演示</script>\n&lt;原文&gt;  "
ITEMS = [
    {"description": "服务", "quantity": 2, "unit_price": 1250},
    {"description": "服务", "quantity": 1, "unit_price": 300},
]
TOTAL_CENTS = 2800

# 期望出现在 HTML 源文中的转义形式（标准库 html.escape 默认转 & < > " '）。
ESCAPED_CUSTOMER = " 演示客户 &lt;甲&gt;&amp;乙 "
ESCAPED_NOTE = "  &lt;script&gt;演示&lt;/script&gt;\n&amp;lt;原文&amp;gt;  "

NO_NOTE_NUMBER = "Q-COPY-002"
WHITESPACE_NOTE_NUMBER = "Q-COPY-003"
WHITESPACE_NOTE = " \n "
INT64_NUMBER = "Q-COPY-INT64"
INT64_CUSTOMER = "演示客户"


def make_payload(number=NUMBER, note=NOTE):
    payload = {
        "number": number,
        "customer": CUSTOMER,
        "items": [dict(item) for item in ITEMS],
    }
    if note is not None:
        payload["note"] = note
    return payload


# ---- 固定预期文档：由样例数据逐字符推出，写死在测试中 ----

EXPECTED_MAIN_DOCUMENT = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>报价单 Q-COPY-001</title>
<style>
  body { font-family: sans-serif; margin: 2em; }
  table { border-collapse: collapse; margin-top: 1em; }
  th, td { border: 1px solid #999; padding: 0.4em 0.8em; }
  .num { text-align: right; white-space: nowrap; }
  tfoot td { font-weight: bold; }
  dl dd { white-space: pre-wrap; }
  tbody td:nth-child(2) { white-space: pre-wrap; }
  .note-text { white-space: pre-wrap; }
</style>
</head>
<body>
<h1>报价单</h1>
<dl>
  <dt>报价编号</dt><dd>Q-COPY-001</dd>
  <dt>客户</dt><dd> 演示客户 &lt;甲&gt;&amp;乙 </dd>
</dl>
<section class="note">
  <h2>客户说明</h2>
  <p class="note-text">  &lt;script&gt;演示&lt;/script&gt;
&amp;lt;原文&amp;gt;  </p>
</section>
<table>
  <thead>
    <tr><th>#</th><th>说明</th><th>数量</th><th>单价（元）</th><th>行金额（元）</th></tr>
  </thead>
  <tbody>
      <tr><td>1</td><td>服务</td><td class="num">2</td><td class="num">12.50</td><td class="num">25.00</td></tr>
      <tr><td>2</td><td>服务</td><td class="num">1</td><td class="num">3.00</td><td class="num">3.00</td></tr>
  </tbody>
  <tfoot>
    <tr><td colspan="4">总计（元）</td><td class="num">28.00</td></tr>
  </tfoot>
</table>
</body>
</html>
"""

EXPECTED_NO_NOTE_DOCUMENT = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>报价单 Q-COPY-002</title>
<style>
  body { font-family: sans-serif; margin: 2em; }
  table { border-collapse: collapse; margin-top: 1em; }
  th, td { border: 1px solid #999; padding: 0.4em 0.8em; }
  .num { text-align: right; white-space: nowrap; }
  tfoot td { font-weight: bold; }
  dl dd { white-space: pre-wrap; }
  tbody td:nth-child(2) { white-space: pre-wrap; }
</style>
</head>
<body>
<h1>报价单</h1>
<dl>
  <dt>报价编号</dt><dd>Q-COPY-002</dd>
  <dt>客户</dt><dd> 演示客户 &lt;甲&gt;&amp;乙 </dd>
</dl>
<table>
  <thead>
    <tr><th>#</th><th>说明</th><th>数量</th><th>单价（元）</th><th>行金额（元）</th></tr>
  </thead>
  <tbody>
      <tr><td>1</td><td>服务</td><td class="num">2</td><td class="num">12.50</td><td class="num">25.00</td></tr>
      <tr><td>2</td><td>服务</td><td class="num">1</td><td class="num">3.00</td><td class="num">3.00</td></tr>
  </tbody>
  <tfoot>
    <tr><td colspan="4">总计（元）</td><td class="num">28.00</td></tr>
  </tfoot>
</table>
</body>
</html>
"""

EXPECTED_WHITESPACE_NOTE_DOCUMENT = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>报价单 Q-COPY-003</title>
<style>
  body { font-family: sans-serif; margin: 2em; }
  table { border-collapse: collapse; margin-top: 1em; }
  th, td { border: 1px solid #999; padding: 0.4em 0.8em; }
  .num { text-align: right; white-space: nowrap; }
  tfoot td { font-weight: bold; }
  dl dd { white-space: pre-wrap; }
  tbody td:nth-child(2) { white-space: pre-wrap; }
  .note-text { white-space: pre-wrap; }
</style>
</head>
<body>
<h1>报价单</h1>
<dl>
  <dt>报价编号</dt><dd>Q-COPY-003</dd>
  <dt>客户</dt><dd> 演示客户 &lt;甲&gt;&amp;乙 </dd>
</dl>
<section class="note">
  <h2>客户说明</h2>
  <p class="note-text"> """ + """
 </p>
</section>
<table>
  <thead>
    <tr><th>#</th><th>说明</th><th>数量</th><th>单价（元）</th><th>行金额（元）</th></tr>
  </thead>
  <tbody>
      <tr><td>1</td><td>服务</td><td class="num">2</td><td class="num">12.50</td><td class="num">25.00</td></tr>
      <tr><td>2</td><td>服务</td><td class="num">1</td><td class="num">3.00</td><td class="num">3.00</td></tr>
  </tbody>
  <tfoot>
    <tr><td colspan="4">总计（元）</td><td class="num">28.00</td></tr>
  </tfoot>
</table>
</body>
</html>
"""

EXPECTED_INT64_DOCUMENT = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>报价单 Q-COPY-INT64</title>
<style>
  body { font-family: sans-serif; margin: 2em; }
  table { border-collapse: collapse; margin-top: 1em; }
  th, td { border: 1px solid #999; padding: 0.4em 0.8em; }
  .num { text-align: right; white-space: nowrap; }
  tfoot td { font-weight: bold; }
  dl dd { white-space: pre-wrap; }
  tbody td:nth-child(2) { white-space: pre-wrap; }
</style>
</head>
<body>
<h1>报价单</h1>
<dl>
  <dt>报价编号</dt><dd>Q-COPY-INT64</dd>
  <dt>客户</dt><dd>演示客户</dd>
</dl>
<table>
  <thead>
    <tr><th>#</th><th>说明</th><th>数量</th><th>单价（元）</th><th>行金额（元）</th></tr>
  </thead>
  <tbody>
      <tr><td>1</td><td>服务</td><td class="num">1</td><td class="num">92233720368547758.07</td><td class="num">92233720368547758.07</td></tr>
  </tbody>
  <tfoot>
    <tr><td colspan="4">总计（元）</td><td class="num">92233720368547758.07</td></tr>
  </tfoot>
</table>
</body>
</html>
"""


class VisibleTextParser(HTMLParser):
    """收集可见文本、起始标签与说明区域正文。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.chunks = []
        self.start_tags = []
        self._in_note_text = False
        self._note_chunks = []

    def handle_starttag(self, tag, attrs):
        self.start_tags.append(tag)
        if tag == "p" and ("class", "note-text") in attrs:
            self._in_note_text = True

    def handle_endtag(self, tag):
        if tag == "p" and self._in_note_text:
            self._in_note_text = False

    def handle_data(self, data):
        self.chunks.append(data)
        if self._in_note_text:
            self._note_chunks.append(data)

    @property
    def visible_text(self):
        return "".join(self.chunks)

    @property
    def note_text(self):
        return "".join(self._note_chunks)


def parse_document(document):
    parser = VisibleTextParser()
    parser.feed(document)
    parser.close()
    return parser


class ExportPreviewFlowTestBase(unittest.TestCase):
    """每个用例使用独立临时目录，只通过公开命令行入口驱动 quote.py。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="quote-export-preview-test-")
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

    def run_quote_bytes(self, *cli_args):
        """以字节方式捕获，便于逐字节断言导出的原始 UTF-8 内容。"""
        return subprocess.run(
            [sys.executable, QUOTE_PY, *cli_args],
            capture_output=True,
            cwd=self.workdir,
        )

    def write_input(self, payload, name):
        path = os.path.join(self.workdir, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        return path

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

    def roundtrip(self, payload, number, tag):
        """save(源库) -> export -> 导出字节原样 save(新库) -> 两库各自 preview。

        各步均断言退出码 0、标准错误为空；返回
        (source_db, copy_db, source_html, copy_html, exported_bytes)。
        """
        source_db = os.path.join(self.workdir, f"{tag}-source.sqlite")
        copy_db = os.path.join(self.workdir, f"{tag}-copy.sqlite")

        # 第一步：保存到源库。
        input_path = self.write_input(payload, f"{tag}-input.json")
        save_result = self.run_quote("save", "--db", source_db, "--input", input_path)
        self.assertEqual(
            save_result.returncode, 0,
            msg=f"样例 {number} 源库保存应成功: {save_result.stderr}",
        )
        self.assertEqual(
            save_result.stdout, f"{number}\n",
            msg="保存成功后标准输出只能是编号加换行",
        )
        self.assertEqual(save_result.stderr, "", msg="保存成功时标准错误必须为空")

        source_snapshot = self.snapshot(source_db)

        # 第二步：导出源库，保留原始 UTF-8 字节。
        export_result = self.run_quote_bytes(
            "export", "--db", source_db, "--number", number
        )
        self.assertEqual(
            export_result.returncode, 0,
            msg=f"样例 {number} 导出应成功: {export_result.stderr!r}",
        )
        self.assertEqual(
            export_result.stderr, b"", msg="导出成功时标准错误必须为空"
        )
        exported = export_result.stdout
        exported.decode("utf-8")  # 必须是完整合法的 UTF-8

        # 第三步：导出字节原样写入文件，保存到另一份新数据库。
        exported_path = os.path.join(self.workdir, f"{tag}-exported.json")
        with open(exported_path, "wb") as f:
            f.write(exported)
        resave_result = self.run_quote(
            "save", "--db", copy_db, "--input", exported_path
        )
        self.assertEqual(
            resave_result.returncode, 0,
            msg=f"样例 {number} 目标库保存应成功: {resave_result.stderr}",
        )
        self.assertEqual(
            resave_result.stdout, f"{number}\n",
            msg="重新保存成功后标准输出只能是编号加换行",
        )
        self.assertEqual(resave_result.stderr, "", msg="重新保存成功时标准错误必须为空")

        # 第四步：源库与目标库分别预览到两个新路径。
        html_bytes = []
        for label, db_path in (("source", source_db), ("copy", copy_db)):
            output_path = os.path.join(self.workdir, f"{tag}-{label}.html")
            preview_result = self.run_quote(
                "preview", "--db", db_path, "--number", number,
                "--output", output_path,
            )
            self.assertEqual(
                preview_result.returncode, 0,
                msg=f"样例 {number} 预览应成功: {preview_result.stderr}",
            )
            self.assertEqual(
                preview_result.stdout, f"{output_path}\n",
                msg="预览成功后标准输出只能是目标路径加换行",
            )
            self.assertEqual(
                preview_result.stderr, "", msg="预览成功时标准错误必须为空"
            )
            with open(output_path, "rb") as f:
                html_bytes.append(f.read())

        # 导出与预览只读取源库：数据与表结构必须保持不变。
        self.assertEqual(
            self.snapshot(source_db), source_snapshot,
            msg=f"样例 {number} 导出与预览后源库数据与结构必须不变",
        )

        return source_db, copy_db, html_bytes[0], html_bytes[1], exported


class ExportPreviewRoundtripTestCase(ExportPreviewFlowTestBase):
    """主样例：导出再保存后，两份预览逐字节一致且逐字符等于固定预期文档。"""

    def test_main_sample_roundtrip_byte_identical_and_matches_fixed_document(self):
        # 期望常量自检：它们确实是标准库对用户原文的转义结果，防止测试常量写错。
        self.assertEqual(html.escape(CUSTOMER), ESCAPED_CUSTOMER)
        self.assertEqual(html.escape(NOTE), ESCAPED_NOTE)

        source_db, copy_db, source_html, copy_html, exported = self.roundtrip(
            make_payload(), NUMBER, "main"
        )

        # 重新保存后落库内容：客户与说明逐字符不变；相同说明的明细仍是两行
        # 且顺序不变；数量与单价保持整数，行金额与合计为整数分。
        quotes, items, _ = self.snapshot(copy_db)
        self.assertEqual(
            quotes, [(NUMBER, CUSTOMER, TOTAL_CENTS, NOTE)],
            msg="重新保存后客户与说明必须逐字符不变（首尾空格、换行、尖括号原样）",
        )
        self.assertEqual(
            items,
            [
                (NUMBER, 0, "服务", 2, 1250, 2500),
                (NUMBER, 1, "服务", 1, 300, 300),
            ],
            msg="相同说明的明细仍是两行且顺序不变，行金额保持整数分",
        )
        for row in items:
            self.assertIsInstance(row[3], int, msg="数量必须保持整数")
            self.assertIsInstance(row[4], int, msg="单价必须保持整数")

        # 导出 JSON 中数量与单价也是整数（不是布尔、不是浮点）。
        exported_obj = json.loads(exported.decode("utf-8"))
        self.assertEqual(exported_obj["customer"], CUSTOMER)
        self.assertEqual(exported_obj["note"], NOTE)
        self.assertEqual(
            [(i["description"], i["quantity"], i["unit_price"])
             for i in exported_obj["items"]],
            [("服务", 2, 1250), ("服务", 1, 300)],
        )
        for item in exported_obj["items"]:
            self.assertIsInstance(item["quantity"], int)
            self.assertNotIsInstance(item["quantity"], bool)
            self.assertIsInstance(item["unit_price"], int)
            self.assertNotIsInstance(item["unit_price"], bool)

        # 两份 HTML 逐字节一致。
        self.assertEqual(
            source_html, copy_html,
            msg="源库与目标库生成的两份预览必须逐字节一致",
        )

        document = source_html.decode("utf-8")

        # 金额：行金额 25.00、3.00，总计 28.00。
        self.assertIn(
            '<td class="num">25.00</td>', document,
            msg="第一行行金额应显示 25.00 元",
        )
        self.assertEqual(
            document.count('<td class="num">3.00</td>'), 2,
            msg="第二行单价与行金额应各出现一次 3.00",
        )
        self.assertIn(
            '<tr><td colspan="4">总计（元）</td><td class="num">28.00</td></tr>',
            document,
            msg="总计应显示 28.00 元",
        )

        # 说明区域位于客户信息之后、明细表格之前。
        self.assertIn('<section class="note">', document, "应出现客户说明区域")
        dl_end = document.index("</dl>")
        section_at = document.index('<section class="note">')
        table_at = document.index("<table>")
        self.assertLess(dl_end, section_at, "说明区域应位于客户信息之后")
        self.assertLess(section_at, table_at, "说明区域应位于明细表格之前")

        # 尖括号与已有实体串显示原文，不产生 script 元素。
        self.assertNotIn("<script", document.lower())
        self.assertNotIn("</script", document.lower())
        self.assertIn("&amp;lt;原文&amp;gt;", document)
        self.assertNotIn("&lt;原文&gt;", document)
        parser = parse_document(document)
        self.assertNotIn(
            "script", parser.start_tags, msg="页面不得出现 script 元素"
        )
        self.assertEqual(
            parser.note_text, NOTE,
            msg="解析出的说明文本应逐字符等于输入原文（首尾空格与换行保留）",
        )
        self.assertIn(CUSTOMER, parser.visible_text, msg="客户首尾空格不得被删除")
        self.assertIn("<script>演示</script>", parser.visible_text)
        self.assertIn("&lt;原文&gt;", parser.visible_text)

        # 整篇文档核对固定预期（不只比较两份结果是否相同）。
        self.assertEqual(
            document, EXPECTED_MAIN_DOCUMENT,
            msg="源库预览整篇必须与固定预期文档逐字符一致",
        )
        self.assertEqual(
            copy_html.decode("utf-8"), EXPECTED_MAIN_DOCUMENT,
            msg="目标库预览整篇必须与固定预期文档逐字符一致",
        )

    def test_resave_same_export_in_target_db_rejected_as_duplicate(self):
        """目标库再次保存同一导出内容：退出 1、标准输出为空、既有数据不变。"""
        _, copy_db, _, _, exported = self.roundtrip(make_payload(), NUMBER, "dup")

        before = self.snapshot(copy_db)
        exported_path = os.path.join(self.workdir, "dup-exported.json")
        with open(exported_path, "wb") as f:
            f.write(exported)
        result = self.run_quote("save", "--db", copy_db, "--input", exported_path)
        self.assertEqual(
            result.returncode, 1,
            msg=f"重复编号应退出码 1，实际 stderr={result.stderr!r}",
        )
        self.assertEqual(result.stdout, "", msg="拒绝时标准输出必须为空")
        self.assertEqual(
            result.stderr, f"错误: 报价编号已存在: {NUMBER}\n",
            msg="标准错误应说明报价编号已存在",
        )
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(
            self.snapshot(copy_db), before,
            msg="重复保存被拒绝后，既有报价和明细必须保持不变",
        )


class ExportPreviewNoteVariantsTestCase(ExportPreviewFlowTestBase):
    """同一往返流程覆盖说明的省略与纯空白两种形态。"""

    def test_omitted_note_roundtrip_hides_note_section(self):
        """省略说明：重新保存后说明区域隐藏，其余内容与固定预期一致。"""
        _, copy_db, source_html, copy_html, exported = self.roundtrip(
            make_payload(number=NO_NOTE_NUMBER, note=None),
            NO_NOTE_NUMBER,
            "nonote",
        )

        # 导出对象不含 note；目标库中说明为 NULL。
        exported_obj = json.loads(exported.decode("utf-8"))
        self.assertNotIn("note", exported_obj, msg="省略说明时导出不得包含 note")
        quotes, _, _ = self.snapshot(copy_db)
        self.assertEqual(
            quotes, [(NO_NOTE_NUMBER, CUSTOMER, TOTAL_CENTS, None)],
            msg="省略说明重新保存后数据库 note 应为 NULL",
        )

        # 两份预览逐字节一致，且说明区域隐藏。
        self.assertEqual(
            source_html, copy_html,
            msg="省略说明时两份预览必须逐字节一致",
        )
        document = source_html.decode("utf-8")
        self.assertNotIn('<section class="note">', document, "不应出现客户说明区域")
        self.assertNotIn("客户说明", document)
        self.assertNotIn("note-text", document)
        parser = parse_document(document)
        self.assertEqual(parser.note_text, "")

        # 整篇文档核对固定预期。
        self.assertEqual(
            document, EXPECTED_NO_NOTE_DOCUMENT,
            msg="省略说明的预览整篇必须与固定预期文档逐字符一致",
        )
        self.assertEqual(
            copy_html.decode("utf-8"), EXPECTED_NO_NOTE_DOCUMENT,
        )

    def test_whitespace_note_roundtrip_preserves_section_and_text(self):
        """说明为 " \\n "：说明区域保留，正文逐字符等于原文。"""
        _, copy_db, source_html, copy_html, exported = self.roundtrip(
            make_payload(number=WHITESPACE_NOTE_NUMBER, note=WHITESPACE_NOTE),
            WHITESPACE_NOTE_NUMBER,
            "wsnote",
        )

        # 纯空白说明原样导出、原样落库。
        exported_obj = json.loads(exported.decode("utf-8"))
        self.assertEqual(
            exported_obj["note"], WHITESPACE_NOTE,
            msg="纯空白说明必须原样导出",
        )
        quotes, _, _ = self.snapshot(copy_db)
        self.assertEqual(
            quotes, [(WHITESPACE_NOTE_NUMBER, CUSTOMER, TOTAL_CENTS, WHITESPACE_NOTE)],
            msg="纯空白说明重新保存后必须逐字符不变",
        )

        # 两份预览逐字节一致，说明区域保留且正文为原文。
        self.assertEqual(
            source_html, copy_html,
            msg="纯空白说明时两份预览必须逐字节一致",
        )
        document = source_html.decode("utf-8")
        self.assertIn('<section class="note">', document, "应保留客户说明区域")
        self.assertIn(
            '<p class="note-text"> \n </p>', document,
            msg="说明正文应逐字符保留首尾空格与换行",
        )
        parser = parse_document(document)
        self.assertEqual(
            parser.note_text, WHITESPACE_NOTE,
            msg="解析出的说明文本应逐字符等于原文",
        )

        # 整篇文档核对固定预期。
        self.assertEqual(
            document, EXPECTED_WHITESPACE_NOTE_DOCUMENT,
            msg="纯空白说明的预览整篇必须与固定预期文档逐字符一致",
        )
        self.assertEqual(
            copy_html.decode("utf-8"), EXPECTED_WHITESPACE_NOTE_DOCUMENT,
        )


class ExportPreviewInt64TestCase(ExportPreviewFlowTestBase):
    """单行大整数报价：往返全程没有整数精度损失。"""

    def test_int64_max_unit_price_roundtrip_without_precision_loss(self):
        payload = {
            "number": INT64_NUMBER,
            "customer": INT64_CUSTOMER,
            "items": [
                {"description": "服务", "quantity": 1, "unit_price": INT64_MAX},
            ],
        }
        _, copy_db, source_html, copy_html, exported = self.roundtrip(
            payload, INT64_NUMBER, "int64"
        )

        # 导出原文中单价是完整的十进制整数，解析后仍等于 int64 上限。
        exported_text = exported.decode("utf-8")
        self.assertIn(
            str(INT64_MAX), exported_text,
            msg="导出 JSON 中单价必须是完整的 9223372036854775807",
        )
        exported_obj = json.loads(exported_text)
        exported_price = exported_obj["items"][0]["unit_price"]
        self.assertIsInstance(exported_price, int)
        self.assertEqual(
            exported_price, INT64_MAX,
            msg="导出单价必须精确保留有符号 64 位整数上限",
        )

        # 重新保存后单价、行金额与合计仍是精确的整数。
        quotes, items, _ = self.snapshot(copy_db)
        self.assertEqual(
            quotes, [(INT64_NUMBER, INT64_CUSTOMER, INT64_MAX, None)],
            msg="重新保存后合计必须精确等于 9223372036854775807 分",
        )
        self.assertEqual(
            items, [(INT64_NUMBER, 0, "服务", 1, INT64_MAX, INT64_MAX)],
            msg="重新保存后单价与行金额必须精确等于 int64 上限",
        )
        for row in items:
            self.assertIsInstance(row[4], int)
            self.assertIsInstance(row[5], int)

        # 两份预览逐字节一致，金额显示为 92233720368547758.07 元。
        self.assertEqual(
            source_html, copy_html,
            msg="大整数报价的两份预览必须逐字节一致",
        )
        document = source_html.decode("utf-8")
        self.assertEqual(
            document.count('<td class="num">92233720368547758.07</td>'), 3,
            msg="单价、行金额与总计应各显示一次 92233720368547758.07",
        )

        # 整篇文档核对固定预期。
        self.assertEqual(
            document, EXPECTED_INT64_DOCUMENT,
            msg="大整数报价的预览整篇必须与固定预期文档逐字符一致",
        )
        self.assertEqual(
            copy_html.decode("utf-8"), EXPECTED_INT64_DOCUMENT,
        )


# 项目目录清洁性：测试开始前记录项目根内容，全部用例结束后不得新增任何文件。
_PROJECT_ROOT_ENTRIES_AT_START = set(os.listdir(PROJECT_ROOT))


def tearDownModule():
    leftovers = set(os.listdir(PROJECT_ROOT)) - _PROJECT_ROOT_ENTRIES_AT_START
    assert not leftovers, f"测试在项目根目录留下了文件: {sorted(leftovers)}"


if __name__ == "__main__":
    unittest.main()
