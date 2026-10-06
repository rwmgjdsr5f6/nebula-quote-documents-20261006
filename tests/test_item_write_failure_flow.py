"""保存过程中明细写入失败的回归测试：主记录与全部明细原子回滚。

仅依赖 Python 标准库。用例在独立的临时目录中通过 README 记载的
公开命令（save）操作 quote.py 子进程，合成 UTF-8 JSON 与当前版本
创建（已具备 note 列）的 SQLite 数据库，结束后随临时目录一并清理，
不读取也不污染项目内已有业务数据。

覆盖范围（仅针对 save 过程中的明细写入失败，不涉及输入校验或重复
编号——那些路径已由其他用例覆盖）：
  1. 固定样例 Q-KEEP（客户“合成甲”，说明含首尾空格与换行，一条
     数量 1、单价 100 分的保留服务）先保存成功并记录基准快照；
  2. 在测试库中安装仅针对 Q-TXN 第二条明细的 SQLite 触发器，
     令其在该语句写入时以 RAISE(ABORT) 抛出约束错误
     “演示明细拒绝写入”。该故障只拒绝当前语句，不自行撤销此前
     已写入的主记录与第一条明细——回滚必须由 save 自身保证；
  3. 提交合法报价 Q-TXN（客户“合成乙”，说明“演示说明”，明细依次为
     咨询 2×1250 与资料 1×300）：save 必须退出码 1、标准输出为空、
     标准错误只有“错误: 数据库写入失败: 演示明细拒绝写入”一行及
     换行、无异常堆栈；重读数据库，Q-TXN 的主记录与两条明细均不
     存在，Q-KEEP 的全部字段、说明原文、明细标识与金额不变，且
     失败没有占用自增标识；
  4. 解除同一测试库的拒绝条件（删除触发器）后，再次提交完全相同
     的 Q-TXN 输入：退出码 0，标准输出仅为编号加换行，标准错误
     为空；数据库恰好新增一张报价与两条有序明细，行金额 2500、
     300 分，合计为整数 2800 分，客户与说明原样保留，原报价不变。

运行方式（项目根目录）：
    python3 -m unittest discover -s tests
全部通过时退出码为 0，任一断言失败会指出对应阶段与实际结果。
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

# ---- 固定合成样例 ----

KEEP_NUMBER = "Q-KEEP"
KEEP_CUSTOMER = "合成甲"
# 首尾空格与换行都是说明原文的一部分，失败前后必须逐字符保留。
KEEP_NOTE = "  保留说明首行\n第二行  "
KEEP_ITEM = {"description": "保留服务", "quantity": 1, "unit_price": 100}
KEEP_LINE_AMOUNT = 100

TXN_NUMBER = "Q-TXN"
TXN_CUSTOMER = "合成乙"
TXN_NOTE = "演示说明"
TXN_ITEMS = [
    {"description": "咨询", "quantity": 2, "unit_price": 1250},
    {"description": "资料", "quantity": 1, "unit_price": 300},
]
TXN_EXPECTED_LINE_AMOUNTS = [2500, 300]
TXN_EXPECTED_TOTAL = 2800

# 触发器只在 Q-TXN 的第二条明细（资料）写入时拒绝当前语句。
REJECT_TRIGGER = "reject_q_txn_second_item"
REJECT_MESSAGE = "演示明细拒绝写入"
EXPECTED_FAILURE_STDERR = f"错误: 数据库写入失败: {REJECT_MESSAGE}\n"


def keep_payload():
    """每次返回一份全新的 Q-KEEP 报价 JSON 结构。"""
    return {
        "number": KEEP_NUMBER,
        "customer": KEEP_CUSTOMER,
        "note": KEEP_NOTE,
        "items": [dict(KEEP_ITEM)],
    }


def txn_payload():
    """每次返回一份全新的 Q-TXN 报价 JSON 结构（两次提交内容完全相同）。"""
    return {
        "number": TXN_NUMBER,
        "customer": TXN_CUSTOMER,
        "note": TXN_NOTE,
        "items": [dict(item) for item in TXN_ITEMS],
    }


class ItemWriteFailureRollbackTestCase(unittest.TestCase):
    """明细写入失败时，整张报价（主记录与全部明细）必须一起保存或一起撤销。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="quote-item-fail-test-")
        self.addCleanup(self._tmp.cleanup)
        self.workdir = self._tmp.name
        self.db_path = os.path.join(self.workdir, "quotes.sqlite")

    # ---- 基础设施：只通过公开命令行入口驱动 save ----

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
        return self.run_quote("save", "--db", self.db_path, "--input", input_path)

    def install_reject_trigger(self):
        """安装故障：仅拒绝 Q-TXN 第二条明细的本次写入（语句级 ABORT）。

        RAISE(ABORT) 属于 SQLite 约束错误，它只回滚触发错误的这一条
        语句，不会自行撤销同一事务内此前写入的主记录和第一条明细；
        因此 save 必须用自身的事务把整次提交一并撤销。
        """
        conn = sqlite3.connect(self.db_path)
        try:
            # 触发器定义不接受绑定参数；此处仅内插本模块的固定测试常量
            # （编号、触发器名与拒绝信息均为合成字面量，不含外部输入）。
            conn.execute(
                f"""
                CREATE TRIGGER {REJECT_TRIGGER}
                BEFORE INSERT ON items
                WHEN NEW.quote_number = '{TXN_NUMBER}'
                     AND NEW.position = 1
                     AND NEW.description = '资料'
                BEGIN
                    SELECT RAISE(ABORT, '{REJECT_MESSAGE}');
                END
                """
            )
            conn.commit()
        finally:
            conn.close()

    def remove_reject_trigger(self):
        """解除同一测试库上的拒绝条件。"""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(f"DROP TRIGGER {REJECT_TRIGGER}")
            conn.commit()
        finally:
            conn.close()

    def snapshot(self):
        """读取完整快照：quotes 行（含 note）与 items 行（含自增 id）。"""
        conn = sqlite3.connect(self.db_path)
        try:
            quote_rows = conn.execute(
                "SELECT number, customer, total, note FROM quotes ORDER BY number"
            ).fetchall()
            item_rows = conn.execute(
                "SELECT id, quote_number, position, description, quantity, "
                "unit_price, line_amount FROM items ORDER BY id"
            ).fetchall()
            sequence_rows = conn.execute(
                "SELECT name, seq FROM sqlite_sequence ORDER BY name"
            ).fetchall()
        finally:
            conn.close()
        return quote_rows, item_rows, sequence_rows

    def expected_keep_snapshot(self):
        """Q-KEEP 落库后的预期内容（明细自增 id 从 1 开始）。"""
        return (
            [(KEEP_NUMBER, KEEP_CUSTOMER, KEEP_LINE_AMOUNT, KEEP_NOTE)],
            [(1, KEEP_NUMBER, 0, "保留服务", 1, 100, KEEP_LINE_AMOUNT)],
        )

    # ---- 分阶段断言 ----

    def assert_failed_save_result(self, result):
        """阶段[明细写入失败]：退出码 1、标准输出为空、标准错误仅一行、无堆栈。"""
        phase = "明细写入失败"
        self.assertEqual(
            result.returncode, 1,
            msg=f"阶段[{phase}]save 应退出码 1，实际 {result.returncode}；"
                f"stdout={result.stdout!r}，stderr={result.stderr!r}",
        )
        self.assertEqual(
            result.stdout, "",
            msg=f"阶段[{phase}]失败时标准输出必须为空，实际：{result.stdout!r}",
        )
        self.assertEqual(
            result.stderr, EXPECTED_FAILURE_STDERR,
            msg=f"阶段[{phase}]标准错误只能是数据库写入失败提示加换行，"
                f"实际：{result.stderr!r}",
        )
        self.assertNotIn(
            "Traceback", result.stderr,
            msg=f"阶段[{phase}]失败路径不得向标准错误抛出异常堆栈，"
                f"实际：{result.stderr!r}",
        )

    def assert_rollback_left_only_keep(self, keep_snapshot):
        """阶段[失败后回滚核对]：Q-TXN 不存在，Q-KEEP 与失败前完全一致。"""
        phase = "失败后回滚核对"
        quote_rows, item_rows, sequence_rows = self.snapshot()

        self.assertEqual(
            quote_rows, keep_snapshot[0],
            msg=f"阶段[{phase}]quotes 表必须只保留 Q-KEEP 且字段逐字段不变，"
                f"实际：{quote_rows!r}",
        )
        self.assertNotIn(
            TXN_NUMBER, [row[0] for row in quote_rows],
            msg=f"阶段[{phase}]Q-TXN 主记录不得残留，实际：{quote_rows!r}",
        )
        self.assertNotIn(
            TXN_CUSTOMER, [row[1] for row in quote_rows],
            msg=f"阶段[{phase}]Q-TXN 的客户不得以任何形式残留",
        )

        self.assertEqual(
            item_rows, keep_snapshot[1],
            msg=f"阶段[{phase}]items 表必须只保留 Q-KEEP 的原明细，"
                f"Q-TXN 的两条明细（含第一条已写入语句）均须随事务撤销，"
                f"实际：{item_rows!r}",
        )
        self.assertNotIn(
            TXN_NUMBER, [row[1] for row in item_rows],
            msg=f"阶段[{phase}]不得残留任何属于 Q-TXN 的明细，实际：{item_rows!r}",
        )

        # 失败不得占用自增标识：items 序列仍停在 Q-KEEP 的 1。
        self.assertEqual(
            sequence_rows, [("items", 1)],
            msg=f"阶段[{phase}]失败尝试不得占用自增标识或留下序列残行，"
                f"实际 sqlite_sequence：{sequence_rows!r}",
        )

    def assert_retry_saved_exactly(self, result, keep_snapshot):
        """阶段[解除故障后重试]：成功输出编号，且恰好新增一张报价、两条明细。"""
        phase = "解除故障后重试"
        self.assertEqual(
            result.returncode, 0,
            msg=f"阶段[{phase}]解除拒绝后相同输入应保存成功（退出码 0），"
                f"实际 {result.returncode}；stderr={result.stderr!r}",
        )
        self.assertEqual(
            result.stdout, f"{TXN_NUMBER}\n",
            msg=f"阶段[{phase}]保存成功后标准输出只能是编号加换行，"
                f"实际：{result.stdout!r}",
        )
        self.assertEqual(
            result.stderr, "",
            msg=f"阶段[{phase}]保存成功时标准错误必须为空，实际：{result.stderr!r}",
        )

        quote_rows, item_rows, _ = self.snapshot()

        # 恰好新增一张报价：总数为 2，Q-KEEP 原样在前，Q-TXN 字段精确。
        self.assertEqual(
            len(quote_rows), 2,
            msg=f"阶段[{phase}]quotes 表应恰好有两张报价，实际 {len(quote_rows)} 条："
                f"{quote_rows!r}",
        )
        self.assertEqual(
            quote_rows,
            keep_snapshot[0] + [
                (TXN_NUMBER, TXN_CUSTOMER, TXN_EXPECTED_TOTAL, TXN_NOTE)
            ],
            msg=f"阶段[{phase}]Q-KEEP 必须保持不变，Q-TXN 的客户、整数合计与"
                f"说明原文应原样保存，实际：{quote_rows!r}",
        )

        # 恰好新增两条有序明细：Q-KEEP 原明细不变，其后为咨询、资料。
        self.assertEqual(
            len(item_rows), 3,
            msg=f"阶段[{phase}]items 表应恰好有三条明细，实际 {len(item_rows)} 条："
                f"{item_rows!r}",
        )
        self.assertEqual(
            item_rows,
            keep_snapshot[1]
            + [
                (2, TXN_NUMBER, 0, "咨询", 2, 1250, 2500),
                (3, TXN_NUMBER, 1, "资料", 1, 300, 300),
            ],
            msg=f"阶段[{phase}]Q-TXN 两条明细应按输入顺序保存，行金额为 "
                f"2500、300 分且自增标识紧接 Q-KEEP（说明失败未占用编号或残行），"
                f"实际：{item_rows!r}",
        )

        txn_items = [row for row in item_rows if row[1] == TXN_NUMBER]
        self.assertEqual(
            [row[3] for row in txn_items], ["咨询", "资料"],
            msg=f"阶段[{phase}]明细顺序应为咨询在前、资料在后",
        )
        self.assertEqual(
            [row[6] for row in txn_items], TXN_EXPECTED_LINE_AMOUNTS,
            msg=f"阶段[{phase}]行金额应为整数 2500、300 分",
        )
        txn_quote = next(row for row in quote_rows if row[0] == TXN_NUMBER)
        self.assertEqual(
            txn_quote[2], TXN_EXPECTED_TOTAL,
            msg=f"阶段[{phase}]合计应为整数 {TXN_EXPECTED_TOTAL} 分，"
                f"实际：{txn_quote[2]!r}",
        )
        self.assertIsInstance(
            txn_quote[2], int,
            msg=f"阶段[{phase}]合计必须以整数存储，实际类型：{type(txn_quote[2])}",
        )

    # ---- 主流程 ----

    def test_quote_and_items_roll_back_together_when_item_write_fails(self):
        # 期望常量自检：行金额与合计确实由整数乘法得到，防止测试常量写错。
        self.assertEqual(
            [item["quantity"] * item["unit_price"] for item in TXN_ITEMS],
            TXN_EXPECTED_LINE_AMOUNTS,
        )
        self.assertEqual(sum(TXN_EXPECTED_LINE_AMOUNTS), TXN_EXPECTED_TOTAL)
        self.assertEqual(KEEP_ITEM["quantity"] * KEEP_ITEM["unit_price"], KEEP_LINE_AMOUNT)

        # 阶段一：通过公开 save 命令创建当前版本数据库（具备 note 列），
        # Q-KEEP 保存成功并记录失败前基准快照。
        keep_result = self.save(keep_payload(), "keep.json")
        self.assertEqual(keep_result.returncode, 0, msg=keep_result.stderr)
        self.assertEqual(
            keep_result.stdout, f"{KEEP_NUMBER}\n",
            msg="阶段[前置保存]Q-KEEP 成功后标准输出只能是编号加换行",
        )
        self.assertEqual(keep_result.stderr, "", msg="阶段[前置保存]标准错误必须为空")
        keep_snapshot = self.expected_keep_snapshot()
        self.assertEqual(
            self.snapshot()[:2], keep_snapshot,
            msg="阶段[前置保存]Q-KEEP 的字段、说明原文与明细应与固定样例一致",
        )

        # 安装只拒绝 Q-TXN 第二条明细的约束故障。
        self.install_reject_trigger()

        # 阶段二：提交合法的 Q-TXN，第二条明细写入触发约束错误。
        failed_result = self.save(txn_payload(), "txn.json")
        self.assert_failed_save_result(failed_result)

        # 阶段三：主记录与全部明细一起撤销，Q-KEEP 与失败前完全一致。
        self.assert_rollback_left_only_keep(keep_snapshot)

        # 解除同一测试库的拒绝条件，再次提交完全相同的 Q-TXN 输入。
        self.remove_reject_trigger()
        retry_result = self.save(txn_payload(), "txn-retry.json")

        # 阶段四：保存成功，恰好新增一张报价和两条有序明细，原报价不变。
        self.assert_retry_saved_exactly(retry_result, keep_snapshot)


if __name__ == "__main__":
    unittest.main()
