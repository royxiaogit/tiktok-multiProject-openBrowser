"""
TikTok Shop 销售数据抓取模块
- 营业额页面：今日 GMV + 今日售出件数
- 回款页面：可提现金额
"""

import json
import re
import socket
import subprocess
import time
import logging
from datetime import datetime
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent / "config" / "shops.json"

CURRENCY_SYMBOL = {"MY": "RM", "TH": "฿", "VN": "₫", "PH": "₱"}
CURRENCY_CODE   = {"MY": "MYR", "TH": "THB", "VN": "VND", "PH": "PHP"}


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
        return json.load(f)


# ─────────────────────────────────────────────
# 截图辅助
# ─────────────────────────────────────────────

def _screenshot(page, label: str, settings: dict, full_page: bool = True) -> str:
    """整页截图，返回保存路径（失败返回空串）"""
    try:
        d = Path(__file__).parent / settings.get("reports_dir", "reports") / "screenshots"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{label}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        # 滚到顶部再截图，保证整页内容完整
        try:
            page.evaluate("window.scrollTo(0, 0)")
            time.sleep(0.5)
        except Exception:
            pass
        page.screenshot(path=str(path), full_page=full_page)
        logger.info(f"  截图: {path.name}")
        return str(path)
    except Exception as e:
        logger.warning(f"  截图失败: {e}")
        return ""


# ─────────────────────────────────────────────
# 登录检测 / 等待登录
# ─────────────────────────────────────────────

def _is_login_page(url: str) -> bool:
    return any(k in url for k in ("login", "register", "passport"))


def _wait_for_login(page, shop: dict, settings: dict) -> bool:
    """
    打开店铺首页，确认是否已登录。
    若未登录：提示用户在窗口里登录，并轮询等待（最多 login_wait_seconds 秒），
    用户登录完成后自动继续。返回 True=已登录，False=超时仍未登录。
    """
    base   = shop["seller_center_url"].rstrip("/")
    wait_s = settings.get("login_wait_seconds", 180)

    try:
        page.goto(base + "/homepage", wait_until="domcontentloaded", timeout=30000)
    except PlaywrightTimeout:
        pass
    time.sleep(3)

    if not _is_login_page(page.url):
        return True

    logger.warning(
        f"  ⚠ 未登录！请在弹出的 Chrome 窗口里登录 TikTok（{shop['name']}）。\n"
        f"    登录成功后脚本会自动继续，最多等待 {wait_s} 秒..."
    )

    waited = 0
    while waited < wait_s:
        time.sleep(5)
        waited += 5
        try:
            page.reload(wait_until="domcontentloaded", timeout=20000)
        except Exception:
            pass
        if not _is_login_page(page.url):
            logger.info("  ✓ 检测到已登录，继续抓取")
            time.sleep(2)
            return True

    logger.warning("  ✗ 等待登录超时，跳过该店铺")
    return False


# ─────────────────────────────────────────────
# 页面1：营业额（今日 GMV + 售出件数）
# ─────────────────────────────────────────────

def _scrape_analytics(page, shop: dict, settings: dict) -> tuple:
    """返回 (gmv, items_sold, screenshot_path)"""
    country  = shop["country"].upper()
    base     = shop["seller_center_url"].rstrip("/")
    url      = f"{base}/compass/data-overview?shop_region={country}"
    sym      = CURRENCY_SYMBOL.get(country, "")

    logger.info(f"  [营业额] 访问 {url}")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except PlaywrightTimeout:
        logger.warning("  [营业额] 页面加载超时")
        return "N/A", "N/A", ""

    time.sleep(4)

    if any(k in page.url for k in ("login", "register", "passport")):
        logger.warning("  [营业额] 检测到登录页，请重新登录")
        return "需要登录", "需要登录", ""

    # ── 选"Last 30 days"筛选 ──
    _select_last30days(page)
    time.sleep(3)

    shot = _screenshot(page, f"{country}_analytics", settings)

    # ── 提取数据 ──
    gmv   = _extract_gmv(page, sym)
    items = _extract_items_sold(page)

    logger.info(f"  [营业额] GMV={gmv}  售出={items}")
    return gmv, items, shot


