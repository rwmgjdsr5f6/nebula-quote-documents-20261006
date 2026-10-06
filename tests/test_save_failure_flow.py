"""保存过程中明细写入失败的事务回滚回归测试。

仅依赖 Python 标准库。用例在独立的临时目录中通过 README 记载的
公开命令（save）操作 quote.py 子进程，合成 UTF-8 JSON 与当前版本
创建、已具备 note 列的 SQLite 数据库，结束后随临时目录一并清理，
不读取也不污染项目内已有业务数据，不使用第三方包，也不以桩函数
替代保存行为。

场景（固定样例）：
  1. 先保存 Q-KEEP（客户“合成甲”，说明含首尾空格与换行，一条
     数量 1、单价 100 分的保留服务）作为不得受影响的前置数据；
  2. 在测试库上安装触发器，使 Q-TXN 保存时只有第二条明细
     （position = 1）的写入语句触发 SQLite 约束错误
     “演示明细拒绝写入”，该故障只拒绝当前语句，不自行撤销
     同事务内先前写入的主记录与第一条明细——以此检验 save
     自身的事务边界：主记录与全部明细必须一起保存或一起撤销；
  3. 断言本次 save 退出码 1、标准输出为空、标准错误恰为
     “错误: 数据库写入失败: 演示明细拒绝写入”加换行且无堆栈，
     回读数据库确认 Q-TXN 主记录与两条明细均不存在，Q-KEEP
     全部字段、说明原文、明细标识与金额与失败前完全一致；
  4. 解除同一测试库的拒绝条件（DROP TRIGGER）后，再次提交
     字节完全相同的 Q-TXN 输入：退出码 0、标准输出仅为编号加
     换行、标准错误为空；数据库恰好新增一张报价与两条有序明细，
     行金额 2500、300 分，合计整数 2800 分，客户与说明原样保留，
     Q-KEEP 仍不变，且自增序列无残痕，证明失败既未占用编号也未
     留下残行。

运行方式（项目根目录）：
    python3 -m unittest discover -s tests
全部通过时退出码为 0，任一断言失败时指出对应阶段与实际结果。
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

# ---- 固定合成样例：先保存、任何时候都必须保持不变的报价 ----

KEEP_NUMBER = "Q-KEEP"
KEEP_CUSTOMER = "合成甲"
# 说明含首尾空格与换行，必须逐字符原样保留。
KEEP_NOTE = "  保留报价说明\n第二行说明  "
KEEP_ITEMS = [
    {"description": "保留服务", "quantity": 1, "unit_price": 100},
]
KEEP_LINE_AMOUNTS = [100]
KEEP_TOTAL = 100

# ---- 固定合成样例：第二条明细写入失败、随后重试成功的报价 ----

TXN_NUMBER = "Q-TXN"
TXN_CUSTOMER = "合成乙"
TXN_NOTE = "演示说明"
TXN_ITEMS = [
    {"description": "咨询", "quantity": 2, "unit_price": 1250},
    {"description": "资料", "quantity": 1, "unit_price": 300},
]
TXN_LINE_AMOUNTS = [2500, 300]
TXN_TOTAL = 2800

# 只在 Q-TXN 第二条明细（position 从 0 起，故第二条为 1）写入时触发。
# RAISE(ABORT) 是 SQLite 触发器约束错误：仅中止当前语句，不自行
# 撤销同一事务内先前的写入，因此能真正检验 save 的事务回滚边界。
TRIGGER_NAME = "reject_quote_txn_second_item"
REJECT_MESSAGE = "演示明细拒绝写入"
EXPECTED_FAIL_STDERR = f"错误: 数据库写入失败: {REJECT_MESSAGE}\n"


def keep_payload():
    return {
        "number": KEEP_NUMBER,
        "customer": KEEP_CUSTOMER,
        "note": KEEP_NOTE,
        "items": [dict(item) for item in KEEP_ITEMS],
    }


def txn_payload():
    return {
        "number": TXN_NUMBER,
        "customer": TXN_CUSTOMER,
        "note": TXN_NOTE,
        "items": [dict(item) for item in TXN_ITEMS],
    }


class ItemWriteFailureRollbackTestCase(unittest.TestCase):
    """明细写入失败时，新报价的主记录与全部明细必须原子撤销。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="quote-save-fail-test-")
        self.addCleanup(self._tmp.cleanup)
        self.workdir = self._tmp.name
        self.db_path = os.path.join(self.workdir, "quotes.sqlite")

    # ---- 基础设施：全部通过公开命令或直接回读 SQLite 完成 ----

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

    def save(self, input_path):
        return self.run_quote("save", "--db", self.db_path, "--input", input_path)

    def connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def read_quotes(self):
        """全部 quotes 行（含 note），按编号排序。"""
        conn = self.connect()
        try:
            return conn.execute(
                "SELECT number, customer, total, note FROM quotes ORDER BY number"
            ).fetchall()
        finally:
            conn.close()

    def read_items(self):
        """全部 items 行（含自增 id），按 id 排序以核对标识与顺序。"""
        conn = self.connect()
        try:
            return conn.execute(
                "SELECT id, quote_number, position, description, quantity, "
                "unit_price, line_amount FROM items ORDER BY id"
            ).fetchall()
        finally:
            conn.close()

    def read_items_for(self, number):
        conn = self.connect()
        try:
            return conn.execute(
                "SELECT position, description, quantity, unit_price, line_amount "
                "FROM items WHERE quote_number = ? ORDER BY position",
                (number,),
            ).fetchall()
        finally:
            conn.close()

    def read_item_sequence(self):
        """items 表 AUTOINCREMENT 计数器；无残痕时应与最大 id 一致。"""
        conn = self.connect()
        try:
            return conn.execute(
                "SELECT name, seq FROM sqlite_sequence WHERE name = 'items'"
            ).fetchall()
        finally:
            conn.close()

    def install_reject_trigger(self):
        """让 Q-TXN 第二条明细的写入语句单独失败，其他写入不受影响。"""
        conn = self.connect()
        try:
            conn.execute(
                f"""
                CREATE TRIGGER {TRIGGER_NAME}
                BEFORE INSERT ON items
                WHEN NEW.quote_number = '{TXN_NUMBER}' AND NEW.position = 1
                BEGIN
                    SELECT RAISE(ABORT, '{REJECT_MESSAGE}');
                END
                """
            )
            conn.commit()
        finally:
            conn.close()

    def remove_reject_trigger(self):
        conn = self.connect()
        try:
            conn.execute(f"DROP TRIGGER {TRIGGER_NAME}")
            conn.commit()
        finally:
            conn.close()

    def trigger_present(self):
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name = ?",
                (TRIGGER_NAME,),
            ).fetchone()
        finally:
            conn.close()
        return row is not None

    def expected_keep_quote_row(self):
        return (KEEP_NUMBER, KEEP_CUSTOMER, KEEP_TOTAL, KEEP_NOTE)

    def expected_keep_item_row(self, item_id):
        return (item_id, KEEP_NUMBER, 0, "保留服务", 1, 100, 100)

    # ---- 主流程 ----

    def test_item_write_failure_rolls_back_whole_quote_then_retry_succeeds(self):
        # 期望常量自检：行金额与合计确为整数分计算结果，防止测试常量写错。
        self.assertEqual(
            [item["quantity"] * item["unit_price"] for item in KEEP_ITEMS],
            KEEP_LINE_AMOUNTS,
        )
        self.assertEqual(sum(KEEP_LINE_AMOUNTS), KEEP_TOTAL)
        self.assertEqual(
            [item["quantity"] * item["unit_price"] for item in TXN_ITEMS],
            TXN_LINE_AMOUNTS,
        )
        self.assertEqual(sum(TXN_LINE_AMOUNTS), TXN_TOTAL)
        self.assertIsInstance(TXN_TOTAL, int)
        self.assertTrue(KEEP_NOTE.startswith("  "))
        self.assertTrue(KEEP_NOTE.endswith("  "))
        self.assertIn("\n", KEEP_NOTE)

        # ---- 阶段一：先保存固定样例 Q-KEEP ----
        keep_input = self.write_input(keep_payload(), "keep.json")
        first_save = self.save(keep_input)
        self.assertEqual(
            first_save.returncode, 0,
            msg=f"阶段一：Q-KEEP 保存应退出码 0，实际 {first_save.returncode}；"
                f"stdout={first_save.stdout!r}，stderr={first_save.stderr!r}",
        )
        self.assertEqual(
            first_save.stdout, f"{KEEP_NUMBER}\n",
            msg=f"阶段一：成功后标准输出只能是编号加换行，实际 {first_save.stdout!r}",
        )
        self.assertEqual(
            first_save.stderr, "",
            msg=f"阶段一：成功后标准错误必须为空，实际 {first_save.stderr!r}",
        )

        quotes_before = self.read_quotes()
        items_before = self.read_items()
        self.assertEqual(
            quotes_before, [self.expected_keep_quote_row()],
            msg=f"阶段一：quotes 表应只有 Q-KEEP 且字段、说明原文与合计正确，"
                f"实际 {quotes_before!r}",
        )
        self.assertEqual(
            items_before, [self.expected_keep_item_row(1)],
            msg=f"阶段一：items 表应只有 Q-KEEP 的一条明细（自增 id=1），"
                f"实际 {items_before!r}",
        )
        sequence_before = self.read_item_sequence()
        self.assertEqual(
            sequence_before, [("items", 1)],
            msg=f"阶段一：自增序列应停在 1，实际 {sequence_before!r}",
        )

        # ---- 阶段二：安装只针对 Q-TXN 第二条明细的拒绝触发器后提交 ----
        self.install_reject_trigger()
        self.assertTrue(
            self.trigger_present(),
            msg="阶段二：前置条件——拒绝触发器应已存在于测试库",
        )
        # 输入文件在失败与重试两个阶段共用同一份字节内容。
        txn_input = self.write_input(txn_payload(), "txn.json")

        failed_save = self.save(txn_input)
        self.assertEqual(
            failed_save.returncode, 1,
            msg=f"阶段二：明细写入失败时应退出码 1，实际 {failed_save.returncode}；"
                f"stdout={failed_save.stdout!r}，stderr={failed_save.stderr!r}",
        )
        self.assertEqual(
            failed_save.stdout, "",
            msg=f"阶段二：失败时标准输出必须为空，实际 {failed_save.stdout!r}",
        )
        self.assertEqual(
            failed_save.stderr, EXPECTED_FAIL_STDERR,
            msg=f"阶段二：标准错误只能是约定的一行加换行，"
                f"实际 {failed_save.stderr!r}",
        )
        self.assertNotIn(
            "Traceback", failed_save.stderr,
            msg=f"阶段二：失败路径不得向标准错误泄漏异常堆栈，"
                f"实际 {failed_save.stderr!r}",
        )

        # ---- 阶段三：回读数据库——Q-TXN 整体不存在，Q-KEEP 逐字段不变 ----
        quotes_after_failure = self.read_quotes()
        items_after_failure = self.read_items()

        self.assertEqual(
            len(quotes_after_failure), 1,
            msg=f"阶段三：失败后 quotes 表必须仍只有 1 张报价，"
                f"实际 {len(quotes_after_failure)} 条：{quotes_after_failure!r}",
        )
        self.assertEqual(
            quotes_after_failure, quotes_before,
            msg=f"阶段三：失败后 Q-KEEP 的编号、客户、整数合计与说明原文"
                f"必须与失败前完全一致，实际 {quotes_after_failure!r}",
        )
        self.assertEqual(
            quotes_after_failure, [self.expected_keep_quote_row()],
            msg=f"阶段三：Q-KEEP 说明首尾空格与换行必须逐字符保留，"
                f"实际 {quotes_after_failure!r}",
        )

        txn_quote_rows = [
            row for row in quotes_after_failure if row[0] == TXN_NUMBER
        ]
        self.assertEqual(
            txn_quote_rows, [],
            msg=f"阶段三：Q-TXN 主记录必须随事务一并撤销，"
                f"实际仍存在：{txn_quote_rows!r}",
        )

        self.assertEqual(
            len(items_after_failure), 1,
            msg=f"阶段三：失败后 items 表必须仍只有 1 条明细，"
                f"实际 {len(items_after_failure)} 条：{items_after_failure!r}",
        )
        self.assertEqual(
            items_after_failure, items_before,
            msg=f"阶段三：失败后 Q-KEEP 明细的自增标识、顺序、说明与金额"
                f"必须与失败前完全一致，实际 {items_after_failure!r}",
        )
        self.assertEqual(
            items_after_failure, [self.expected_keep_item_row(1)],
            msg=f"阶段三：Q-KEEP 明细应为 id=1、行金额 100 分，"
                f"实际 {items_after_failure!r}",
        )

        txn_item_rows = self.read_items_for(TXN_NUMBER)
        self.assertEqual(
            txn_item_rows, [],
            msg=f"阶段三：Q-TXN 的两条明细（含故障前已写入的第一条）"
                f"都必须随主记录一并撤销，实际残留：{txn_item_rows!r}",
        )

        # 第一条明细在故障前曾取得自增 id；事务回滚后计数器也不得推进。
        self.assertEqual(
            self.read_item_sequence(), sequence_before,
            msg="阶段三：失败不得推进 items 自增序列（不得留下残痕）",
        )

        # 触发器在失败后仍然存在：拒绝条件尚未解除。
        self.assertTrue(
            self.trigger_present(),
            msg="阶段三：失败本身不得改变触发器，解除只能由测试显式完成",
        )

        # ---- 阶段四：解除同一测试库的拒绝条件，重交完全相同的输入 ----
        self.remove_reject_trigger()
        self.assertFalse(
            self.trigger_present(),
            msg="阶段四：DROP TRIGGER 后拒绝条件应已解除",
        )
        # 沿用阶段二写入的同一份 txn.json（字节完全相同）。
        retry_save = self.save(txn_input)
        self.assertEqual(
            retry_save.returncode, 0,
            msg=f"阶段四：解除故障后重交相同输入应退出码 0，"
                f"实际 {retry_save.returncode}；stderr={retry_save.stderr!r}",
        )
        self.assertEqual(
            retry_save.stdout, f"{TXN_NUMBER}\n",
            msg=f"阶段四：成功后标准输出只能是编号加换行，"
                f"实际 {retry_save.stdout!r}",
        )
        self.assertEqual(
            retry_save.stderr, "",
            msg=f"阶段四：成功后标准错误必须为空，实际 {retry_save.stderr!r}",
        )

        # ---- 阶段五：回读数据库——恰好新增一报价两明细，原报价不变 ----
        quotes_final = self.read_quotes()
        items_final = self.read_items()

        self.assertEqual(
            len(quotes_final), 2,
            msg=f"阶段五：重试后 quotes 表必须恰好有 2 张报价，"
                f"实际 {len(quotes_final)} 条：{quotes_final!r}",
        )
        self.assertEqual(
            len(items_final), 3,
            msg=f"阶段五：重试后 items 表必须恰好有 3 条明细，"
                f"实际 {len(items_final)} 条：{items_final!r}",
        )

        self.assertEqual(
            quotes_final,
            [self.expected_keep_quote_row(),
             (TXN_NUMBER, TXN_CUSTOMER, TXN_TOTAL, TXN_NOTE)],
            msg=f"阶段五：Q-KEEP 必须保持不变，Q-TXN 客户与说明原样保留、"
                f"合计为整数 2800 分，实际 {quotes_final!r}",
        )
        txn_total = [row[2] for row in quotes_final if row[0] == TXN_NUMBER][0]
        self.assertIsInstance(
            txn_total, int,
            msg=f"阶段五：合计必须以整数存储，实际类型 {type(txn_total).__name__}",
        )
        self.assertEqual(txn_total, 2800)

        self.assertEqual(
            items_final,
            [
                self.expected_keep_item_row(1),
                (2, TXN_NUMBER, 0, "咨询", 2, 1250, 2500),
                (3, TXN_NUMBER, 1, "资料", 1, 300, 300),
            ],
            msg=f"阶段五：Q-KEEP 明细不变，Q-TXN 两条明细须按输入顺序"
                f"排列、行金额为 2500 与 300 分、自增 id 连续无残痕，"
                f"实际 {items_final!r}",
        )

        txn_items = self.read_items_for(TXN_NUMBER)
        self.assertEqual(
            [row[0] for row in txn_items], [0, 1],
            msg=f"阶段五：Q-TXN 明细位置须依次为 0、1，实际 {txn_items!r}",
        )
        self.assertEqual(
            [row[4] for row in txn_items], [2500, 300],
            msg=f"阶段五：Q-TXN 行金额须依次为 2500、300 分，"
                f"实际 {txn_items!r}",
        )
        self.assertEqual(
            [row[1] for row in txn_items], ["咨询", "资料"],
            msg=f"阶段五：Q-TXN 明细说明须依次为咨询、资料，"
                f"实际 {txn_items!r}",
        )

        # 自增序列只反映三张真实存在的明细，失败尝试未占用编号段。
        self.assertEqual(
            self.read_item_sequence(), [("items", 3)],
            msg="阶段五：自增序列应停在 3，证明失败未留下残行",
        )


if __name__ == "__main__":
    unittest.main()
