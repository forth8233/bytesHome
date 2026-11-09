# -*- coding: utf-8 -*-
"""生成"语音词库导入模板.xlsx"，入库供 Web 页面下载。

模板结构与解析器 backend/app/fastpath/voice_lexicon.py::parse_workbook / parse_row 对齐：
- 只取第一个工作表；
- 第一行会被解析器当作表头跳过（next(rows)），因此第一行放列名；
- 自第二行起为示例数据，导入时会被真实解析。
- 列顺序（0 基）：A 词库名称 | B 词条 | C 推断功能 | D 标识符 | E 别名 | F 属性类型。
  属性类型：1 = 内置/系统词库，2 = 自定义词库（2 不会被二次导入覆盖）。

运行：python tools/gen_voice_template.py
输出：backend/app/fastpath/voice_template.xlsx
"""
from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

# 列名按官方系统词库表头（与解析列位对齐，不参与解析）
HEADER = ["词库名称", "词条", "推断功能", "标识符", "别名", "属性类型"]

# 示例数据：(词库名称, 词条, 推断功能, 标识符, 别名, 属性类型)
# 词库名称必须是 LEXICON_BY_NAME 中的键；属性类型 1=内置/系统，2=自定义。
SAMPLES = [
    ("楼层名称", "一楼", "", "", "", 2),
    ("楼层名称", "二楼", "", "", "2楼/层二", 2),
    ("区域名称", "客厅", "", "", "起居室/大厅", 2),
    ("区域名称", "书房", "", "", "工作间/办公区", 2),
    ("设备名称", "餐厅筒灯", "", "", "筒灯", 2),
    ("设备名称", "主卧空调", "", "", "", 2),
    ("场景名称", "离家", "", "", "外出/出门", 2),
    ("场景名称", "回家", "", "", "到家/返回", 2),
    ("场景名称", "观影", "", "", "看电影/影院模式", 2),
    ("功能名称", "打开", "", "", "开启/开启一下", 2),
    ("功能名称", "关闭", "", "", "关上/关掉", 2),
    ("功能名称", "设置温度", "", "set_temperature", "调温度/设定温度", 2),
    ("枚举值", "冷水", "", "cold", "凉水", 2),
    ("枚举值", "热水", "", "hot", "", 2),
    ("属性数值", "一半", "", "50", "百分之五十", 2),
    ("属性数值", "百分之三十", "", "30", "三成", 2),
    ("最大值", "最高", "", "100", "顶格/最大", 2),
    ("最小值", "最低", "", "0", "最小/归零", 2),
    ("相对调高", "调高一点", "increase", "1", "开大一点", 2),
    ("相对调低", "调低一点", "decrease", "1", "关小一点", 2),
    ("查询", "现在多少度", "query", "", "温度多少", 2),
    ("一点", "一点", "", "", "一些/少许", 1),
    ("单位", "度", "", "", "", 1),
    ("单位", "瓦", "", "", "", 1),
    ("设备类型", "灯光", "", "light", "灯", 2),
    ("设备类型", "空调", "", "climate", "", 2),
]

OUT = Path(__file__).resolve().parent.parent / "backend" / "app" / "fastpath" / "voice_template.xlsx"


def main() -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "语音词库导入模板"

    header_fill = PatternFill("solid", fgColor="4472C4")
    header_font = Font(color="FFFFFF", bold=True)
    for col, name in enumerate(HEADER, start=1):
        cell = ws.cell(row=1, column=col, value=name)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")

    for row_idx, sample in enumerate(SAMPLES, start=2):
        for col_idx, value in enumerate(sample, start=1):
            ws.cell(row=row_idx, column=col_idx, value=value)

    widths = [12, 16, 12, 18, 20, 12]
    for i, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = width

    ws.freeze_panes = "A2"
    wb.save(OUT)
    print(f"generated: {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()