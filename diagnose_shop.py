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


# 判断"加载完成"的 JS：返回正文长度 + 当前可见的转圈/骨架元素数量
_PROBE_JS = """
() => {
  const vis = el => {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0';
  };
  let spin = 0;
  try {
    const sel = '[class*="loading"i],[class*="spin"i],[class*="skeleton"i],[aria-busy="true"]';
    spin = [...document.querySelectorAll(sel)].filter(vis).length;
  } catch (e) {}
  const t = (document.body && document.body.innerText)
    ? document.body.innerText.replace(/\\s+/g, '') : '';
  return { len: t.length, spin };
}
"""


def _probe(page):
    try:
        return page.evaluate(_PROBE_JS)
    except Exception:
        return {"len": 0, "spin": 999}


def wait_until_ready(page, max_wait=60, min_text_len=600):
    """
    真正等页面加载完（以【正文内容是否稳定】为准）：
    - 等 networkidle
    - 反复检测正文长度，直到【内容充足且连续稳定】
    - 转圈/骨架数量只作"加速参考"，不作硬性判定：
      TikTok 首页等页面存在常驻的 spin/loading/skeleton 类元素，
      若强制要求转圈=0，会把已经加载好的页面永远误判为"未加载"，
      导致每页都白等满 max_wait + 反复刷新。
    - 返回最终 (是否加载成功, 正文长度, 可见loading数)
    """
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass

    deadline = time.time() + max_wait
    last_len = -1
    stable = 0
    sig = _probe(page)
    while time.time() < deadline:
        sig = _probe(page)
        content_ok = sig["len"] >= min_text_len       # 内容已出现
        unchanged  = abs(sig["len"] - last_len) <= 30  # 正文基本不再变化
        last_len   = sig["len"]

        if content_ok and unchanged:
            need = 2 if sig["spin"] == 0 else 3        # 无转圈更快确认；有常驻转圈则多稳一会儿
            stable += 1
            if stable >= need:
                break
        else:
            stable = 0
        time.sleep(1.0)

    # 触发懒加载：滚到底再滚回顶部
    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(1.2)
        page.evaluate("window.scrollTo(0, 0)")
        time.sleep(1.0)
    except Exception:
        pass
    time.sleep(1.5)

    # 判定"已加载"以正文内容为准（常驻转圈不应否定一个内容完整的页面）
    loaded = sig["len"] >= min_text_len
    return loaded, sig["len"], sig["spin"]


def goto_and_wait(page, url, label, max_wait=60):
    """打开 URL 并等加载完；若加载后仍是空白页则刷新重试一次"""
    for attempt in range(2):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=25000)
        except PWTimeout:
            print(f"    ⚠ [{label}] 导航超时")
        loaded, length, spin = wait_until_ready(page, max_wait=max_wait)
        if loaded:
            return True, length, spin
        if attempt == 0:
            print(f"    ↻ [{label}] 仍未加载完（正文{length}字/转圈{spin}），刷新重试...")
            try:
                page.reload(wait_until="domcontentloaded", timeout=25000)
            except Exception:
                pass
    return False, length, spin


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
        loaded, length, spin = goto_and_wait(page, f"{base}/homepage", "首页")
        links = discover_links(page, base)
        report["discovered_links"] = links
        print(f"    {'✓' if loaded else '⚠ 未完全加载'}  发现 {len(links)} 个导航链接（正文{length}字）")
        report["pages"]["homepage"] = {
            "label": "首页概况", "url": page.url, "source": "直接",
            "loaded": loaded, "text_len": length,
            "error": page_has_error(page), "screenshot": screenshot(page, "homepage"),
        }

        # ── 2. 逐个访问关心的页面（用真实链接，等加载完再截图）──
        for key, pattern, fallback, label in WANTED:
            if key == "homepage":
                continue
            url, source = match_url(key, pattern, fallback, base, links)
            print(f"  [{label}] ({source}) {url}")

            loaded, length, spin = goto_and_wait(page, url, label)

            # analytics 页：加载完后再选 Last 30 days，并再次等稳定
            if key == "analytics":
                try:
                    btn = page.get_by_text(re.compile(r"last\s*30", re.I)).first
                    if btn.is_visible(timeout=2500):
                        btn.click()
                        print("    已选 Last 30 days")
                        loaded, length, spin = wait_until_ready(page)
                except Exception:
                    pass

            err = page_has_error(page)
            shot = screenshot(page, key)
            report["pages"][key] = {
                "label": label, "url": url, "final_url": page.url,
                "source": source, "loaded": loaded, "text_len": length,
                "visible_loading": spin, "error": err, "screenshot": shot,
            }
            if err:
                flag = "❌ 错误页(No matching route)"
            elif not loaded:
                flag = f"⚠ 未完全加载(正文{length}字/转圈{spin})"
            else:
                flag = "✓ 已加载"
            print(f"    {flag}  截图: {Path(shot).name if not shot.startswith('[') else shot}")

        page.close()
        browser.close()

    report_path = OUT_DIR / f"diagnosis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✓ 完成，报告: {report_path}")
    print("  请把【没有报错的】页面截图发我，我来做运营诊断")


if __name__ == "__main__":
    main()
