"""导出后重新保存仍生成相同预览的端到端回归测试（export -> save -> preview）。

仅依赖 Python 标准库。所有用例在独立的临时目录中通过 README 记载的
公开命令（save / export / preview）操作 quote.py 子进程：先在源库保存
固定样例，再把 export 输出的原始 UTF-8 字节原样交给另一份新数据库
重新保存，分别从两个数据库向两个全新路径生成预览并核对最终 HTML。

覆盖范围：
  1. 主样例 Q-COPY-001：客户与说明含首尾空格、尖括号、与号、原有实体串；
     两条同说明明细；客户与说明逐字符往返、明细两行顺序不变、数量与单价
     保持整数；两份预览逐字节一致，且按固定预期核对最终文档（行金额
     25.00/3.00、总计 28.00、说明区域位置、原文显示且不产生 script 元素），
     不只比较两份结果是否相同；
  2. 同一流程覆盖省略说明与 " \\n " 纯空白说明：分别隐藏/保留说明区域，
     并核对说明原文；
  3. 数量 1、单价 9223372036854775807（int64 上限）的单行报价：预览金额
     92233720368547758.07，往返无整数精度损失；
  4. 在目标库再次保存同一导出内容：退出 1、标准输出为空、标准错误说明
     报价编号已存在，既有报价与明细不变；
  5. 导出与预览均只读：源库数据与表结构保持不变，不留下 WAL/SHM 侧车文件。

运行方式（项目根目录）：
    python3 -m unittest discover -s tests
全部通过时退出码为 0，任一失败时非零退出并指出对应样例与不符项。
预期值全部来自下方固定样例常量，不调用 quote.py 的内部渲染函数生成答案。
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

# ---- 主样例 Q-COPY-001：客户首尾空格、尖括号、与号；说明含 script 与原实体串 ----

NUMBER = "Q-COPY-001"
CUSTOMER = " 演示客户 <甲>&乙 "
NOTE = "  <script>演示</script>\n&lt;原文&gt;  "
DESCRIPTION = "服务"
ITEMS = [
    {"description": "服务", "quantity": 2, "unit_price": 1250},
    {"description": "服务", "quantity": 1, "unit_price": 300},
]
TOTAL_CENTS = 2800

# 固定预期：标准库 html.escape 的转义形式（仅用于与字面常量自检，
# 不调用 quote.py 的内部渲染函数）。
ESCAPED_CUSTOMER = " 演示客户 &lt;甲&gt;&amp;乙 "
ESCAPED_NOTE = (
    "  &lt;script&gt;演示&lt;/script&gt;\n&amp;lt;原文&amp;gt;  "
)

# 明细表中两行与总计的固定 HTML 片段。
ROW_1 = (
    '<tr><td>1</td><td>服务</td>'
    '<td class="num">2</td>'
    '<td class="num">12.50</td>'
    '<td class="num">25.00</td></tr>'
)
ROW_2 = (
    '<tr><td>2</td><td>服务</td>'
    '<td class="num">1</td>'
    '<td class="num">3.00</td>'
    '<td class="num">3.00</td></tr>'
)
TOTAL_FOOT = (
    '<tr><td colspan="4">总计（元）</td>'
    '<td class="num">28.00</td></tr>'
)

# ---- 说明变体与大整数样例 ----

NOTE_OMITTED_NUMBER = "Q-COPY-NONOTE"
NOTE_WHITESPACE = " \n "
NOTE_WS_NUMBER = "Q-COPY-WS"

BIG_NUMBER = "Q-COPY-BIG"
BIG_UNIT_PRICE = 9223372036854775807  # int64 有符号上限
BIG_YUAN = "92233720368547758.07"


class _NoteSentinel:
    """哨兵：表示 payload 中省略 note 字段。"""


_NOTE_SENTINEL = _NoteSentinel()


def build_payload(number, customer=CUSTOMER, note=_NOTE_SENTINEL, items=None):
    """构造输入 JSON；note 为哨兵时省略该字段。"""
    payload = {
        "number": number,
        "customer": customer,
        "items": [dict(item) for item in (ITEMS if items is None else items)],
    }
    if note is not _NOTE_SENTINEL:
        payload["note"] = note
    return payload


class NoteSectionParser(HTMLParser):
    """统计说明区域、提取 <p class="note-text"> 文本并记录全部起始标签。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.section_count = 0
        self.start_tags = []
        self._in_note_text = False
        self._note_chunks = []

    def handle_starttag(self, tag, attrs):
        self.start_tags.append(tag)
        if tag == "section" and ("class", "note") in attrs:
            self.section_count += 1
        if tag == "p" and ("class", "note-text") in attrs:
            self._in_note_text = True

    def handle_endtag(self, tag):
        if tag == "p" and self._in_note_text:
            self._in_note_text = False

    def handle_data(self, data):
        if self._in_note_text:
            self._note_chunks.append(data)

    @property
    def note_text(self):
        return "".join(self._note_chunks)


