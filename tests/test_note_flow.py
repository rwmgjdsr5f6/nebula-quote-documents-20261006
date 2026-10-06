"""客户说明（note）回归测试：保存 -> SQLite 原样保留 -> HTML 预览区域。

仅依赖 Python 标准库。所有用例在独立的临时目录中通过 README 记载的
公开命令（save / preview）操作 quote.py 子进程，使用合成客户数据，
结束后由临时目录统一清理产生的 JSON、SQLite 与 HTML 文件，
不读取项目内业务数据，也不在项目目录留下任何文件。

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

# ---- 合成报价：固定一条咨询明细，数量 2，单价 1250 分，合计 2500 分 ----

NUMBER = "Q-NOTE-001"
CUSTOMER = "演示客户 <甲>&乙"
DESCRIPTION = "咨询"
QUANTITY = 2
UNIT_PRICE = 1250
TOTAL = 2500  # 分，预览显示 25.00 元

# 有效说明样例：首尾空格、换行、<script>演示</script> 与 &lt;原文&gt; 均为原文的一部分。
NOTE = "  首行保留空格 \n含脚本 <script>演示</script> 与实体 &lt;原文&gt;\n\n末行  "
# 期望出现在 HTML 源文中的转义形式（标准库 html.escape 默认转 & < > " '，
# 换行与空格不属于转义对象，原样留在源文中）。
ESCAPED_NOTE = (
    "  首行保留空格 \n"
    "含脚本 &lt;script&gt;演示&lt;/script&gt; 与实体 &amp;lt;原文&amp;gt;\n"
    "\n末行  "
)

# 只有空格与换行的非空说明：仍被保存并显示说明区域。
BLANK_NOTE = "  \n \n  "

# 旧格式数据库中的演示报价（无 note 列时代的记录）。
LEGACY_NUMBER = "Q-LEGACY-001"
LEGACY_CUSTOMER = "旧格式客户"
LEGACY_NOTE_NUMBER = "Q-LEGACY-002"

NOTE_ERROR_LINE = "错误: note 必须是字符串\n"


def make_payload(note=NOTE, number=NUMBER):
    """固定一条咨询明细的报价；note 为 OMIT 时表示省略该字段。"""
    payload = {
        "number": number,
        "customer": CUSTOMER,
        "items": [
            {"description": DESCRIPTION, "quantity": QUANTITY, "unit_price": UNIT_PRICE},
        ],
    }
    if note is not OMIT:
        payload["note"] = note
    return payload


class _Omit:
    """区分“省略 note 字段”与“显式传入 None（JSON null）”。"""


OMIT = _Omit()


class NoteTextExtractor(HTMLParser):
    """提取 <p class="note-text"> 内的文本，并记录是否出现 script 元素。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_note = False
        self.note_chunks = []
        self.start_tags = []

    def handle_starttag(self, tag, attrs):
        self.start_tags.append(tag)
        if tag == "p" and ("class", "note-text") in attrs:
            self.in_note = True

    def handle_endtag(self, tag):
        if tag == "p" and self.in_note:
            self.in_note = False

    def handle_data(self, data):
        if self.in_note:
            self.note_chunks.append(data)


