"""旧库首次遇到重复编号的回归测试：补列保留与拒绝保存的交汇行为。

仅依赖 Python 标准库。用例在独立的临时目录中通过 README 记载的
公开命令（save / preview）操作 quote.py 子进程，使用合成数据，
不读取项目内业务数据，结束后随临时目录一并清理。

场景：旧格式数据库（quotes 无 note 列）中已有一张报价，首次向该库
提交同编号的新报价时，save 先为旧库补充 note 列（已提交的结构变化
保留），随后因编号冲突拒绝写入（本次报价内容不落库）。本测试分别
核对结构变化与业务数据，避免把拒绝保存等同于数据库完全没有变化。

覆盖范围：
  1. 前置：旧库 quotes 只有 number/customer/total 三列，确认无 note 列，
     并按 preview 入口生成基准 HTML；
  2. 以同编号提交一份本身合法的变更报价：退出码 1、标准输出为空、
     标准错误恰为“错误: 报价编号已存在: Q-LEGACY-001”加换行、无异常堆栈；
  3. 拒绝后 quotes 已新增 note 列（结构变化保留），旧报价 note 为 NULL，
     报价仍只有原来一张；旧客户、编号、整数合计与全部明细的标识、位置、
     文本、数量、单价及行金额与提交前一致，变更内容不落库；
  4. 拒绝后按同编号向另一新路径生成预览：退出码 0、仅输出目标路径加
     换行、标准错误为空；新旧 HTML 逐字节一致，保留用户文本转义、
     原明细顺序与 28.00 元总计，且无客户说明区域。

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

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUOTE_PY = os.path.join(PROJECT_ROOT, "quote.py")

# ---- 固定合成样例：旧库中已有的一张报价 ----

NUMBER = "Q-LEGACY-001"
# 客户名含尖括号与与号，用于核对预览中的 HTML 转义。
CUSTOMER = "演示客户 <甲>&乙"
ITEMS = [
    # (description, quantity, unit_price, line_amount)，金额单位为分。
    ("咨询", 2, 1250, 2500),
    ("资料", 1, 300, 300),
]
TOTAL_CENTS = 2800

# 重复提交时使用的变更内容：输入本身完全合法，仅编号与旧报价冲突。
CHANGED_CUSTOMER = "另一演示客户"
CHANGED_NOTE = "本次不应保存"
CHANGED_ITEM = ("服务", 1, 9999, 9999)

EXPECTED_DUPLICATE_STDERR = f"错误: 报价编号已存在: {NUMBER}\n"

# 旧格式建表语句：quotes 无 note 列，items 为当前明细结构。
LEGACY_SCHEMA = """
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


def changed_payload():
    """同编号的变更报价：客户、说明与明细全部不同，但格式合法。"""
    return {
        "number": NUMBER,
        "customer": CHANGED_CUSTOMER,
        "note": CHANGED_NOTE,
        "items": [
            {
                "description": CHANGED_ITEM[0],
                "quantity": CHANGED_ITEM[1],
                "unit_price": CHANGED_ITEM[2],
            }
        ],
    }


