"""
TikTok Shop 销售数据抓取模块
使用已登录的 Chrome Profile，无需重复登录
"""

import json
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

# TikTok 卖家后台 Dashboard 路径（各国相同）
DASHBOARD_PATHS = [
    "/compass/shop",
    "/portal/product",
    "/",
]

# 今日数据选择器（按优先级尝试）
# TikTok Seller Center 的 dashboard 通常用这些 class/text
GMV_SELECTORS = [
    # 新版 UI
    '[data-testid="today-gmv-value"]',
    '[data-testid="gmv-value"]',
    # 通用数字卡片
    '.overview-card__value',
    '.metric-value',
    '.data-card__value',
    # 文字匹配兜底（通过标签 + 相邻数值）
    'xpath=//span[contains(text(),"GMV") or contains(text(),"销售额")]/following-sibling::*[1]',
    'xpath=//div[contains(text(),"GMV") or contains(text(),"Sales")]/following::*[contains(@class,"value") or contains(@class,"number")][1]',
]

ORDER_SELECTORS = [
    '[data-testid="today-order-value"]',
    '[data-testid="order-count"]',
    '.overview-card__value',
    'xpath=//span[contains(text(),"订单") or contains(text(),"Order")]/following-sibling::*[1]',
    'xpath=//div[contains(text(),"Order") or contains(text(),"订单")]/following::*[contains(@class,"value") or contains(@class,"number")][1]',
]


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def try_get_text(page, selectors: list, label: str) -> str:
    """依次尝试选择器，返回第一个找到的文本，失败返回 'N/A'"""
    for sel in selectors:
        try:
            if sel.startswith("xpath="):
                locator = page.locator(sel)
            else:
                locator = page.locator(sel).first

            locator.wait_for(timeout=3000)
            text = locator.text_content(timeout=3000)
            if text and text.strip():
                return text.strip()
        except Exception:
            continue

    logger.warning(f"  无法定位 {label}，尝试截图留存...")
    return "N/A"


def scrape_shop(shop: dict, settings: dict) -> dict:
    """抓取单个店铺的今日数据，返回数据字典"""
    result = {
        "shop_name": shop["name"],
        "country": shop["country"],
        "date": datetime.now().strftime("%Y-%m-%d"),
        "gmv": "N/A",
        "orders": "N/A",
        "currency": _get_currency(shop["country"]),
        "status": "failed",
        "error": "",
    }

    logger.info(f"正在处理: {shop['name']} ({shop['country']})")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch_persistent_context(
                user_data_dir=shop["chrome_profile_path"],
                executable_path=settings["chrome_exe_path"],
                headless=settings.get("headless", False),
                viewport=None,
                accept_downloads=True,
                args=[
                    "--no-sandbox",
                    "--start-maximized",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                ],
                ignore_default_args=["--enable-automation"],
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
            )

            page = browser.new_page()
            page.set_default_timeout(settings.get("page_load_timeout", 30000))

            # 尝试访问 Dashboard
            dashboard_url = None
            for path in DASHBOARD_PATHS:
                url = shop["seller_center_url"].rstrip("/") + path
                try:
                    logger.info(f"  访问: {url}")
                    page.goto(url, wait_until="domcontentloaded")
                    time.sleep(settings.get("wait_after_load", 5000) / 1000)

                    # 检查是否需要重新登录
                    current_url = page.url
                    if "login" in current_url or "passport" in current_url:
                        logger.warning(f"  检测到登录页面，该 Profile 可能未登录: {shop['name']}")
                        result["error"] = "需要重新登录，请手动在对应 Chrome Profile 中登录"
                        browser.close()
                        return result

                    dashboard_url = url
                    break
                except PlaywrightTimeout:
                    logger.warning(f"  超时: {url}")
                    continue

            if not dashboard_url:
                result["error"] = "无法加载卖家后台页面"
                browser.close()
                return result

            # 截图留存（便于调试）
            screenshot_dir = Path(settings.get("reports_dir", "reports")) / "screenshots"
            screenshot_dir.mkdir(parents=True, exist_ok=True)
            screenshot_path = screenshot_dir / f"{shop['country']}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            page.screenshot(path=str(screenshot_path))
            logger.info(f"  截图已保存: {screenshot_path}")

            # 抓取 GMV（今日销售额）
            gmv = try_get_text(page, GMV_SELECTORS, "GMV")
            orders = try_get_text(page, ORDER_SELECTORS, "订单数")

            # 如果通用选择器失败，尝试从页面文本中提取数字
            if gmv == "N/A" or orders == "N/A":
                gmv, orders = _fallback_extract(page, shop["country"])

            result["gmv"] = gmv
            result["orders"] = orders
            result["status"] = "success"
            logger.info(f"  成功 - GMV: {gmv}, 订单数: {orders}")

            browser.close()

    except Exception as e:
        result["error"] = str(e)
        logger.error(f"  抓取失败 [{shop['name']}]: {e}")

    return result


def _fallback_extract(page, country: str) -> tuple:
    """
    兜底：从页面完整文本中用规则提取数字。
    TikTok 各国后台的货币符号不同，以此区分 GMV。
    """
    currency_map = {"MY": "RM", "TH": "฿", "VN": "₫", "PH": "₱"}
    currency = currency_map.get(country, "")

    try:
        page_text = page.inner_text("body")
        lines = [l.strip() for l in page_text.splitlines() if l.strip()]

        gmv = "N/A"
        orders = "N/A"

        for i, line in enumerate(lines):
            # 查找含货币符号的行（GMV）
            if currency and currency in line and gmv == "N/A":
                import re
                nums = re.findall(r"[\d,\.]+", line)
                if nums:
                    gmv = f"{currency} {nums[0]}"

            # 查找"Today's Orders"或"今日订单"附近的纯数字
            if ("order" in line.lower() or "订单" in line) and orders == "N/A":
                for j in range(i + 1, min(i + 5, len(lines))):
                    import re
                    if re.match(r"^\d+$", lines[j].replace(",", "")):
                        orders = lines[j]
                        break

        return gmv, orders
    except Exception:
        return "N/A", "N/A"


def _get_currency(country: str) -> str:
    return {"MY": "MYR", "TH": "THB", "VN": "VND", "PH": "PHP"}.get(country, "")


def run_all_shops() -> list:
    """运行所有启用的店铺，返回结果列表"""
    config = load_config()
    settings = config["settings"]
    shops = [s for s in config["shops"] if s.get("enabled", True)]

    logger.info(f"开始抓取 {len(shops)} 个店铺的销售数据...")
    results = []
    for shop in shops:
        result = scrape_shop(shop, settings)
        results.append(result)
        time.sleep(2)  # 店铺之间稍作间隔

    return results
