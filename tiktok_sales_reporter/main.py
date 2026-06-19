"""
TikTok 多店铺销售日报 - 主入口
用法:
  python main.py          # 立即运行一次
  python main.py --schedule   # 每天定时自动运行（需要保持程序运行）
"""

import argparse
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

# 添加父目录到 path，方便直接运行
sys.path.insert(0, str(Path(__file__).parent))

from scraper import run_all_shops
from report import generate_report

logger = logging.getLogger(__name__)


def run_once():
    """运行一次完整抓取并生成报表"""
    print(f"\n{'='*50}")
    print(f"TikTok 销售日报任务开始")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*50}\n")

    # 1. 抓取数据
    results = run_all_shops()

    # 2. 生成报表
    output_path = generate_report(results)

    # 3. 打印摘要
    success = [r for r in results if r["status"] == "success"]
    failed = [r for r in results if r["status"] != "success"]

    print(f"\n{'='*50}")
    print(f"任务完成！")
    print(f"  成功: {len(success)} 个店铺")
    if failed:
        print(f"  失败: {len(failed)} 个店铺")
        for r in failed:
            print(f"    - {r['shop_name']} ({r['country']}): {r.get('error', '未知错误')}")
    print(f"\n报表已保存至:")
    print(f"  {output_path}")
    print(f"{'='*50}\n")

    return output_path


def run_scheduled(run_hour: int = 9, run_minute: int = 0):
    """每天指定时间自动运行（保持程序常驻）"""
    print(f"定时模式启动，每天 {run_hour:02d}:{run_minute:02d} 自动运行")
    print("按 Ctrl+C 停止\n")

    while True:
        now = datetime.now()
        if now.hour == run_hour and now.minute == run_minute:
            try:
                run_once()
            except Exception as e:
                logger.error(f"任务执行失败: {e}")
            time.sleep(61)  # 避免同一分钟内重复触发
        else:
            next_run = now.replace(hour=run_hour, minute=run_minute, second=0)
            if next_run <= now:
                from datetime import timedelta
                next_run += timedelta(days=1)
            wait_seconds = (next_run - now).total_seconds()
            print(f"  下次运行: {next_run.strftime('%Y-%m-%d %H:%M')}  (等待 {int(wait_seconds/3600)}h {int((wait_seconds%3600)/60)}m)")
            time.sleep(60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TikTok 多店铺销售日报")
    parser.add_argument("--schedule", action="store_true", help="启用定时模式（每天自动运行）")
    parser.add_argument("--hour", type=int, default=9, help="定时运行的小时（24小时制，默认9）")
    parser.add_argument("--minute", type=int, default=0, help="定时运行的分钟（默认0）")
    args = parser.parse_args()

    if args.schedule:
        run_scheduled(run_hour=args.hour, run_minute=args.minute)
    else:
        run_once()
