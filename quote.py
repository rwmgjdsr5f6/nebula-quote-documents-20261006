#!/usr/bin/env python3
"""报价单保存、按编号生成 HTML 预览、摘要列表、按编号导出 JSON 与按客户汇总报表。

仅使用 Python 3 标准库与本地 SQLite：
  python quote.py save    --db demo.sqlite --input quote.json
  python quote.py preview --db demo.sqlite --number Q-DEMO-001 --output preview.html
  python quote.py list    --db demo.sqlite [--customer 客户名] [--output list.html]
  python quote.py export  --db demo.sqlite --number Q-DEMO-001 [--output quote.json]
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
    # 有说明时在客户信息之后、明细表格之前展示；pre-wrap 保留空格与换行。
    # 无说明时不出现该区域，页面其余内容与样式与原先逐字节一致。
    # 编号、客户与明细说明同样以 pre-wrap 展示：首尾空格、连续空格与换行
    # （含连续换行形成的空行）按原样呈现；多行客户仍在客户信息区域，
    # 多行说明仍在同一行明细的单元格内，不拆出新的表格行。
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


def render_list_html(rows):
    """渲染报价列表离线 HTML：rows 为 (number, customer, total_cents)。

    编号与客户文本经 HTML 转义后放入 pre-wrap 单元格：首尾、连续空格与
    换行均可见，多行文本仍属于同一报价行，不拆出新行，也不生成标签或
    脚本。合计直接读取库中保存值，以分单位 Python 整数精确换算为元并
    固定两位小数，不重新计算明细、不使用浮点；零金额显示 0.00，超出
    有符号 64 位上限的整数也不丢精度。无报价行时保留表头并显示空态提示。
    """
    e = html.escape
    if rows:
        body_rows = "\n".join(
            "      <tr>"
            f"<td>{e(number)}</td>"
            f"<td>{e(customer)}</td>"
            f'<td class="num">{format_yuan(total)}</td>'
            "</tr>"
            for number, customer, total in rows
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
<title>报价列表</title>
<style>
  body {{ font-family: sans-serif; margin: 2em; }}
  table {{ border-collapse: collapse; margin-top: 1em; }}
  th, td {{ border: 1px solid #999; padding: 0.4em 0.8em; }}
  .num {{ text-align: right; white-space: nowrap; }}
  tbody td:nth-child(1), tbody td:nth-child(2) {{ white-space: pre-wrap; }}
  .empty {{ margin-top: 1em; }}
</style>
</head>
<body>
<h1>报价列表</h1>
<table>
  <thead>
    <tr><th>报价编号</th><th>客户</th><th>合计（元）</th></tr>
  </thead>
{rows_block}</table>
{empty_notice}</body>
</html>
"""


def validate_list_customer_filter(customer_filter):
    """校验列表的客户筛选值。

    返回原值（含两端空白，供逐字符精确匹配）；空字符串或纯空白抛出
    ValueError。纯字符串运算，不访问数据库、不创建任何文件。
    """
    if customer_filter is not None and not customer_filter.strip():
        raise ValueError("客户筛选值不能为空白")
    return customer_filter


def organize_list_rows(rows, customer_filter=None):
    """对读取出的 (number, customer, total) 摘要行执行精确筛选与排序。

    这是列表筛选与排序规则的唯一规则来源，全部基于入参序列计算，不打开
    数据库、不创建文件、不写标准输出，可脱离命令行与数据库独立验证；
    公开 list 入口同样经此函数处理，二者始终使用同一套规则：
      - 筛选：提供 customer_filter 时只保留客户原文与之逐字符相同的行，
        大小写、首尾空格、连续空格与换行都参与比较，% 与 _ 按普通字符
        处理，不做子串搜索、LIKE 通配或任何归一化；
      - 排序：按编号原文的 UTF-8 字节升序，等价于 SQLite TEXT 的
        BINARY 排序，与输入（保存）顺序无关；
      - total 原样保留：即库中保存的合计（分单位整数），不重算明细。
    返回 [(number, customer, total), ...]。
    """
    if customer_filter is not None:
        rows = [
            (number, customer, total)
            for number, customer, total in rows
            if customer == customer_filter
        ]
    return sorted(rows, key=lambda row: row[0].encode("utf-8"))


def render_list_json(rows):
    """渲染 list 的单行 JSON 文本（以换行结尾），不落盘、不写标准输出。

    每行仅 number、customer、total 三个字段，total 为分单位 JSON 整数，
    不换算为元、浮点或字符串；ensure_ascii=False 保留编号与客户原文，
    json.dumps 只做 JSON 所需转义（引号、换行等），不做 HTML 转义。
    空结果为 "[]\n"。
    """
    records = [
        {"number": number, "customer": customer, "total": total}
        for number, customer, total in rows
    ]
    return json.dumps(records, ensure_ascii=False) + "\n"


