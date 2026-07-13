"""creator_export.parse_export_xlsx 纯解析逻辑单测。

只测 openpyxl 解析(export_notes 依赖真 page,留集成/真机验证)。
每个用例用 openpyxl.Workbook 在 tmp_path 现造 fixture .xlsx 喂给 parse_export_xlsx。
"""

import openpyxl

from app.browser.creator_export import COLUMN_MAPPING, parse_export_xlsx

# 表头顺序固定用 COLUMN_MAPPING 的中文键,保证与生产导出列一致。
_HEADERS = list(COLUMN_MAPPING.keys())


def _write_xlsx(tmp_path, headers, rows):
    """把 headers + rows 写成 .xlsx 落到 tmp_path,返回文件路径字符串。"""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(list(headers))
    for row in rows:
        ws.append(list(row))
    path = tmp_path / "export_test.xlsx"
    wb.save(str(path))
    return str(path)


def _sample_row():
    """一行齐全的样例数据,顺序与 _HEADERS 对齐。"""
    return [
        "标题A",                       # 笔记标题
        "2026年05月22日10时59分14秒",   # 首次发布时间
        100,                          # 点赞
        50,                           # 收藏
        20,                           # 评论
        5,                            # 弹幕
        8,                            # 分享
        3,                            # 转载
        12,                           # 涨粉
        0.12,                         # 封面点击率
        10000,                        # 曝光
        3000,                         # 观看量
        45.5,                         # 人均观看时长
    ]


def test_parse_maps_13_fields(tmp_path):
    """13 中文表头 + 1 行 → 返回 dict 覆盖 13 字段并注入 account_id。"""
    path = _write_xlsx(tmp_path, _HEADERS, [_sample_row()])
    rows = parse_export_xlsx(path, account_id=7)

    assert len(rows) == 1
    note = rows[0]
    # COLUMN_MAPPING 的 13 个英文字段应全部就位
    for field in COLUMN_MAPPING.values():
        assert field in note, f"缺字段 {field}"
    assert note["title"] == "标题A"
    assert note["publish_time"] == "2026年05月22日10时59分14秒"
    assert note["likes"] == 100
    # account_id 注入
    assert note["account_id"] == 7


def test_parse_cover_ctr_percentage(tmp_path):
    """cover_ctr 为 0.12(小数比率)→ 换算成 12.0;已是 12.0(百分数)→ 保持。"""
    row_ratio = _sample_row()
    row_ratio[_HEADERS.index("封面点击率")] = 0.12
    row_pct = _sample_row()
    row_pct[_HEADERS.index("封面点击率")] = 12.0

    path = _write_xlsx(tmp_path, _HEADERS, [row_ratio, row_pct])
    rows = parse_export_xlsx(path, account_id=1)

    assert rows[0]["cover_ctr"] == 12.0   # 0.12 * 100
    assert rows[1]["cover_ctr"] == 12.0   # 已是百分数,不再乘


def test_parse_int_and_float_columns(tmp_path):
    """整数列走 int()、人均观看时长走 float;带千分位逗号也能解析。"""
    row = _sample_row()
    row[_HEADERS.index("点赞")] = "1,234"       # 千分位字符串
    row[_HEADERS.index("人均观看时长")] = "45.5"
    path = _write_xlsx(tmp_path, _HEADERS, [row])
    rows = parse_export_xlsx(path, account_id=1)

    note = rows[0]
    assert note["likes"] == 1234
    assert isinstance(note["likes"], int)
    assert note["avg_view_duration"] == 45.5
    assert isinstance(note["avg_view_duration"], float)


def test_parse_missing_column_tolerant(tmp_path):
    """缺「涨粉」「人均观看时长」两列 → 对应字段给默认(0 / 0.0),不崩。"""
    headers = [h for h in _HEADERS if h not in ("涨粉", "人均观看时长")]
    full = _sample_row()
    row = [full[_HEADERS.index(h)] for h in headers]
    path = _write_xlsx(tmp_path, headers, [row])
    rows = parse_export_xlsx(path, account_id=1)

    note = rows[0]
    assert note["follows"] == 0            # 缺整数列 → 0
    assert note["avg_view_duration"] == 0.0  # 缺浮点列 → 0.0
    assert note["likes"] == 100            # 存在列照常解析


def test_parse_empty_sheet(tmp_path):
    """仅表头无数据行 → 返回空列表。"""
    path = _write_xlsx(tmp_path, _HEADERS, [])
    rows = parse_export_xlsx(path, account_id=1)
    assert rows == []
