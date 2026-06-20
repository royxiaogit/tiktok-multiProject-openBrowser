"""
Excel 报表生成模块
将各店铺销售数据汇总为格式化的 Excel 文件
"""

import json
from datetime import datetime
from pathlib import Path

import openpyxl
from openpyxl.styles import (
    Font, PatternFill, Alignment, Border, Side, numbers
)
from openpyxl.utils import get_column_letter

CONFIG_PATH = Path(__file__).parent / "config" / "shops.json"

# 国旗 emoji 映射
FLAG = {"MY": "🇲🇾", "TH": "🇹🇭", "VN": "🇻🇳", "PH": "🇵🇭"}

# 各区颜色
COUNTRY_COLORS = {
    "MY": "1A6BAE",   # 蓝
    "TH": "D32F2F",   # 红
    "VN": "C62828",   # 深红
    "PH": "1565C0",   # 深蓝
}

HEADER_FILL = PatternFill("solid", fgColor="1F3864")
SUBHEADER_FILL = PatternFill("solid", fgColor="2E75B6")
ALT_FILL = PatternFill("solid", fgColor="D6E4F0")
SUCCESS_FILL = PatternFill("solid", fgColor="E8F5E9")
FAIL_FILL = PatternFill("solid", fgColor="FFEBEE")

THIN_BORDER = Border(
    left=Side(style="thin", color="BDBDBD"),
    right=Side(style="thin", color="BDBDBD"),
    top=Side(style="thin", color="BDBDBD"),
    bottom=Side(style="thin", color="BDBDBD"),
)


def _cell(ws, row, col, value, bold=False, fill=None, align="center", font_color="000000", size=11, number_format=None):
    cell = ws.cell(row=row, column=col, value=value)
    cell.font = Font(bold=bold, color=font_color, size=size)
    cell.alignment = Alignment(horizontal=align, vertical="center", wrap_text=True)
    cell.border = THIN_BORDER
    if fill:
        cell.fill = fill
    if number_format:
        cell.number_format = number_format
    return cell


def generate_report(results: list, output_dir: str = None) -> str:
    """
    生成 Excel 报表，返回文件路径。
    results: scraper.run_all_shops() 的返回值
    """
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    if output_dir is None:
        output_dir = Path(__file__).parent / config["settings"].get("reports_dir", "reports")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    today = datetime.now().strftime("%Y-%m-%d")
    filename = output_dir / f"TikTok销售日报_{today}.xlsx"

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = f"{today} 销售日报"

    # ── 标题行 ──
    ws.merge_cells("A1:F1")
    title_cell = ws["A1"]
    title_cell.value = f"TikTok 多店铺销售日报  |  {today}"
    title_cell.font = Font(bold=True, color="FFFFFF", size=15)
    title_cell.fill = HEADER_FILL
    title_cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 36

    # ── 生成时间 ──
    ws.merge_cells("A2:F2")
    ts_cell = ws["A2"]
    ts_cell.value = f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  |  共 {len(results)} 个店铺"
    ts_cell.font = Font(color="FFFFFF", size=10)
    ts_cell.fill = SUBHEADER_FILL
    ts_cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[2].height = 20

    # ── 表头 ──
    headers = ["序号", "国家", "店铺名称", "今日 GMV", "今日订单数", "状态"]
    col_widths = [6, 10, 25, 22, 14, 12]
    for col, (h, w) in enumerate(zip(headers, col_widths), 1):
        _cell(ws, 3, col, h, bold=True, fill=SUBHEADER_FILL, font_color="FFFFFF", size=11)
        ws.column_dimensions[get_column_letter(col)].width = w
    ws.row_dimensions[3].height = 24

    # ── 数据行 ──
    success_count = 0
    total_by_country = {}

    for idx, r in enumerate(results, 1):
        row = idx + 3
        fill = SUCCESS_FILL if r["status"] == "success" else FAIL_FILL
        if idx % 2 == 0 and r["status"] == "success":
            fill = ALT_FILL

        country = r["country"]
        flag = FLAG.get(country, "")
        status_text = "✅ 成功" if r["status"] == "success" else f"❌ {r.get('error', '失败')[:20]}"

        _cell(ws, row, 1, idx, fill=fill)
        _cell(ws, row, 2, f"{flag} {country}", fill=fill)
        _cell(ws, row, 3, r["shop_name"], fill=fill, align="left")
        _cell(ws, row, 4, r["gmv"], fill=fill, align="right")
        _cell(ws, row, 5, r["orders"], fill=fill)
        _cell(ws, row, 6, status_text, fill=fill)

        ws.row_dimensions[row].height = 22

        if r["status"] == "success":
            success_count += 1
            total_by_country.setdefault(country, []).append(r)

    # ── 汇总区域 ──
    summary_start = len(results) + 5
    ws.merge_cells(f"A{summary_start}:F{summary_start}")
    _cell(ws, summary_start, 1, "汇总统计", bold=True, fill=HEADER_FILL, font_color="FFFFFF", size=12)
    ws.merge_cells(f"A{summary_start}:F{summary_start}")
    ws.row_dimensions[summary_start].height = 28

    stat_row = summary_start + 1
    _cell(ws, stat_row, 1, "指标", bold=True, fill=SUBHEADER_FILL, font_color="FFFFFF")
    _cell(ws, stat_row, 2, "数值", bold=True, fill=SUBHEADER_FILL, font_color="FFFFFF")
    ws.merge_cells(f"B{stat_row}:F{stat_row}")

    stats = [
        ("总店铺数", str(len(results))),
        ("成功抓取", f"{success_count} 个"),
        ("失败店铺", f"{len(results) - success_count} 个"),
        ("覆盖国家", ", ".join(sorted(set(r["country"] for r in results if r["status"] == "success")))),
    ]

    for i, (label, value) in enumerate(stats):
        r = stat_row + 1 + i
        fill = ALT_FILL if i % 2 == 0 else None
        _cell(ws, r, 1, label, bold=True, fill=fill, align="left")
        ws.merge_cells(f"B{r}:F{r}")
        _cell(ws, r, 2, value, fill=fill, align="left")
        ws.row_dimensions[r].height = 20

    # ── 冻结表头 ──
    ws.freeze_panes = "A4"

    # ── 注释 sheet ──
    ws_note = wb.create_sheet("使用说明")
    notes = [
        "TikTok 多店铺销售日报 - 使用说明",
        "",
        "1. GMV = Gross Merchandise Value（商品交易总额）",
        "2. 数据为当天实时抓取，仅供参考",
        "3. 状态「需要重新登录」= 对应 Chrome Profile 的 TikTok 登录已过期，请手动重新登录",
        "4. screenshots 目录保存了每次抓取的截图，可用于核对数据",
        "",
        "配置文件位置: config/shops.json",
        "  - 修改 chrome_profile_path 以匹配你的 Chrome Profile 路径",
        "  - enabled: false 可临时禁用某个店铺",
        "",
        f"报表生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
    ]
    for i, note in enumerate(notes, 1):
        ws_note.cell(row=i, column=1, value=note)
        ws_note.column_dimensions["A"].width = 70

    wb.save(str(filename))
    return str(filename)