def _select_last30days(page):
    """点击日期筛选里的 Last 30 days 选项"""
    target = re.compile(r"last\s*30", re.I)

    presets = [
        lambda: page.get_by_role("button", name=target).first,
        lambda: page.get_by_role("option", name=target).first,
        lambda: page.locator("li").filter(has_text=target).first,
        lambda: page.locator("span").filter(has_text=target).first,
        lambda: page.get_by_text(target).first,
    ]

    # 直接尝试（下拉可能已展开）
    for fn in presets:
        try:
            loc = fn()
            if loc.is_visible(timeout=1000):
                loc.click()
                logger.info("  [Last30d] 直接点击成功")
                return
        except Exception:
            continue

    # 先点开日期选择器
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
            opener.click()
            time.sleep(1)
            for fn in presets:
                try:
                    loc = fn()
                    if loc.is_visible(timeout=1000):
                        loc.click()
                        logger.info("  [Last30d] 打开选择器后点击成功")
                        return
                except Exception:
                    continue
            break
        except Exception:
            continue

    logger.warning("  [Last30d] 未能点击 Last 30 days，使用当前页面数据")


def _extract_gmv(page, sym: str) -> str:
    """从页面文本中提取 GMV 金额（含小数，页面可能把小数拆成单独元素）"""
    try:
        text = page.inner_text("body")
        # 整数部分 + 可选小数部分（小数可能被换行/空格隔开，如 "RM313\n.20"）
        pattern = rf'{re.escape(sym)}\s*([\d,]+)\s*([.．]\s*\d+)?'
        m = re.search(pattern, text)
        if m:
            intpart = m.group(1).replace(" ", "")
            dec = m.group(2)
            if dec:
                dec = dec.replace(" ", "").replace("．", ".")
                return f"{sym} {intpart}{dec}"
            return f"{sym} {intpart}"
    except Exception as e:
        logger.warning(f"  GMV 提取失败: {e}")
    return "N/A"


def _extract_items_sold(page) -> str:
    """从页面文本中提取售出件数"""
    try:
        text = page.inner_text("body")
        # 匹配 "X Items sold" 或单独的数字在 "Items sold" 附近
        patterns = [
            r'([\d,]+)\s*[Ii]tems?\s*sold',
            r'[Ii]tems?\s*sold\s*[\n\r\s]*([\d,]+)',
            r'[Ii]tems?\s*[Ss]old.*?([\d,]+)',
        ]
        for pat in patterns:
            m = re.search(pat, text)
            if m:
                return m.group(1).strip()
    except Exception as e:
        logger.warning(f"  售出件数提取失败: {e}")
    return "N/A"


# ─────────────────────────────────────────────
# 页面2：回款（可提现金额）
# ─────────────────────────────────────────────

def _scrape_finance(page, shop: dict, settings: dict) -> tuple:
    """返回 (available_to_withdraw, screenshot_path)"""
    country = shop["country"].upper()
    base    = shop["seller_center_url"].rstrip("/")
    url     = f"{base}/finance/withdraw-new?shop_region={country}"
    sym     = CURRENCY_SYMBOL.get(country, "")

    logger.info(f"  [回款] 访问 {url}")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except PlaywrightTimeout:
        logger.warning("  [回款] 页面加载超时")
        return "N/A", ""

    time.sleep(3)

    if any(k in page.url for k in ("login", "register", "passport")):
        return "需要登录", ""

    shot = _screenshot(page, f"{country}_finance", settings)

    try:
        text  = page.inner_text("body")
        lines = text.splitlines()

        # 找到含 "Available to withdraw" 的行，取其附近的金额
        for i, line in enumerate(lines):
            if "available to withdraw" in line.lower():
                search_block = "\n".join(lines[max(0, i-2): i+8])
                m = re.search(rf'{re.escape(sym)}\s*([\d,]+(?:[.．]\d+)?)', search_block)
                if m:
                    val = f"{sym} {m.group(1)}"
                    logger.info(f"  [回款] 可提现={val}")
                    return val, shot

        # 兜底：直接全文搜货币金额（第一个）
        m = re.search(rf'{re.escape(sym)}\s*([\d,]+(?:[.．]\d+)?)', text)
        if m:
            return f"{sym} {m.group(1)}", shot

    except Exception as e:
        logger.warning(f"  [回款] 提取失败: {e}")

    return "N/A", shot


# ─────────────────────────────────────────────
# Chrome 自动检测 / 启动
# ─────────────────────────────────────────────

