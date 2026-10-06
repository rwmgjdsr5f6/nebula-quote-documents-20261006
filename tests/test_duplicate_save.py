"""重复编号保存回归测试：拒绝重复提交后原报价仍完整可用。

仅依赖 Python 标准库。所有用例在独立的临时目录中通过 README 记载的
公开命令（save / preview）操作 quote.py 子进程，合成客户与演示报价，
不读取项目内业务数据，也不在项目目录留下 JSON、SQLite 或 HTML 文件。

固定样例：编号 Q-DUP-001、客户“演示客户甲”，说明含首尾空格、换行与
“仅作演示<甲>&乙”；两条明细（咨询 2×1250 分、资料 1×300 分），合计 2800 分。
首次保存后先生成基准预览，再提交完全相同的 JSON 与编号相同但内容变更的
JSON，两次均应被拒绝（退出码 1、标准错误恰为“错误: 报价编号已存在: …”），
且每次拒绝后数据库仍只有原报价与原两条明细，按原编号生成的新预览与基准
逐字节一致。

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

# ---- 合成报价（用户原文，首尾空格、换行与特殊字符均为样例的一部分）----

NUMBER = "Q-DUP-001"
CUSTOMER = "演示客户甲"
NOTE = "  仅作演示<甲>&乙\n第二行备注  \n"
ITEMS = [
    {"description": "咨询", "quantity": 2, "unit_price": 1250},
    {"description": "资料", "quantity": 1, "unit_price": 300},
]
TOTAL_CENTS = 2800
# 期望落库的明细行：(position, description, quantity, unit_price, line_amount)。
EXPECTED_ITEM_ROWS = [
    (NUMBER, 0, "咨询", 2, 1250, 2500),
    (NUMBER, 1, "资料", 1, 300, 300),
]

# 编号相同但内容全部变更的报价：仍符合输入格式，必须被整体拒绝。
CHANGED_CUSTOMER = "演示客户乙"
CHANGED_ITEM = {"description": "变更服务", "quantity": 1, "unit_price": 9999}

DUPLICATE_ERROR = f"错误: 报价编号已存在: {NUMBER}\n"


def make_payload():
    return {
        "number": NUMBER,
        "customer": CUSTOMER,
        "note": NOTE,
        "items": [dict(item) for item in ITEMS],
    }


def make_changed_payload():
    """编号相同，但客户变更、省略 note、明细改为一条变更服务。"""
    return {
        "number": NUMBER,
        "customer": CHANGED_CUSTOMER,
        "items": [dict(CHANGED_ITEM)],
    }


class NoteTextParser(HTMLParser):
    """提取 <p class="note-text"> 的可见文本，用于核对说明原文逐字符保留。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._in_note_text = False
        self._note_chunks = []

    def handle_starttag(self, tag, attrs):
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


