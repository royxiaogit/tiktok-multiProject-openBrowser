"""
打开各店铺对应的 Chrome（带远程调试端口），供 main.py 通过 CDP 连接抓数据。

用法：
    python tiktok_sales_reporter\\launch_browser.py

- 每个 enabled 的店铺会打开一个独立的 Chrome 窗口
- 第一次打开后请在窗口里登录 TikTok，登录后【保持窗口开启】
- 之后每天只要这些窗口还开着、还在登录态，直接运行 main.py 即可抓数据
- 抓数据时脚本只会新开一个标签，抓完自动关掉，不会动你的登录标签
"""

import json
import subprocess
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "config" / "shops.json"


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def main():
    config   = load_config()
    settings = config["settings"]
    chrome   = settings["chrome_exe_path"]

    shops = [s for s in config["shops"] if s.get("enabled", True)]
    if not shops:
        print("没有启用的店铺（shops.json 里 enabled 都为 false）")
        return

    print(f"准备打开 {len(shops)} 个 Chrome 窗口...\n")

    for shop in shops:
        profile_path  = Path(shop["chrome_profile_path"])
        user_data_dir = str(profile_path.parent)
        profile_dir   = profile_path.name
        port          = shop.get("debug_port", settings.get("debug_port", 9222))
        url           = shop["seller_center_url"]

        args = [
            chrome,
            f"--user-data-dir={user_data_dir}",
            f"--profile-directory={profile_dir}",
            f"--remote-debugging-port={port}",
            "--no-first-run",
            "--no-default-browser-check",
            "--no-restore-session-state",
            "--restore-last-session=false",
            url,
        ]
        subprocess.Popen(args)
        print(f"  已打开 [{shop['name']}]  端口={port}  profile={profile_dir}")

    print(
        "\n下一步：\n"
        "  1. 在每个窗口里登录对应的 TikTok 店铺（已登录的可跳过）\n"
        "  2. 登录后【保持窗口开启】，不要关闭\n"
        "  3. 运行: python tiktok_sales_reporter\\main.py\n"
    )


if __name__ == "__main__":
    main()