def parse_document(document):
    parser = NoteSectionParser()
    parser.feed(document)
    parser.close()
    return parser


class ExportPreviewFlowBase(unittest.TestCase):
    """每个用例使用独立临时目录，只通过公开命令行入口驱动 quote.py。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="quote-export-preview-")
        self.addCleanup(self._tmp.cleanup)
        self.workdir = self._tmp.name

    # ---- 基础设施 ----

    def run_quote(self, *cli_args):
        """以字节方式捕获，便于逐字节断言编码、换行与中文错误信息。"""
        return subprocess.run(
            [sys.executable, QUOTE_PY, *cli_args],
            capture_output=True,
            cwd=self.workdir,
        )

    def path(self, name):
        return os.path.join(self.workdir, name)

    def write_input(self, payload, name):
        path = self.path(name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        return path

    def save_ok(self, payload, db_name, input_name, label):
        """保存并断言：退出 0、标准输出只有编号加换行、标准错误为空。"""
        db_path = self.path(db_name)
        input_path = self.write_input(payload, input_name)
        result = self.run_quote("save", "--db", db_path, "--input", input_path)
        self.assertEqual(
            result.returncode, 0,
            msg=f"样例[{label}]保存应退出 0，实际 {result.returncode}，"
                f"stderr={result.stderr.decode('utf-8', 'replace')!r}",
        )
        self.assertEqual(
            result.stderr, b"",
            msg=f"样例[{label}]保存成功时标准错误必须为空，"
                f"实际：{result.stderr!r}",
        )
        self.assertEqual(
            result.stdout, f"{payload['number']}\n".encode("utf-8"),
            msg=f"样例[{label}]保存成功后标准输出只能是编号加换行",
        )
        return db_path

    def export_ok(self, db_path, number, label):
        """导出并断言：退出 0、标准错误为空；返回原始 UTF-8 字节。"""
        result = self.run_quote("export", "--db", db_path, "--number", number)
        self.assertEqual(
            result.returncode, 0,
            msg=f"样例[{label}]导出应退出 0，实际 {result.returncode}，"
                f"stderr={result.stderr.decode('utf-8', 'replace')!r}",
        )
        self.assertEqual(
            result.stderr, b"",
            msg=f"样例[{label}]导出成功时标准错误必须为空，"
                f"实际：{result.stderr!r}",
        )
        raw = result.stdout
        self.assertTrue(
            raw.endswith(b"\n") and raw.count(b"\n") == 1 and b"\r" not in raw,
            msg=f"样例[{label}]导出字节必须仅以单个 LF 结尾、无 CR，实际：{raw!r}",
        )
        raw.decode("utf-8")  # 必须是完整合法的 UTF-8
        return raw

    def resave_exported_ok(self, raw_export_bytes, db_name, input_name, label):
        """把导出的原始字节原样写入文件并交给 save 存入另一份新数据库。"""
        input_path = self.path(input_name)
        with open(input_path, "wb") as f:
            f.write(raw_export_bytes)
        db_path = self.path(db_name)
        number = json.loads(raw_export_bytes.decode("utf-8"))["number"]
        result = self.run_quote("save", "--db", db_path, "--input", input_path)
        self.assertEqual(
            result.returncode, 0,
            msg=f"样例[{label}]导出内容重新保存应退出 0，实际 "
                f"{result.returncode}，"
                f"stderr={result.stderr.decode('utf-8', 'replace')!r}",
        )
        self.assertEqual(
            result.stderr, b"",
            msg=f"样例[{label}]重新保存成功时标准错误必须为空，"
                f"实际：{result.stderr!r}",
        )
        self.assertEqual(
            result.stdout, f"{number}\n".encode("utf-8"),
            msg=f"样例[{label}]重新保存成功后标准输出只能是编号加换行",
        )
        return db_path

    def preview_ok(self, db_path, number, output_name, label):
        """预览并断言：退出 0、标准输出只有目标路径加换行、标准错误为空。"""
        output_path = self.path(output_name)
        result = self.run_quote(
            "preview", "--db", db_path, "--number", number, "--output", output_path
        )
        self.assertEqual(
            result.returncode, 0,
            msg=f"样例[{label}]预览应退出 0，实际 {result.returncode}，"
                f"stderr={result.stderr.decode('utf-8', 'replace')!r}",
        )
        self.assertEqual(
            result.stderr, b"",
            msg=f"样例[{label}]预览成功时标准错误必须为空，"
                f"实际：{result.stderr!r}",
        )
        self.assertEqual(
            result.stdout, f"{output_path}\n".encode("utf-8"),
            msg=f"样例[{label}]预览成功后标准输出只能是目标路径加换行",
        )
        self.assertTrue(
            os.path.exists(output_path),
            msg=f"样例[{label}]预览成功后目标 HTML 必须存在",
        )
        with open(output_path, "rb") as f:
            return f.read()

    def snapshot(self, db_path):
        """完整快照：quotes 行、items 行（含自增 id）与表结构。"""
        conn = sqlite3.connect(db_path)
        try:
            quotes = conn.execute(
                "SELECT number, customer, total, note FROM quotes ORDER BY number"
            ).fetchall()
            items = conn.execute(
                "SELECT id, quote_number, position, description, quantity, "
                "unit_price, line_amount FROM items "
                "ORDER BY quote_number, position, id"
            ).fetchall()
            master = conn.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY name"
            ).fetchall()
        finally:
            conn.close()
        return quotes, items, master

    def assert_no_sidecars(self, db_path, label):
        for suffix in ("-wal", "-shm"):
            self.assertFalse(
                os.path.exists(db_path + suffix),
                msg=f"样例[{label}]只读操作不应留下侧车文件：{db_path}{suffix}",
            )

    def export_resave_preview(self, payload, tag, label):
        """完整往返：源库保存 -> 导出原始字节 -> 新库重存 -> 两个新路径预览。"""
        number = payload["number"]
        source_db = self.save_ok(
            payload, f"source-{tag}.sqlite", f"input-{tag}.json", label
        )
        raw_export = self.export_ok(source_db, number, label)
        copy_db = self.resave_exported_ok(
            raw_export, f"copy-{tag}.sqlite", f"exported-{tag}.json", label
        )
        source_html = self.preview_ok(
            source_db, number, f"source-{tag}.html", label
        )
        copy_html = self.preview_ok(
            copy_db, number, f"copy-{tag}.html", label
        )
        self.assertEqual(
            source_html, copy_html,
            msg=f"样例[{label}]源库与重新保存后的新库预览必须逐字节一致",
        )
        source_html.decode("utf-8")
        copy_html.decode("utf-8")
        return {
            "number": number,
            "source_db": source_db,
            "copy_db": copy_db,
            "raw_export": raw_export,
            "source_html": source_html,
            "copy_html": copy_html,
        }


class ExportResavePreviewTestCase(ExportPreviewFlowBase):
    """主样例：导出 -> 重新保存 -> 两个新路径预览，逐字节一致且固定预期相符。"""

    def test_export_resave_preview_byte_identical_with_fixed_expectations(self):
        label = "主样例Q-COPY-001"

        # 固定转义常量自检：确实是标准库对样例原文的转义结果，防止常量写错。
        self.assertEqual(html.escape(CUSTOMER), ESCAPED_CUSTOMER)
        self.assertEqual(html.escape(NOTE), ESCAPED_NOTE)
        self.assertEqual(2 * 1250, 2500)
        self.assertEqual(1 * 300, 300)
        self.assertEqual(2500 + 300, TOTAL_CENTS)
        self.assertEqual(BIG_UNIT_PRICE, INT64_MAX)

        payload = build_payload(NUMBER, CUSTOMER, NOTE, ITEMS)
        source_db = self.save_ok(payload, "source.sqlite", "input.json", label)

        # 源库导出前快照与文件字节：导出与预览都必须只读。
        snapshot_before = self.snapshot(source_db)
        with open(source_db, "rb") as f:
            bytes_before_export = f.read()

        raw_export = self.export_ok(source_db, NUMBER, label)

        # 导出后源库文件逐字节不变，且不产生 WAL/SHM 侧车文件。
        with open(source_db, "rb") as f:
            self.assertEqual(
                f.read(), bytes_before_export,
                msg=f"样例[{label}]只读导出前后源库文件必须逐字节一致",
            )
        self.assert_no_sidecars(source_db, label)

        # 核对导出的原始 UTF-8 内容本身（固定预期，而非与输入对象比较）。
        export_text = raw_export.decode("utf-8")
        exported = json.loads(export_text)
        self.assertEqual(
            set(exported.keys()), {"number", "customer", "items", "note"},
            msg=f"样例[{label}]导出只能含 save 接受字段且带说明时含 note",
        )
        self.assertEqual(exported["number"], NUMBER)
        self.assertEqual(
            exported["customer"], CUSTOMER,
            msg=f"样例[{label}]客户必须逐字符往返（含首尾空格、尖括号、与号）",
        )
        self.assertEqual(
            exported["note"], NOTE,
            msg=f"样例[{label}]说明必须逐字符往返（含首尾空格与换行）",
        )
        self.assertEqual(
            [item["description"] for item in exported["items"]],
            ["服务", "服务"],
            msg=f"样例[{label}]相同说明的明细仍是两行独立记录",
        )
        self.assertEqual(
            [item["quantity"] for item in exported["items"]], [2, 1],
            msg=f"样例[{label}]数量顺序必须为 2、1",
        )
        self.assertEqual(
            [item["unit_price"] for item in exported["items"]], [1250, 300],
            msg=f"样例[{label}]分单位单价顺序必须为 1250、300",
        )
        for item in exported["items"]:
            self.assertIsInstance(item["quantity"], int)
            self.assertNotIsInstance(item["quantity"], bool)
            self.assertIsInstance(item["unit_price"], int)
            self.assertNotIsInstance(item["unit_price"], bool)
        # 导出 JSON 不做 HTML 转义：客户中的尖括号、与号以原文出现；
        # 说明里的 <script> 同样裸写，而用户原有的实体串 "&lt;原文&gt;"
        # 必须作为原文字面保留（不得被解码，也不得被再转义成 &amp;lt;）。
        self.assertIn(CUSTOMER, export_text)
        self.assertIn("<script>演示</script>", export_text)
        self.assertIn("&lt;原文&gt;", export_text)
        self.assertNotIn("&amp;lt;", export_text)
        self.assertNotIn("<原文>", export_text)

        # 原始导出字节原样交给另一份新数据库保存。
        copy_db = self.resave_exported_ok(
            raw_export, "copy.sqlite", "exported.json", label
        )

        # 新库落库内容：客户与说明逐字符不变；同说明两行、顺序不变；整数金额。
        copy_quotes, copy_items, _ = self.snapshot(copy_db)
        self.assertEqual(
            copy_quotes, [(NUMBER, CUSTOMER, TOTAL_CENTS, NOTE)],
            msg=f"样例[{label}]重新保存后客户、合计与说明必须与固定样例一致",
        )
        self.assertEqual(
            copy_items,
            [
                (1, NUMBER, 0, "服务", 2, 1250, 2500),
                (2, NUMBER, 1, "服务", 1, 300, 300),
            ],
            msg=f"样例[{label}]重新保存后两条同说明明细的顺序、自增标识、"
                f"整数数量/单价/行金额必须保持不变",
        )
        for row in copy_items:
            self.assertIsInstance(row[4], int)
            self.assertIsInstance(row[5], int)
            self.assertIsInstance(row[6], int)

        # 分别从源库与新库向两个全新路径生成预览：均成功且逐字节一致。
        source_html = self.preview_ok(source_db, NUMBER, "source.html", label)
        copy_html = self.preview_ok(copy_db, NUMBER, "copy.html", label)
        self.assertEqual(
            source_html, copy_html,
            msg=f"样例[{label}]两份预览必须逐字节一致",
        )

        # 预览后源库数据与表结构不变，且不产生 WAL/SHM 侧车文件。
        self.assertEqual(
            self.snapshot(source_db), snapshot_before,
            msg=f"样例[{label}]预览后源库报价、明细与表结构必须保持不变",
        )
        self.assert_no_sidecars(source_db, label)

        # ---- 对最终文档逐项核对固定预期（不只是两份结果互相比对）----
        document = copy_html.decode("utf-8")

        # 编号与客户：源文为转义形式，客户首尾空格保留在 <dd> 文本内。
        self.assertIn("<title>报价单 Q-COPY-001</title>", document)
        self.assertIn("<dd>Q-COPY-001</dd>", document)
        self.assertIn(
            f"<dd>{ESCAPED_CUSTOMER}</dd>", document,
            msg=f"样例[{label}]客户必须转义显示且首尾空格保留",
        )
        self.assertNotIn("<dd> 演示客户 <甲>&乙 </dd>", document)

        # 说明区域位于客户信息之后、明细表之前。
        dl_end = document.index("</dl>")
        section_at = document.index('<section class="note">')
        table_at = document.index("<table>")
        self.assertLess(dl_end, section_at)
        self.assertLess(section_at, table_at)
        self.assertIn(
            '<section class="note">\n  <h2>客户说明</h2>\n'
            f'  <p class="note-text">{ESCAPED_NOTE}</p>\n</section>\n<table>',
            document,
            msg=f"样例[{label}]说明区域结构、首尾空格与换行必须逐字符固定",
        )

        # 尖括号与原有实体串显示原文：script 被转义，原 &lt; 再转义一层。
        self.assertNotIn("<script", document.lower())
        self.assertNotIn("</script", document.lower())
        self.assertIn("&lt;script&gt;演示&lt;/script&gt;", document)
        self.assertIn("&amp;lt;原文&amp;gt;", document)
        self.assertNotIn("&lt;原文&gt;", document)
        parser = parse_document(document)
        self.assertEqual(parser.section_count, 1)
        self.assertNotIn("script", parser.start_tags)
        self.assertEqual(
            parser.note_text, NOTE,
            msg=f"样例[{label}]解析后的说明文本必须逐字符等于原文",
        )

        # 两条同说明明细：两行、顺序、数量/单价/行金额固定；总计 28.00。
        markers = [
            ROW_1,
            ROW_2,
            TOTAL_FOOT,
        ]
        positions = [document.index(marker) for marker in markers]
        self.assertEqual(
            positions, sorted(positions),
            msg=f"样例[{label}]明细顺序必须为 行1(服务,2,12.50,25.00) -> "
                "行2(服务,1,3.00,3.00) -> 总计 28.00",
        )
        self.assertEqual(
            document.count("<td>服务</td>"), 2,
            msg=f"样例[{label}]相同说明必须渲染为独立两行",
        )
        self.assertEqual(
            document.count('<td class="num">25.00</td>'), 1,
            msg=f"样例[{label}]第一行行金额 25.00 只应出现一次",
        )
        self.assertEqual(
            document.count('<td class="num">3.00</td>'), 2,
            msg=f"样例[{label}]第二行单价与行金额均为 3.00，应出现两次",
        )
        self.assertIn(
            '<td class="num">28.00</td>', document,
            msg=f"样例[{label}]总计必须显示 28.00",
        )

        # ---- 目标库再次保存同一导出内容：必须以编号已存在拒绝 ----
        dup_input = self.path("exported-again.json")
        with open(dup_input, "wb") as f:
            f.write(raw_export)
        dup_result = self.run_quote(
            "save", "--db", copy_db, "--input", dup_input
        )
        self.assertEqual(
            dup_result.returncode, 1,
            msg=f"样例[{label}]重复保存应退出 1，实际 {dup_result.returncode}",
        )
        self.assertEqual(
            dup_result.stdout, b"",
            msg=f"样例[{label}]重复保存被拒绝时标准输出必须为空",
        )
        self.assertEqual(
            dup_result.stderr,
            f"错误: 报价编号已存在: {NUMBER}\n".encode("utf-8"),
            msg=f"样例[{label}]标准错误必须说明报价编号已存在",
        )
        self.assertNotIn(b"Traceback", dup_result.stderr)

        # 拒绝后目标库既有报价与明细不变（仍只有一条报价、两条明细）。
        dup_quotes, dup_items, dup_master = self.snapshot(copy_db)
        self.assertEqual(dup_quotes, [(NUMBER, CUSTOMER, TOTAL_CENTS, NOTE)])
        self.assertEqual(
            dup_items,
            [
                (1, NUMBER, 0, "服务", 2, 1250, 2500),
                (2, NUMBER, 1, "服务", 1, 300, 300),
            ],
            msg=f"样例[{label}]重复保存被拒绝后既有报价与明细必须保持不变",
        )

        # 拒绝后再从目标库向第三个新路径预览：仍成功且与前两份逐字节一致。
        again_html = self.preview_ok(copy_db, NUMBER, "copy-again.html", label)
        self.assertEqual(
            again_html, copy_html,
            msg=f"样例[{label}]拒绝重复保存后预览仍必须与此前预览逐字节一致",
        )
        self.assert_no_sidecars(copy_db, label)

        # 全部操作结束后源库仍与最初快照一致。
        self.assertEqual(
            self.snapshot(source_db), snapshot_before,
            msg=f"样例[{label}]全部流程后源库数据与结构必须保持不变",
        )


class NoteVariantRoundTripTestCase(ExportPreviewFlowBase):
    """同一导出-重存流程覆盖省略说明与 " \\n " 纯空白说明。"""

    def test_omitted_note_hidden_and_whitespace_note_kept(self):
        variants = [
            (
                "nonote",
                "省略说明Q-COPY-NONOTE",
                build_payload(NOTE_OMITTED_NUMBER, note=_NOTE_SENTINEL),
                None,
            ),
            (
                "wsnote",
                "纯空白说明Q-COPY-WS",
                build_payload(NOTE_WS_NUMBER, note=NOTE_WHITESPACE),
                NOTE_WHITESPACE,
            ),
        ]
        for tag, label, payload, expected_note in variants:
            with self.subTest(样例=label):
                flow = self.export_resave_preview(payload, tag, label)
                raw_export = flow["raw_export"]
                document = flow["copy_html"].decode("utf-8")
                exported = json.loads(raw_export.decode("utf-8"))

                # 两条明细与金额在两个变体中固定不变。
                self.assertIn(ROW_1, document)
                self.assertIn(ROW_2, document)
                self.assertIn(TOTAL_FOOT, document)
                self.assertIn(f"<dd>{ESCAPED_CUSTOMER}</dd>", document)
                self.assertEqual(
                    [item["description"] for item in exported["items"]],
                    ["服务", "服务"],
                )
                self.assertEqual(
                    [(i["quantity"], i["unit_price"]) for i in exported["items"]],
                    [(2, 1250), (1, 300)],
                )

                parser = parse_document(document)
                if expected_note is None:
                    # 省略说明：导出 JSON 不带 note；预览无说明区域与样式。
                    self.assertNotIn("note", exported)
                    self.assertNotIn('<section class="note">', document)
                    self.assertNotIn("客户说明", document)
                    self.assertNotIn("note-text", document)
                    self.assertNotIn("white-space: pre-wrap", document)
                    self.assertEqual(parser.section_count, 0)
                    self.assertEqual(parser.note_text, "")
                    # 客户信息之后直接是明细表。
                    self.assertIn("</dl>\n<table>", document)
                    # 新库中 note 为 NULL。
                    quotes, _, _ = self.snapshot(flow["copy_db"])
                    self.assertIsNone(quotes[0][3])
                else:
                    # 纯空白说明：导出逐字符保留；预览显示说明区域及原文。
                    self.assertEqual(exported["note"], " \n ")
                    self.assertIn('<section class="note">', document)
                    self.assertIn("white-space: pre-wrap", document)
                    # <p> 文本内逐字符保留一个前导空格、换行、一个尾随空格。
                    self.assertIn(
                        '<p class="note-text"> \n </p>', document,
                        msg=f"样例[{label}]纯空白说明必须保留空格与换行",
                    )
                    dl_end = document.index("</dl>")
                    section_at = document.index('<section class="note">')
                    table_at = document.index("<table>")
                    self.assertLess(dl_end, section_at)
                    self.assertLess(section_at, table_at)
                    self.assertEqual(parser.section_count, 1)
                    self.assertEqual(
                        parser.note_text, " \n ",
                        msg=f"样例[{label}]解析后的说明必须逐字符为空格-换行-空格",
                    )
                    quotes, _, _ = self.snapshot(flow["copy_db"])
                    self.assertEqual(quotes[0][3], " \n ")


class Int64MaxRoundTripTestCase(ExportPreviewFlowBase):
    """数量 1、单价 int64 上限：往返无整数精度损失，预览金额固定。"""

    def test_int64_max_unit_price_round_trips_without_precision_loss(self):
        label = "大整数Q-COPY-BIG"
        self.assertEqual(BIG_UNIT_PRICE, INT64_MAX)
        self.assertEqual(
            f"{BIG_UNIT_PRICE // 100}.{BIG_UNIT_PRICE % 100:02d}", BIG_YUAN
        )

        payload = build_payload(
            BIG_NUMBER,
            customer="演示客户",
            note=_NOTE_SENTINEL,
            items=[{"description": DESCRIPTION, "quantity": 1,
                    "unit_price": BIG_UNIT_PRICE}],
        )
        flow = self.export_resave_preview(payload, "big", label)
        raw_export = flow["raw_export"]

        # 导出 JSON 中大整数必须是裸整数写法：无引号、无指数、无浮点。
        self.assertIn(b": 9223372036854775807", raw_export)
        self.assertNotIn(b'"9223372036854775807"', raw_export)
        self.assertNotIn(b"e+", raw_export.lower())

        exported = json.loads(raw_export.decode("utf-8"))
        item = exported["items"][0]
        self.assertIsInstance(item["unit_price"], int)
        self.assertNotIsInstance(item["unit_price"], bool)
        self.assertEqual(item["quantity"], 1)
        self.assertEqual(item["unit_price"], BIG_UNIT_PRICE)

        # 新库存储仍为同一整数（单价、行金额、合计均为 int64 上限）。
        quotes, items, _ = self.snapshot(flow["copy_db"])
        self.assertEqual(quotes, [(BIG_NUMBER, "演示客户", BIG_UNIT_PRICE, None)])
        self.assertEqual(
            items,
            [(1, BIG_NUMBER, 0, "服务", 1, BIG_UNIT_PRICE, BIG_UNIT_PRICE)],
        )

        # 最终文档固定预期：单价、行金额、总计三处均为 92233720368547758.07。
        document = flow["copy_html"].decode("utf-8")
        big_cell = f'<td class="num">{BIG_YUAN}</td>'
        self.assertEqual(
            document.count(big_cell), 3,
            msg=f"样例[{label}]单价、行金额与总计必须各显示一次 {BIG_YUAN}",
        )
        self.assertIn(
            f'<tr><td>1</td><td>服务</td><td class="num">1</td>'
            f'<td class="num">{BIG_YUAN}</td>'
            f'<td class="num">{BIG_YUAN}</td></tr>',
            document,
        )
        self.assertIn(
            f'<tr><td colspan="4">总计（元）</td><td class="num">{BIG_YUAN}</td>'
            '</tr>',
            document,
        )
        for wrong in ("9.223372036854776e+18", "92233720368547758.00",
                      "92233720368547760"):
            self.assertNotIn(wrong, document, msg=f"样例[{label}]不得出现精度损失写法")

        # 无说明变体不出现说明区域；源库只读不变。
        self.assertNotIn('<section class="note">', document)
        self.assertEqual(
            self.snapshot(flow["source_db"])[0:2],
            self.snapshot(flow["copy_db"])[0:2],
            msg=f"样例[{label}]源库与新库的报价及明细内容必须一致",
        )
        self.assert_no_sidecars(flow["source_db"], label)
        self.assert_no_sidecars(flow["copy_db"], label)


# 项目目录清洁性：测试开始前记录项目根内容，全部用例结束后不得新增任何文件。
_PROJECT_ROOT_ENTRIES_AT_START = set(os.listdir(PROJECT_ROOT))


def tearDownModule():
    leftovers = set(os.listdir(PROJECT_ROOT)) - _PROJECT_ROOT_ENTRIES_AT_START
    assert not leftovers, f"测试在项目根目录留下了文件: {sorted(leftovers)}"


if __name__ == "__main__":
    unittest.main()
