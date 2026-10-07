#!/usr/bin/env python3
"""报价单保存、按编号生成 HTML 预览、摘要列表、按编号导出 JSON 与按客户汇总报表。

仅使用 Python 3 标准库与本地 SQLite：
  python quote.py save    --db demo.sqlite --input quote.json
  python quote.py preview --db demo.sqlite --number Q-DEMO-001 --output preview.html
  python quote.py list    --db demo.sqlite
  python quote.py export  --db demo.sqlite --number Q-DEMO-001
  python quote.py report  --db demo.sqlite [--customer 客户名] [--output report.html]

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
    total    INTEGER NOT NULL,
    note     TEXT
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
    """校验并规范化输入，返回 (number, customer, note, items, total)。

    note 为 None 表示无说明；非空说明原样保留，不做去除空白处理。
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

    # note 可选：省略或空字符串表示无说明；一旦出现（含 null）就必须是字符串，
    # 非空内容原样保留。
    note = None
    if "note" in payload:
        note = payload["note"]
        if not isinstance(note, str):
            raise ValidationError("note 必须是字符串")
        if note == "":
            note = None

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

    return number, customer, note, items, total


def open_database(path):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def ensure_schema(conn):
    conn.executescript(SCHEMA)
    # 旧库没有 note 列：就地补列，已有报价的 note 为 NULL（无说明），无需重建。
    columns = {row[1] for row in conn.execute("PRAGMA table_info(quotes)")}
    if "note" not in columns:
        conn.execute("ALTER TABLE quotes ADD COLUMN note TEXT")


def cmd_save(args):
    try:
        with open(args.input, encoding="utf-8") as f:
            raw_text = f.read()
    except OSError as exc:
        return fail(f"无法读取输入文件 {args.input}: {exc}")
    except UnicodeDecodeError:
        # 严格按 UTF-8 解码：任何非法字节都整份拒绝，不忽略、不替换。
        return fail(f"输入文件不是有效的 UTF-8: {args.input}")

    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        return fail(f"JSON 解析失败: {exc}")

    try:
        number, customer, note, items, total = validate_quote(payload)
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
                    "INSERT INTO quotes(number, customer, total, note) VALUES (?, ?, ?, ?)",
                    (number, customer, total, note),
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


def open_readonly_database(path):
    """以只读方式打开数据库：不存在或不可访问时不创建任何文件。"""
    db_uri = f"file:{pathname2url(os.path.abspath(path))}?mode=ro"
    conn = sqlite3.connect(db_uri, uri=True)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def find_quote(conn, number, extra_columns=()):
    """按编号精确匹配报价主行，返回 (customer, note, extras)；不存在返回 None。

    编号按原文精确匹配（SQLite TEXT 默认 BINARY 比较）：大小写与空格都
    参与匹配，不做任何归一化。旧库缺 note 列（只读打开不能补列）时一律
    视为无说明（note 为 NULL），不补列、不改动已有数据。extra_columns
    为调用方各自需要的附加列（如 preview 的 total），查询结果按给定
    顺序放在 extras 列表中返回。
    """
    quote_columns = {row[1] for row in conn.execute("PRAGMA table_info(quotes)")}
    note_select = "note" if "note" in quote_columns else "NULL"
    extra_select = "".join(f", {column}" for column in extra_columns)
    row = conn.execute(
        f"SELECT customer, {note_select}{extra_select} FROM quotes WHERE number = ?",
        (number,),
    ).fetchone()
    if row is None:
        return None
    customer, note, *extras = row
    return customer, note, extras


def fetch_items(conn, number, columns):
    """按 position 顺序读取明细的指定列：保持顺序，不合并重复说明。"""
    return conn.execute(
        f"SELECT {', '.join(columns)} FROM items WHERE quote_number = ? "
        "ORDER BY position",
        (number,),
    ).fetchall()


def format_yuan(cents):
    """非负整数分 -> 元字符串，保留两位小数。"""
    return f"{cents // 100}.{cents % 100:02d}"


