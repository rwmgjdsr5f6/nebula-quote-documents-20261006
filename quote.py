#!/usr/bin/env python3
"""报价单保存与 HTML 预览。

用法:
    python quote.py save    --db demo.sqlite --input quote.json
    python quote.py preview --db demo.sqlite --number Q-DEMO-001 --output preview.html

仅使用 Python 3 标准库与本地 SQLite。金额以分为单位的整数存储与计算。
"""

import argparse
import html
import json
import os
import sqlite3
import sys

SQLITE_INT64_MAX = 2 ** 63 - 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS quotes (
    number TEXT PRIMARY KEY,
    customer TEXT NOT NULL,
    total INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    quote_number TEXT NOT NULL REFERENCES quotes(number),
    position INTEGER NOT NULL,
    description TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    unit_price INTEGER NOT NULL,
    line_amount INTEGER NOT NULL
);
"""


class QuoteError(Exception):
    """业务错误：信息输出到标准错误并以非零状态退出。"""


def fail(message):
    print(f"错误: {message}", file=sys.stderr)
    sys.exit(1)


def is_valid_int(value):
    """布尔值不算整数。"""
    return isinstance(value, int) and not isinstance(value, bool)


def require_text(value, field):
    if not isinstance(value, str):
        raise QuoteError(f"字段 {field} 必须是字符串")
    if not value.strip():
        raise QuoteError(f"字段 {field} 不能只包含空白")
    return value


def parse_quote(data):
    """校验输入结构，返回 (number, customer, items, total)，items 为字典列表。"""
    if not isinstance(data, dict):
        raise QuoteError("输入 JSON 顶层必须是对象")

    for field in ("number", "customer", "items"):
        if field not in data:
            raise QuoteError(f"缺少必填字段 {field}")

    number = require_text(data["number"], "number")
    customer = require_text(data["customer"], "customer")

    items_raw = data["items"]
    if not isinstance(items_raw, list):
        raise QuoteError("字段 items 必须是数组")
    if not items_raw:
        raise QuoteError("明细 items 不能为空")

    items = []
    total = 0
    for index, entry in enumerate(items_raw, start=1):
        if not isinstance(entry, dict):
            raise QuoteError(f"第 {index} 条明细必须是对象")
        for field in ("description", "quantity", "unit_price"):
            if field not in entry:
                raise QuoteError(f"第 {index} 条明细缺少字段 {field}")

        description = require_text(entry["description"], f"第 {index} 条明细 description")

        quantity = entry["quantity"]
        if not is_valid_int(quantity) or quantity <= 0:
            raise QuoteError(f"第 {index} 条明细 quantity 必须是正整数")
        if quantity > SQLITE_INT64_MAX:
            raise QuoteError(f"第 {index} 条明细 quantity 超出 SQLite 64 位整数范围")

        unit_price = entry["unit_price"]
        if not is_valid_int(unit_price) or unit_price < 0:
            raise QuoteError(f"第 {index} 条明细 unit_price 必须是非负整数")
        if unit_price > SQLITE_INT64_MAX:
            raise QuoteError(f"第 {index} 条明细 unit_price 超出 SQLite 64 位整数范围")

        line_amount = quantity * unit_price
        if line_amount > SQLITE_INT64_MAX:
            raise QuoteError(f"第 {index} 条明细行金额超出 SQLite 64 位整数范围")

        total += line_amount
        if total > SQLITE_INT64_MAX:
            raise QuoteError("合计金额超出 SQLite 64 位整数范围")

        items.append({
            "description": description,
            "quantity": quantity,
            "unit_price": unit_price,
            "line_amount": line_amount,
        })

    return number, customer, items, total


def open_db(db_path):
    try:
        conn = sqlite3.connect(db_path)
    except sqlite3.Error as exc:
        raise QuoteError(f"无法打开数据库 {db_path}: {exc}")
    try:
        conn.executescript(SCHEMA)
    except sqlite3.Error as exc:
        conn.close()
        raise QuoteError(f"无法初始化数据库 {db_path}: {exc}")
    return conn


def cmd_save(args):
    try:
        with open(args.input, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        fail(f"输入文件不存在: {args.input}")
    except (OSError, UnicodeDecodeError) as exc:
        fail(f"无法读取输入文件 {args.input}: {exc}")
    except json.JSONDecodeError as exc:
        fail(f"输入文件不是合法 JSON: {exc}")

    try:
        number, customer, items, total = parse_quote(data)
    except QuoteError as exc:
        fail(str(exc))

    try:
        conn = open_db(args.db)
    except QuoteError as exc:
        fail(str(exc))

    try:
        with conn:  # 单事务：任何失败整体回滚，不留部分数据
            conn.execute(
                "INSERT INTO quotes (number, customer, total) VALUES (?, ?, ?)",
                (number, customer, total),
            )
            conn.executemany(
                "INSERT INTO items (quote_number, position, description, quantity, unit_price, line_amount)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (number, pos, it["description"], it["quantity"], it["unit_price"], it["line_amount"])
                    for pos, it in enumerate(items)
                ],
            )
    except sqlite3.IntegrityError:
        fail(f"报价编号已存在: {number}")
    except sqlite3.Error as exc:
        fail(f"写入数据库失败: {exc}")
    finally:
        conn.close()

    print(number)


def format_yuan(cents):
    """分 -> 元，保留两位小数。"""
    yuan, rem = divmod(cents, 100)
    return f"{yuan}.{rem:02d}"


def render_html(number, customer, items, total):
    esc = html.escape
    rows = []
    for it in items:
        rows.append(
            "      <tr>"
            f"<td>{esc(it['description'])}</td>"
            f"<td>{it['quantity']}</td>"
            f"<td>{format_yuan(it['unit_price'])}</td>"
            f"<td>{format_yuan(it['line_amount'])}</td>"
            "</tr>"
        )
    rows_html = "\n".join(rows)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>报价单 {esc(number)}</title>
</head>
<body>
  <h1>报价单</h1>
  <p>报价编号: {esc(number)}</p>
  <p>客户: {esc(customer)}</p>
  <table border="1">
    <thead>
      <tr><th>说明</th><th>数量</th><th>单价(元)</th><th>行金额(元)</th></tr>
    </thead>
    <tbody>
{rows_html}
    </tbody>
  </table>
  <p>总计: {format_yuan(total)} 元</p>
</body>
</html>
"""


