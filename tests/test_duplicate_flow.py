"""重复报价编号保存的回归测试：拒绝重复提交后，原报价仍完整可用。

仅依赖 Python 标准库。用例在独立的临时目录中通过 README 记载的
公开命令（save / preview）操作 quote.py 子进程，合成 JSON、SQLite
与 HTML 预览文件，结束后随临时目录一并清理，不读取也不污染项目内
已有业务数据。

覆盖范围（仅限当前 save 创建的数据库）：
  1. 固定样例 Q-DUP-001 首次保存成功并生成基准预览；
  2. 再次提交完全相同的 JSON、以及同编号但内容变更的 JSON，
     两种输入本身均合法，都必须以退出码 1 拒绝，且原报价与明细
     （含客户、说明原文、整数金额、明细标识与排列顺序）保持不变；
  3. 拒绝后按原编号向新路径生成预览，仍成功且与基准预览逐字节一致。

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

# ---- 固定合成样例 ----

NUMBER = "Q-DUP-001"
CUSTOMER = "演示客户甲"
# 首尾空格、换行以及尖括号与与号都是说明原文的一部分。
NOTE = "  仅作演示<甲>&乙\n第二次补充\n  "
ITEMS = [
    {"description": "咨询", "quantity": 2, "unit_price": 1250},
    {"description": "资料", "quantity": 1, "unit_price": 300},
]
EXPECTED_TOTAL = 2800
EXPECTED_LINE_AMOUNTS = [2500, 300]

# 第二次重复提交时使用的变更内容：客户改变、省略 note、只留一条高价明细。
CHANGED_CUSTOMER = "演示客户乙"
CHANGED_ITEM = {"description": "变更服务", "quantity": 1, "unit_price": 9999}

EXPECTED_DUPLICATE_STDERR = f"错误: 报价编号已存在: {NUMBER}\n"


def original_payload():
    """每次返回一份全新的原报价 JSON 结构。"""
    return {
        "number": NUMBER,
        "customer": CUSTOMER,
        "note": NOTE,
        "items": [dict(item) for item in ITEMS],
    }


def changed_payload():
    """同编号的变更报价：输入格式本身完全合法，仅编号与原报价冲突。"""
    return {
        "number": NUMBER,
        "customer": CHANGED_CUSTOMER,
        # 故意省略 note 字段。
        "items": [dict(CHANGED_ITEM)],
    }


class DuplicateNumberSaveTestCase(unittest.TestCase):
    """重复编号的 save 必须被拒绝，且数据库与预览仍只反映原报价。"""

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

    def write_input(self, payload, name):
        path = os.path.join(self.workdir, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        return path

    def save(self, payload, input_name):
        input_path = self.write_input(payload, input_name)
        result = self.run_quote("save", "--db", self.db_path, "--input", input_path)
        return result

    def snapshot(self):
        """读取数据库完整快照：quotes 行（含 note）与 items 行（含自增 id）。"""
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

    def expected_original_snapshot(self):
        return (
            [(NUMBER, CUSTOMER, EXPECTED_TOTAL, NOTE)],
            [
                (1, NUMBER, 0, "咨询", 2, 1250, 2500),
                (2, NUMBER, 1, "资料", 1, 300, 300),
            ],
        )

    def assert_duplicate_rejected(self, result, label):
        """重复编号：退出码 1、标准输出为空、标准错误恰为约定的一行、无堆栈。"""
        self.assertEqual(
            result.returncode, 1,
            msg=f"样例[{label}]重复提交应退出码 1，实际 {result.returncode}；"
                f"stdout={result.stdout!r}，stderr={result.stderr!r}",
        )
        self.assertEqual(
            result.stdout, "",
            msg=f"样例[{label}]被拒绝时标准输出必须为空，实际：{result.stdout!r}",
        )
        self.assertEqual(
            result.stderr, EXPECTED_DUPLICATE_STDERR,
            msg=f"样例[{label}]标准错误只能是编号已存在提示加换行，"
                f"实际：{result.stderr!r}",
        )
        self.assertNotIn(
            "Traceback", result.stderr,
            msg=f"样例[{label}]拒绝路径不得出现异常堆栈，实际：{result.stderr!r}",
        )

    def assert_database_unchanged(self, expected_snapshot, label):
        """拒绝后数据库仍只有原报价及原两条明细，标识、顺序、原文全部保留。"""
        quote_rows, item_rows = self.snapshot()

        self.assertEqual(
            len(quote_rows), 1,
            msg=f"样例[{label}]拒绝后 quotes 表应仍只有 1 条原报价，"
                f"实际 {len(quote_rows)} 条：{quote_rows!r}",
        )
        self.assertEqual(
            quote_rows, expected_snapshot[0],
            msg=f"样例[{label}]拒绝后原报价的编号、客户、整数合计与说明原文"
                f"必须逐字段保留，实际：{quote_rows!r}",
        )

        self.assertEqual(
            len(item_rows), 2,
            msg=f"样例[{label}]拒绝后 items 表应仍只有原两条明细，不得追加，"
                f"实际 {len(item_rows)} 条：{item_rows!r}",
        )
        self.assertEqual(
            item_rows, expected_snapshot[1],
            msg=f"样例[{label}]拒绝后明细的自增标识、所属编号、排列顺序、说明"
                f"与整数金额必须全部保留，实际：{item_rows!r}",
        )

        # 变更内容不得以任何形式落库。
        customers = [row[1] for row in quote_rows]
        descriptions = [row[3] for row in item_rows]
        unit_prices = [row[5] for row in item_rows]
        self.assertNotIn(
            CHANGED_CUSTOMER, customers,
            msg=f"样例[{label}]变更后的客户名称不得写入数据库",
        )
        self.assertNotIn(
            CHANGED_ITEM["description"], descriptions,
            msg=f"样例[{label}]变更后的明细说明不得写入数据库",
        )
        self.assertNotIn(
            CHANGED_ITEM["unit_price"], unit_prices,
            msg=f"样例[{label}]变更后的单价不得写入数据库",
        )

    def assert_preview_succeeds(self, output_name):
        output_path = os.path.join(self.workdir, output_name)
        result = self.run_quote(
            "preview", "--db", self.db_path,
            "--number", NUMBER, "--output", output_path,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(
            result.stdout, f"{output_path}\n",
            msg="预览成功后标准输出只能是目标路径加换行",
        )
        self.assertEqual(result.stderr, "", msg="预览成功时标准错误必须为空")
        self.assertTrue(os.path.exists(output_path))
        with open(output_path, "rb") as f:
            return f.read()

    def assert_shows_original_quote(self, document, label):
        """预览内容只反映原报价：原客户、原说明（转义并保留空白）、28.00 元。"""
        # 原客户与原明细保留，变更内容不出现。
        self.assertIn("<dd>演示客户甲</dd>", document, msg=f"样例[{label}]应保留原客户")
        self.assertNotIn("演示客户乙", document, msg=f"样例[{label}]不得出现变更后的客户")
        self.assertNotIn("变更服务", document, msg=f"样例[{label}]不得出现变更后的明细")
        self.assertNotIn("99.99", document, msg=f"样例[{label}]不得出现变更后的金额")

        # 说明尖括号与与号转义，首尾空格与换行逐字符保留在 <p> 文本内。
        escaped_note = html.escape(NOTE)
        self.assertIn(
            f'<p class="note-text">{escaped_note}</p>', document,
            msg=f"样例[{label}]说明应整体转义且保留原空格与换行",
        )
        self.assertNotIn(
            "仅作演示<甲>&乙", document,
            msg=f"样例[{label}]说明中的尖括号与与号不得以原文形式出现",
        )

        # 明细行金额 25.00 / 3.00，总计 28.00 元，顺序与首次保存一致。
        markers = [
            "咨询",
            '<td class="num">25.00</td>',
            "资料",
            '<td class="num">3.00</td>',
            '<td class="num">28.00</td>',
        ]
        for marker in markers:
            self.assertIn(marker, document, msg=f"样例[{label}]预览缺少预期内容：{marker}")
        positions = [document.index(marker) for marker in markers]
        self.assertEqual(
            positions, sorted(positions),
            msg=f"样例[{label}]明细顺序应仍是咨询在前、资料在后，最后为 28.00 总计",
        )

    # ---- 主流程 ----

    def test_duplicate_submissions_rejected_original_quote_intact(self):
        # 期望常量自检：转义形式确实由标准库产生，合计与行金额计算正确，
        # 防止测试常量本身写错。
        self.assertEqual(
            [item["quantity"] * item["unit_price"] for item in ITEMS],
            EXPECTED_LINE_AMOUNTS,
        )
        self.assertEqual(sum(EXPECTED_LINE_AMOUNTS), EXPECTED_TOTAL)
        self.assertIn("&lt;甲&gt;&amp;乙", html.escape(NOTE))
        self.assertTrue(html.escape(NOTE).startswith("  仅作演示"))
        self.assertTrue(html.escape(NOTE).endswith("\n  "))

        # 首次保存：退出码 0，标准输出仅为编号加换行，标准错误为空。
        first_save = self.save(original_payload(), "original.json")
        self.assertEqual(first_save.returncode, 0, msg=first_save.stderr)
        self.assertEqual(
            first_save.stdout, f"{NUMBER}\n",
            msg="首次保存成功后标准输出只能是编号加换行",
        )
        self.assertEqual(first_save.stderr, "", msg="首次保存成功时标准错误必须为空")

        # 首次落库：原客户、说明原文、整数合计、明细标识与顺序。
        expected_snapshot = self.expected_original_snapshot()
        self.assertEqual(
            self.snapshot(), expected_snapshot,
            msg="首次保存后的数据库内容应与固定样例完全一致",
        )

        # 基准预览：成功并只反映原报价。
        baseline_bytes = self.assert_preview_succeeds("baseline.html")
        baseline_document = baseline_bytes.decode("utf-8")
        self.assert_shows_original_quote(baseline_document, label="基准预览")

        # 两种重复提交：输入本身都合法，仅编号与库中报价冲突。
        # 每次拒绝后都核对数据库，并用全新的输出路径按原编号再生成预览。
        duplicate_cases = [
            ("完全相同的JSON", original_payload(), "dup-same.json", "after-same.html"),
            ("同编号变更内容报价", changed_payload(), "dup-changed.json", "after-changed.html"),
        ]
        for label, payload, input_name, preview_name in duplicate_cases:
            with self.subTest(重复提交样例=label):
                result = self.save(payload, input_name)
                self.assert_duplicate_rejected(result, label)
                self.assert_database_unchanged(expected_snapshot, label)

                # 拒绝后按原编号向新的文件路径生成预览：仍成功，输出该路径，
                # 与基准预览逐字节一致，只反映原客户、原说明与 28.00 元总计。
                after_bytes = self.assert_preview_succeeds(preview_name)
                self.assertEqual(
                    after_bytes, baseline_bytes,
                    msg=f"样例[{label}]拒绝后重新生成的预览必须与基准预览逐字节一致",
                )
                self.assert_shows_original_quote(
                    after_bytes.decode("utf-8"), label=f"样例[{label}]拒绝后预览"
                )


if __name__ == "__main__":
    unittest.main()
