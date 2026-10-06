#!/usr/bin/env python3
"""报价单保存与按编号生成 HTML 预览。

仅使用 Python 3 标准库与本地 SQLite：
  python quote.py save    --db demo.sqlite --input quote.json
  python quote.py preview --db demo.sqlite --number Q-DEMO-001 --output preview.html

金额一律以分为单位的整数存储；预览时换算为元并保留两位小数。
"""

import argparse
import html
import json
import os
import sqlite3
import sys
from urllib.request import pathname2url

INT64_MAX = (1 << 63) - 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS quotes (
    number   TEXT PRIMARY KEY,
    customer TEXT NOT NULL,
    total    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    quote_number TEXT NOT NULL REFERENCES quotes(number),
    position     INTEGER NOT NULL,
    description  TEXT NOT NULL,
    quantity     INTEGER NOT NULL,
    unit_price   INTEGER NOT NULL,
    line_amount  INTEGER NOT NULL
);
"""


class ValidationError(Exception):
    """输入内容不符合保存要求。"""


class DuplicateNumber(Exception):
    """报价编号已存在。"""


def fail(message):
    """向标准错误输出原因，返回非零退出码。"""
    print(f"错误: {message}", file=sys.stderr)
    return 1


def is_plain_int(value):
    """整数且不是布尔值（JSON 的 true/false 不是整数）。"""
    return isinstance(value, int) and not isinstance(value, bool)


def require_nonblank_str(value, label):
    if not isinstance(value, str):
        raise ValidationError(f"{label}必须是字符串")
    if not value.strip():
        raise ValidationError(f"{label}不能只有空白")
    return value


def validate_quote(payload):
    """校验并规范化输入，返回 (number, customer, items, total)。

    items 中每项为 (description, quantity, unit_price, line_amount)。
    合法文本原样保留，不做去除空白处理。
    """
    if not isinstance(payload, dict):
        raise ValidationError("输入根元素必须是 JSON 对象")

    for field in ("number", "customer", "items"):
        if field not in payload:
            raise ValidationError(f"缺少必填字段: {field}")

    number = require_nonblank_str(payload["number"], "编号")
    customer = require_nonblank_str(payload["customer"], "客户")

    raw_items = payload["items"]
    if not isinstance(raw_items, list):
        raise ValidationError("items 必须是数组")
    if len(raw_items) == 0:
        raise ValidationError("明细不能为空")

    items = []
    for index, raw_item in enumerate(raw_items):
        label = f"第 {index + 1} 条明细"
        if not isinstance(raw_item, dict):
            raise ValidationError(f"{label}必须是对象")
        for field in ("description", "quantity", "unit_price"):
            if field not in raw_item:
                raise ValidationError(f"{label}缺少必填字段: {field}")

        description = require_nonblank_str(raw_item["description"], f"{label}的说明")

        quantity = raw_item["quantity"]
        if not is_plain_int(quantity):
            raise ValidationError(f"{label}的数量必须是整数（布尔值不算整数）")
        if quantity <= 0:
            raise ValidationError(f"{label}的数量必须是正整数")

        unit_price = raw_item["unit_price"]
        if not is_plain_int(unit_price):
            raise ValidationError(f"{label}的单价必须是整数（布尔值不算整数）")
        if unit_price < 0:
            raise ValidationError(f"{label}的单价必须为非负整数")

        if quantity > INT64_MAX or unit_price > INT64_MAX:
            raise ValidationError(f"{label}的数量或金额超出有符号64位整数范围")

        line_amount = quantity * unit_price
        if line_amount > INT64_MAX:
            raise ValidationError(f"{label}的行金额超出有符号64位整数范围")

        items.append((description, quantity, unit_price, line_amount))

    total = sum(line_amount for _, _, _, line_amount in items)
    if total > INT64_MAX:
        raise ValidationError("合计金额超出有符号64位整数范围")

    return number, customer, items, total


def open_database(path):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def ensure_schema(conn):
    conn.executescript(SCHEMA)


def cmd_save(args):
    try:
        with open(args.input, encoding="utf-8") as f:
            raw_text = f.read()
    except OSError as exc:
        return fail(f"无法读取输入文件 {args.input}: {exc}")
    except UnicodeDecodeError:
        return fail(f"输入文件不是有效的 UTF-8: {args.input}")

    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        return fail(f"JSON 解析失败: {exc}")

    try:
        number, customer, items, total = validate_quote(payload)
    except ValidationError as exc:
        return fail(str(exc))

    # 校验全部通过后才触碰数据库。
    try:
        conn = open_database(args.db)
    except sqlite3.Error as exc:
        return fail(f"无法打开数据库 {args.db}: {exc}")

    try:
        with conn:
            ensure_schema(conn)
        with conn:
            try:
                conn.execute(
                    "INSERT INTO quotes(number, customer, total) VALUES (?, ?, ?)",
                    (number, customer, total),
                )
            except sqlite3.IntegrityError:
                raise DuplicateNumber(number)
            conn.executemany(
                "INSERT INTO items(quote_number, position, description, "
                "quantity, unit_price, line_amount) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (number, position, description, quantity, unit_price, line_amount)
                    for position, (description, quantity, unit_price, line_amount) in enumerate(items)
                ],
            )
    except DuplicateNumber:
        return fail(f"报价编号已存在: {number}")
    except sqlite3.Error as exc:
        return fail(f"数据库写入失败: {exc}")
    finally:
        conn.close()

    print(number)
    return 0


def format_yuan(cents):
    """非负整数分 -> 元字符串，保留两位小数。"""
    return f"{cents // 100}.{cents % 100:02d}"


def render_html(number, customer, items, total):
    e = html.escape
    rows = "\n".join(
        "      <tr>"
        f"<td>{position + 1}</td>"
        f"<td>{e(description)}</td>"
        f'<td class="num">{quantity}</td>'
        f'<td class="num">{format_yuan(unit_price)}</td>'
        f'<td class="num">{format_yuan(line_amount)}</td>'
        "</tr>"
        for position, (description, quantity, unit_price, line_amount) in enumerate(items)
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>报价单 {e(number)}</title>
<style>
  body {{ font-family: sans-serif; margin: 2em; }}
  table {{ border-collapse: collapse; margin-top: 1em; }}
  th, td {{ border: 1px solid #999; padding: 0.4em 0.8em; }}
  .num {{ text-align: right; white-space: nowrap; }}
  tfoot td {{ font-weight: bold; }}
</style>
</head>
<body>
<h1>报价单</h1>
<dl>
  <dt>报价编号</dt><dd>{e(number)}</dd>
  <dt>客户</dt><dd>{e(customer)}</dd>
</dl>
<table>
  <thead>
    <tr><th>#</th><th>说明</th><th>数量</th><th>单价（元）</th><th>行金额（元）</th></tr>
  </thead>
  <tbody>
{rows}
  </tbody>
  <tfoot>
    <tr><td colspan="4">总计（元）</td><td class="num">{format_yuan(total)}</td></tr>
  </tfoot>
</table>
</body>
</html>
"""


