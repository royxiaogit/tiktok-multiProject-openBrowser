"""
TikTok Shop 后台全导航爬取工具
------------------------------------------------
- 连接已开启的 Chrome (CDP)，自动遍历左侧导航的【所有页面】，
  包括展开 Account health 等父菜单下的子页面
- 不猜 URL：通过点击真实导航项跳转，避免 "No matching route"
- 每个页面等到内容真正加载完成再整页截图（复用 diagnose_shop 的等待逻辑）
- 所有截图汇总进【一个 Excel】：
    · "目录" sheet：所有页面 + 加载状态 + 跳转链接
    · 每个页面一个 sheet（按导航名命名）：整页截图 + 建议抓取的数据

用法：
    python crawl_backend.py
"""

import json
import re
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

try:
    from openpyxl.drawing.image import Image as XLImage
    _IMG_OK = True
except Exception:
    _IMG_OK = False

# 复用诊断脚本里"等页面真正加载完"的逻辑（单一来源，避免重复）
from diagnose_shop import wait_until_ready, goto_and_wait, port_open

CONFIG_PATH = Path("tiktok_sales_reporter/config/shops.json")
OUT_DIR = Path("tiktok_sales_reporter/reports/backend_crawl")
SHOT_DIR = OUT_DIR / "screenshots"
SHOT_DIR.mkdir(parents=True, exist_ok=True)

IMG_TARGET_WIDTH = 1000   # Excel 内嵌图缩放到的宽度（像素）

# ── 每类页面"建议抓取的数据"（按导航名关键词匹配，用于在 sheet 里给运营提示）──
SCRAPE_HINTS = [
    (r"home",                       "待发货数、待退货数、被拒商品、低库存/断货数、差评数、Shop Health 违规数（一站式预警源）"),
    (r"shop health|account health", "Violation points 违规分、当前处罚、各类违规明细、距 24 分限流阈值还差多少"),
    (r"store rating",               "店铺评分、各维度评分、差评趋势、是否触发 Affiliate 限制风险"),
    (r"creator",                    "达人合作健康分、达人违规、合作达人数"),
    (r"security",                   "账号安全状态、登录设备、异常登录提醒"),
    (r"order",                      "待发货/已发货/已完成/已取消数量、超时未发货订单（重点）"),
    (r"product",                    "在售/下架/被拒数量、各 SKU 库存、断货 SKU 清单"),
    (r"logistic",                   "待打印面单、待揽收、运输异常/卡件"),
    (r"finance",                    "可提现金额、待结算、已结算、结算周期"),
    (r"analytic",                   "GMV、订单量、访客、转化率、流量来源（直播/视频/达人/搜索）"),
    (r"affiliate",                  "合作达人数、达人带货 GMV、佣金支出、待审达人申请"),
    (r"marketing",                  "进行中活动、广告花费、ROI、优惠券核销"),
    (r"live|video",                 "直播场次、直播 GMV、观看数、视频挂车转化"),
    (r"growth",                     "成长任务进度、可报名的平台活动/大促"),
    (r"quick access",               "常用入口快捷方式（一般无需抓取）"),
]


def scrape_hint(label: str) -> str:
    low = label.lower()
    for pat, hint in SCRAPE_HINTS:
        if re.search(pat, low):
            return hint
    return "（待人工确认该页面的关键指标）"


# 这些不是真正的内容页（左上角 LOGO/品牌名等），遍历时跳过
SKIP_LABELS = {"seller center", "tiktok shop", "tiktok seller center"}


def _skip(label: str) -> bool:
    return label.strip().lower() in SKIP_LABELS


# ─────────────────────────────────────────────
# 左侧导航：几何识别（不依赖 class 名）
# ─────────────────────────────────────────────