def _port_open(port: int) -> bool:
    """检查本地端口是否有进程在监听"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _ensure_chrome(shop: dict, settings: dict) -> int:
    """
    确保对应店铺的 Chrome 已在 debug_port 上运行并监听 CDP。
    - 如果已经在跑 → 直接返回端口，完全不碰其他 Chrome 窗口
    - 如果没在跑 → 自动启动一个新 Chrome 窗口（使用 chrome_profile_path，
      与用户其他 Chrome 实例完全独立，不会干扰）
    """
    port = shop.get("debug_port", settings.get("debug_port", 9222))

    if _port_open(port):
        logger.info(f"  Chrome 已在端口 {port} 运行，直接连接")
        return port

    # 自动启动 Chrome（新窗口，独立 user-data-dir，不影响任何现有窗口）
    profile_path  = Path(shop["chrome_profile_path"])
    user_data_dir = str(profile_path.parent)
    profile_dir   = profile_path.name

    logger.info(f"  端口 {port} 未监听，自动启动 Chrome（profile={profile_dir}）...")
    subprocess.Popen([
        settings["chrome_exe_path"],
        f"--user-data-dir={user_data_dir}",
        f"--profile-directory={profile_dir}",
        f"--remote-debugging-port={port}",
        "--no-first-run",
        "--no-default-browser-check",
        "--no-restore-session-state",
        shop["seller_center_url"],
    ])

    # 等待 Chrome 就绪（最多 20 秒）
    for _ in range(20):
        time.sleep(1)
        if _port_open(port):
            logger.info(f"  Chrome 就绪（端口 {port}）")
            time.sleep(2)   # 等页面基本加载
            return port

    raise RuntimeError(
        f"Chrome 在 {port} 端口启动超时。\n"
        f"请手动运行: python tiktok_sales_reporter\\launch_browser.py"
    )


# ─────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────

def scrape_shop(shop: dict, settings: dict) -> dict:
    result = {
        "shop_name":            shop["name"],
        "country":              shop["country"],
        "date":                 datetime.now().strftime("%Y-%m-%d"),
        "today_gmv":            "N/A",
        "today_items_sold":     "N/A",
        "available_to_withdraw":"N/A",
        "currency":             CURRENCY_CODE.get(shop["country"].upper(), ""),
        "analytics_screenshot": "",
        "finance_screenshot":   "",
        "status":               "failed",
        "error":                "",
    }

    logger.info(f"\n{'─'*40}")
    logger.info(f"处理: {shop['name']} ({shop['country']})")

    try:
        # 自动检测/启动 Chrome（不会关闭任何已有的 Chrome 窗口）
        port = _ensure_chrome(shop, settings)

        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(
                f"http://127.0.0.1:{port}", timeout=15000
            )

            # 复用已登录的 context，不新建 profile
            context = browser.contexts[0] if browser.contexts else browser.new_context()

            # 只开一个新标签，抓完就关，保留你所有其他标签
            page = context.new_page()
            page.set_default_timeout(settings.get("page_load_timeout", 30000))

            try:
                # 0. 先确认登录（未登录则等用户在窗口里登录完再继续）
                if not _wait_for_login(page, shop, settings):
                    result["today_gmv"]             = "需要登录"
                    result["today_items_sold"]      = "需要登录"
                    result["available_to_withdraw"] = "需要登录"
                    result["error"]                 = "需要重新登录"
                    return result

                # 1. 营业额页面
                gmv, items, a_shot = _scrape_analytics(page, shop, settings)
                result["today_gmv"]            = gmv
                result["today_items_sold"]     = items
                result["analytics_screenshot"] = a_shot

                # 2. 回款页面
                withdraw, f_shot = _scrape_finance(page, shop, settings)
                result["available_to_withdraw"] = withdraw
                result["finance_screenshot"]    = f_shot

                if gmv != "需要登录":
                    result["status"] = "success"
                else:
                    result["error"] = "需要重新登录"
            finally:
                try:
                    page.close()   # 只关新标签
                except Exception:
                    pass

            browser.close()   # 断开 CDP 连接，不关 Chrome

    except Exception as e:
        result["error"] = str(e)[:80]
        logger.error(f"抓取失败 [{shop['name']}]: {e}")

    return result


def run_all_shops() -> list:
    config  = load_config()
    settings = config["settings"]
    shops   = [s for s in config["shops"] if s.get("enabled", True)]

    logger.info(f"开始抓取 {len(shops)} 个店铺的销售数据...")
    results = []
    for shop in shops:
        result = scrape_shop(shop, settings)
        results.append(result)
        time.sleep(2)

    return results