def render_html(number, customer, note, items, total):
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
    # 编号、客户（dl dd）与明细说明（tbody 第 2 列）以 pre-wrap 呈现：
    # 首尾空格、连续空格与换行按原样显示，连续换行保留空行；多行客户仍在
    # 客户信息区域，多行说明仍属于同一行明细。文本仍整体 HTML 转义。
    # 有说明时在客户信息之后、明细表格之前展示；pre-wrap 保留空格与换行。
    # 无说明时不出现该区域，页面其余内容与样式与原先逐字节一致。
    note_style = ""
    note_section = ""
    if note:
        note_style = "\n  .note-text { white-space: pre-wrap; }"
        note_section = (
            '<section class="note">\n'
            "  <h2>客户说明</h2>\n"
            f'  <p class="note-text">{e(note)}</p>\n'
            "</section>\n"
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
  dl dd {{ white-space: pre-wrap; }}
  tbody td:nth-child(2) {{ white-space: pre-wrap; }}{note_style}
</style>
</head>
<body>
<h1>报价单</h1>
<dl>
  <dt>报价编号</dt><dd>{e(number)}</dd>
  <dt>客户</dt><dd>{e(customer)}</dd>
</dl>
{note_section}<table>
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


def write_new_file(path, data):
    """以 O_EXCL 新建文件并循环写完整份字节，绝不覆盖已有文件。

    成功返回 None；失败返回面向用户的原因字符串。os.write 一次可能只接受
    部分字节，因此循环写剩余内容；返回 0 字节表示写入未取得进展，按失败
    结束而不是空转。失败时目标里可能保留此前已写出的部分内容。
    """
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except OSError as exc:
        return str(exc)

    error = None
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written == 0:
                error = "写入未取得进展（write 返回 0 字节）"
                break
            view = view[written:]
    except OSError as exc:
        error = str(exc)
    finally:
        try:
            os.close(fd)
        except OSError as exc:
            if error is None:
                error = str(exc)
    return error


def cmd_preview(args):
    # 输出目标已存在时直接拒绝，绝不覆盖。
    if os.path.exists(args.output):
        return fail(f"输出文件已存在: {args.output}")

    # 预览需要已存的合计与行金额：直接读取，不重新计算。
    try:
        conn = open_readonly_database(args.db)
        try:
            found = find_quote(conn, args.number, ("total",))
            if found is None:
                return fail(f"报价编号不存在: {args.number}")
            customer, note, (total,) = found
            item_rows = fetch_items(
                conn, args.number,
                ("description", "quantity", "unit_price", "line_amount"),
            )
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return fail(f"无法读取数据库 {args.db}: {exc}")

    document = render_html(args.number, customer, note, item_rows, total)

    # O_EXCL 兜底：即使并发出现同名文件也不覆盖。
    error = write_new_file(args.output, document.encode("utf-8"))
    if error is not None:
        return fail(f"无法写入输出文件 {args.output}: {error}")

    print(args.output)
    return 0


def cmd_list(args):
    # 客户筛选值在访问数据库前校验：空字符串或纯空白直接拒绝；
    # 含有效文字时保留两端空白，原样参与精确匹配。
    customer = args.customer
    if customer is not None and not customer.strip():
        return fail("客户筛选值不能为空白")

    # 以只读方式打开，数据库不存在或不可访问时不创建任何文件。
    db_uri = f"file:{pathname2url(os.path.abspath(args.db))}?mode=ro"
    try:
        conn = sqlite3.connect(db_uri, uri=True)
        try:
            # 缺表、缺列或文件不是有效 SQLite 都会在此抛出 sqlite3.Error。
            # number 使用默认 BINARY 排序，与保存先后无关；旧库缺 note 列不影响本查询。
            # 客户筛选用 = 精确比较（BINARY 排序规则）：大小写、空格、换行都参与
            # 匹配，% 与 _ 等符号按普通字符处理，不做子串搜索或归一化。
            if customer is None:
                rows = conn.execute(
                    "SELECT number, customer, total FROM quotes ORDER BY number"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT number, customer, total FROM quotes "
                    "WHERE customer = ? ORDER BY number",
                    (customer,),
                ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return fail(f"无法读取数据库 {args.db}: {exc}")

    # 原文输出编号与客户，不做转义或裁剪；total 为分单位整数，不换算。
    records = [
        {"number": number, "customer": customer, "total": total}
        for number, customer, total in rows
    ]
    sys.stdout.write(json.dumps(records, ensure_ascii=False) + "\n")
    return 0


def cmd_export(args):
    try:
        conn = open_readonly_database(args.db)
        try:
            # 缺表或缺列都按无法读取数据库处理；旧库缺 note 列是唯一例外，
            # 由 find_quote 按无说明处理，不补列、不改动已有数据。
            # 导出只需要下列各列：total 与 line_amount 缺失不影响导出。
            quote_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(quotes)")
            }
            required_quote_columns = {"number", "customer"}
            if not quote_columns:
                raise sqlite3.Error("缺少 quotes 表")
            if not required_quote_columns <= quote_columns:
                raise sqlite3.Error(
                    "quotes 表缺少必要列: "
                    + ", ".join(sorted(required_quote_columns - quote_columns))
                )
            item_columns = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
            required_item_columns = {
                "quote_number", "position", "description", "quantity", "unit_price",
            }
            if not item_columns:
                raise sqlite3.Error("缺少 items 表")
            if not required_item_columns <= item_columns:
                raise sqlite3.Error(
                    "items 表缺少必要列: "
                    + ", ".join(sorted(required_item_columns - item_columns))
                )

            found = find_quote(conn, args.number)
            if found is None:
                return fail(f"报价编号不存在: {args.number}")
            customer, note, _ = found
            item_rows = fetch_items(
                conn, args.number, ("description", "quantity", "unit_price")
            )
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return fail(f"无法读取数据库 {args.db}: {exc}")

    # 导出结构与 save 接受的输入一致：number、customer、items，
    # 非空说明才带 note；不输出内部标识、行金额或合计。
    items = [
        {"description": description, "quantity": quantity, "unit_price": unit_price}
        for description, quantity, unit_price in item_rows
    ]
    quote = {"number": args.number, "customer": customer, "items": items}
    if note:
        # 非空（含纯空白）说明原样输出；NULL 或空字符串省略 note。
        quote["note"] = note

    # 全部读取成功后才输出整份 JSON：失败路径绝不留下部分 JSON。
    # ensure_ascii=False 保留原文；json.dumps 只做 JSON 所需转义，不做 HTML 转义。
    sys.stdout.write(json.dumps(quote, ensure_ascii=False) + "\n")
    return 0


def render_report_html(records):
    """渲染客户汇总离线 HTML：records 为 (customer, count, subtotal_cents)。

    客户文本经 HTML 转义后放入 pre-wrap 单元格，首尾、连续空格与换行均
    可见，不生成标签或脚本。金额为分单位 Python 整数（可超出 64 位），
    直接整值换算为元并固定两位小数，不使用浮点。无客户行时显示空态提示。
    """
    e = html.escape
    if records:
        body_rows = "\n".join(
            "      <tr>"
            f"<td>{e(customer)}</td>"
            f'<td class="num">{count}</td>'
            f'<td class="num">{format_yuan(subtotal)}</td>'
            "</tr>"
            for customer, count, subtotal in records
        )
        rows_block = f"  <tbody>\n{body_rows}\n  </tbody>\n"
        empty_notice = ""
    else:
        rows_block = "  <tbody>\n  </tbody>\n"
        empty_notice = '  <p class="empty">没有匹配的报价</p>\n'
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>客户报价汇总</title>
<style>
  body {{ font-family: sans-serif; margin: 2em; }}
  table {{ border-collapse: collapse; margin-top: 1em; }}
  th, td {{ border: 1px solid #999; padding: 0.4em 0.8em; }}
  .num {{ text-align: right; white-space: nowrap; }}
  tbody td:first-child {{ white-space: pre-wrap; }}
  .empty {{ margin-top: 1em; }}
</style>
</head>
<body>
<h1>客户报价汇总</h1>
<table>
  <thead>
    <tr><th>客户</th><th>报价张数</th><th>累计金额（元）</th></tr>
  </thead>
{rows_block}</table>
{empty_notice}</body>
</html>
"""


def cmd_report(args):
    # 客户筛选值在访问数据库与创建输出文件前校验：空字符串或纯空白直接拒绝；
    # 含有效文字时保留两端空白，原样参与精确匹配。
    customer_filter = args.customer
    if customer_filter is not None and not customer_filter.strip():
        return fail("客户筛选值不能为空白")

    # 输出目标已存在时在读库之前直接拒绝，绝不覆盖，也不触碰数据库。
    output = args.output
    if output is not None and os.path.exists(output):
        return fail(f"输出文件已存在: {output}")

    # 以只读方式打开，数据库不存在或不可访问时不创建任何文件。
    try:
        conn = open_readonly_database(args.db)
        try:
            # 缺表、缺列或文件不是有效 SQLite 都会在此抛出 sqlite3.Error。
            # 报表只需要 number、customer、total 三列；旧库缺 note 列不影响，
            # 不补列、不改动已有数据；items 表不参与汇总，无需存在。
            quote_columns = {row[1] for row in conn.execute("PRAGMA table_info(quotes)")}
            required_columns = {"number", "customer", "total"}
            if not quote_columns:
                raise sqlite3.Error("缺少 quotes 表")
            if not required_columns <= quote_columns:
                raise sqlite3.Error(
                    "quotes 表缺少必要列: "
                    + ", ".join(sorted(required_columns - quote_columns))
                )
            # 累计以已保存的报价合计为准，直接读取 total，不重新计算明细；
            # 每张报价只取一行，含多条明细也不会重复计数。
            rows = conn.execute("SELECT customer, total FROM quotes").fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return fail(f"无法读取数据库 {args.db}: {exc}")

    # 在 Python 中分组累计：SQLite 的 SUM 受有符号 64 位整数限制，多张合法
    # 报价的累计可能超出该范围，Python 整数可以精确表示任意大的合计。
    # 客户名逐字符相同才归入同一组（dict 键即精确相等）：大小写、首尾空格、
    # 连续空格与换行都参与比较，% 与 _ 按普通字符处理，不做任何归一化。
    groups = {}
    for customer, total in rows:
        # 提供筛选值时只统计客户原文逐字符相同的报价（Python 字符串精确相等）：
        # 大小写、首尾空格、连续空格与换行都参与比较，% 与 _ 按普通字符处理。
        if customer_filter is not None and customer != customer_filter:
            continue
        count, subtotal = groups.get(customer, (0, 0))
        groups[customer] = (count + 1, subtotal + total)

    # 按客户原文的 UTF-8 字节升序排列，等价于 SQLite BINARY 排序，
    # 与报价保存顺序无关。
    records = [
        (customer, count, subtotal)
        for customer, (count, subtotal) in sorted(
            groups.items(), key=lambda item: item[0].encode("utf-8")
        )
    ]

    if output is None:
        # 未指定 --output：保留既有单行 JSON 行为。
        # 张数与累计金额均为 JSON 整数（分单位），不换算为元；
        # ensure_ascii=False 保留客户原文，json.dumps 只做 JSON 所需转义。
        payload = [
            {"customer": customer, "quote_count": count, "total": subtotal}
            for customer, count, subtotal in records
        ]
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return 0

    document = render_report_html(records)

    # O_EXCL 兜底：即使并发出现同名文件也不覆盖；输出目录不存在等写入
    # 失败在此报错，此前数据库只读访问不会留下任何改动。
    error = write_new_file(output, document.encode("utf-8"))
    if error is not None:
        return fail(f"无法写入输出文件 {output}: {error}")

    print(output)
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

    list_parser = subparsers.add_parser("list", help="列出已保存报价的编号、客户与合计")
    list_parser.add_argument("--db", required=True, help="SQLite 数据库路径")
    list_parser.add_argument(
        "--customer",
        default=None,
        help="按客户名精确筛选（逐字符一致；省略时列出全部）",
    )
    list_parser.set_defaults(handler=cmd_list)

    export_parser = subparsers.add_parser(
        "export", help="按编号导出单张报价的 save 兼容 JSON（只输出到标准输出）"
    )
    export_parser.add_argument("--db", required=True, help="SQLite 数据库路径")
    export_parser.add_argument("--number", required=True, help="报价编号（原文精确匹配）")
    export_parser.set_defaults(handler=cmd_export)

    report_parser = subparsers.add_parser(
        "report", help="按客户汇总报价张数与累计金额（只输出到标准输出）"
    )
    report_parser.add_argument("--db", required=True, help="SQLite 数据库路径")
    report_parser.add_argument(
        "--customer",
        default=None,
        help="按客户名精确筛选（逐字符一致；省略时汇总全部客户）",
    )
    report_parser.add_argument(
        "--output",
        default=None,
        help="生成离线 HTML 汇总到该路径（目录需已存在）；省略时输出单行 JSON",
    )
    report_parser.set_defaults(handler=cmd_report)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
