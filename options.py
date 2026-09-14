"""页面下拉选项构造。

唯一职责：用行主键拼键，返回 {显示标签: 整行}。

为什么必须这样：选项字典的键如果只用业务字段（小区+户型+价格、客户姓名……），
出现两条相同字段的记录时，后者会在字典里把前者整个覆盖，页面只剩一个选项，
用户选哪条都会写到幸存那条记录上——张冠李戴，且不留报错。
键里带主键 id 后键天然唯一，不可能互相覆盖。
本模块不依赖 streamlit，方便直接单测。
"""


def keyed_options(rows, label, id_key="id"):
    """rows: sqlite3.Row 列表；label(row) -> 不带 id 的展示文本。

    返回 {f"#{id} {label}": row}，标签对人可读、键对机器唯一。
    """
    return {f"#{row[id_key]} {label(row)}": row for row in rows}
