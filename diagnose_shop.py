"""
TikTok Shop 店铺诊断脚本 v2
- 连接已开启的 Chrome (port 9222)
- 先从左侧导航读取【真实】页面链接（不再猜 URL）
- 每个页面真正等到加载完成（骨架屏/转圈消失）再整页截图
- 输出 JSON 报告（含发现的真实链接）
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

# 我们关心的页面：用关键词去匹配导航里的真实链接
# (key, 匹配菜单文字的正则, 兜底URL路径, 中文标签)
WANTED = [
    ("homepage",   r"home",                          "/homepage",                      "首页概况"),
    ("analytics",  r"data|analytic|compass|business", "/compass/data-overview",        "营业额/GMV"),
    ("orders",     r"order|manage order",            "/order",                          "订单管理"),
    ("products",   r"manage product|product list|all product", "/product/list",         "商品列表"),
    ("acct_health",r"account health|health",         "/account-health",                "账号健康"),
    ("reviews",    r"review|rating",                 "/review",                         "评价管理"),
    ("returns",    r"return|refund|after.?sale|reverse", "/order/return-refund",        "退款退货"),
    ("finance",    r"withdraw|finance|balance|payment", "/finance/withdraw-new",        "回款/可提现"),
    ("promotions", r"promotion|campaign|deal|flash",  "/promotion",                    "促销活动"),
]


def port_open(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


def wait_until_ready(page, max_wait=35):
    """真正等页面加载完：networkidle + 骨架屏/转圈消失 + 内容稳定"""
    # 1. 等网络基本空闲（TikTok 有长连接，超时无所谓）
    try:
        page.wait_for_load_state("networkidle", timeout=12000)
    except Exception:
        pass

    # 2. 轮询：骨架屏 / loading / 转圈 是否还可见
    loading_selectors = [
        '[class*="skeleton"]:visible',
        '[class*="Skeleton"]:visible',
        '[class*="loading"]:visible',
        '[class*="Loading"]:visible',
        '[class*="spinner"]:visible',
        '[class*="spin"]:visible',
        '[aria-busy="true"]:visible',
        'svg[class*="loading"]:visible',
    ]
    deadline = time.time() + max_wait
    stable = 0
    while time.time() < deadline:
        loading = 0
        for sel in loading_selectors:
            try:
                loading += page.locator(sel).count()
            except Exception:
                pass
        if loading == 0:
            stable += 1
            if stable >= 2:          # 连续 2 次都没有 loading 才算稳
                break
        else:
            stable = 0
        time.sleep(0.8)

    # 3. 触发懒加载：滚到底再滚回顶部
    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(1.2)
        page.evaluate("window.scrollTo(0, 0)")
        time.sleep(1.0)
    except Exception:
        pass

    # 4. 额外缓冲，让图表渲染稳定
    time.sleep(1.5)


def screenshot(page, name):
    path = OUT_DIR / f"{name}_{datetime.now().strftime('%H%M%S')}.png"
    try:
        page.screenshot(path=str(path), full_page=True)
        return str(path)
    except Exception as e:
        return f"[截图失败: {e}]"


def discover_links(page, base):
    """从当前页面（首页）抓取所有指向 seller center 的导航链接"""
    try:
        raw = page.eval_on_selector_all(
            "a[href]",
            "els => els.map(e => ({href: e.href, text: (e.innerText||'').trim()}))",
        )
    except Exception:
        raw = []
    host = base.split("//")[-1]
    links = []
    seen = set()
    for it in raw:
        href = it.get("href", "")
        text = it.get("text", "")
        if host in href and href not in seen:
            seen.add(href)
            links.append({"href": href, "text": text})
    return links


def match_url(key, pattern, fallback_path, base, links):
    """优先用导航里匹配到的真实链接，匹配不到才用兜底路径"""
    rgx = re.compile(pattern, re.I)
    for lk in links:
        hay = f"{lk['text']} {lk['href']}"
        if rgx.search(hay):
            return lk["href"], "导航发现"
    return f"{base}{fallback_path}?shop_region=MY", "兜底猜测"


def page_has_error(page):
    """检测 No matching route / 404 等错误页"""
    try:
        txt = page.inner_text("body")[:500].lower()
        for kw in ("no matching route", "page not found", "404", "出错了"):
            if kw in txt:
                return True
    except Exception:
        pass
    return False


def main():
    if not CONFIG_PATH.exists():
        print("找不到 shops.json"); return

    config   = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    shop     = next((s for s in config["shops"] if s.get("enabled", True)), None)
    port     = shop.get("debug_port", 9222)
    base     = shop["seller_center_url"].rstrip("/")

    if not port_open(port):
        print(f"❌ 端口 {port} 未开启，请先打开 Chrome"); return

    print(f"✓ 连接 Chrome (port={port}) — {shop['name']}")
    print(f"  截图目录: {OUT_DIR}\n")

    report = {"shop": shop["name"], "country": shop["country"],
              "time": datetime.now().isoformat(), "discovered_links": [], "pages": {}}

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=10000)
        ctx     = browser.contexts[0] if browser.contexts else browser.new_context()
        page    = ctx.new_page()
        page.set_default_timeout(20000)

        # ── 1. 先开首页，读取真实导航链接 ──
        print("  [首页] 加载并读取导航菜单...")
        try:
            page.goto(f"{base}/homepage", wait_until="domcontentloaded", timeout=25000)
        except PWTimeout:
            pass
        wait_until_ready(page)
        links = discover_links(page, base)
        report["discovered_links"] = links
        print(f"    发现 {len(links)} 个导航链接")
        # 首页截图
        report["pages"]["homepage"] = {
            "label": "首页概况", "url": page.url, "source": "直接",
            "error": page_has_error(page), "screenshot": screenshot(page, "homepage"),
        }

        # ── 2. 逐个访问关心的页面（用真实链接，等加载完再截图）──
        for key, pattern, fallback, label in WANTED:
            if key == "homepage":
                continue
            url, source = match_url(key, pattern, fallback, base, links)
            print(f"  [{label}] ({source}) {url}")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=25000)
            except PWTimeout:
                print("    ⚠ 加载超时")

            # analytics 页先选 Last 30 days
            if key == "analytics":
                wait_until_ready(page)
                try:
                    btn = page.get_by_text(re.compile(r"last\s*30", re.I)).first
                    if btn.is_visible(timeout=2500):
                        btn.click()
                        print("    已选 Last 30 days")
                except Exception:
                    pass

            wait_until_ready(page)
            err = page_has_error(page)
            shot = screenshot(page, key)
            report["pages"][key] = {
                "label": label, "url": url, "final_url": page.url,
                "source": source, "error": err, "screenshot": shot,
            }
            flag = "❌ 错误页(No matching route)" if err else "✓"
            print(f"    {flag}  截图: {Path(shot).name if not shot.startswith('[') else shot}")

        page.close()
        browser.close()

    report_path = OUT_DIR / f"diagnosis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✓ 完成，报告: {report_path}")
    print("  请把【没有报错的】页面截图发我，我来做运营诊断")


if __name__ == "__main__":
    main()