def read_list_rows(conn):
    """只读校验 quotes 表必要列并读取列表所需的 (number, customer, total) 行。

    列表只需要 number、customer、total 三列；旧库缺 note 列或缺 items 表
    都不影响，不补列、不改动已有数据。缺 quotes 表或必要列、文件不是
    有效 SQLite 等都抛出 sqlite3.Error。此处不做筛选与排序——规则在
    organize_list_rows 中，以便脱离数据库独立验证；合计直接读取已保存
    的 total，不 JOIN items、不重新计算明细。
    """
    quote_columns = {row[1] for row in conn.execute("PRAGMA table_info(quotes)")}
    required_columns = {"number", "customer", "total"}
    if not quote_columns:
        raise sqlite3.Error("缺少 quotes 表")
    if not required_columns <= quote_columns:
        raise sqlite3.Error(
            "quotes 表缺少必要列: "
            + ", ".join(sorted(required_columns - quote_columns))
        )
    return conn.execute("SELECT number, customer, total FROM quotes").fetchall()


def cmd_list(args):
    # 本函数只做流程编排与失败收口；客户筛选校验在 validate_list_customer_filter
    # 中，筛选与排序规则在 organize_list_rows 纯函数中，数据库访问在
    # read_list_rows 中，输出渲染在 render_list_json / render_list_html 中。

    # 客户筛选值在访问数据库与创建输出文件前校验：空字符串或纯空白直接拒绝；
    # 含有效文字时保留两端空白，原样参与精确匹配。
    try:
        customer_filter = validate_list_customer_filter(args.customer)
    except ValueError as exc:
        return fail(str(exc))

    # 输出目标已存在时在读库之前直接拒绝，绝不覆盖，也不触碰数据库。
    output = args.output
    if output is not None and os.path.exists(output):
        return fail(f"输出文件已存在: {output}")

    # 以只读方式打开，数据库不存在或不可访问时不创建任何文件。
    try:
        conn = open_readonly_database(args.db)
        try:
            # 缺表、缺列或文件不是有效 SQLite 都在 read_list_rows 中
            # 抛出 sqlite3.Error；旧库缺 note 列或缺 items 表都不影响读取，
            # 不补结构、不改动已有记录。
            rows = read_list_rows(conn)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return fail(f"无法读取数据库 {args.db}: {exc}")

    # 读库之后的筛选与排序全部走纯函数：客户逐字符精确匹配、编号 BINARY
    # 升序均不触碰数据库或文件，与脱离命令行的独立验证共用同一套规则。
    rows = organize_list_rows(rows, customer_filter)

    if output is None:
        # 未指定 --output：保留既有单行 JSON 行为，不创建任何文件。
        sys.stdout.write(render_list_json(rows))
        return 0

    document = render_list_html(rows)

    # O_EXCL 兜底：即使并发出现同名文件也不覆盖；输出目录不存在等写入
    # 失败在此报错，此前数据库只读访问不会留下任何改动。
    error = write_new_file(output, document.encode("utf-8"))
    if error is not None:
        return fail(f"无法写入输出文件 {output}: {error}")

    print(output)
    return 0


def render_quote_json(number, customer, note, items):
    """把一张已读取的报价及其有序明细渲染为单行导出 JSON 文本（以换行结尾）。

    这是 export 文本生成规则的唯一规则来源，全部基于入参计算，不打开
    数据库、不创建文件、不写标准输出，可脱离命令行与数据库独立验证；
    公开 export 入口同样经此函数生成文本，二者始终使用同一套规则：
      - 结构与 save 接受的输入一致：number、customer、items，非空说明
        才含 note；不输出内部标识、行金额或合计；
      - items 为有序的 (description, quantity, unit_price) 序列，逐条
        转为只含 description、quantity、unit_price 三键的对象，顺序
        不变，重复说明保持为独立条目；
      - 数量与单价原样作为 JSON 整数输出（分单位；Python 整数不受
        有符号 64 位限制），不换算为元、浮点或字符串；
      - 编号、客户、说明与明细文字逐字符保留，ensure_ascii=False
        使中文、尖括号、与号原样输出，json.dumps 只做 JSON 所需转义
        （引号、换行等），不做 HTML 转义；
      - note 为 None 或空字符串时省略；纯空白说明原样保留；
      - 只处理已读取的合法内容，不做校验，也不重算行金额或合计。
    返回 json.dumps(..., ensure_ascii=False) 加恰好一个 LF：一个物理
    行、无 CR；UTF-8 无 BOM 的编码由调用方落盘或写标准输出时完成。
    """
    records = [
        {"description": description, "quantity": quantity, "unit_price": unit_price}
        for description, quantity, unit_price in items
    ]
    quote = {"number": number, "customer": customer, "items": records}
    if note:
        # 非空（含纯空白）说明原样输出；NULL 或空字符串省略 note。
        quote["note"] = note
    return json.dumps(quote, ensure_ascii=False) + "\n"