# 取左列(约 x<300)里、单行短文本的可点击项 => 导航菜单项
_SIDEBAR_JS = r"""
() => {
  const out = [];
  const all = document.querySelectorAll('a,[role="menuitem"],li,div,span');
  for (const el of all) {
    const r = el.getBoundingClientRect();
    if (r.right <= 300 && r.left >= 0 && r.left < 42 &&
        r.height >= 18 && r.height <= 72 && r.width > 70) {
      const txt = (el.innerText || '').trim();
      if (!txt || txt.length > 28 || txt.includes('\n')) continue;
      if (/^\d+$/.test(txt)) continue;                 // 纯数字跳过
      out.push({ text: txt, top: Math.round(r.top), href: el.tagName === 'A' ? el.href : '' });
    }
  }
  out.sort((a, b) => a.top - b.top);
  const seen = new Set(); const res = [];
  for (const it of out) { if (!seen.has(it.text)) { seen.add(it.text); res.push(it); } }
  return res;
}
"""

# 按文字在左列里定位元素 → 先 scrollIntoView 再取坐标，解决菜单展开后其他项被
# 推出可视区导致 getBoundingClientRect() 高度/坐标失效的问题。
_LOCATE_JS = r"""
(args) => {
  const { label } = args;
  let best = null, bestArea = 1e12;
  const all = document.querySelectorAll('a,[role="menuitem"],li,div,span');
  for (const el of all) {
    if ((el.innerText || '').trim() !== label) continue;
    const r = el.getBoundingClientRect();
    // 只限左列（x 轴）；y 轴不限——元素可能已滚出可视区
    if (r.right > 350 || r.left > 60 || r.width < 50) continue;
    const area = r.width * (r.height || 30);
    if (area < bestArea) { bestArea = area; best = el; }
  }
  if (!best) return null;
  // 把元素滚入可视区，然后再取坐标
  best.scrollIntoView({ block: 'nearest', behavior: 'instant' });
  const r2 = best.getBoundingClientRect();
  if (r2.width < 10 || r2.height < 10) return null;   // 真正隐藏的元素
  return { x: r2.left + r2.width / 2, y: r2.top + r2.height / 2 };
}
"""


def sidebar_items(page):
    try:
        return page.evaluate(_SIDEBAR_JS)
    except Exception:
        return []


def click_nav(page, label, top=None) -> bool:
    """点击左侧导航项；top 参数保留仅供日志，不再作坐标匹配依据。"""
    try:
        pt = page.evaluate(_LOCATE_JS, {"label": label})
    except Exception:
        pt = None
    if not pt:
        return False
    try:
        page.mouse.click(pt["x"], pt["y"])
        return True
    except Exception:
        return False


# ─────────────────────────────────────────────
# 截图 + 单页记录
# ─────────────────────────────────────────────

def capture(page, label, group, url, loaded=True, length=0, spin=0):
    safe = re.sub(r"[^0-9A-Za-z]+", "_", label).strip("_")[:30] or "page"
    path = SHOT_DIR / f"{safe}_{datetime.now().strftime('%H%M%S')}.png"
    shot = ""
    try:
        page.evaluate("window.scrollTo(0,0)")
        time.sleep(0.3)
        page.screenshot(path=str(path), full_page=True)
        shot = str(path)
    except Exception as e:
        print(f"      截图失败: {e}")
    status = "✓ 已加载" if loaded else f"⚠ 未完全加载({length}字/转圈{spin})"
    print(f"    [{group + '/' if group else ''}{label}] {status}")
    return {
        "label": label, "group": group, "url": url,
        "loaded": bool(loaded), "text_len": length,
        "hint": scrape_hint(label), "screenshot": shot,
    }


# ─────────────────────────────────────────────
# 遍历整个左侧导航
# ─────────────────────────────────────────────