class NoteFlowTestCase(unittest.TestCase):
    """每个用例使用独立临时目录，只通过公开命令行入口驱动 quote.py。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="quote-note-test-")
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

    def assert_save_ok(self, result, number):
        """成功保存的公开约定：退出码 0、标准错误为空、标准输出只有编号加换行。"""
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(
            result.stdout, f"{number}\n",
            msg="保存成功后标准输出只能是编号加换行",
        )
        self.assertEqual(result.stderr, "", msg="保存成功时标准错误必须为空")

    def assert_preview_ok(self, result, output_path):
        """成功预览的公开约定：退出码 0、标准错误为空、标准输出只有路径加换行。"""
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(
            result.stdout, f"{output_path}\n",
            msg="预览成功后标准输出只能是目标路径加换行",
        )
        self.assertEqual(result.stderr, "", msg="预览成功时标准错误必须为空")

    def read_quote_row(self, db_path, number):
        """直接读取 SQLite 中的 quotes 行（含 note 列）。"""
        conn = sqlite3.connect(db_path)
        try:
            return conn.execute(
                "SELECT number, customer, total, note FROM quotes WHERE number = ?",
                (number,),
            ).fetchone()
        finally:
            conn.close()

    def read_item_rows(self, db_path, number):
        conn = sqlite3.connect(db_path)
        try:
            return conn.execute(
                "SELECT description, quantity, unit_price, line_amount "
                "FROM items WHERE quote_number = ? ORDER BY position",
                (number,),
            ).fetchall()
        finally:
            conn.close()

    def quote_columns(self, db_path):
        conn = sqlite3.connect(db_path)
        try:
            return {row[1] for row in conn.execute("PRAGMA table_info(quotes)")}
        finally:
            conn.close()

    def read_document(self, output_path):
        """生成文件可按 UTF-8 严格解码（非法字节会直接让本用例失败）。"""
        with open(output_path, "rb") as f:
            return f.read().decode("utf-8")

    def assert_note_section(self, document, expected_note, escaped_note):
        """说明区域位于客户信息之后、明细表格之前；正文转义且保留空格与换行。"""
        self.assertIn('<section class="note">', document)
        self.assertIn("<h2>客户说明</h2>", document)
        self.assertIn(
            f'<p class="note-text">{escaped_note}</p>', document,
            msg="说明正文在源文中必须转义，且首尾空格与换行原样保留",
        )
        # 位置：客户信息（</dl>）之后、明细表格（<table>）之前。
        customer_end = document.index("</dl>")
        section_at = document.index('<section class="note">')
        table_at = document.index("<table>")
        self.assertLess(customer_end, section_at, msg="说明区域应在客户信息之后")
        self.assertLess(section_at, table_at, msg="说明区域应在明细表格之前")

        # 用户输入不能产生 script 元素。
        self.assertNotIn("<script", document.lower())
        self.assertNotIn("</script", document.lower())

        # 解析出的说明文本仍等于输入，且页面没有 script 元素。
        extractor = NoteTextExtractor()
        extractor.feed(document)
        self.assertNotIn("script", extractor.start_tags, msg="页面不得出现 script 元素")
        self.assertEqual(
            "".join(extractor.note_chunks), expected_note,
            msg="解析出的说明文本应逐字符等于输入",
        )

    def assert_no_note_section(self, document):
        self.assertNotIn('<section class="note">', document)
        self.assertNotIn("客户说明", document)
        self.assertNotIn("note-text", document)

    def assert_fixed_amounts(self, document):
        """固定明细：数量 2、单价 12.50 元、行金额与总计 25.00 元。"""
        self.assertIn('<td class="num">2</td>', document)
        self.assertIn('<td class="num">12.50</td>', document)
        self.assertEqual(
            document.count('<td class="num">25.00</td>'), 2,
            msg="行金额与总计应各显示一次 25.00",
        )

    # ---- 有效说明：保存 -> 落库逐字符相等 -> 预览区域 ----

    def test_note_roundtrip_save_then_preview(self):
        # 期望常量自检：它确实是标准库对说明原文的转义结果，防止测试常量写错。
        self.assertEqual(html.escape(NOTE), ESCAPED_NOTE)

        save_result, db_path = self.save(make_payload())
        self.assert_save_ok(save_result, NUMBER)

        # 落库：说明逐字符等于输入（首尾空格与换行不删改），合计 2500 分。
        quote_row = self.read_quote_row(db_path, NUMBER)
        self.assertIsNotNone(quote_row, "保存后数据库中应存在该编号")
        self.assertEqual(quote_row[0], NUMBER)
        self.assertEqual(quote_row[1], CUSTOMER)
        self.assertEqual(quote_row[2], TOTAL)
        self.assertEqual(
            quote_row[3], NOTE,
            msg="数据库中的说明应逐字符等于输入",
        )
        self.assertEqual(
            self.read_item_rows(db_path, NUMBER),
            [(DESCRIPTION, QUANTITY, UNIT_PRICE, TOTAL)],
        )

        output_path = os.path.join(self.workdir, "preview.html")
        preview_result = self.run_preview(db_path, NUMBER, output_path)
        self.assert_preview_ok(preview_result, output_path)

        document = self.read_document(output_path)
        self.assert_note_section(document, NOTE, ESCAPED_NOTE)
        self.assert_fixed_amounts(document)

    # ---- 省略 note 或空字符串：不显示说明区域 ----

    def test_note_omitted_or_empty_hides_section(self):
        cases = [
            ("省略note字段", make_payload(note=OMIT), "Q-NOTE-OMIT"),
            ("空字符串note", make_payload(note=""), "Q-NOTE-EMPTY"),
        ]
        db_path = os.path.join(self.workdir, "quotes.sqlite")
        for index, (label, payload, number) in enumerate(cases):
            with self.subTest(样例=label):
                payload["number"] = number
                save_result, _ = self.save(
                    payload, db_name="quotes.sqlite", input_name=f"input-{index}.json"
                )
                self.assert_save_ok(save_result, number)

                # 省略或空字符串都表示无说明：落库为 NULL。
                quote_row = self.read_quote_row(db_path, number)
                self.assertIsNone(
                    quote_row[3],
                    msg=f"{label}应落库为无说明（NULL），实际：{quote_row[3]!r}",
                )

                output_path = os.path.join(self.workdir, f"preview-{index}.html")
                preview_result = self.run_preview(db_path, number, output_path)
                self.assert_preview_ok(preview_result, output_path)

                document = self.read_document(output_path)
                self.assert_no_note_section(document)
                self.assert_fixed_amounts(document)

    # ---- 只有空格与换行的非空说明：仍被保存并显示区域 ----

    def test_whitespace_only_note_is_kept_and_shown(self):
        save_result, db_path = self.save(make_payload(note=BLANK_NOTE))
        self.assert_save_ok(save_result, NUMBER)

        quote_row = self.read_quote_row(db_path, NUMBER)
        self.assertEqual(
            quote_row[3], BLANK_NOTE,
            msg="只有空格与换行的非空说明应逐字符保存",
        )

        output_path = os.path.join(self.workdir, "preview.html")
        preview_result = self.run_preview(db_path, NUMBER, output_path)
        self.assert_preview_ok(preview_result, output_path)

        document = self.read_document(output_path)
        self.assert_note_section(document, BLANK_NOTE, html.escape(BLANK_NOTE))
        self.assert_fixed_amounts(document)

    # ---- 非字符串 note：拒绝保存，无副作用 ----

    def test_invalid_note_types_rejected_without_creating_database(self):
        cases = [
            ("null", None),
            ("数字", 123),
            ("布尔值", True),
            ("数组", ["说明"]),
            ("对象", {"text": "说明"}),
        ]
        for index, (label, bad_note) in enumerate(cases):
            with self.subTest(样例=label):
                db_name = f"reject-{index}.sqlite"
                result, db_path = self.save(
                    make_payload(note=bad_note),
                    db_name=db_name,
                    input_name=f"bad-{index}.json",
                )
                self.assertEqual(
                    result.returncode, 1,
                    msg=f"note 为{label}应退出码 1，实际 {result.returncode}",
                )
                self.assertEqual(result.stdout, "", msg="拒绝时标准输出必须为空")
                self.assertEqual(
                    result.stderr, NOTE_ERROR_LINE,
                    msg="标准错误只能是“错误: note 必须是字符串”及换行",
                )
                self.assertFalse(
                    os.path.exists(db_path),
                    msg="校验失败不能创建原本不存在的数据库文件",
                )

    def test_invalid_note_types_leave_existing_database_untouched(self):
        # 先保存一份合法报价作为既有内容。
        good_result, db_path = self.save(make_payload(number="Q-KEEP-001"))
        self.assert_save_ok(good_result, "Q-KEEP-001")
        before_quotes = self.read_quote_row(db_path, "Q-KEEP-001")
        before_items = self.read_item_rows(db_path, "Q-KEEP-001")

        bad_notes = [None, 123, True, ["说明"], {"text": "说明"}]
        for index, bad_note in enumerate(bad_notes):
            with self.subTest(序号=index):
                result, _ = self.save(
                    make_payload(note=bad_note, number=f"Q-BAD-NOTE-{index}"),
                    db_name="quotes.sqlite",
                    input_name=f"bad-{index}.json",
                )
                self.assertEqual(result.returncode, 1, msg=result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, NOTE_ERROR_LINE)

        # 已有数据库的报价与明细保持不变，坏编号的报价没有落库。
        self.assertEqual(self.read_quote_row(db_path, "Q-KEEP-001"), before_quotes)
        self.assertEqual(self.read_item_rows(db_path, "Q-KEEP-001"), before_items)
        conn = sqlite3.connect(db_path)
        try:
            remaining = conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(remaining, 1, msg="被拒绝的保存不得留下任何报价")


class LegacyDatabaseNoteTestCase(unittest.TestCase):
    """旧格式数据库（无 note 列、已有一张演示报价）的兼容边界。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="quote-legacy-test-")
        self.addCleanup(self._tmp.cleanup)
        self.workdir = self._tmp.name
        self.db_path = os.path.join(self.workdir, "legacy.sqlite")
        self._create_legacy_database()

    def _create_legacy_database(self):
        """按旧格式建库：quotes 无 note 列，已有一张演示报价。"""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "CREATE TABLE quotes ("
                "number TEXT PRIMARY KEY, customer TEXT NOT NULL, "
                "total INTEGER NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE items ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "quote_number TEXT NOT NULL, position INTEGER NOT NULL, "
                "description TEXT NOT NULL, quantity INTEGER NOT NULL, "
                "unit_price INTEGER NOT NULL, line_amount INTEGER NOT NULL)"
            )
            conn.execute(
                "INSERT INTO quotes(number, customer, total) VALUES (?, ?, ?)",
                (LEGACY_NUMBER, LEGACY_CUSTOMER, TOTAL),
            )
            conn.execute(
                "INSERT INTO items(quote_number, position, description, "
                "quantity, unit_price, line_amount) VALUES (?, 0, ?, ?, ?, ?)",
                (LEGACY_NUMBER, DESCRIPTION, QUANTITY, UNIT_PRICE, TOTAL),
            )
            conn.commit()
        finally:
            conn.close()

    def run_quote(self, *cli_args):
        return subprocess.run(
            [sys.executable, QUOTE_PY, *cli_args],
            capture_output=True,
            text=True,
            cwd=self.workdir,
        )

    def legacy_snapshot(self):
        """(quotes 列名集合, quotes 全部行, items 全部行)。"""
        conn = sqlite3.connect(self.db_path)
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(quotes)")}
            select_note = "note" in columns
            if select_note:
                quotes = conn.execute(
                    "SELECT number, customer, total, note FROM quotes ORDER BY number"
                ).fetchall()
            else:
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
        return columns, quotes, items

    def test_legacy_preview_then_save_with_note(self):
        before = self.legacy_snapshot()
        self.assertNotIn("note", before[0], "前置条件：旧库 quotes 表没有 note 列")
        self.assertEqual(before[1], [(LEGACY_NUMBER, LEGACY_CUSTOMER, TOTAL)])

        # ---- 第一步：直接预览旧报价，按无说明处理 ----
        legacy_output = os.path.join(self.workdir, "legacy-preview.html")
        preview_result = self.run_quote(
            "preview", "--db", self.db_path,
            "--number", LEGACY_NUMBER, "--output", legacy_output,
        )
        self.assertEqual(preview_result.returncode, 0, msg=preview_result.stderr)
        self.assertEqual(preview_result.stdout, f"{legacy_output}\n")
        self.assertEqual(preview_result.stderr, "")

        with open(legacy_output, "rb") as f:
            document = f.read().decode("utf-8")
        self.assertNotIn('<section class="note">', document)
        self.assertNotIn("客户说明", document)
        self.assertIn(f"<dd>{html.escape(LEGACY_CUSTOMER)}</dd>", document)
        self.assertIn('<td class="num">12.50</td>', document)
        self.assertEqual(document.count('<td class="num">25.00</td>'), 2)

        # 预览是只读的：数据库结构与原有记录不变（仍无 note 列）。
        self.assertEqual(
            self.legacy_snapshot(), before,
            msg="预览旧库后数据库结构与原有记录必须保持不变",
        )

        # ---- 第二步：向同一旧库保存另一编号且带说明的报价 ----
        input_path = os.path.join(self.workdir, "new-quote.json")
        with open(input_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "number": LEGACY_NOTE_NUMBER,
                    "customer": CUSTOMER,
                    "note": NOTE,
                    "items": [
                        {"description": DESCRIPTION, "quantity": QUANTITY,
                         "unit_price": UNIT_PRICE},
                    ],
                },
                f, ensure_ascii=False,
            )
        save_result = self.run_quote(
            "save", "--db", self.db_path, "--input", input_path
        )
        self.assertEqual(save_result.returncode, 0, msg=save_result.stderr)
        self.assertEqual(save_result.stdout, f"{LEGACY_NOTE_NUMBER}\n")
        self.assertEqual(save_result.stderr, "")

        # 保存后旧报价仍无说明（note 为 NULL），新报价说明逐字符等于输入，
        # 原有金额和明细不变。
        columns, quotes, items = self.legacy_snapshot()
        self.assertIn("note", columns, "保存后旧库应自动补充 note 列")
        self.assertEqual(
            quotes,
            [
                (LEGACY_NUMBER, LEGACY_CUSTOMER, TOTAL, None),
                (LEGACY_NOTE_NUMBER, CUSTOMER, TOTAL, NOTE),
            ],
            msg="旧报价仍无说明，新报价说明逐字符保存，金额不变",
        )
        self.assertEqual(
            items,
            [
                (LEGACY_NUMBER, 0, DESCRIPTION, QUANTITY, UNIT_PRICE, TOTAL),
                (LEGACY_NOTE_NUMBER, 0, DESCRIPTION, QUANTITY, UNIT_PRICE, TOTAL),
            ],
            msg="原有明细不变，新报价明细按输入落库",
        )

        # 新报价的说明能够正确预览：区域、转义、解析文本均符合约定。
        new_output = os.path.join(self.workdir, "new-preview.html")
        new_preview = self.run_quote(
            "preview", "--db", self.db_path,
            "--number", LEGACY_NOTE_NUMBER, "--output", new_output,
        )
        self.assertEqual(new_preview.returncode, 0, msg=new_preview.stderr)
        self.assertEqual(new_preview.stdout, f"{new_output}\n")
        self.assertEqual(new_preview.stderr, "")

        with open(new_output, "rb") as f:
            new_document = f.read().decode("utf-8")
        self.assertIn('<section class="note">', new_document)
        self.assertIn(f'<p class="note-text">{ESCAPED_NOTE}</p>', new_document)
        self.assertNotIn("<script", new_document.lower())
        extractor = NoteTextExtractor()
        extractor.feed(new_document)
        self.assertNotIn("script", extractor.start_tags)
        self.assertEqual("".join(extractor.note_chunks), NOTE)
        self.assertEqual(new_document.count('<td class="num">25.00</td>'), 2)

        # 旧报价再次预览仍按无说明处理。
        legacy_output2 = os.path.join(self.workdir, "legacy-preview-2.html")
        re_preview = self.run_quote(
            "preview", "--db", self.db_path,
            "--number", LEGACY_NUMBER, "--output", legacy_output2,
        )
        self.assertEqual(re_preview.returncode, 0, msg=re_preview.stderr)
        with open(legacy_output2, "rb") as f:
            re_document = f.read().decode("utf-8")
        self.assertNotIn('<section class="note">', re_document)
        self.assertNotIn("客户说明", re_document)


if __name__ == "__main__":
    unittest.main()
