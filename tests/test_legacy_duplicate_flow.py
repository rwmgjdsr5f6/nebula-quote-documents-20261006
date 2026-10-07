"""旧库首次遇到重复编号的回归测试：补列保留，但本次报价内容绝不落库。

仅依赖 Python 标准库。用例在独立的临时目录中手工准备旧格式 SQLite
（quotes 只有 number、customer、total 三列，不含 note；items 为当前
明细结构），再通过 README 记载的公开命令（save / preview）操作
quote.py 子进程，使用合成数据，结束后随临时目录一并清理，不读取也
不污染项目内已有业务数据。

覆盖的交汇行为（两种既有行为同时触发时的确定结果）：
  1. save 会为旧库补充 note 列——即使本次保存因重复编号被拒绝，
     已经完成的结构补充仍然保留（旧报价 note 为 NULL）；
  2. 重复编号被拒绝——退出码 1，标准输出为空，标准错误恰为
     “错误: 报价编号已存在: Q-LEGACY-001”加换行，无异常堆栈，
     本次提交的客户、说明与明细一律不落库；
  3. 结构变化与业务数据分别核对：拒绝保存不等于数据库完全没有变化；
  4. 拒绝后按原编号向新路径预览，与旧库状态下的基准预览逐字节一致。

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

# ---- 固定合成样例：旧库中已存在的报价 ----

NUMBER = "Q-LEGACY-001"
# 尖括号与与号都是客户名原文的一部分，预览中必须转义。
CUSTOMER = "演示客户 <甲>&乙"
ITEMS = [
    # (description, quantity, unit_price, line_amount)，单位均为分。
    ("咨询", 2, 1250, 2500),
    ("资料", 1, 300, 300),
]
TOTAL_CENTS = 2800

# 重复提交时使用的变更内容：输入本身完全合法，仅编号与旧报价冲突。
CHANGED_CUSTOMER = "另一演示客户"
CHANGED_NOTE = "本次不应保存"
CHANGED_ITEM = {"description": "服务", "quantity": 1, "unit_price": 9999}

EXPECTED_DUPLICATE_STDERR = f"错误: 报价编号已存在: {NUMBER}\n"


def changed_payload():
    """同编号的变更报价：合法 UTF-8 JSON，仅编号与旧报价冲突。"""
    return {
        "number": NUMBER,
        "customer": CHANGED_CUSTOMER,
        "note": CHANGED_NOTE,
        "items": [dict(CHANGED_ITEM)],
    }


class LegacyDuplicateSaveTestCase(unittest.TestCase):
    """旧库首次遇到重复编号：补列保留、业务数据不变、预览逐字节一致。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="quote-legacy-dup-")
        self.addCleanup(self._tmp.cleanup)
        self.workdir = self._tmp.name
        self.db_path = os.path.join(self.workdir, "legacy.sqlite")
        # 直接以旧格式建库：quotes 无 note 列，items 为当前明细结构，
        # 已有一张固定样例报价。
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
                (NUMBER, CUSTOMER, TOTAL_CENTS),
            )
            conn.executemany(
                "INSERT INTO items(quote_number, position, description, "
                "quantity, unit_price, line_amount) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (NUMBER, position, description, quantity, unit_price, line_amount)
                    for position, (description, quantity, unit_price, line_amount)
                    in enumerate(ITEMS)
                ],
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
        """quotes 表当前列名列表（结构快照）。"""
        conn = sqlite3.connect(self.db_path)
        try:
            return [row[1] for row in conn.execute("PRAGMA table_info(quotes)")]
        finally:
            conn.close()

    def business_snapshot(self):
        """业务数据快照：quotes 全部行与 items 全部行（含自增标识）。"""
        conn = sqlite3.connect(self.db_path)
        try:
            quote_rows = conn.execute(
                "SELECT number, customer, total FROM quotes ORDER BY number"
            ).fetchall()
            item_rows = conn.execute(
                "SELECT id, quote_number, position, description, quantity, "
                "unit_price, line_amount FROM items "
                "ORDER BY quote_number, position"
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

    # ---- 主流程 ----

    def test_legacy_duplicate_rejected_but_note_column_kept(self):
        # 期望常量自检：行金额与合计计算正确，客户名转义形式确实由标准库
        # 产生，防止测试常量本身写错。
        self.assertEqual(
            [quantity * unit_price for _, quantity, unit_price, _ in ITEMS],
            [line_amount for _, _, _, line_amount in ITEMS],
        )
        self.assertEqual(
            sum(line_amount for _, _, _, line_amount in ITEMS), TOTAL_CENTS,
        )
        self.assertEqual(html.escape(CUSTOMER), "演示客户 &lt;甲&gt;&amp;乙")

        # 前置条件：旧库 quotes 表确实没有 note 列。
        columns_before = self.quote_columns()
        self.assertEqual(columns_before, ["number", "customer", "total"],
                         msg="前置条件：旧库 quotes 表应只有三列，不含 note")

        # 提交前的业务数据快照，供拒绝后逐字段比对。
        snapshot_before = self.business_snapshot()
        self.assertEqual(snapshot_before[0], [(NUMBER, CUSTOMER, TOTAL_CENTS)])
        self.assertEqual(
            [row[1:] for row in snapshot_before[1]],
            [(NUMBER, position, description, quantity, unit_price, line_amount)
             for position, (description, quantity, unit_price, line_amount)
             in enumerate(ITEMS)],
            msg="前置条件：旧库明细应与固定样例一致",
        )

        # 基准预览：旧库（无 note 列）状态下生成，退出码 0，
        # 标准输出仅为目标路径加换行，标准错误为空。
        baseline_result, baseline_path = self.preview("baseline.html")
        self.assertEqual(baseline_result.returncode, 0, msg=baseline_result.stderr)
        self.assertEqual(baseline_result.stdout, f"{baseline_path}\n")
        self.assertEqual(baseline_result.stderr, "")
        with open(baseline_path, "rb") as f:
            baseline_bytes = f.read()

        # 重复提交：合法 UTF-8 JSON，沿用旧编号，客户、说明、明细全部变更。
        input_path = os.path.join(self.workdir, "changed.json")
        with open(input_path, "w", encoding="utf-8") as f:
            json.dump(changed_payload(), f, ensure_ascii=False)
        save_result = self.run_quote(
            "save", "--db", self.db_path, "--input", input_path
        )

        # 保存被拒绝：退出码 1，标准输出为空，标准错误恰为约定的一行，
        # 以换行结束，不显示异常堆栈。
        self.assertEqual(
            save_result.returncode, 1,
            msg=f"重复编号应退出码 1，实际 {save_result.returncode}；"
                f"stdout={save_result.stdout!r}，stderr={save_result.stderr!r}",
        )
        self.assertEqual(
            save_result.stdout, "",
            msg=f"拒绝时标准输出必须为空，实际：{save_result.stdout!r}",
        )
        self.assertEqual(
            save_result.stderr, EXPECTED_DUPLICATE_STDERR,
            msg=f"标准错误只能是编号已存在提示加换行，实际：{save_result.stderr!r}",
        )
        self.assertNotIn(
            "Traceback", save_result.stderr,
            msg=f"拒绝路径不得出现异常堆栈，实际：{save_result.stderr!r}",
        )

        # ---- 结构变化：note 列补充已经保留 ----
        columns_after = self.quote_columns()
        self.assertEqual(
            columns_after, ["number", "customer", "total", "note"],
            msg="保存虽被拒绝，旧库应已补上 note 列（结构变化保留）",
        )

        # ---- 业务数据：与提交前完全一致 ----
        snapshot_after = self.business_snapshot()
        self.assertEqual(
            snapshot_after, snapshot_before,
            msg="拒绝后报价与明细的标识、位置、文本、数量、单价及行金额"
                "必须与提交前一致",
        )
        quote_rows, item_rows = snapshot_after
        self.assertEqual(len(quote_rows), 1, msg="报价仍应只有原来一张")
        self.assertEqual(quote_rows[0][0], NUMBER)
        self.assertEqual(quote_rows[0][1], CUSTOMER, msg="旧客户名不得被改写")
        self.assertEqual(quote_rows[0][2], TOTAL_CENTS,
                         msg="合计应仍为整数 2800 分")
        self.assertEqual(
            type(quote_rows[0][2]), int,
            msg="合计应以整数存储",
        )

        # 旧报价的 note 列为 NULL（无说明），本次提交的说明不得落库。
        conn = sqlite3.connect(self.db_path)
        try:
            note_value = conn.execute(
                "SELECT note FROM quotes WHERE number = ?", (NUMBER,)
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertIsNone(note_value, msg="旧报价补列后的 note 应为 NULL")

        # 变更内容不得以任何形式落库。
        self.assertNotIn(CHANGED_CUSTOMER, [row[1] for row in quote_rows],
                         msg="变更后的客户不得写入数据库")
        self.assertNotIn(CHANGED_NOTE, [note_value],
                         msg="本次提交的说明不得写入数据库")
        self.assertNotIn(CHANGED_ITEM["description"],
                         [row[3] for row in item_rows],
                         msg="变更后的明细说明不得写入数据库")
        self.assertNotIn(CHANGED_ITEM["unit_price"],
                         [row[5] for row in item_rows],
                         msg="变更后的单价不得写入数据库")

        # ---- 拒绝后预览：与基准预览逐字节一致 ----
        after_result, after_path = self.preview("after-reject.html")
        self.assertEqual(after_result.returncode, 0, msg=after_result.stderr)
        self.assertEqual(after_result.stdout, f"{after_path}\n")
        self.assertEqual(after_result.stderr, "")
        with open(after_path, "rb") as f:
            after_bytes = f.read()
        self.assertEqual(
            after_bytes, baseline_bytes,
            msg="补列后再次预览应与旧库状态下的基准预览逐字节一致",
        )

        document = after_bytes.decode("utf-8")
        # 用户文本转义：客户名中的尖括号与与号必须以转义形式出现。
        self.assertIn(f"<dd>{html.escape(CUSTOMER)}</dd>", document,
                      msg="客户名应整体转义后展示")
        self.assertNotIn(f"<dd>{CUSTOMER}</dd>", document,
                         msg="客户名中的尖括号与与号不得以原文形式出现")
        # 变更内容不得出现在预览中。
        self.assertNotIn(CHANGED_CUSTOMER, document)
        self.assertNotIn(CHANGED_NOTE, document)
        self.assertNotIn("99.99", document)
        # 原明细顺序：咨询在前、资料在后，最后为 28.00 元总计。
        markers = [
            "咨询",
            '<td class="num">25.00</td>',
            "资料",
            '<td class="num">3.00</td>',
            '<tr><td colspan="4">总计（元）</td><td class="num">28.00</td></tr>',
        ]
        for marker in markers:
            self.assertIn(marker, document, msg=f"预览缺少预期内容：{marker}")
        positions = [document.index(marker) for marker in markers]
        self.assertEqual(
            positions, sorted(positions),
            msg="明细顺序应仍是咨询在前、资料在后，最后为 28.00 元总计",
        )
        # 无说明区域。
        self.assertNotIn('<section class="note">', document,
                         msg="旧报价预览不应出现客户说明区域")
        self.assertNotIn("客户说明", document)
        self.assertNotIn("note-text", document)


if __name__ == "__main__":
    unittest.main()