def crawl(page, base):
    pages = []
    print("  打开首页并读取左侧导航...")
    loaded, length, spin = goto_and_wait(page, f"{base}/homepage", "Homepage")
    time.sleep(1)

    top_items = sidebar_items(page)
    labels = [i["text"] for i in top_items]
    print(f"  顶层导航 {len(top_items)} 项: {labels}")

    # 首页本身作为一页
    pages.append(capture(page, "Homepage", "", page.url, loaded, length, spin))
    visited = {"Homepage"}

    for it in top_items:
        label = it["text"]
        if label in visited or _skip(label):
            continue

        # 每次点新的顶层菜单前，先回首页让侧栏恢复干净状态：
        # Products 展开 7 个子项后，其下的 Logistics/Marketing 等会被推出侧栏
        # 可视区，必须在未展开的首页侧栏里点击才可靠。
        cur_url = page.url
        if "homepage" not in cur_url:
            try:
                page.goto(f"{base}/homepage", wait_until="domcontentloaded", timeout=20000)
                time.sleep(1.5)
            except Exception:
                pass

        before = {x["text"] for x in sidebar_items(page)}
        if not click_nav(page, label, it["top"]):
            print(f"    ✗ 无法点击导航项: {label}")
            continue
        time.sleep(1.8)

        after_items = sidebar_items(page)
        children = [x for x in after_items
                    if x["text"] not in before and x["text"] not in visited]

        if children:
            # 父菜单：逐个抓子页面
            print(f"  ▼ {label} 展开子菜单: {[c['text'] for c in children]}")
            visited.add(label)
            for c in children:
                if c["text"] in visited:
                    continue
                if not click_nav(page, c["text"], c["top"]):
                    # 子项可能因父菜单收起而消失，重新展开父菜单再点
                    click_nav(page, label, it["top"])
                    time.sleep(1.2)
                    click_nav(page, c["text"], c["top"])
                ld, ln, sp = wait_until_ready(page)
                pages.append(capture(page, c["text"], label, page.url, ld, ln, sp))
                visited.add(c["text"])
        else:
            # 叶子页面
            ld, ln, sp = wait_until_ready(page)
            pages.append(capture(page, label, "", page.url, ld, ln, sp))
            visited.add(label)

    return pages


# ─────────────────────────────────────────────
# 汇总成一个 Excel
# ─────────────────────────────────────────────

HEADER_FILL = PatternFill("solid", fgColor="1F3864")
SUB_FILL = PatternFill("solid", fgColor="2E75B6")
OK_FILL = PatternFill("solid", fgColor="E8F5E9")
WARN_FILL = PatternFill("solid", fgColor="FFF4E5")
ALT_FILL = PatternFill("solid", fgColor="D6E4F0")
BORDER = Border(*(Side(style="thin", color="BDBDBD"),) * 4)


def _c(ws, r, col, v, bold=False, fill=None, align="left", color="000000", size=11, wrap=False):
    cell = ws.cell(row=r, column=col, value=v)
    cell.font = Font(bold=bold, color=color, size=size)
    cell.alignment = Alignment(horizontal=align, vertical="center", wrap_text=wrap)
    cell.border = BORDER
    if fill:
        cell.fill = fill
    return cell


def _safe_sheet_name(name, used):
    clean = re.sub(r"[\[\]:*?/\\]", "-", name)[:28]
    cand = clean or "page"
    i = 1
    while cand in used:
        cand = f"{clean[:25]}_{i}"
        i += 1
    used.add(cand)
    return cand


def _embed(ws, path, row):
    if not (_IMG_OK and path and Path(path).exists()):
        _c(ws, row, 1, "（截图不可用，请确认已安装 pillow: pip install pillow）", color="C00000")
        return
    try:
        img = XLImage(path)
        if img.width and img.width > IMG_TARGET_WIDTH:
            ratio = IMG_TARGET_WIDTH / float(img.width)
            img.width = IMG_TARGET_WIDTH
            img.height = int(img.height * ratio)
        ws.add_image(img, f"A{row}")
    except Exception as e:
        _c(ws, row, 1, f"（截图嵌入失败: {e}）", color="C00000")


