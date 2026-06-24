"""
TikTok Shop 后台关键页面精准抓取工具
----------------------------------------------------
只抓用户指定的 8 个页面，精确执行每页所需的交互操作：
  1. Homepage
  2. Manage orders          (Orders → Manage orders)
  3. Manage promotions      (Marketing → Promotions → 点 Manage promotions tab)
  4. Analytics Last 30 days (Analytics → 选 Last 30 days)
  5. Shop health            (Account health → Shop health)
  6. Store rating           (Account health → Store rating)
  7. Transactions           (Finance → Transactions)
  8. Withdrawals            (Finance → Withdrawals)

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

# ── 每页"建议抓取的数据"提示（Excel sheet 内显示给运营参考）──
SCRAPE_HINTS = [
    (r"home",              "待发货数、待退货数、被拒商品数、低库存/断货SKU数、差评数、Shop Health 违规分（一站式预警首选）"),
    (r"manage order",      "各状态订单量（待发货/已发货/已完成/已取消）、超时未发货订单列表（逾期风险）"),
    (r"manage promotion",  "进行中促销数、各活动 GMV 贡献、优惠券核销率、折扣商品点击转化"),
    (r"analytic",          "近30天 GMV 趋势、订单量、访客、转化率、流量来源拆分（直播/视频/达人/搜索/Feed）"),
    (r"shop health",       "Violation points 违规分、当前处罚级别、各类违规明细、距 24 分限流阈值还差多少"),
    (r"store rating",      "店铺综合评分、各维度分（物流/服务/商品质量）、差评数量趋势、是否触发 Affiliate 限制风险"),
    (r"transaction",       "每笔结算明细、已完成订单金额、平台手续费、近期结算总额"),
    (r"withdrawal",        "可提现余额、待结算金额、提现申请记录、结算周期与到账时间"),
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
# 精准抓取：每个目标页面的点击序列
# ─────────────────────────────────────────────

# 侧栏滚到顶（展开子菜单后部分项会被推出可视区，回首页前先重置滚动）
_SCROLL_TOP_JS = r"""
() => {
  const els = document.querySelectorAll('div,nav,aside,ul');
  for (const el of els) {
    const r = el.getBoundingClientRect();
    if (r.left < 40 && r.width > 120 && r.width < 340 &&
        el.scrollHeight > el.clientHeight + 40) {
      el.scrollTop = 0;
    }
  }
}
"""

# 在主内容区（x > 160）按文本找可点击元素，用于点击页面内 tab / 日期选项
_CONTENT_CLICK_JS = r"""
(pat) => {
  const rgx = new RegExp(pat, 'i');
  const tags = 'button,[role="tab"],[role="option"],li,a,span,div';
  for (const el of document.querySelectorAll(tags)) {
    const r = el.getBoundingClientRect();
    if (r.left < 160 || r.width < 40 || r.height < 14) continue;
    const txt = (el.innerText || '').replace(/\s+/g, ' ').trim();
    if (!txt || txt.length > 60) continue;
    if (rgx.test(txt)) return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
  }
  return null;
}
"""


def _scroll_sidebar_top(page):
    try:
        page.evaluate(_SCROLL_TOP_JS)
        time.sleep(0.3)
    except Exception:
        pass


def _go_home(page, base):
    """回首页，让侧栏恢复未展开的干净状态。"""
    try:
        page.goto(f"{base}/homepage", wait_until="domcontentloaded", timeout=20000)
    except Exception:
        pass
    wait_until_ready(page, max_wait=30)
    _scroll_sidebar_top(page)
    time.sleep(0.4)


def _nav(page, label) -> bool:
    """点击侧栏导航项，失败后滚顶重试一次。"""
    ok = click_nav(page, label)
    if not ok:
        _scroll_sidebar_top(page)
        time.sleep(0.3)
        ok = click_nav(page, label)
    if not ok:
        print(f"      ⚠ 无法点击侧栏: {label}")
    return ok


def _content_click(page, pattern, poll=8, interval=0.6) -> bool:
    """在主内容区按正则文本找元素并点击，最多 poll 次轮询等待元素出现。"""
    for _ in range(poll):
        try:
            pt = page.evaluate(_CONTENT_CLICK_JS, pattern)
            if pt:
                page.mouse.click(pt["x"], pt["y"])
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def _shot(page, label, group):
    """等页面加载完，截图并返回记录。"""
    ld, ln, sp = wait_until_ready(page)
    return capture(page, label, group, page.url, ld, ln, sp)


def crawl(page, base):
    """
    按固定顺序精准抓取 8 个关键页面。
    每页都从首页出发，通过点击真实侧栏/Tab 项导航，等内容稳定后截图。
    """
    results = []
    print("  开始精准抓取 8 个目标页面...\n")

    # ── 1. Homepage ─────────────────────────────────────────────────
    _go_home(page, base)
    results.append(_shot(page, "Homepage", ""))

    # ── 2. Orders → Manage orders ───────────────────────────────────
    _go_home(page, base)
    _nav(page, "Orders")
    time.sleep(2)
    # Manage orders 可能是侧栏子项，也可能是页面内 Tab
    if not _nav(page, "Manage orders"):
        _content_click(page, r"manage\s*order")
    results.append(_shot(page, "Manage orders", "Orders"))

    # ── 3. Marketing → Promotions → Manage promotions tab ───────────
    _go_home(page, base)
    _nav(page, "Marketing")
    time.sleep(2)
    _nav(page, "Promotions")
    wait_until_ready(page, max_wait=30)
    # 点页面内 "Manage promotions" 标签
    if not _content_click(page, r"manage\s*promotion"):
        print("      ⚠ 未找到 Manage promotions tab，截取当前状态")
    time.sleep(1)
    results.append(_shot(page, "Manage promotions", "Marketing"))

    # ── 4. Analytics — Last 30 days ─────────────────────────────────
    _go_home(page, base)
    _nav(page, "Analytics")
    wait_until_ready(page, max_wait=45)
    # 先点日期下拉（通常显示 "Last 7 days"），展开后再选 "30 days"
    if not _content_click(page, r"last\s*7\s*days?|last 7", poll=6):
        # 某些版本直接有 "Last 30 days" 选项，无需先展开
        pass
    time.sleep(0.6)
    if not _content_click(page, r"last\s*30\s*days?|30\s*days?", poll=6):
        print("      ⚠ 未找到 Last 30 days 选项，截取默认视图")
    wait_until_ready(page, max_wait=30)
    results.append(_shot(page, "Analytics (Last 30 days)", ""))

    # ── 5 & 6. Account health → Shop health / Store rating ──────────
    for sub in ["Shop health", "Store rating"]:
        _go_home(page, base)
        _nav(page, "Account health")
        time.sleep(2.5)          # 等子菜单展开动画
        if not _nav(page, sub):
            print(f"      ⚠ 侧栏未找到 {sub}，尝试内容区点击...")
            _content_click(page, re.escape(sub))
        results.append(_shot(page, sub, "Account health"))

    # ── 7 & 8. Finance → Transactions / Withdrawals ─────────────────
    for sub in ["Transactions", "Withdrawals"]:
        _go_home(page, base)
        _nav(page, "Finance")
        time.sleep(2.5)
        if not _nav(page, sub):
            print(f"      ⚠ 侧栏未找到 {sub}，尝试内容区点击...")
            _content_click(page, re.escape(sub))
        results.append(_shot(page, sub, "Finance"))

    return results


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
