"""
TikTok Shop 销售数据抓取模块
- 营业额页面：今日 GMV + 今日售出件数
- 回款页面：可提现金额
"""

import json
import re
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

def _screenshot(page, label: str, settings: dict):
    try:
        d = Path(settings.get("reports_dir", "reports")) / "screenshots"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{label}_{datetime.now().strftime('%H%M%S')}.png"
        page.screenshot(path=str(path))
        logger.info(f"  截图: {path.name}")
    except Exception:
        pass


# ─────────────────────────────────────────────
# 页面1：营业额（今日 GMV + 售出件数）
# ─────────────────────────────────────────────

def _scrape_analytics(page, shop: dict, settings: dict) -> tuple:
    """返回 (today_gmv, today_items_sold)"""
    country  = shop["country"].upper()
    base     = shop["seller_center_url"].rstrip("/")
    url      = f"{base}/compass/data-overview?shop_region={country}"
    sym      = CURRENCY_SYMBOL.get(country, "")

    logger.info(f"  [营业额] 访问 {url}")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except PlaywrightTimeout:
        logger.warning("  [营业额] 页面加载超时")
        return "N/A", "N/A"

    time.sleep(4)

    if any(k in page.url for k in ("login", "register", "passport")):
        logger.warning("  [营业额] 检测到登录页，请重新登录")
        return "需要登录", "需要登录"

    # ── 点击"Today"筛选按钮 ──
    _click_today(page)
    time.sleep(3)

    _screenshot(page, f"{country}_analytics", settings)

    # ── 提取数据 ──
    gmv   = _extract_gmv(page, sym)
    items = _extract_items_sold(page)

    logger.info(f"  [营业额] GMV={gmv}  售出={items}")
    return gmv, items


def _click_today(page):
    """尝试多种方式点击 Today 预设按钮"""
    # 先尝试直接可见的 Today 按钮
    candidates = [
        lambda: page.get_by_role("button", name=re.compile(r"^Today$", re.I)).first,
        lambda: page.get_by_text("Today", exact=True).first,
        lambda: page.locator("li").filter(has_text=re.compile(r"^Today$")).first,
        lambda: page.locator("span").filter(has_text=re.compile(r"^Today$")).first,
    ]
    for fn in candidates:
        try:
            loc = fn()
            if loc.is_visible(timeout=2000):
                loc.click()
                logger.info("  [Today] 点击成功")
                return
        except Exception:
            continue

    # 如果没找到，尝试先打开日期选择器再找
    try:
        date_inputs = [
            page.locator('[class*="date-picker"], [class*="DatePicker"], [class*="date-range"]').first,
            page.locator('input[placeholder*="date"], input[placeholder*="Date"]').first,
        ]
        for di in date_inputs:
            try:
                if di.is_visible(timeout=1500):
                    di.click()
                    time.sleep(1)
                    # 再找 Today
                    for fn in candidates:
                        try:
                            loc = fn()
                            if loc.is_visible(timeout=1500):
                                loc.click()
                                logger.info("  [Today] 打开日历后点击成功")
                                return
                        except Exception:
                            continue
                    break
            except Exception:
                continue
    except Exception:
        pass

    logger.warning("  [Today] 未能点击 Today 按钮，使用当前页面数据")


def _extract_gmv(page, sym: str) -> str:
    """从页面文本中提取 GMV 金额"""
    try:
        text = page.inner_text("body")
        # 找出所有货币金额
        pattern = rf'{re.escape(sym)}\s*([\d,\.]+)'
        matches = re.findall(pattern, text)
        if matches:
            # 取第一个出现的（通常是最显眼的 GMV 数值）
            return f"{sym} {matches[0]}"
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

def _scrape_finance(page, shop: dict, settings: dict) -> str:
    """返回 available_to_withdraw 字符串"""
    country = shop["country"].upper()
    base    = shop["seller_center_url"].rstrip("/")
    url     = f"{base}/finance/withdraw-new?shop_region={country}"
    sym     = CURRENCY_SYMBOL.get(country, "")

    logger.info(f"  [回款] 访问 {url}")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except PlaywrightTimeout:
        logger.warning("  [回款] 页面加载超时")
        return "N/A"

    time.sleep(3)

    if any(k in page.url for k in ("login", "register", "passport")):
        return "需要登录"

    _screenshot(page, f"{country}_finance", settings)

    try:
        text  = page.inner_text("body")
        lines = text.splitlines()

        # 找到含 "Available to withdraw" 的行，取其附近的金额
        for i, line in enumerate(lines):
            if "available to withdraw" in line.lower():
                search_block = "\n".join(lines[max(0, i-2): i+8])
                m = re.search(rf'{re.escape(sym)}\s*([\d,\.]+)', search_block)
                if m:
                    val = f"{sym} {m.group(1)}"
                    logger.info(f"  [回款] 可提现={val}")
                    return val

        # 兜底：直接全文搜货币金额（第一个）
        m = re.search(rf'{re.escape(sym)}\s*([\d,\.]+)', text)
        if m:
            return f"{sym} {m.group(1)}"

    except Exception as e:
        logger.warning(f"  [回款] 提取失败: {e}")

    return "N/A"


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
        "status":               "failed",
        "error":                "",
    }

    logger.info(f"\n{'─'*40}")
    logger.info(f"处理: {shop['name']} ({shop['country']})")

    try:
        profile_path  = Path(shop["chrome_profile_path"])
        user_data_dir = str(profile_path.parent)
        profile_dir   = profile_path.name

        with sync_playwright() as p:
            browser = p.chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                executable_path=settings["chrome_exe_path"],
                headless=settings.get("headless", False),
                viewport={"width": 1440, "height": 900},
                accept_downloads=True,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    f"--profile-directory={profile_dir}",
                ],
                ignore_default_args=["--enable-automation"],
                locale="en-US",
            )

            page = browser.new_page()
            page.set_default_timeout(settings.get("page_load_timeout", 30000))

            # 1. 营业额页面
            gmv, items = _scrape_analytics(page, shop, settings)
            result["today_gmv"]        = gmv
            result["today_items_sold"] = items

            # 2. 回款页面
            result["available_to_withdraw"] = _scrape_finance(page, shop, settings)

            if gmv != "需要登录":
                result["status"] = "success"
            else:
                result["error"] = "需要重新登录"

            browser.close()

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