class LegacyDuplicateSaveTestCase(unittest.TestCase):
    """旧库首次重复提交：note 补列保留，报价内容拒绝，预览不受影响。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="quote-legacy-dup-")
        self.addCleanup(self._tmp.cleanup)
        self.workdir = self._tmp.name
        self.db_path = os.path.join(self.workdir, "legacy.sqlite")
        # 直接以旧格式建库并写入固定样例报价，不经过 quote.py。
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executescript(LEGACY_SCHEMA)
            conn.execute(
                "INSERT INTO quotes(number, customer, total) VALUES (?, ?, ?)",
                (NUMBER, CUSTOMER, TOTAL_CENTS),
            )
            for position, (description, quantity, unit_price, line_amount) in enumerate(ITEMS):
                conn.execute(
                    "INSERT INTO items(quote_number, position, description, "
                    "quantity, unit_price, line_amount) VALUES (?, ?, ?, ?, ?, ?)",
                    (NUMBER, position, description, quantity, unit_price, line_amount),
                )
            conn.commit()
        finally:
            conn.close()

    # ---- 基础设施 ----

    def run_quote(self, *cli_args):
        """在临时目录中以独立进程运行 quote.py 的公开命令。"""
        return subprocess.run(
            [sys.executable, QUOTE_PY, *cli_args],
            capture_output=True,
            text=True,
            cwd=self.workdir,
        )

    def quote_columns(self):
        """quotes 表的列名集合，用于核对 note 列在拒绝前后的有无。"""
        conn = sqlite3.connect(self.db_path)
        try:
            return {row[1] for row in conn.execute("PRAGMA table_info(quotes)")}
        finally:
            conn.close()

    def snapshot(self):
        """业务数据快照：quotes 行（含 note）与 items 行（含自增 id）。"""
        conn = sqlite3.connect(self.db_path)
        try:
            quote_rows = conn.execute(
                "SELECT number, customer, total, note FROM quotes ORDER BY number"
            ).fetchall()
            item_rows = conn.execute(
                "SELECT id, quote_number, position, description, quantity, "
                "unit_price, line_amount FROM items ORDER BY quote_number, position"
            ).fetchall()
        finally:
            conn.close()
        return quote_rows, item_rows

    def preview(self, output_name):
        output_path = os.path.join(self.workdir, output_name)
        result = self.run_quote(
            "preview", "--db", self.db_path,
            "--number", NUMBER, "--output", output_path,
        )
        return result, output_path

    def assert_preview_ok(self, result, output_path, label):
        self.assertEqual(
            result.returncode, 0,
            msg=f"样例[{label}]预览应退出码 0，实际 {result.returncode}；"
                f"stderr={result.stderr!r}",
        )
        self.assertEqual(
            result.stdout, f"{output_path}\n",
            msg=f"样例[{label}]预览成功后标准输出只能是目标路径加换行，"
                f"实际：{result.stdout!r}",
        )
        self.assertEqual(
            result.stderr, "",
            msg=f"样例[{label}]预览成功时标准错误必须为空，实际：{result.stderr!r}",
        )
        with open(output_path, "rb") as f:
            return f.read()

    def assert_shows_legacy_quote(self, document, label):
        """预览只反映旧报价：客户转义、明细原顺序、28.00 元总计、无说明区域。"""
        escaped_customer = html.escape(CUSTOMER)
        self.assertIn(
            f"<dd>{escaped_customer}</dd>", document,
            msg=f"样例[{label}]客户应按原文转义显示",
        )
        self.assertNotIn(
            f"<dd>{CUSTOMER}</dd>", document,
            msg=f"样例[{label}]客户中的尖括号与与号不得以未转义形式出现",
        )
        # 变更内容不得以任何形式出现在预览中。
        self.assertNotIn(CHANGED_CUSTOMER, document, msg=f"样例[{label}]不得出现变更后的客户")
        self.assertNotIn(CHANGED_NOTE, document, msg=f"样例[{label}]不得出现变更后的说明")
        self.assertNotIn(CHANGED_ITEM[0], document, msg=f"样例[{label}]不得出现变更后的明细")
        self.assertNotIn("99.99", document, msg=f"样例[{label}]不得出现变更后的金额")

        # 无客户说明区域（旧报价 note 为 NULL）。
        self.assertNotIn('<section class="note">', document,
                         msg=f"样例[{label}]不应出现客户说明区域")
        self.assertNotIn("客户说明", document)
        self.assertNotIn("note-text", document)

        # 明细顺序与金额：咨询 25.00 在前、资料 3.00 在后，总计 28.00 元。
        markers = [
            "咨询",
            '<td class="num">25.00</td>',
            "资料",
            '<td class="num">3.00</td>',
            '<tr><td colspan="4">总计（元）</td><td class="num">28.00</td></tr>',
        ]
        for marker in markers:
            self.assertIn(marker, document, msg=f"样例[{label}]预览缺少预期内容：{marker}")
        positions = [document.index(marker) for marker in markers]
        self.assertEqual(
            positions, sorted(positions),
            msg=f"样例[{label}]明细顺序应仍是咨询在前、资料在后，最后为 28.00 总计",
        )

    # ---- 主流程 ----

    def test_legacy_duplicate_save_rejected_but_note_column_kept(self):
        # 期望常量自检：行金额与合计计算正确，客户转义形式确实由标准库产生，
        # 防止测试常量本身写错。
        self.assertEqual(
            [quantity * unit_price for _, quantity, unit_price, _ in ITEMS],
            [line_amount for _, _, _, line_amount in ITEMS],
        )
        self.assertEqual(
            sum(line_amount for _, _, _, line_amount in ITEMS), TOTAL_CENTS,
        )
        self.assertIn("&lt;甲&gt;&amp;乙", html.escape(CUSTOMER))

        # 前置：旧库 quotes 确实没有 note 列。
        self.assertEqual(
            self.quote_columns(), {"number", "customer", "total"},
            msg="前置条件：旧库 quotes 应只有 number、customer、total 三列",
        )

        # 基准预览：旧库上直接生成，退出码 0，标准输出仅为目标路径加换行。
        baseline_result, baseline_path = self.preview("baseline.html")
        baseline_bytes = self.assert_preview_ok(baseline_result, baseline_path, "基准预览")
        baseline_document = baseline_bytes.decode("utf-8")
        self.assert_shows_legacy_quote(baseline_document, "基准预览")

        # 基准预览是只读操作：不得补 note 列。
        self.assertNotIn(
            "note", self.quote_columns(),
            msg="预览不得改变旧库结构（不补 note 列）",
        )

        # 重复提交：本身合法的变更报价，仅编号与旧报价冲突。
        input_path = os.path.join(self.workdir, "duplicate.json")
        with open(input_path, "w", encoding="utf-8") as f:
            json.dump(changed_payload(), f, ensure_ascii=False)
        save_result = self.run_quote("save", "--db", self.db_path, "--input", input_path)

        # 拒绝路径：退出码 1、标准输出为空、标准错误恰为约定一行、无异常堆栈。
        self.assertEqual(
            save_result.returncode, 1,
            msg=f"重复提交应退出码 1，实际 {save_result.returncode}；"
                f"stdout={save_result.stdout!r}，stderr={save_result.stderr!r}",
        )
        self.assertEqual(
            save_result.stdout, "",
            msg=f"被拒绝时标准输出必须为空，实际：{save_result.stdout!r}",
        )
        self.assertEqual(
            save_result.stderr, EXPECTED_DUPLICATE_STDERR,
            msg=f"标准错误只能是编号已存在提示加换行，实际：{save_result.stderr!r}",
        )
        self.assertNotIn(
            "Traceback", save_result.stderr,
            msg=f"拒绝路径不得出现异常堆栈，实际：{save_result.stderr!r}",
        )

        # 结构变化（与业务数据分开核对）：note 列已补充并保留。
        self.assertIn(
            "note", self.quote_columns(),
            msg="保存失败后，已完成的 note 补列仍应保留在 quotes 表上",
        )

        # 业务数据：报价仍只有原来一张，旧报价 note 为 NULL，全部字段与明细不变。
        quote_rows, item_rows = self.snapshot()
        self.assertEqual(
            quote_rows, [(NUMBER, CUSTOMER, TOTAL_CENTS, None)],
            msg="拒绝后应仍只有原报价：编号、客户、整数合计不变，note 为 NULL",
        )
        self.assertEqual(
            item_rows,
            [
                (1, NUMBER, 0, "咨询", 2, 1250, 2500),
                (2, NUMBER, 1, "资料", 1, 300, 300),
            ],
            msg="拒绝后明细的自增标识、所属编号、位置、文本、数量、单价及行金额"
                "必须全部与提交前一致",
        )

        # 变更内容不得以任何形式落库。
        conn = sqlite3.connect(self.db_path)
        try:
            leaked = conn.execute(
                "SELECT COUNT(*) FROM quotes WHERE customer = ? OR note = ?",
                (CHANGED_CUSTOMER, CHANGED_NOTE),
            ).fetchone()[0]
            leaked_items = conn.execute(
                "SELECT COUNT(*) FROM items WHERE description = ? OR unit_price = ?",
                (CHANGED_ITEM[0], CHANGED_ITEM[2]),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(leaked, 0, msg="变更后的客户与说明不得写入数据库")
        self.assertEqual(leaked_items, 0, msg="变更后的明细不得写入数据库")

        # 拒绝后按同一编号向另一个不存在的路径生成预览：
        # 退出码 0、仅输出目标路径加换行、标准错误为空。
        after_result, after_path = self.preview("after-reject.html")
        after_bytes = self.assert_preview_ok(after_result, after_path, "拒绝后预览")

        # 新旧 HTML 逐字节一致：转义、明细顺序、28.00 元总计与无说明区域均不变。
        self.assertEqual(
            after_bytes, baseline_bytes,
            msg="补列并拒绝保存后，重新生成的预览必须与基准预览逐字节一致",
        )
        self.assert_shows_legacy_quote(after_bytes.decode("utf-8"), "拒绝后预览")


if __name__ == "__main__":
    unittest.main()