def cmd_preview(args):
    # 输出目标已存在时直接拒绝，绝不覆盖。
    if os.path.exists(args.output):
        return fail(f"输出文件已存在: {args.output}")

    # 以只读方式打开，数据库不可访问或不存在时不创建任何文件。
    db_uri = f"file:{pathname2url(os.path.abspath(args.db))}?mode=ro"
    try:
        conn = sqlite3.connect(db_uri, uri=True)
        conn.execute("PRAGMA foreign_keys = ON")
        cursor = conn.execute(
            "SELECT customer, total FROM quotes WHERE number = ?",
            (args.number,),
        )
        quote_row = cursor.fetchone()
        if quote_row is None:
            conn.close()
            return fail(f"报价编号不存在: {args.number}")
        customer, total = quote_row
        item_rows = conn.execute(
            "SELECT description, quantity, unit_price, line_amount "
            "FROM items WHERE quote_number = ? ORDER BY position",
            (args.number,),
        ).fetchall()
        conn.close()
    except sqlite3.Error as exc:
        return fail(f"无法读取数据库 {args.db}: {exc}")

    document = render_html(args.number, customer, item_rows, total)

    # O_EXCL 兜底：即使并发出现同名文件也不覆盖。
    try:
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            os.write(fd, document.encode("utf-8"))
        finally:
            os.close(fd)
    except OSError as exc:
        return fail(f"无法写入输出文件 {args.output}: {exc}")

    print(args.output)
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description="报价单保存与 HTML 预览")
    subparsers = parser.add_subparsers(dest="command", required=True)

    save_parser = subparsers.add_parser("save", help="保存报价单到 SQLite")
    save_parser.add_argument("--db", required=True, help="SQLite 数据库路径")
    save_parser.add_argument("--input", required=True, help="UTF-8 JSON 报价单路径")
    save_parser.set_defaults(handler=cmd_save)

    preview_parser = subparsers.add_parser("preview", help="按编号生成 HTML 预览")
    preview_parser.add_argument("--db", required=True, help="SQLite 数据库路径")
    preview_parser.add_argument("--number", required=True, help="报价编号（原文精确匹配）")
    preview_parser.add_argument("--output", required=True, help="输出 HTML 路径（目录需已存在）")
    preview_parser.set_defaults(handler=cmd_preview)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