def save_excel(shop, pages):
    today = datetime.now().strftime("%Y-%m-%d")
    out = OUT_DIR / f"TikTok后台全页面_{shop['name']}_{today}.xlsx"
    if out.exists():
        out = OUT_DIR / f"TikTok后台全页面_{shop['name']}_{today}_{datetime.now().strftime('%H%M%S')}.xlsx"

    # 先给每页分配唯一 sheet 名（目录链接和实际 sheet 共用同一份）
    used = {"目录"}
    for pg in pages:
        base_name = (f"{pg['group']}-{pg['label']}" if pg["group"] else pg["label"])
        pg["sheet"] = _safe_sheet_name(base_name, used)

    wb = openpyxl.Workbook()
    idx = wb.active
    idx.title = "目录"

    widths = [6, 18, 26, 46, 14, 40]
    heads = ["序号", "分组", "页面（导航名）", "URL", "加载状态", "建议抓取的数据"]
    for col, w in enumerate(widths, 1):
        idx.column_dimensions[get_column_letter(col)].width = w

    idx.merge_cells("A1:F1")
    t = idx["A1"]
    t.value = f"TikTok 后台全页面爬取 — {shop['name']} ({shop['country']})  |  {today}"
    t.font = Font(bold=True, color="FFFFFF", size=15)
    t.fill = HEADER_FILL
    t.alignment = Alignment(horizontal="center", vertical="center")
    idx.row_dimensions[1].height = 36

    for col, h in enumerate(heads, 1):
        _c(idx, 2, col, h, bold=True, fill=SUB_FILL, color="FFFFFF", align="center", wrap=True)
    idx.row_dimensions[2].height = 28

    for i, pg in enumerate(pages, 1):
        row = i + 2
        fill = OK_FILL if pg["loaded"] else WARN_FILL
        _c(idx, row, 1, i, fill=fill, align="center")
        _c(idx, row, 2, pg["group"] or "—", fill=fill, align="center")
        link = _c(idx, row, 3, pg["label"], fill=fill, color="0563C1")
        link.hyperlink = f"#'{pg['sheet']}'!A1"
        link.font = Font(color="0563C1", underline="single", size=11)
        _c(idx, row, 4, pg["url"], fill=fill)
        _c(idx, row, 5, "✓ 已加载" if pg["loaded"] else "⚠ 未完整", fill=fill, align="center")
        _c(idx, row, 6, pg["hint"], fill=fill, wrap=True)
        idx.row_dimensions[row].height = 30

    idx.freeze_panes = "A3"

    # 每页一个 sheet：页头信息 + 建议抓取 + 整页截图
    for pg in pages:
        ws = wb.create_sheet(pg["sheet"])
        ws.column_dimensions["A"].width = 28
        title = pg["label"] + (f"  （{pg['group']}）" if pg["group"] else "")
        ws.merge_cells("A1:H1")
        _c(ws, 1, 1, title, bold=True, fill=HEADER_FILL, color="FFFFFF", size=13)
        ws.row_dimensions[1].height = 30

        ws.merge_cells("A2:H2")
        _c(ws, 2, 1, f"URL: {pg['url']}", color="555555", size=10)
        ws.merge_cells("A3:H3")
        _c(ws, 3, 1, f"建议抓取：{pg['hint']}", fill=WARN_FILL, wrap=True, size=10)
        ws.merge_cells("A4:H4")
        state = "✓ 已加载完整" if pg["loaded"] else f"⚠ 未完全加载（正文 {pg['text_len']} 字，可能需重跑）"
        _c(ws, 4, 1, state, color=("1B5E20" if pg["loaded"] else "C00000"), size=10)

        _embed(ws, pg["screenshot"], 6)

    wb.save(str(out))
    return out


def main():
    if not CONFIG_PATH.exists():
        print("找不到 shops.json"); return
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    shop = next((s for s in config["shops"] if s.get("enabled", True)), None)
    port = shop.get("debug_port", 9222)
    base = shop["seller_center_url"].rstrip("/")

    if not port_open(port):
        print(f"❌ 端口 {port} 未开启，请先打开已登录的 Chrome"); return

    print(f"✓ 连接 Chrome (port={port}) — {shop['name']}")
    print(f"  截图目录: {SHOT_DIR}\n")

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=10000)
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.new_page()
        page.set_default_timeout(20000)
        pages = crawl(page, base)
        page.close()
        browser.close()

    out = save_excel(shop, pages)
    print(f"\n✓ 完成！共 {len(pages)} 个页面")
    print(f"  Excel: {out}")


if __name__ == "__main__":
    main()
