"""导出文本生成规则（render_quote_json）的独立验证，并与公开 export 命令对照。

本文件分两部分：

1. 规则独立验证（ExportRulesIndependentTestCase）：直接调用
   render_quote_json，入参全部是内存中“已读取的合法报价”——报价编号、
   客户、说明与按顺序排列的 (description, quantity, unit_price) 明细，
   不访问数据库、不创建文件；该纯函数本身也不写标准输出。

2. 公开命令对照（ExportRulesCommandTestCase）：把同一组固定样例经公开
   save 命令落库后运行 export（省略或指定 --output），断言子进程输出
   （标准输出 / 文件字节、退出码、标准错误）与“独立规则对同一报价的
   渲染结果”逐字节一致，即公开 export 入口与独立验证使用的是同一套规则。

固定主样例 Q-RULE-001：两条同名明细“服务”，数量 2 与 1，单价 1250 与 0；
客户含真实换行、尖括号、与号和引号；说明首尾带空格并含换行与 <甲>&乙。
另用有符号 64 位上限单价 9223372036854775807 核对精度，用 note 缺失、
NULL、空字符串与纯空白四种形态核对 note 的省略规则。

运行方式（项目根目录）：
    python3 -m unittest discover -s tests
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 导入产品模块做独立验证时不得在项目根留下 __pycache__（既有测试有目录
# 清洁断言）；与子进程方式一致，验证本身不创建任何文件。
sys.dont_write_bytecode = True
sys.path.insert(0, PROJECT_ROOT)
QUOTE_PY = os.path.join(PROJECT_ROOT, "quote.py")

import quote

INT64_MAX = (1 << 63) - 1

# ---- 固定主样例：内存中一张已读取的合法报价（items 为有序三元组）----

NUMBER = "Q-RULE-001"
CUSTOMER = '演示客户\n<甲>&"乙"'
NOTE = " 说明<甲>&乙\n第二行 "
ITEMS = [
    ("服务", 2, 1250),
    ("服务", 1, 0),
]

# 同一组数据的 save 合法输入：行金额与合计由保存侧计算，导出不读取它们。
PAYLOAD = {
    "number": NUMBER,
    "customer": CUSTOMER,
    "note": NOTE,
    "items": [
        {"description": "服务", "quantity": 2, "unit_price": 1250},
        {"description": "服务", "quantity": 1, "unit_price": 0},
    ],
}

# ---- 固定预期导出文档：由样例数据按公开行为逐字符推出并写死 ----
# json.dumps 默认分隔符为 ", " 与 ": "，ensure_ascii=False 保留原文，
# 键顺序为 number、customer、items、note；客户引号写成 \"，客户与说明
# 中的换行写成反斜杠加 n 两个字符，整篇为一个物理行，末尾恰好一个 LF。
EXPECTED_DOCUMENT = (
    '{"number": "Q-RULE-001", "customer": "演示客户\\n<甲>&\\"乙\\"", '
    '"items": [{"description": "服务", "quantity": 2, "unit_price": 1250}, '
    '{"description": "服务", "quantity": 1, "unit_price": 0}], '
    '"note": " 说明<甲>&乙\\n第二行 "}\n'
)
EXPECTED_BYTES = EXPECTED_DOCUMENT.encode("utf-8")


class ExportRulesIndependentTestCase(unittest.TestCase):
    """对内存报价直接套用转换规则：不访问数据库、不创建文件、不写标准输出。"""

    def test_independent_call_returns_fixed_single_line_document(self):
        text = quote.render_quote_json(NUMBER, CUSTOMER, NOTE, ITEMS)

        self.assertIsInstance(text, str)
        # 逐字节（逐字符）等于写死的固定预期，而不是用实现现算答案。
        self.assertEqual(text, EXPECTED_DOCUMENT)

        # 物理形态：一个物理行，末尾恰好一个 LF，无 CR。
        self.assertTrue(text.endswith("\n"), msg="必须以换行结束")
        self.assertEqual(text.count("\n"), 1, msg="只能有末尾一个换行")
        self.assertNotIn("\r", text, msg="不得使用 CRLF 换行")

        document = text[:-1]
        parsed = json.loads(text)

        # 结构与 save 接受的输入一致，且带说明时 note 位于最后。
        self.assertEqual(
            set(parsed.keys()), {"number", "customer", "items", "note"},
            msg="只能包含 save 接受的字段，且带说明时包含 note",
        )
        self.assertEqual(parsed["number"], NUMBER)
        self.assertEqual(
            parsed["customer"], CUSTOMER,
            msg="客户解析后必须与入参原文逐字符一致（含换行、尖括号、与号、引号）",
        )
        self.assertEqual(
            parsed["note"], NOTE,
            msg="说明解析后必须逐字符保留首尾空格、换行与尖括号与号",
        )

        # 明细：两条同名说明保持独立、顺序不变；数量 2/1、单价 1250/0。
        self.assertEqual(len(parsed["items"]), 2)
        for item in parsed["items"]:
            self.assertEqual(
                set(item.keys()), {"description", "quantity", "unit_price"},
                msg="明细不得包含内部标识、行金额或其他字段",
            )
        self.assertEqual(
            [item["description"] for item in parsed["items"]],
            ["服务", "服务"],
            msg="重复说明必须保持为独立的两行",
        )
        self.assertEqual(
            [(item["quantity"], item["unit_price"]) for item in parsed["items"]],
            [(2, 1250), (1, 0)],
            msg="明细顺序、零单价必须逐值保留",
        )
        for item in parsed["items"]:
            self.assertIsInstance(item["quantity"], int)
            self.assertNotIsInstance(item["quantity"], bool)
            self.assertIsInstance(item["unit_price"], int)
            self.assertNotIsInstance(item["unit_price"], bool)

        # 不夹带内部标识、行金额或合计：解析后的对象无此键，原文也无此键名。
        for forbidden in ("id", "position", "quote_number", "line_amount", "total"):
            self.assertNotIn(forbidden, parsed)
            for item in parsed["items"]:
                self.assertNotIn(forbidden, item)
            self.assertNotIn(
                f'"{forbidden}"', document,
                msg=f"导出原文不得出现字段 {forbidden!r}",
            )

    def test_text_is_json_escaped_only_without_html_entities_or_u_escapes(self):
        text = quote.render_quote_json(NUMBER, CUSTOMER, NOTE, ITEMS)

        # 尖括号、与号与中文以原字符出现；引号与换行只做 JSON 转义。
        self.assertIn("<甲>", text)
        self.assertIn("&乙", text)
        self.assertIn('\\"乙\\"', text, msg="客户中的引号必须按 JSON 转义为 \\\"")
        self.assertIn("演示客户\\n<甲>", text, msg="客户中的换行必须转义为 \\n")
        self.assertIn("说明<甲>&乙\\n第二行", text)
        self.assertIn("服务", text)
        for entity in ("&lt;", "&gt;", "&amp;", "&quot;", "&#39;", "&#60;", "&#62;"):
            self.assertNotIn(entity, text, msg=f"不得做 HTML 转义: {entity}")
        self.assertNotIn("\\u003c", text)
        self.assertNotIn("\\u0026", text)
        self.assertNotIn("\\u670d", text, msg="中文不得以 \\uXXXX 形式输出")

    def test_note_missing_null_and_empty_omitted_but_whitespace_kept(self):
        # 入参层面的四种形态：缺失语义用 None 表示（读取侧对旧库缺列与
        # SQL NULL 都给 None），空字符串显式给出，纯空白仍是非空说明。
        omitted_cases = [
            ("Q-NONE", None),
            ("Q-EMPTY", ""),
        ]
        for number, note in omitted_cases:
            with self.subTest(number=number, note=repr(note)):
                text = quote.render_quote_json(number, "客户", note, ITEMS)
                parsed = json.loads(text)
                self.assertNotIn("note", parsed, msg=f"{note!r} 必须省略 note")
                self.assertEqual(
                    set(parsed.keys()), {"number", "customer", "items"},
                )
                self.assertNotIn('"note"', text)

        whitespace = "  \n\t "
        text = quote.render_quote_json("Q-WS", "客户", whitespace, ITEMS)
        parsed = json.loads(text)
        self.assertEqual(parsed["note"], whitespace, msg="纯空白说明必须原样保留")
        self.assertIn('"note": "  \\n\\t "', text)

        # 键顺序：省略 note 时 items 是最后一个键。
        self.assertTrue(
            text.rstrip("\n").endswith('}') and '"items"' in text,
        )

    def test_int64_max_unit_price_keeps_full_precision_as_json_integer(self):
        items = [("大额服务", 1, INT64_MAX)]
        text = quote.render_quote_json("Q-BIG", "客户", None, items)
        self.assertEqual(text.count("\n"), 1)
        parsed = json.loads(text)
        self.assertNotIn("note", parsed)
        self.assertEqual(
            parsed["items"],
            [{"description": "大额服务", "quantity": 1, "unit_price": INT64_MAX}],
        )
        unit_price = parsed["items"][0]["unit_price"]
        self.assertIsInstance(unit_price, int)
        self.assertNotIsInstance(unit_price, bool)
        # 原文必须写出完整 19 位数字，不带小数点、指数或引号。
        self.assertIn(f'"unit_price": {INT64_MAX}', text)
        self.assertNotIn(str(INT64_MAX) + ".0", text)
        self.assertNotIn("e+", text)

    def test_conversion_is_pure_and_writes_nothing_beside_return_value(self):
        workdir = tempfile.mkdtemp(prefix="quote-export-rules-pure-")
        before = set(os.listdir(workdir))
        cwd_before = os.getcwd()
        try:
            os.chdir(workdir)
            result = quote.render_quote_json(NUMBER, CUSTOMER, NOTE, ITEMS)
        finally:
            os.chdir(cwd_before)
        self.assertEqual(result, EXPECTED_DOCUMENT)
        self.assertEqual(
            set(os.listdir(workdir)), before,
            msg="纯函数不得在当前目录创建任何文件",
        )
        os.rmdir(workdir)


class ExportRulesCommandTestCase(unittest.TestCase):
    """同一组固定数据：公开 export 命令输出必须与独立转换结果逐字节一致。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="quote-export-rules-")
        self.addCleanup(self._tmp.cleanup)
        self.workdir = self._tmp.name
        self.db_path = self.save(PAYLOAD)

    def run_quote(self, *cli_args):
        return subprocess.run(
            [sys.executable, QUOTE_PY, *cli_args],
            capture_output=True,
            cwd=self.workdir,
        )

    def write_input(self, payload, name):
        path = os.path.join(self.workdir, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        return path

    def save(self, payload, db_name="quotes.sqlite", input_name="quote.json"):
        db_path = os.path.join(self.workdir, db_name)
        input_path = self.write_input(payload, input_name)
        result = subprocess.run(
            [sys.executable, QUOTE_PY, "save", "--db", db_path, "--input", input_path],
            capture_output=True,
            text=True,
            cwd=self.workdir,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "")
        return db_path

    def test_stdout_export_matches_independent_call_byte_for_byte(self):
        result = self.run_quote("export", "--db", self.db_path, "--number", NUMBER)
        self.assertEqual(result.returncode, 0, msg=result.stderr.decode())
        self.assertEqual(result.stderr, b"", msg="导出成功时标准错误必须为空")

        expected = quote.render_quote_json(NUMBER, CUSTOMER, NOTE, ITEMS)
        # 独立调用文本、写死的固定预期、命令行标准输出三者逐字节一致。
        self.assertEqual(expected.encode("utf-8"), EXPECTED_BYTES)
        self.assertEqual(
            result.stdout, EXPECTED_BYTES,
            msg="命令行导出必须与独立转换规则同源、逐字节一致",
        )

    def test_file_export_matches_independent_call_and_stdout_shape(self):
        output_name = "exported.json"
        output_path = os.path.join(self.workdir, output_name)
        result = self.run_quote(
            "export", "--db", self.db_path, "--number", NUMBER,
            "--output", output_name,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr.decode())
        self.assertEqual(result.stderr, b"")
        self.assertEqual(
            result.stdout, f"{output_name}\n".encode("utf-8"),
            msg="成功保存时标准输出仅为传入的输出路径加一个换行",
        )

        with open(output_path, "rb") as f:
            file_bytes = f.read()
        self.assertFalse(file_bytes.startswith(b"\xef\xbb\xbf"), msg="不得带 BOM")
        self.assertEqual(
            file_bytes,
            quote.render_quote_json(NUMBER, CUSTOMER, NOTE, ITEMS).encode("utf-8"),
            msg="文件内容必须与独立转换结果逐字节一致",
        )
        self.assertEqual(file_bytes, EXPECTED_BYTES)

    def test_note_omission_variants_in_database_match_independent_calls(self):
        items = [("明细", 1, 0)]

        # 无 note 字段保存：读取侧得到 None；纯空白说明原样保存。
        self.save(
            {"number": "Q-NONE", "customer": "客户",
             "items": [{"description": "明细", "quantity": 1, "unit_price": 0}]},
            input_name="none.json",
        )
        self.save(
            {"number": "Q-WS", "customer": "客户", "note": "  \n\t ",
             "items": [{"description": "明细", "quantity": 1, "unit_price": 0}]},
            input_name="ws.json",
        )
        # save 会把空字符串说明规范化为 NULL，因此用直连方式构造一条
        # note 为空字符串的旧形态报价。
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO quotes(number, customer, total, note) "
                "VALUES ('Q-EMPTY', '客户', 0, '')"
            )
            conn.execute(
                "INSERT INTO items(quote_number, position, description, "
                "quantity, unit_price, line_amount) VALUES "
                "('Q-EMPTY', 0, '明细', 1, 0, 0)"
            )
            conn.commit()
        finally:
            conn.close()

        cases = [
            ("Q-NONE", None),
            ("Q-EMPTY", ""),
            ("Q-WS", "  \n\t "),
        ]
        for number, note in cases:
            with self.subTest(number=number):
                result = self.run_quote("export", "--db", self.db_path, "--number", number)
                self.assertEqual(result.returncode, 0, msg=result.stderr.decode())
                self.assertEqual(
                    result.stdout.decode("utf-8"),
                    quote.render_quote_json(number, "客户", note, items),
                    msg=f"note 形态 {note!r} 的命令行导出必须与独立调用一致",
                )

    def test_int64_max_export_matches_independent_call(self):
        big_items = [("大额服务", 1, INT64_MAX)]
        self.save(
            {"number": "Q-BIG", "customer": "客户",
             "items": [{"description": "大额服务", "quantity": 1,
                        "unit_price": INT64_MAX}]},
            input_name="big.json",
        )
        result = self.run_quote("export", "--db", self.db_path, "--number", "Q-BIG")
        self.assertEqual(result.returncode, 0, msg=result.stderr.decode())
        self.assertEqual(
            result.stdout.decode("utf-8"),
            quote.render_quote_json("Q-BIG", "客户", None, big_items),
        )
        self.assertIn(str(INT64_MAX), result.stdout.decode("utf-8"))


# 项目目录清洁性：测试开始前记录项目根内容，全部用例结束后不得新增任何文件。
_PROJECT_ROOT_ENTRIES_AT_START = set(os.listdir(PROJECT_ROOT))


def tearDownModule():
    leftovers = set(os.listdir(PROJECT_ROOT)) - _PROJECT_ROOT_ENTRIES_AT_START
    assert not leftovers, f"测试在项目根目录留下了文件: {sorted(leftovers)}"


if __name__ == "__main__":
    unittest.main()