def cmd_preview(args):
    try:
        conn = open_db(args.db)
    except QuoteError as exc:
        fail(str(exc))

    try:
        row = conn.execute(
            "SELECT customer, total FROM quotes WHERE number = ?", (args.number,)
        ).fetchone()
        if row is None:
            fail(f"报价编号不存在: {args.number}")
        customer, total = row
        items = [
            {"description": d, "quantity": q, "unit_price": p, "line_amount": a}
            for d, q, p, a in conn.execute(
                "SELECT description, quantity, unit_price, line_amount"
                " FROM items WHERE quote_number = ? ORDER BY position",
                (args.number,),
            )
        ]
    except sqlite3.Error as exc:
        conn.close()
        fail(f"读取数据库失败: {exc}")
    conn.close()

    content = render_html(args.number, customer, items, total)

    if os.path.exists(args.output):
        fail(f"输出文件已存在: {args.output}")
    try:
        # 'x' 模式：目标已存在时失败，不覆盖
        with open(args.output, "x", encoding="utf-8") as fh:
            fh.write(content)
    except FileExistsError:
        fail(f"输出文件已存在: {args.output}")
    except OSError as exc:
        fail(f"无法写入输出文件 {args.output}: {exc}")

    print(args.output)


def main(argv=None):
    parser = argparse.ArgumentParser(description="报价单保存与 HTML 预览")
    sub = parser.add_subparsers(dest="command", required=True)

    p_save = sub.add_parser("save", help="保存报价单")
    p_save.add_argument("--db", required=True, help="SQLite 数据库路径")
    p_save.add_argument("--input", required=True, help="UTF-8 JSON 输入文件")
    p_save.set_defaults(func=cmd_save)

    p_preview = sub.add_parser("preview", help="按编号生成 HTML 预览")
    p_preview.add_argument("--db", required=True, help="SQLite 数据库路径")
    p_preview.add_argument("--number", required=True, help="报价编号")
    p_preview.add_argument("--output", required=True, help="HTML 输出路径")
    p_preview.set_defaults(func=cmd_preview)

    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
