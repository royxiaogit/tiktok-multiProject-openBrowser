"""
TikTok Shop 店铺诊断脚本
连接已开启的 Chrome (port 9222)，巡查关键页面并截图，生成诊断报告。
"""

import json
import re
import time
import socket
from datetime import datetime
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

CONFIG_PATH = Path("tiktok_sales_reporter/config/shops.json")
OUT_DIR = Path("tiktok_sales_reporter/reports/diagnosis")
OUT_DIR.mkdir(parents=True, exist_ok=True)

PAGES = [
    ("homepage",   "{base}/homepage",                                                  "首页概况"),
    ("analytics",  "{base}/compass/data-overview?shop_region=MY",                     "营业额/GMV"),
    ("orders",     "{base}/order/list?shop_region=MY",                                 "订单列表"),
    ("products",   "{base}/product/list?shop_region=MY",                               "商品列表"),
    ("inventory",  "{base}/product/manage-inventory?shop_region=MY",                   "库存管理"),
    ("finance",    "{base}/finance/withdraw-new?shop_region=MY",                       "回款/可提现"),
    ("acct_health","{base}/account-health?shop_region=MY",                             "账号健康"),
    ("reviews",    "{base}/review/list?shop_region=MY",                                "评价管理"),
    ("promotions", "{base}/promotion/activity?shop_region=MY",                         "促销活动"),
    ("returns",    "{base}/order/return-refund?shop_region=MY",                        "退款退货"),
]


def port_open(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


def wait_load(page, extra=2):
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    time.sleep(extra)


def screenshot(page, name):
    path = OUT_DIR / f"{name}_{datetime.now().strftime('%H%M%S')}.png"
    try:
        page.evaluate("window.scrollTo(0,0)")
        time.sleep(0.5)
        page.screenshot(path=str(path), full_page=True)
        return str(path)
    except Exception as e:
        return f"[截图失败: {e}]"


def extract_text(page):
    try:
        return page.inner_text("body")
    except Exception:
        return ""


def find_numbers(text, *keywords):
    """在文本中 keywords 附近找数字"""
    results = {}
    lines = text.splitlines()
    for kw in keywords:
        for i, line in enumerate(lines):
            if kw.lower() in line.lower():
                block = " ".join(lines[max(0, i-1):i+4])
                nums = re.findall(r"[\d,]+(?:\.\d+)?", block)
                if nums:
                    results[kw] = nums[0]
                break
    return results


def main():
    if not CONFIG_PATH.exists():
        print("找不到 shops.json，请先配置")
        return

    config   = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    shop     = next((s for s in config["shops"] if s.get("enabled", True)), None)
    settings = config["settings"]
    port     = shop.get("debug_port", 9222)
    base     = shop["seller_center_url"].rstrip("/")

    if not port_open(port):
        print(f"❌ 端口 {port} 未开启，请先打开 Chrome（运行 launch_browser.py）")
        return

    print(f"✓ 连接到 Chrome (port={port}) — {shop['name']}")
    print(f"  开始巡查各页面，截图保存至: {OUT_DIR}\n")

    report = {
        "shop":    shop["name"],
        "country": shop["country"],
        "time":    datetime.now().isoformat(),
        "pages":   {},
    }

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=10000)
        ctx     = browser.contexts[0] if browser.contexts else browser.new_context()
        page    = ctx.new_page()
        page.set_default_timeout(20000)

        for key, url_tpl, label in PAGES:
            url = url_tpl.format(base=base)
            print(f"  [{label}] {url}")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=20000)
            except PWTimeout:
                print(f"    ⚠ 加载超时")

            wait_load(page)

            current_url = page.url
            redirected  = "login" in current_url or "passport" in current_url
            text        = extract_text(page)
            shot        = screenshot(page, key)

            # 针对各页面提取关键数字
            data = {}
            if key == "analytics":
                data = find_numbers(text, "GMV", "Items sold", "Orders", "Visitors", "Conversion")
                # 选 last 30 days
                try:
                    btn = page.get_by_text(re.compile(r"last\s*30", re.I)).first
                    if btn.is_visible(timeout=2000):
                        btn.click()
                        time.sleep(3)
                        text = extract_text(page)
                        data = find_numbers(text, "GMV", "Items sold", "Orders", "Visitors", "Conversion")
                        shot = screenshot(page, key + "_30d")
                except Exception:
                    pass

            elif key == "orders":
                data = find_numbers(text, "To ship", "Shipped", "Completed", "Cancelled", "Return")

            elif key == "products":
                data = find_numbers(text, "Active", "In Stock", "Out of Stock", "Inactive")

            elif key == "inventory":
                # 查找库存为0的商品
                low_stock = len(re.findall(r'\b0\b', text))
                data = {"zero_stock_mentions": str(low_stock)}
                data.update(find_numbers(text, "Out of Stock", "Low Stock"))

            elif key == "finance":
                data = find_numbers(text, "Available", "Pending", "Released", "Total")

            elif key == "acct_health":
                data = find_numbers(text, "Score", "Violation", "Warning", "Point")

            elif key == "reviews":
                data = find_numbers(text, "Rating", "Reviews", "star", "Replied", "Pending")

            elif key == "returns":
                data = find_numbers(text, "Return", "Refund", "Pending", "Processing")

            report["pages"][key] = {
                "label":      label,
                "url":        url,
                "final_url":  current_url,
                "redirected": redirected,
                "data":       data,
                "screenshot": shot,
            }

            status = "🔒 未登录" if redirected else "✓"
            print(f"    {status}  数据: {data}  截图: {Path(shot).name if shot and not shot.startswith('[') else shot}")

        page.close()
        browser.close()

    # 保存 JSON 报告
    report_path = OUT_DIR / f"diagnosis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✓ 诊断完成，报告: {report_path}")
    print(f"  截图目录: {OUT_DIR}")
    return report


if __name__ == "__main__":
    main()
