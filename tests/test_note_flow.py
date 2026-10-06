"""客户说明（note）回归测试：保存 -> SQLite 持久化 -> HTML 预览。

仅依赖 Python 标准库。所有用例在独立的临时目录中通过 README 记载的
公开命令（save / preview）操作 quote.py 子进程，使用合成客户数据，
不读取项目内业务数据，也不在项目目录留下 JSON、SQLite 或 HTML 文件。

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

# ---- 合成报价：一条咨询明细，数量 2，单价 1250 分，合计 2500 分（25.00 元）----

NUMBER = "Q-NOTE-001"
CUSTOMER = "演示客户"
ITEM = {"description": "咨询", "quantity": 2, "unit_price": 1250}
TOTAL_CENTS = 2500

# 有效说明样例：首尾空格、换行、<script>演示</script> 与 &lt;原文&gt; 均为原文的一部分。
NOTE = "  说明首行 <script>演示</script>\n第二行 &lt;原文&gt;  \n\n"
# 只有空格与换行的非空字符串：仍应被保存并显示说明区域。
NOTE_WHITESPACE = "  \n\n "


def make_payload(note=NOTE, number=NUMBER):
    payload = {
        "number": number,
        "customer": CUSTOMER,
        "items": [dict(ITEM)],
    }
    if note is not _OMITTED:
        payload["note"] = note
    return payload


class _Omitted:
    """哨兵：表示 payload 中省略 note 字段。"""


_OMITTED = _Omitted()


class NoteSectionParser(HTMLParser):
    """提取 <p class="note-text"> 的文本，并记录全部起始标签。"""

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

    def write_input(self, payload, name="input.json"):
        path = os.path.join(self.workdir, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        return path

    def save(self, payload, db_name="quotes.sqlite", input_name="input.json"):
        db_path = os.path.join(self.workdir, db_name)
        input_path = self.write_input(payload, name=input_name)
        result = self.run_quote("save", "--db", db_path, "--input", input_path)
        return result, db_path

    def preview(self, db_path, number, output_name="preview.html"):
        output_path = os.path.join(self.workdir, output_name)
        result = self.run_quote(
            "preview", "--db", db_path, "--number", number, "--output", output_path
        )
        return result, output_path

    def read_quote(self, db_path, number):
        """直接读取 SQLite，返回 quotes 行 (number, customer, total, note)。"""
        conn = sqlite3.connect(db_path)
        try:
            return conn.execute(
                "SELECT number, customer, total, note FROM quotes WHERE number = ?",
                (number,),
            ).fetchone()
        finally:
            conn.close()

    def read_items(self, db_path, number):
        conn = sqlite3.connect(db_path)
        try:
            return conn.execute(
                "SELECT quote_number, position, description, quantity, "
                "unit_price, line_amount FROM items "
                "WHERE quote_number = ? ORDER BY position",
                (number,),
            ).fetchall()
        finally:
            conn.close()

    def assert_save_ok(self, result, number):
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(
            result.stdout, f"{number}\n",
            msg="保存成功后标准输出只能是编号加换行",
        )
        self.assertEqual(result.stderr, "", msg="保存成功时标准错误必须为空")

    def assert_preview_ok(self, result, output_path):
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(
            result.stdout, f"{output_path}\n",
            msg="预览成功后标准输出只能是目标路径加换行",
        )
        self.assertEqual(result.stderr, "", msg="预览成功时标准错误必须为空")

    def read_document(self, output_path):
        with open(output_path, "rb") as f:
            return f.read().decode("utf-8")

    def assert_fixed_quote_amounts(self, document):
        """固定报价：单价 12.50 元，行金额与总计均为 25.00 元。"""
        self.assertIn('<td class="num">2</td>', document, "数量应显示 2")
        self.assertIn('<td class="num">12.50</td>', document, "单价应显示 12.50 元")
        self.assertIn(
            '<tr><td colspan="4">总计（元）</td><td class="num">25.00</td></tr>',
            document,
            msg="总计应显示 25.00 元",
        )
        self.assertEqual(
            document.count('<td class="num">25.00</td>'), 2,
            msg="行金额与总计应各出现一次 25.00",
        )

    def assert_note_section(self, document, note):
        """说明区域位于客户信息之后、明细表格之前；正文保留原文并转义。"""
        self.assertIn('<section class="note">', document, "应出现客户说明区域")
        self.assertIn("<h2>客户说明</h2>", document)
        # 源文中说明正文为转义形式，首尾空格与换行原样保留在 <p> 文本内。
        self.assertIn(
            f'<p class="note-text">{html.escape(note)}</p>', document,
            msg="说明正文应整体转义且逐字符保留空格与换行",
        )
        dl_end = document.index("</dl>")
        section_at = document.index('<section class="note">')
        table_at = document.index("<table>")
        self.assertLess(dl_end, section_at, "说明区域应位于客户信息之后")
        self.assertLess(section_at, table_at, "说明区域应位于明细表格之前")

        parser = parse_document(document)
        self.assertEqual(parser.section_count, 1, "说明区域应恰好出现一次")
        self.assertNotIn("script", parser.start_tags, "页面不得出现 script 元素")
        self.assertEqual(
            parser.note_text, note,
            msg="解析出的说明文本应逐字符等于输入原文",
        )

    def assert_no_note_section(self, document):
        self.assertNotIn('<section class="note">', document, "不应出现客户说明区域")
        self.assertNotIn("客户说明", document)
        self.assertNotIn("note-text", document)
        parser = parse_document(document)
        self.assertEqual(parser.section_count, 0)
        self.assertEqual(parser.note_text, "")

    # ---- 正常流程：带说明保存后再预览 ----

    def test_note_roundtrip_save_then_preview(self):
        # 期望常量自检：说明正文的转义形式确实由标准库产生，防止测试常量写错。
        escaped_note = html.escape(NOTE)
        self.assertIn("&lt;script&gt;演示&lt;/script&gt;", escaped_note)
        self.assertIn("&amp;lt;原文&amp;gt;", escaped_note)
        self.assertTrue(escaped_note.startswith("  说明首行 "))
        self.assertTrue(escaped_note.endswith("  \n\n"))

        # 保存：退出码 0，标准输出只有编号，标准错误为空。
        save_result, db_path = self.save(make_payload())
        self.assert_save_ok(save_result, NUMBER)

        # 落库：说明逐字符等于输入；金额仍为整数分。
        quote_row = self.read_quote(db_path, NUMBER)
        self.assertIsNotNone(quote_row, "保存后数据库中应存在该编号")
        self.assertEqual(quote_row[0], NUMBER)
        self.assertEqual(quote_row[1], CUSTOMER)
        self.assertEqual(quote_row[2], TOTAL_CENTS, "合计应为 2500 分")
        self.assertEqual(
            quote_row[3], NOTE,
            msg="数据库中的说明应逐字符等于输入（首尾空格与换行不删改）",
        )
        self.assertEqual(
            self.read_items(db_path, NUMBER),
            [(NUMBER, 0, "咨询", 2, 1250, 2500)],
        )

        # 预览：退出码 0，标准输出只有目标路径，标准错误为空。
        preview_result, output_path = self.preview(db_path, NUMBER)
        self.assert_preview_ok(preview_result, output_path)
        document = self.read_document(output_path)

        # 用户输入不能产生 script 元素：源文无 <script 起始标签。
        self.assertNotIn("<script", document.lower())
        self.assertNotIn("</script", document.lower())
        # 已有实体样式的 &lt;原文&gt; 是用户原文，必须再转义一层。
        self.assertIn("&amp;lt;原文&amp;gt;", document)
        self.assertNotIn("&lt;原文&gt;", document)

        self.assert_note_section(document, NOTE)
        self.assert_fixed_quote_amounts(document)

    def test_omitted_or_empty_note_hides_section(self):
        """省略 note 或传入空字符串：不显示说明区域，其余内容不变。"""
        cases = [
            ("省略note", make_payload(note=_OMITTED)),
            ("空字符串", make_payload(note="")),
        ]
        for index, (label, payload) in enumerate(cases):
            with self.subTest(样例=label):
                number = f"Q-NOTE-NONE-{index}"
                payload["number"] = number
                save_result, db_path = self.save(
                    payload, db_name=f"none-{index}.sqlite",
                    input_name=f"none-{index}.json",
                )
                self.assert_save_ok(save_result, number)

                quote_row = self.read_quote(db_path, number)
                self.assertIsNone(quote_row[3], "无说明时数据库 note 应为 NULL")

                preview_result, output_path = self.preview(
                    db_path, number, output_name=f"none-{index}.html"
                )
                self.assert_preview_ok(preview_result, output_path)
                document = self.read_document(output_path)
                self.assert_no_note_section(document)
                self.assert_fixed_quote_amounts(document)

    def test_whitespace_only_note_saved_and_shown(self):
        """只有空格与换行的非空说明：仍被保存并显示说明区域。"""
        save_result, db_path = self.save(make_payload(note=NOTE_WHITESPACE))
        self.assert_save_ok(save_result, NUMBER)

        quote_row = self.read_quote(db_path, NUMBER)
        self.assertEqual(
            quote_row[3], NOTE_WHITESPACE,
            msg="只有空白的非空说明应逐字符保存",
        )

        preview_result, output_path = self.preview(db_path, NUMBER)
        self.assert_preview_ok(preview_result, output_path)
        document = self.read_document(output_path)
        self.assert_note_section(document, NOTE_WHITESPACE)
        self.assert_fixed_quote_amounts(document)


class NoteRejectionTestCase(unittest.TestCase):
    """note 类型非法的拒绝路径：可预测报错、无数据库副作用。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="quote-note-reject-")
        self.addCleanup(self._tmp.cleanup)
        self.workdir = self._tmp.name

    def run_quote(self, *cli_args):
        return subprocess.run(
            [sys.executable, QUOTE_PY, *cli_args],
            capture_output=True,
            text=True,
            cwd=self.workdir,
        )

    def write_input(self, payload, name):
        path = os.path.join(self.workdir, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        return path

    def assert_note_rejected(self, result, db_path):
        self.assertEqual(
            result.returncode, 1,
            msg=f"非法 note 应退出码 1，实际 {result.returncode}",
        )
        self.assertEqual(result.stdout, "", msg="拒绝时标准输出必须为空")
        self.assertEqual(
            result.stderr, "错误: note 必须是字符串\n",
            msg="标准错误应只有“错误: note 必须是字符串”及换行",
        )

    def invalid_note_payloads(self):
        return [
            ("null", None),
            ("数字", 42),
            ("布尔值", True),
            ("数组", ["说明"]),
            ("对象", {"text": "说明"}),
        ]

    def test_invalid_note_types_rejected_without_creating_database(self):
        for index, (label, bad_note) in enumerate(self.invalid_note_payloads()):
            with self.subTest(样例=label):
                payload = make_payload(note=bad_note, number=f"Q-NOTE-BAD-{index}")
                db_path = os.path.join(self.workdir, f"reject-{index}.sqlite")
                input_path = self.write_input(payload, f"reject-{index}.json")
                result = self.run_quote(
                    "save", "--db", db_path, "--input", input_path
                )
                self.assert_note_rejected(result, db_path)
                self.assertFalse(
                    os.path.exists(db_path),
                    msg="校验失败不能创建原本不存在的数据库文件",
                )

    def test_invalid_note_leaves_existing_database_untouched(self):
        """已有数据库：非法 note 拒绝后，已有报价与明细保持不变。"""
        db_path = os.path.join(self.workdir, "existing.sqlite")
        good_payload = make_payload(note=NOTE, number="Q-NOTE-KEEP")
        good_input = self.write_input(good_payload, "good.json")
        setup_result = self.run_quote("save", "--db", db_path, "--input", good_input)
        self.assertEqual(setup_result.returncode, 0, msg=setup_result.stderr)

        def snapshot():
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
            finally:
                conn.close()
            return quotes, items

        before = snapshot()
        self.assertEqual(
            before[0], [("Q-NOTE-KEEP", CUSTOMER, TOTAL_CENTS, NOTE)],
            msg="前置报价应带说明保存成功",
        )

        for index, (label, bad_note) in enumerate(self.invalid_note_payloads()):
            with self.subTest(样例=label):
                payload = make_payload(note=bad_note, number=f"Q-NOTE-BAD-{index}")
                input_path = self.write_input(payload, f"bad-{index}.json")
                result = self.run_quote(
                    "save", "--db", db_path, "--input", input_path
                )
                self.assert_note_rejected(result, db_path)

        self.assertEqual(
            snapshot(), before,
            msg="非法 note 拒绝后已有报价与明细必须保持不变",
        )


class LegacyDatabaseNoteTestCase(unittest.TestCase):
    """旧格式数据库（无 note 列）的兼容边界：先预览旧报价，再保存带说明的新报价。"""

    LEGACY_NUMBER = "Q-LEGACY-001"
    NEW_NUMBER = "Q-LEGACY-002"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="quote-note-legacy-")
        self.addCleanup(self._tmp.cleanup)
        self.workdir = self._tmp.name
        self.db_path = os.path.join(self.workdir, "legacy.sqlite")
        # 直接以旧格式建库：quotes 无 note 列，已有一张演示报价。
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executescript(
                """
                CREATE TABLE quotes (
                    number   TEXT PRIMARY KEY,
                    customer TEXT NOT NULL,
                    total    INTEGER NOT NULL
                );
                CREATE TABLE items (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    quote_number TEXT NOT NULL REFERENCES quotes(number),
                    position     INTEGER NOT NULL,
                    description  TEXT NOT NULL,
                    quantity     INTEGER NOT NULL,
                    unit_price   INTEGER NOT NULL,
                    line_amount  INTEGER NOT NULL
                );
                """
            )
            conn.execute(
                "INSERT INTO quotes(number, customer, total) VALUES (?, ?, ?)",
                (self.LEGACY_NUMBER, CUSTOMER, TOTAL_CENTS),
            )
            conn.execute(
                "INSERT INTO items(quote_number, position, description, "
                "quantity, unit_price, line_amount) VALUES (?, ?, ?, ?, ?, ?)",
                (self.LEGACY_NUMBER, 0, "咨询", 2, 1250, 2500),
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

    def schema_snapshot(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
            ).fetchall()
        finally:
            conn.close()

    def legacy_rows(self):
        """旧报价在补列前后的快照：不带 note 列读取，避免依赖列是否存在。"""
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

    def test_legacy_preview_then_save_with_note(self):
        schema_before = self.schema_snapshot()
        quotes_ddl = {
            row[1]: row[2] for row in schema_before if row[0] == "table"
        }
        self.assertNotIn(
            "note", quotes_ddl["quotes"],
            msg="前置条件：旧库 quotes 建表语句不含 note 列",
        )
        rows_before = self.legacy_rows()
        self.assertEqual(
            rows_before[0], [(self.LEGACY_NUMBER, CUSTOMER, TOTAL_CENTS)],
        )
        self.assertEqual(
            rows_before[1],
            [(self.LEGACY_NUMBER, 0, "咨询", 2, 1250, 2500)],
        )

        # 第一步：直接预览旧报价，按无说明处理。
        legacy_output = os.path.join(self.workdir, "legacy.html")
        preview_result = self.run_quote(
            "preview", "--db", self.db_path,
            "--number", self.LEGACY_NUMBER, "--output", legacy_output,
        )
        self.assertEqual(preview_result.returncode, 0, msg=preview_result.stderr)
        self.assertEqual(preview_result.stdout, f"{legacy_output}\n")
        self.assertEqual(preview_result.stderr, "")
        with open(legacy_output, "rb") as f:
            legacy_document = f.read().decode("utf-8")
        self.assertNotIn('<section class="note">', legacy_document,
                         "旧报价预览应按无说明处理")
        self.assertNotIn("客户说明", legacy_document)
        self.assertIn(
            '<tr><td colspan="4">总计（元）</td><td class="num">25.00</td></tr>',
            legacy_document,
            msg="旧报价总计应显示 25.00 元",
        )

        # 预览后数据库结构与原有记录不变（只读打开，不补列、不改行）。
        self.assertEqual(
            self.schema_snapshot(), schema_before,
            msg="预览不得改变旧库结构（不补 note 列）",
        )
        self.assertEqual(
            self.legacy_rows(), rows_before,
            msg="预览不得改变旧库记录",
        )

        # 第二步：向同一旧库保存另一编号且带说明的报价。
        new_payload = make_payload(note=NOTE, number=self.NEW_NUMBER)
        input_path = os.path.join(self.workdir, "new.json")
        with open(input_path, "w", encoding="utf-8") as f:
            json.dump(new_payload, f, ensure_ascii=False)
        save_result = self.run_quote(
            "save", "--db", self.db_path, "--input", input_path
        )
        self.assertEqual(save_result.returncode, 0, msg=save_result.stderr)
        self.assertEqual(save_result.stdout, f"{self.NEW_NUMBER}\n")
        self.assertEqual(save_result.stderr, "")

        # 保存后：旧报价仍无说明（note 为 NULL），新报价说明原样可读。
        conn = sqlite3.connect(self.db_path)
        try:
            note_rows = conn.execute(
                "SELECT number, note FROM quotes ORDER BY number"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(
            note_rows,
            [(self.LEGACY_NUMBER, None), (self.NEW_NUMBER, NOTE)],
            msg="旧报价仍无说明，新报价说明应逐字符等于输入",
        )

        # 原有金额与明细不变，新报价金额正确。
        quotes_after, items_after = self.legacy_rows()
        self.assertEqual(
            quotes_after,
            [
                (self.LEGACY_NUMBER, CUSTOMER, TOTAL_CENTS),
                (self.NEW_NUMBER, CUSTOMER, TOTAL_CENTS),
            ],
            msg="原有报价金额不变，新报价合计 2500 分",
        )
        self.assertEqual(
            items_after,
            [
                (self.LEGACY_NUMBER, 0, "咨询", 2, 1250, 2500),
                (self.NEW_NUMBER, 0, "咨询", 2, 1250, 2500),
            ],
            msg="原有明细不变，新报价明细按输入保存",
        )

        # 新报价预览：说明区域正确展示，说明文本解析后等于输入。
        new_output = os.path.join(self.workdir, "new.html")
        new_preview = self.run_quote(
            "preview", "--db", self.db_path,
            "--number", self.NEW_NUMBER, "--output", new_output,
        )
        self.assertEqual(new_preview.returncode, 0, msg=new_preview.stderr)
        self.assertEqual(new_preview.stdout, f"{new_output}\n")
        self.assertEqual(new_preview.stderr, "")
        with open(new_output, "rb") as f:
            new_document = f.read().decode("utf-8")
        self.assertIn('<section class="note">', new_document)
        self.assertIn(
            f'<p class="note-text">{html.escape(NOTE)}</p>', new_document,
            msg="新报价说明正文应整体转义且逐字符保留",
        )
        parser = parse_document(new_document)
        self.assertNotIn("script", parser.start_tags)
        self.assertEqual(parser.note_text, NOTE)

        # 旧报价再次预览：仍按无说明处理，与首次预览逐字节一致。
        legacy_output_2 = os.path.join(self.workdir, "legacy-2.html")
        re_preview = self.run_quote(
            "preview", "--db", self.db_path,
            "--number", self.LEGACY_NUMBER, "--output", legacy_output_2,
        )
        self.assertEqual(re_preview.returncode, 0, msg=re_preview.stderr)
        with open(legacy_output_2, "rb") as f:
            self.assertEqual(
                f.read().decode("utf-8"), legacy_document,
                msg="补列后旧报价预览应与首次预览逐字节一致（仍无说明）",
            )


if __name__ == "__main__":
    unittest.main()