def cmd_export(args):
    # 输出目标已存在时在读库之前直接拒绝，绝不覆盖，也不触碰数据库。
    output = args.output
    if output is not None and os.path.exists(output):
        return fail(f"输出文件已存在: {output}")

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
            items = fetch_items(
                conn, args.number, ("description", "quantity", "unit_price")
            )
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return fail(f"无法读取数据库 {args.db}: {exc}")

    # 全部读取成功后才经纯函数生成整份 JSON：不做校验、不重算金额；
    # 失败路径绝不留下部分 JSON。ensure_ascii=False 保留原文，
    # json.dumps 只做 JSON 所需转义，不做 HTML 转义。
    document = render_quote_json(args.number, customer, note, items)

    if output is None:
        # 未指定 --output：保留既有单行 JSON 行为，不创建任何文件。
        sys.stdout.write(document)
        return 0

    # O_EXCL 兜底：即使并发出现同名文件也不覆盖；UTF-8 编码不带 BOM，
    # 内容为单行 JSON 加一个 LF。输出目录不存在等写入失败在此报错，
    # 此前数据库只读访问不会留下任何改动。
    error = write_new_file(output, document.encode("utf-8"))
    if error is not None:
        return fail(f"无法写入输出文件 {output}: {error}")

    print(output)
    return 0


def validate_report_customer_filter(customer_filter):
    """校验客户筛选值。

    返回原值（含两端空白，供逐字符精确匹配）；空字符串或纯空白抛出
    ValueError。纯字符串运算，不访问数据库、不创建任何文件。
    """
    if customer_filter is not None and not customer_filter.strip():
        raise ValueError("客户筛选值不能为空白")
    return customer_filter


def summarize_quote_rows(rows, customer_filter=None):
    """对读取出的 (customer, total) 行执行精确筛选、分组累计与排序。

    这是报表统计口径的唯一规则来源，全部基于入参序列计算，不打开数据库、
    不创建输出文件，可独立验证：
      - 筛选：提供 customer_filter 时只保留客户原文与之逐字符相同的行，
        大小写、首尾空格、连续空格与换行都参与比较，% 与 _ 按普通字符
        处理，不做子串搜索、LIKE 通配或任何归一化；
      - 分组：客户名逐字符相同才归入同一组（dict 键即精确相等）；
      - 计数：每张报价（每个入参行）只计一次，零金额同样计数；
      - 累计：直接累加库中已保存的 total（分单位整数），不重新计算明细；
        整数加法在 Python 中进行，超出有符号 64 位上限仍精确；
      - 排序：按客户原文的 UTF-8 字节升序，等价于 SQLite TEXT 的
        BINARY 排序，与报价保存顺序无关。
    返回 [(customer, count, subtotal_cents), ...]。
    """
    groups = {}
    for customer, total in rows:
        if customer_filter is not None and customer != customer_filter:
            continue
        count, subtotal = groups.get(customer, (0, 0))
        groups[customer] = (count + 1, subtotal + total)

    return [
        (customer, count, subtotal)
        for customer, (count, subtotal) in sorted(
            groups.items(), key=lambda item: item[0].encode("utf-8")
        )
    ]


def summarize_report_grand_total(records):
    """对本次筛选分组后的 records 再求一次合计：(总张数, 总分单位金额)。

    合计仅覆盖本次结果（records 中各组），不含未命中客户；空结果为
    (0, 0)。Python 整数求和不受有符号 64 位限制。纯计算，不访问
    数据库或文件。
    """
    total_count = sum(count for _, count, _ in records)
    total_amount = sum(subtotal for _, _, subtotal in records)
    return total_count, total_amount


def render_report_json(records):
    """渲染 report 的单行 JSON 文本（以换行结尾），不落盘。

    每客户仅 customer、quote_count、total 三个字段，张数与累计均为
    JSON 整数（分单位），不换算为元、浮点或字符串；ensure_ascii=False
    保留客户原文，json.dumps 只做 JSON 所需转义（引号、换行等），
    不做 HTML 转义。空结果为 "[]\n"。
    """
    payload = [
        {"customer": customer, "quote_count": count, "total": subtotal}
        for customer, count, subtotal in records
    ]
    return json.dumps(payload, ensure_ascii=False) + "\n"