class DuplicateSaveTestCase(unittest.TestCase):
    """每个用例使用独立临时目录，只通过公开命令行入口驱动 quote.py。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="quote-dup-test-")
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

    def save_payload(self, payload, input_name):
        input_path = os.path.join(self.workdir, input_name)
        with open(input_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        return self.run_quote("save", "--db", self.db_path, "--input", input_path)

    def preview(self, output_name):
        output_path = os.path.join(self.workdir, output_name)
        result = self.run_quote(
            "preview", "--db", self.db_path,
            "--number", NUMBER, "--output", output_path,
        )
        return result, output_path

    def snapshot(self):
        """数据库快照：quotes 全部行与 items 全部行（含自增 id，按 id 排列）。"""
        conn = sqlite3.connect(self.db_path)
        try:
            quote_rows = conn.execute(
                "SELECT number, customer, total, note FROM quotes ORDER BY number"
            ).fetchall()
            item_rows = conn.execute(
                "SELECT id, quote_number, position, description, quantity, "
                "unit_price, line_amount FROM items ORDER BY id"
            ).fetchall()
        finally:
            conn.close()
        return quote_rows, item_rows

    # ---- 公共断言 ----

    def assert_original_state(self, snapshot, label):
        """数据库仍只有原报价与原两条明细：标识、顺序、文本与金额全部保留。"""
        quote_rows, item_rows = snapshot
        self.assertEqual(
            quote_rows, [(NUMBER, CUSTOMER, TOTAL_CENTS, NOTE)],
            msg=f"{label}：数据库应仍只有原报价，客户、合计 2800 分与说明原文不变",
        )
        self.assertEqual(
            [row[0] for row in item_rows], [1, 2],
            msg=f"{label}：明细标识应仍为首次保存的 1、2，不追加新明细",
        )
        self.assertEqual(
            [row[1:] for row in item_rows], EXPECTED_ITEM_ROWS,
            msg=f"{label}：原两条明细的排列顺序、说明、数量、单价与行金额不变",
        )

    def assert_duplicate_rejected(self, result, label):
        self.assertEqual(
            result.returncode, 1,
            msg=f"{label}：重复编号应退出码 1，实际 {result.returncode}，"
                f"stderr={result.stderr!r}",
        )
        self.assertEqual(
            result.stdout, "",
            msg=f"{label}：拒绝时标准输出必须为空",
        )
        self.assertEqual(
            result.stderr, DUPLICATE_ERROR,
            msg=f"{label}：标准错误应只有“错误: 报价编号已存在: {NUMBER}”及换行，"
                f"不得出现异常堆栈",
        )
        self.assertNotIn(
            "Traceback", result.stderr,
            msg=f"{label}：拒绝重复编号不得出现异常堆栈",
        )

    def assert_preview_matches_baseline(self, baseline_bytes, output_name, label):
        """按原编号向新路径生成预览：成功且与基准逐字节一致。"""
        result, output_path = self.preview(output_name)
        self.assertEqual(result.returncode, 0, msg=f"{label}：{result.stderr}")
        self.assertEqual(
            result.stdout, f"{output_path}\n",
            msg=f"{label}：预览成功后标准输出只能是目标路径加换行",
        )
        self.assertEqual(
            result.stderr, "",
            msg=f"{label}：预览成功时标准错误必须为空",
        )
        with open(output_path, "rb") as f:
            new_bytes = f.read()
        self.assertEqual(
            new_bytes, baseline_bytes,
            msg=f"{label}：拒绝重复提交后，新预览应与基准预览逐字节一致",
        )
        return new_bytes.decode("utf-8")

    def assert_original_document(self, document, label):
        """预览保留原客户、说明原文（转义、空格与换行）与 28.00 元总计。"""
        self.assertIn(
            f"<dd>{html.escape(CUSTOMER)}</dd>", document,
            msg=f"{label}：预览应保留原客户“{CUSTOMER}”",
        )
        self.assertNotIn(
            CHANGED_CUSTOMER, document,
            msg=f"{label}：预览不得出现变更后的客户“{CHANGED_CUSTOMER}”",
        )
        escaped_note = html.escape(NOTE)
        # 说明中的尖括号与与号仍正确转义，原空格和换行继续保留。
        self.assertIn("仅作演示&lt;甲&gt;&amp;乙", escaped_note)
        self.assertIn(
            f'<p class="note-text">{escaped_note}</p>', document,
            msg=f"{label}：说明正文应整体转义且逐字符保留首尾空格与换行",
        )
        self.assertNotIn("<甲>", document, msg=f"{label}：说明中的尖括号必须转义")
        parser = NoteTextParser()
        parser.feed(document)
        parser.close()
        self.assertEqual(
            parser.note_text, NOTE,
            msg=f"{label}：解析出的说明文本应逐字符等于输入原文",
        )
        self.assertIn(
            '<tr><td colspan="4">总计（元）</td><td class="num">28.00</td></tr>',
            document,
            msg=f"{label}：总计应仍为 28.00 元",
        )
        self.assertNotIn(
            "99.99", document,
            msg=f"{label}：预览不得出现变更报价的金额",
        )

    def save_original_and_baseline(self):
        """首次保存固定样例并生成基准预览，返回 (落库快照, 基准预览字节)。"""
        save_result = self.save_payload(make_payload(), "original.json")
        self.assertEqual(save_result.returncode, 0, msg=save_result.stderr)
        self.assertEqual(
            save_result.stdout, f"{NUMBER}\n",
            msg="首次保存成功后标准输出只能是编号加换行",
        )
        self.assertEqual(save_result.stderr, "", msg="首次保存成功时标准错误必须为空")

        snapshot = self.snapshot()
        self.assert_original_state(snapshot, "首次保存后")

        baseline_result, baseline_path = self.preview("baseline.html")
        self.assertEqual(baseline_result.returncode, 0, msg=baseline_result.stderr)
        self.assertEqual(baseline_result.stdout, f"{baseline_path}\n")
        self.assertEqual(baseline_result.stderr, "")
        with open(baseline_path, "rb") as f:
            baseline_bytes = f.read()
        return snapshot, baseline_bytes

    # ---- 重复提交样例 ----

    def test_identical_payload_resubmission_rejected(self):
        """向同一数据库提交完全相同的 JSON：拒绝且原报价完整可用。"""
        label = "完全相同的重复提交"
        snapshot, baseline_bytes = self.save_original_and_baseline()

        result = self.save_payload(make_payload(), "duplicate.json")
        self.assert_duplicate_rejected(result, label)

        self.assertEqual(
            self.snapshot(), snapshot,
            msg=f"{label}：拒绝后数据库快照应与首次保存后完全一致",
        )
        self.assert_original_state(self.snapshot(), label)

        document = self.assert_preview_matches_baseline(
            baseline_bytes, "after-identical.html", label,
        )
        self.assert_original_document(document, label)

    def test_changed_payload_same_number_rejected(self):
        """编号相同但客户、说明、明细全部变更的报价：拒绝且不写入变更内容。"""
        label = "编号相同的变更报价"
        snapshot, baseline_bytes = self.save_original_and_baseline()

        result = self.save_payload(make_changed_payload(), "changed.json")
        self.assert_duplicate_rejected(result, label)

        after_quotes, after_items = self.snapshot()
        self.assertEqual(
            (after_quotes, after_items), snapshot,
            msg=f"{label}：拒绝后数据库快照应与首次保存后完全一致",
        )
        self.assert_original_state((after_quotes, after_items), label)
        # 变更内容不得出现在数据库中。
        flat = repr((after_quotes, after_items))
        self.assertNotIn(CHANGED_CUSTOMER, flat,
                         msg=f"{label}：数据库不得写入变更后的客户")
        self.assertNotIn("变更服务", flat,
                         msg=f"{label}：数据库不得写入变更后的明细")
        self.assertNotIn("9999", flat,
                         msg=f"{label}：数据库不得写入变更后的金额")

        document = self.assert_preview_matches_baseline(
            baseline_bytes, "after-changed.html", label,
        )
        self.assert_original_document(document, label)

    def test_both_rejections_in_sequence_keep_original(self):
        """同一数据库连续两次重复提交均被拒绝，原报价始终完整可用。"""
        snapshot, baseline_bytes = self.save_original_and_baseline()

        for label, payload, input_name in [
            ("第一次重复提交（相同 JSON）", make_payload(), "dup-1.json"),
            ("第二次重复提交（变更报价）", make_changed_payload(), "dup-2.json"),
        ]:
            with self.subTest(样例=label):
                result = self.save_payload(payload, input_name)
                self.assert_duplicate_rejected(result, label)
                current = self.snapshot()
                self.assertEqual(
                    current, snapshot,
                    msg=f"{label}：拒绝后数据库应保持首次保存后的状态",
                )
                self.assert_original_state(current, label)

        document = self.assert_preview_matches_baseline(
            baseline_bytes, "after-both.html", "连续两次拒绝后",
        )
        self.assert_original_document(document, "连续两次拒绝后")


if __name__ == "__main__":
    unittest.main()
