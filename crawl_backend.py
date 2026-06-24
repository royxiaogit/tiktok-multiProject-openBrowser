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
import subprocess
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


def _select_last_30_days(page) -> bool:
    """
    点击 Analytics 日期筛选里的 "Last 30 days" 选项。
    沿用 scraper.py 中已验证有效的逻辑：先直接尝试各种定位方式，
    若选项未展开再尝试点开日期选择器后重试。
    """
    target = re.compile(r"last\s*30", re.I)

    presets = [
        lambda: page.get_by_role("button", name=target).first,
        lambda: page.get_by_role("option", name=target).first,
        lambda: page.locator("li").filter(has_text=target).first,
        lambda: page.locator("span").filter(has_text=target).first,
        lambda: page.get_by_text(target).first,
    ]

    # ── 直接尝试（选项面板可能已展开）──
    for fn in presets:
        try:
            loc = fn()
            if loc.is_visible(timeout=1000):
                loc.click()
                print("      [✓] Last 30 days 直接点击成功")
                return True
        except Exception:
            continue

    # ── 先点开日期选择器，再重试 ──
    openers = [
        '[class*="date-picker"]:not(input):not([class*="panel"])',
        '[class*="DatePicker"]:not(input)',
        '[class*="date-range"]',
        '[class*="DateRange"]',
    ]
    for sel in openers:
        try:
            opener = page.locator(sel).first
            if not opener.is_visible(timeout=1000):
                continue
            txt = opener.inner_text()
            print(f"      [日期触发器] '{txt[:30]}' → 点击打开")
            opener.click()
            time.sleep(1.5)
            for fn in presets:
                try:
                    loc = fn()
                    if loc.is_visible(timeout=1500):
                        loc.click()
                        print("      [✓] 打开选择器后 Last 30 days 点击成功")
                        return True
                except Exception:
                    continue
            break
        except Exception:
            continue

    print("      ⚠ 未能点击 Last 30 days，将截取当前默认视图")
    return False


def crawl(page, base):
    """
    按固定顺序精准抓取 8 个关键页面。
    每页都从首页出发，通过点击真实侧栏/Tab 项导航，等内容稳定后截图。
    """
    results = []
    alerts = {}
    print("  开始精准抓取 8 个目标页面...\n")

    # ── 1. Homepage ─────────────────────────────────────────────────
    _go_home(page, base)
    results.append(_shot(page, "Homepage", ""))
    alerts = extract_home_alerts(page)     # 提取首页关键预警，用于推送消息正文

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
    time.sleep(2)          # 等 tab 栏渲染完毕

    # 点 "Manage promotions" tab：优先用 Playwright role 语义定位，兜底用坐标
    tab_ok = False
    try:
        tab = page.get_by_role("tab", name=re.compile(r"manage\s*promotion", re.I)).first
        tab.click(timeout=5000)
        tab_ok = True
    except Exception:
        pass
    if not tab_ok:
        # 兜底：在内容区找文本匹配的元素
        tab_ok = _content_click(page, r"Manage\s*promotions?", poll=12, interval=0.5)
    if not tab_ok:
        print("      ⚠ 未找到 Manage promotions tab，截取当前状态")

    wait_until_ready(page, max_wait=30)
    results.append(_shot(page, "Manage promotions", "Marketing"))

    # ── 4. Analytics — Last 30 days ─────────────────────────────────
    # 用直接 URL 导航（与 scraper.py 已验证有效的方式一致，避免 SPA 状态干扰）
    try:
        page.goto(f"{base}/compass/data-overview?shop_region=MY",
                  wait_until="domcontentloaded", timeout=30000)
    except Exception:
        pass
    wait_until_ready(page, max_wait=45)
    time.sleep(4)          # 等图表首次渲染（与 scraper.py 保持一致）

    _select_last_30_days(page)
    time.sleep(3)          # 等 30 天数据加载完毕
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

    return results, alerts


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


# ─────────────────────────────────────────────
# 首页关键预警提取（用于推送消息正文）
# ─────────────────────────────────────────────

# 首页卡片标签 → 中文（按 innerText 里"标签...数字"的形态抓取）
_HOME_LABELS = [
    ("Orders to ship",    "待发货"),
    ("Pending returns",   "待退货/退款"),
    ("Rejected products", "被拒商品"),
    ("Low stock",         "低库存"),
    ("Negative reviews",  "差评"),
]