def read_report_rows(conn):
    """只读校验 quotes 表必要列并读取报表所需的 (customer, total) 行。

    报表只需要 number、customer、total 三列；旧库缺 note 列不影响，
    不补列、不改动已有数据；items 表不参与汇总，无需存在。缺 quotes
    表或必要列、文件不是有效 SQLite 等都抛出 sqlite3.Error。累计以
    已保存的报价合计为准，直接读取 total，不 JOIN items、不重新计算
    明细；每张报价只取一行，含多条明细也不会重复计数。
    """
    quote_columns = {row[1] for row in conn.execute("PRAGMA table_info(quotes)")}
    required_columns = {"number", "customer", "total"}
    if not quote_columns:
        raise sqlite3.Error("缺少 quotes 表")
    if not required_columns <= quote_columns:
        raise sqlite3.Error(
            "quotes 表缺少必要列: "
            + ", ".join(sorted(required_columns - quote_columns))
        )
    return conn.execute("SELECT customer, total FROM quotes").fetchall()


def render_report_html(records):
    """渲染客户汇总离线 HTML：records 为 (customer, count, subtotal_cents)。

    客户文本经 HTML 转义后放入 pre-wrap 单元格，首尾、连续空格与换行均
    可见，不生成标签或脚本。金额为分单位 Python 整数（可超出 64 位），
    直接整值换算为元并固定两位小数，不使用浮点。无客户行时显示空态提示。
    所有客户行之后另起 tfoot 合计行：合计仅覆盖本次筛选命中的报价，
    每张报价计数一次（含零金额），金额为各客户累计之和；空库或无匹配时
    合计行为 0 张、0.00 元。合计行固定在 tfoot 中，客户原文即使就是
    “合计”，也仍作为 tbody 中的独立客户行显示，不会被替换。
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

    # 合计只统计 records（即本次筛选命中后分组的结果）：张数为各组张数之和，
    # 金额为各组累计之和。Python 整数不受有符号 64 位限制，超出仍精确。
    total_count, total_amount = summarize_report_grand_total(records)
    total_block = (
        "  <tfoot>\n"
        '    <tr><td>合计</td>'
        f'<td class="num">{total_count}</td>'
        f'<td class="num">{format_yuan(total_amount)}</td></tr>\n'
        "  </tfoot>\n"
    )
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
  tfoot td {{ font-weight: bold; }}
  .empty {{ margin-top: 1em; }}
</style>
</head>
<body>
<h1>客户报价汇总</h1>
<table>
  <thead>
    <tr><th>客户</th><th>报价张数</th><th>累计金额（元）</th></tr>
  </thead>
{rows_block}{total_block}</table>
{empty_notice}</body>
</html>
"""


def cmd_report(args):
    # 本函数只做流程编排与失败收口；客户筛选、分组累计与排序的规则在
    # summarize_quote_rows 等纯函数中，数据库访问在 read_report_rows 中，
    # 输出渲染在 render_report_json / render_report_html 中。

    # 客户筛选值在访问数据库与创建输出文件前校验：空字符串或纯空白直接拒绝；
    # 含有效文字时保留两端空白，原样参与精确匹配。
    try:
        customer_filter = validate_report_customer_filter(args.customer)
    except ValueError as exc:
        return fail(str(exc))

    # 输出目标已存在时在读库之前直接拒绝，绝不覆盖，也不触碰数据库。
    output = args.output
    if output is not None and os.path.exists(output):
        return fail(f"输出文件已存在: {output}")

    # 以只读方式打开，数据库不存在或不可访问时不创建任何文件。
    try:
        conn = open_readonly_database(args.db)
        try:
            # 缺表、缺列或文件不是有效 SQLite 都在 read_report_rows 中
            # 抛出 sqlite3.Error。
            rows = read_report_rows(conn)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return fail(f"无法读取数据库 {args.db}: {exc}")

    # 读库之后的全部统计规则都在纯函数中：精确筛选、逐字符分组、每张计一次、
    # 整数累计与 BINARY 排序均不触碰数据库或文件。
    records = summarize_quote_rows(rows, customer_filter)

    if output is None:
        # 未指定 --output：保留既有单行 JSON 行为，不创建任何文件。
        sys.stdout.write(render_report_json(records))
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
    list_parser.add_argument(
        "--output",
        default=None,
        help="生成离线 HTML 列表到该路径（目录需已存在）；省略时输出单行 JSON",
    )
    list_parser.set_defaults(handler=cmd_list)

    export_parser = subparsers.add_parser(
        "export", help="按编号导出单张报价的 save 兼容 JSON"
    )
    export_parser.add_argument("--db", required=True, help="SQLite 数据库路径")
    export_parser.add_argument("--number", required=True, help="报价编号（原文精确匹配）")
    export_parser.add_argument(
        "--output",
        default=None,
        help="将导出 JSON 保存到该路径（目录需已存在，UTF-8 无 BOM）；省略时输出到标准输出",
    )
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
