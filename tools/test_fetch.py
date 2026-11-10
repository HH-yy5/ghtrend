"""抓取链路自检：不启动界面，直接跑一次 fetch_pool，验证修复是否生效。

用法：python tools/test_fetch.py [数据目录]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ghtrend as gt  # noqa: E402


def main() -> int:
    data_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "_test"
    data_dir.mkdir(parents=True, exist_ok=True)
    gt.set_data_dir(data_dir)

    cfg = gt.load_config()
    gh = cfg["github"]
    print(f"[配置] core_pages={gh.get('core_pages')} new_pages={gh.get('new_pages')} "
          f"timeout={gh.get('timeout')} proxy={gh.get('proxy') or '(无)'} "
          f"token={'有' if gh.get('token') else '无'}")

    t0 = time.time()
    try:
        repos = gt.fetch_pool(cfg, show_progress=True)
    except Exception as e:
        print(f"\n[失败] {type(e).__name__}: {e}")
        return 1

    cost = time.time() - t0
    print(f"\n[成功] 用时 {cost:.1f}s，候选池 {len(repos)} 个仓库")
    for r in repos[:5]:
        print(f"   {r['stars']:>7,}  {r['full_name']}")

    path = gt.save_snapshot(data_dir, gt.now_local().date().isoformat(), repos)
    print(f"[快照] {path}")

    cached = list((data_dir / "page_cache").glob("*.json")) if (data_dir / "page_cache").is_dir() else []
    print(f"[页缓存] {len(cached)} 个文件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