def extract_home_alerts(page) -> dict:
    """从首页正文里尽力抓取关键预警数字；失败返回空 dict（不影响主流程）。"""
    alerts = {}
    try:
        txt = page.inner_text("body")
    except Exception:
        return alerts

    for en, cn in _HOME_LABELS:
        m = re.search(re.escape(en) + r"[^\d]{0,12}([\d,]+)", txt)
        if m:
            alerts[cn] = m.group(1)

    # 断货数：Out of stock: 99
    m = re.search(r"Out of stock[:：]?\s*([\d,]+)", txt, re.I)
    if m:
        alerts["断货SKU"] = m.group(1)

    # Shop Health 新增违规：12 new violations
    m = re.search(r"([\d,]+)\s*new violations?", txt, re.I)
    if m:
        alerts["新增违规"] = m.group(1)

    # Store Rating 限流风险（黄色横幅）
    if re.search(r"store rating", txt, re.I) and re.search(r"at risk|restrict", txt, re.I):
        alerts["_store_rating_risk"] = True

    return alerts


def build_caption(shop, alerts) -> str:
    """构造推送消息正文：一眼看清当天关键情况。"""
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [f"📊 TikTok {shop['name']} 后台日报  {today}", "━━━━━━━━━━━━"]

    order = ["待发货", "待退货/退款", "被拒商品", "低库存", "断货SKU", "差评", "新增违规"]
    shown = [f"{k} {alerts[k]}" for k in order if k in alerts]
    if shown:
        # 每行放两项，便于手机阅读
        for i in range(0, len(shown), 2):
            lines.append(" ｜ ".join(shown[i:i + 2]))
    else:
        lines.append("（关键指标提取失败，详见附件 Excel）")

    if alerts.get("_store_rating_risk"):
        lines.append("⚠️ Store Rating 偏低，有 Affiliate 限流风险，请尽快处理")

    lines.append("详细 8 个页面截图见附件 Excel ↓")
    return "\n".join(lines)


# ─────────────────────────────────────────────
# 通过 Hermes Agent 推送（微信 / Discord）
# ─────────────────────────────────────────────

HERMES_ENABLED = True
# 推送目标：微信用 "weixin"；发 Discord 频道用 "discord:#频道名"。
# 不确定确切目标名时，在 WSL 里运行：hermes send --list
HERMES_TARGET = "weixin"


def _to_wsl_path(p: Path) -> str:
    """Windows 路径 C:\\Users\\x\\a.xlsx → WSL 路径 /mnt/c/Users/x/a.xlsx。"""
    s = p.resolve().as_posix()                 # 'C:/Users/x/a.xlsx'
    m = re.match(r"^([A-Za-z]):/(.*)$", s)
    if m:
        return f"/mnt/{m.group(1).lower()}/{m.group(2)}"
    return s                                    # 已是 Linux 路径（在 WSL 里直接跑时）


def send_via_hermes(xlsx_path: Path, caption: str, target: str = HERMES_TARGET) -> bool:
    """
    调用 WSL 里的 Hermes Agent，把 Excel 作为文件附件推送到指定频道。
    用 MEDIA:<path> + [[as_document]] 让 Hermes 以"文件"形式发送（不压缩）。
    """
    if not HERMES_ENABLED:
        return False

    wsl_path = _to_wsl_path(xlsx_path)
    message = f"{caption}\n[[as_document]] MEDIA:{wsl_path}"
    cmd = ["wsl", "hermes", "send", "--to", target, message]

    print(f"\n  通过 Hermes 推送报表到 [{target}] ...")
    print(f"    文件(WSL路径): {wsl_path}")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=180)
        if r.returncode == 0:
            print("  ✓ 已通过 Hermes 推送（请到微信确认收到）")
            return True
        print(f"  ⚠ Hermes 返回非 0 (exit {r.returncode})")
        if r.stdout.strip():
            print(f"    stdout: {r.stdout.strip()[:500]}")
        if r.stderr.strip():
            print(f"    stderr: {r.stderr.strip()[:500]}")
    except FileNotFoundError:
        print("  ⚠ 未找到 wsl 命令：请在 Windows 上运行本脚本，且已安装 WSL")
    except subprocess.TimeoutExpired:
        print("  ⚠ Hermes 发送超时（180s）；确认 WSL 里 gateway 正在运行")
    except Exception as e:
        print(f"  ⚠ Hermes 发送异常: {e}")
    return False


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
        pages, alerts = crawl(page, base)
        page.close()
        browser.close()

    out = save_excel(shop, pages)
    print(f"\n✓ 完成！共 {len(pages)} 个页面")
    print(f"  Excel: {out}")

    # ── 通过 Hermes 推送到微信 ──
    caption = build_caption(shop, alerts)
    print("\n  推送消息预览:")
    for line in caption.splitlines():
        print(f"    {line}")
    send_via_hermes(out, caption)


if __name__ == "__main__":
    main()
