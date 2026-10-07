"""客户汇总报表（report）筛选、分组、累计、排序与渲染职责拆分的回归测试。

仅依赖 Python 标准库。重构后 report 流程分为可独立验证的三段：
  - quote.fetch_quote_totals(conn)   数据库访问边界（本文件不直接覆盖，
                                     其失败路径由 tests/test_report_flow.py 固定）；
  - quote.summarize_quotes(rows, customer_filter)  纯函数：筛选、分组、
                                     累计、排序，不开数据库、不建文件即可验证；
  - quote.render_report_json(records) / quote.render_report_html(records)
                                     纯函数：JSON 与 HTML 输出边界。
本文件前半部分直接调用这些纯函数（不创建任何数据库或输出文件），
后半部分通过 report 公开命令行入口用固定样例做端到端核对，
确认拆分后公开行为与重构前一致。所有子进程用例在独立临时目录中运行，
不读取项目内业务数据，也不在项目目录留下 JSON、SQLite 或 HTML 文件。

运行方式（项目根目录）：
    python3 -m unittest discover -s tests
全部通过时退出码为 0，任一失败时非零退出并指出对应样例与预期。
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUOTE_PY = os.path.join(PROJECT_ROOT, "quote.py")

sys.path.insert(0, PROJECT_ROOT)
import quote  # noqa: E402

INT64_MAX = (1 << 63) - 1

# ---- 固定验收样例 ----
# 同一客户“演示<甲>&乙”两张报价：2800 分（2×1250 + 1×300）与零金额，
# 预期张数 2、累计 2800 分，HTML 显示 28.00 元；
# 同一客户“大额客户”两张各 9223372036854775807 分（INT64_MAX），
# 预期累计精确为 18446744073709551614 分。
CUSTOMER_DEMO = "演示<甲>&乙"
CUSTOMER_BIG = "大额客户"

SAVE_SAMPLES = [
    {
        "number": "Q-S1",
        "customer": CUSTOMER_DEMO,
        "items": [
            {"description": "咨询", "quantity": 2, "unit_price": 1250},
            {"description": "资料", "quantity": 1, "unit_price": 300},
        ],
    },
    {
        "number": "Q-S2",
        "customer": CUSTOMER_DEMO,
        "items": [{"description": "赠品", "quantity": 1, "unit_price": 0}],
    },
    {
        "number": "Q-BIG-1",
        "customer": CUSTOMER_BIG,
        "items": [
            {"description": "上限单价", "quantity": 1, "unit_price": INT64_MAX}
        ],
    },
    {
        "number": "Q-BIG-2",
        "customer": CUSTOMER_BIG,
        "items": [
            {"description": "上限单价", "quantity": 1, "unit_price": INT64_MAX}
        ],
    },
]

# 固定样例对应的 (customer, total) 行，顺序故意打乱，证明排序与读入行序无关。
SAMPLE_ROWS = [
    (CUSTOMER_DEMO, 2800),
    (CUSTOMER_BIG, INT64_MAX),
    (CUSTOMER_DEMO, 0),
    (CUSTOMER_BIG, INT64_MAX),
]

# 纯函数 summarize_quotes 对固定样例的确定结果：
# 按客户原文 UTF-8 字节（BINARY）升序，“大”(E5) 在 “演”(E6) 之前。
EXPECTED_RECORDS = [
    (CUSTOMER_BIG, 2, 2 * INT64_MAX),
    (CUSTOMER_DEMO, 2, 2800),
]

# 同一批记录的确定 JSON 输出：单行、分单位整数、客户原文不转义、末尾一个换行。
EXPECTED_JSON = (
    '[{"customer": "大额客户", "quote_count": 2, "total": 18446744073709551614}, '
    '{"customer": "演示<甲>&乙", "quote_count": 2, "total": 2800}]\n'
)


class SummarizeQuotesTestCase(unittest.TestCase):
    """summarize_quotes 纯函数：不开数据库、不建文件，直接验证汇总规则。"""

    def test_fixed_sample_counts_and_subtotals(self):
        records = quote.summarize_quotes(SAMPLE_ROWS)
        self.assertEqual(records, EXPECTED_RECORDS)
        # 同一客户的 2800 分与零金额两张报价：张数 2、累计 2800。
        self.assertIn((CUSTOMER_DEMO, 2, 2800), records)

    def test_zero_amount_quote_still_counts(self):
        records = quote.summarize_quotes([("甲", 0), ("甲", 5)])
        self.assertEqual(records, [("甲", 2, 5)])

    def test_sum_beyond_int64_is_exact(self):
        records = quote.summarize_quotes([(CUSTOMER_BIG, INT64_MAX)] * 2)
        self.assertEqual(
            records, [(CUSTOMER_BIG, 2, 18446744073709551614)],
            msg="两张 INT64_MAX 分报价的累计必须精确为 18446744073709551614",
        )

    def test_customer_text_differences_form_separate_groups(self):
        # 大小写、首尾空格与换行都参与分组：以下客户互不相同。
        customers = ["Demo", "demo", " Demo ", "Demo\n", "100%_特价"]
        rows = [(customer, 1) for customer in customers]
        records = quote.summarize_quotes(rows)
        # 每个客户各成一组（张数均为 1），按 UTF-8 字节升序：
        # 空格(0x20) < 数字(0x31) < 大写 D(0x44) < 小写 d(0x64)。
        self.assertEqual(
            [customer for customer, _, _ in records],
            [" Demo ", "100%_特价", "Demo", "Demo\n", "demo"],
        )
        self.assertTrue(all(count == 1 for _, count, _ in records))

    def test_filter_is_exact_match(self):
        rows = [("Demo", 100), ("demo", 200), (" Demo ", 300)]
        self.assertEqual(
            quote.summarize_quotes(rows, "Demo"), [("Demo", 1, 100)],
            msg="筛选值逐字符精确匹配，大小写与空格不同的客户不命中",
        )

    def test_filter_treats_percent_and_underscore_as_plain_chars(self):
        rows = [("100%_特价", 100), ("100_特价", 200), ("100X特价", 300)]
        self.assertEqual(
            quote.summarize_quotes(rows, "100%_特价"),
            [("100%_特价", 1, 100)],
            msg="% 与 _ 按普通字符处理，不作 LIKE 通配",
        )
        self.assertEqual(quote.summarize_quotes(rows, "100_特价"), [("100_特价", 1, 200)])

    def test_filter_without_match_returns_empty(self):
        self.assertEqual(quote.summarize_quotes(SAMPLE_ROWS, "无此客户"), [])

    def test_empty_rows_return_empty(self):
        self.assertEqual(quote.summarize_quotes([]), [])
        self.assertEqual(quote.summarize_quotes([], "任意客户"), [])


class RenderReportJsonTestCase(unittest.TestCase):
    """render_report_json 纯函数：单行 JSON、分单位整数、末尾一个换行。"""

    def test_fixed_sample_json_text(self):
        document = quote.render_report_json(EXPECTED_RECORDS)
        self.assertEqual(document, EXPECTED_JSON)
        self.assertEqual(document.count("\n"), 1, msg="输出必须是单行 JSON")
        # 往返解析后字段白名单与整数类型不变。
        for record in json.loads(document):
            self.assertEqual(set(record), {"customer", "quote_count", "total"})
            self.assertIsInstance(record["quote_count"], int)
            self.assertIsInstance(record["total"], int)

    def test_empty_records_render_empty_array(self):
        self.assertEqual(quote.render_report_json([]), "[]\n")


class RenderReportHtmlTestCase(unittest.TestCase):
    """render_report_html 纯函数：客户行、合计行与空态提示。"""

    def test_fixed_sample_shows_yuan_and_total_row(self):
        document = quote.render_report_html(EXPECTED_RECORDS)
        # 客户原文转义后入格，2800 分显示为 28.00 元。
        self.assertIn(
            "<tr><td>演示&lt;甲&gt;&amp;乙</td>"
            '<td class="num">2</td><td class="num">28.00</td></tr>',
            document,
        )
        self.assertIn("184467440737095516.14", document)
        # 合计行仅覆盖本次结果：4 张、18446744073709551614 + 2800 分。
        self.assertIn(
            '<tr><td>合计</td><td class="num">4</td>'
            '<td class="num">184467440737095544.14</td></tr>',
            document,
        )

    def test_empty_records_keep_header_notice_and_zero_total(self):
        document = quote.render_report_html([])
        self.assertIn("<tr><th>客户</th><th>报价张数</th><th>累计金额（元）</th></tr>", document)
        self.assertIn('<p class="empty">没有匹配的报价</p>', document)
        self.assertIn(
            '<tr><td>合计</td><td class="num">0</td>'
            '<td class="num">0.00</td></tr>',
            document,
        )


class ReportPublicEntryTestCase(unittest.TestCase):
    """report 公开命令行入口：固定样例端到端核对拆分后的既有行为。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="quote-report-core-test-")
        self.addCleanup(self._tmp.cleanup)
        self.workdir = self._tmp.name
        self.db_path = os.path.join(self.workdir, "quotes.sqlite")
        for index, payload in enumerate(SAVE_SAMPLES):
            input_path = os.path.join(self.workdir, f"input-{index}.json")
            with open(input_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            result = self.run_quote("save", "--db", self.db_path, "--input", input_path)
            self.assertEqual(result.returncode, 0, msg=f"样例保存应成功: {result.stderr}")

    def run_quote(self, *cli_args):
        return subprocess.run(
            [sys.executable, QUOTE_PY, *cli_args],
            capture_output=True,
            text=True,
            cwd=self.workdir,
        )

    def test_report_json_matches_pure_function_result(self):
        result = self.run_quote("report", "--db", self.db_path)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "")
        # 公开入口输出与纯函数对同样数据的确定结果逐字节一致。
        self.assertEqual(result.stdout, EXPECTED_JSON)
        self.assertEqual(result.stdout, quote.render_report_json(EXPECTED_RECORDS))

    def test_report_filtered_to_single_customer(self):
        result = self.run_quote(
            "report", "--db", self.db_path, "--customer", CUSTOMER_DEMO
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(
            result.stdout,
            '[{"customer": "演示<甲>&乙", "quote_count": 2, "total": 2800}]\n',
            msg="精确筛选后只剩该客户一组：2 张、累计 2800 分",
        )

    def test_report_filter_without_match_outputs_empty_array(self):
        result = self.run_quote(
            "report", "--db", self.db_path, "--customer", "无此客户"
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stdout, "[]\n")

    def test_report_html_output(self):
        out_path = os.path.join(self.workdir, "report.html")
        result = self.run_quote(
            "report", "--db", self.db_path, "--output", out_path
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout, out_path + "\n", msg="标准输出只回显传入路径")
        with open(out_path, encoding="utf-8") as f:
            document = f.read()
        self.assertIn("28.00", document, msg="2800 分应显示为 28.00 元")
        self.assertIn("184467440737095516.14", document)
        self.assertIn(
            '<tr><td>合计</td><td class="num">4</td>'
            '<td class="num">184467440737095544.14</td></tr>',
            document,
        )


# 项目目录清洁性：测试开始前记录项目根内容，全部用例结束后不得新增任何文件
# （所有数据库、输入 JSON 与 HTML 都必须落在临时目录中）。
_PROJECT_ROOT_ENTRIES_AT_START = set(os.listdir(PROJECT_ROOT))


def tearDownModule():
    leftovers = set(os.listdir(PROJECT_ROOT)) - _PROJECT_ROOT_ENTRIES_AT_START
    assert not leftovers, f"测试在项目根目录留下了文件: {sorted(leftovers)}"


if __name__ == "__main__":
    unittest.main()
