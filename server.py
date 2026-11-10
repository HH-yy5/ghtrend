#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GHTrend 桌面版 · 本地服务
================================
把 CLI 引擎包一层 HTTP，配一个 Web UI，用 Edge/Chrome 的应用模式窗口打开，
看起来就是一个桌面应用（没有地址栏、没有标签页）。

设计要点
--------
1. 只监听 127.0.0.1，不对外暴露；静态文件走白名单，杜绝路径穿越。
2. 业务逻辑零重写 —— 全部复用 ghtrend.py 的 produce / enrich_summaries / fetch_pool。
3. SSE（Server-Sent Events）现在就落地：
     · /api/events        长连接事件流（心跳 + 进度 + 数据更新广播）
     · /api/refresh       后台线程抓取，进度通过 SSE 实时推给前端
   后续要加"每日定时推送"，只需在 scheduler 里调 broadcast("board-updated")，
   前端不必改一行代码。
4. 空闲 30 分钟且无连接时自动退出，不会在后台僵着。
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import threading
import time
import traceback
import webbrowser
from collections import Counter
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import ghtrend as gt  # noqa: E402  业务引擎


def _bundle_dir() -> Path:
    """前端静态资源所在目录。

    开发 / CLI 启动：和脚本同级的 web/
    打包后（PyInstaller）：解压出来的 _MEIPASS/web
    两者不能用同一个基准目录 —— 数据要写进用户目录，
    静态资源却在程序包内部，混在一起会导致打包后页面 404。
    """
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if base else SCRIPT_DIR


BUNDLE_DIR = _bundle_dir()
WEB_DIR = BUNDLE_DIR / "web"
DEFAULT_PORT = 8765
IDLE_TIMEOUT = 30 * 60          # 无连接空闲多久自动退出（秒）
ALLOWED_STATIC = {"index.html", "app.css", "app.js"}

LOG_FILE = gt.APP_DIR / "logs" / "server.log"


def _logline(msg: str) -> None:
    """用 pythonw 启动时没有控制台，所有信息必须落盘，否则出问题完全无从排查。"""
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass
    if sys.stderr is not None:
        try:
            sys.stderr.write(msg + "\n")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 状态与事件总线
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 收藏 · 本地资料库
# ---------------------------------------------------------------------------

LIB_FILE = "library.json"


def lib_path(data_dir: Path) -> Path:
    return data_dir / LIB_FILE


def load_library(data_dir: Path) -> dict:
    p = lib_path(data_dir)
    if p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(d, dict) and isinstance(d.get("items"), dict):
                return d
        except (OSError, ValueError):
            pass
    return {"version": 1, "items": {}}


def save_library(data_dir: Path, lib: dict) -> None:
    """先写临时文件再原子替换，避免被强杀时留下半个 JSON。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    target = lib_path(data_dir)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(lib, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(target)


class AppState:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.subs: list[queue.Queue] = []
        self.subs_lock = threading.Lock()
        self.job_lock = threading.Lock()
        self.job = {"state": "idle", "step": "", "count": None, "error": None}
        self.sum_lock = threading.Lock()
        self.sumjob = {"state": "idle", "done": 0, "total": 0, "error": None}
        self.last_activity = time.time()
        self.stop_event = threading.Event()
        # 收藏（本地资料库）。不做账号：单机工具的"登录"没有对手方验证，
        # 密码只能跟本地文件比对，删掉就重置 —— 是仪式，不是安全。
        self.lib_lock = threading.Lock()
        self.lib = load_library(data_dir)
        self._star_cache: tuple[str, float, dict[str, int]] | None = None

    # ---- 事件总线 ----
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=256)
        with self.subs_lock:
            self.subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.subs_lock:
            if q in self.subs:
                self.subs.remove(q)

    def broadcast(self, event: str, data: dict) -> None:
        payload = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        with self.subs_lock:
            targets = list(self.subs)
        for q in targets:
            try:
                q.put_nowait(payload)
            except queue.Full:
                pass

    def touch(self) -> None:
        self.last_activity = time.time()

    @property
    def online(self) -> int:
        with self.subs_lock:
            return len(self.subs)


# ---------------------------------------------------------------------------
# 收藏 · 读写
# ---------------------------------------------------------------------------

def _star_map(state: "AppState") -> dict[str, int]:
    """今天的 full_name -> stars，供收藏视图算「收藏后涨了多少」。

    按快照文件的修改时间判断缓存失效 —— 同一天重新抓取时日期不变但数据变了。
    """
    day = gt.today_str()
    path = gt.snapshot_path(state.data_dir, day)
    stamp = path.stat().st_mtime if path.exists() else 0.0
    cache = state._star_cache
    if cache and cache[0] == day and cache[1] == stamp:
        return cache[2]

    mapping: dict[str, int] = {}
    if stamp:
        try:
            for it in gt.load_snapshot(path).get("repos") or []:
                name = it.get("full_name")
                if name:
                    mapping[name] = int(it.get("stars") or 0)
        except (OSError, ValueError):
            mapping = {}
    state._star_cache = (day, stamp, mapping)
    return mapping


def get_library(state: "AppState") -> dict:
    """收藏列表。掉出当天候选池的项目照样显示，只是数字停在收藏时。"""
    stars = _star_map(state)
    with state.lib_lock:
        raw = [dict(v) for v in state.lib.get("items", {}).values() if isinstance(v, dict)]

    rows = []
    for it in raw:
        name = it.get("name") or ""
        if not name:
            continue
        base = int(it.get("starsAtAdd") or 0)
        now = stars.get(name)
        rows.append({
            "name": name,
            "url": it.get("url") or f"https://github.com/{name}",
            "title": it.get("title") or "",
            "note": it.get("note") or "",
            "language": it.get("language") or "",
            "addedAt": it.get("addedAt") or "",
            "starsAtAdd": base,
            "stars": now if now is not None else base,
            "deltaSinceAdd": (now - base) if (now is not None and base > 0) else None,
            "stale": now is None,      # 今天没抓到它，数字没更新
        })

    # 涨幅大的在前；没涨幅数据的排后面，再按收藏时间倒序
    rows.sort(key=lambda r: (r["deltaSinceAdd"] is None,
                             -(r["deltaSinceAdd"] or 0),
                             r["addedAt"]), reverse=False)
    return {"ok": True, "count": len(rows), "items": rows, "date": gt.today_str()}


def toggle_star(state: "AppState", payload: dict) -> dict:
    name = (payload.get("name") or "").strip()
    if not name:
        return {"ok": False, "message": "缺少项目名"}
    with state.lib_lock:
        items = state.lib.setdefault("items", {})
        if name in items:
            items.pop(name, None)
            starred = False
        else:
            items[name] = {
                "name": name,
                "url": payload.get("url") or f"https://github.com/{name}",
                "addedAt": datetime.now().isoformat(timespec="seconds"),
                "starsAtAdd": int(payload.get("stars") or 0),
                "note": "",
                "title": payload.get("title") or "",
                "language": payload.get("language") or "",
            }
            starred = True
        total = len(items)
        save_library(state.data_dir, state.lib)
    state.broadcast("library", {"count": total, "name": name, "starred": starred})
    return {"ok": True, "starred": starred, "count": total}


def set_note(state: "AppState", payload: dict) -> dict:
    name = (payload.get("name") or "").strip()
    note = (payload.get("note") or "")[:500]
    with state.lib_lock:
        it = state.lib.get("items", {}).get(name)
        if not isinstance(it, dict):
            return {"ok": False, "message": "该项目不在收藏里"}
        it["note"] = note
        save_library(state.data_dir, state.lib)
    return {"ok": True, "note": note}


def import_library(state: "AppState", payload: dict) -> dict:
    """只新增、不覆盖：同名项目保留你现在的备注和收藏时间。"""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, dict):
        return {"ok": False, "message": "文件格式不对，需要 GHTrend 导出的 library.json"}

    added = 0
    with state.lib_lock:
        cur = state.lib.setdefault("items", {})
        for key, val in items.items():
            if not key or not isinstance(val, dict) or key in cur:
                continue
            val.setdefault("name", key)
            cur[key] = val
            added += 1
        save_library(state.data_dir, state.lib)
        total = len(cur)
    return {"ok": True, "added": added, "count": total}


# ---------------------------------------------------------------------------
# 业务编排（复用引擎，不重写逻辑）
# ---------------------------------------------------------------------------

class _Args:
    """伪造成 argparse 的结果，喂给 ghtrend.produce()。

    offline=True 是关键：produce() 在 offline 下不会自己跑网络抓取，
    没有今日快照就直接抛 ApiError —— 这样 API 请求永远是毫秒级返回，
    抓取这种慢操作交给 /api/refresh 的后台线程。
    """

    def __init__(self, top: int = 20, language: str = "", min_delta: int = 0):
        self.top = top
        self.language = language
        self.min_delta = min_delta
        self.refresh = False
        self.offline = True
        self.data_dir = None
        self.no_ai = False
        self.report = False
        self.notify = False
        self.format = "table"
        self.transport = "auto"


def _languages_of(snapshot: dict) -> list[dict]:
    """给前端语言下拉用：只列出真实存在的语言及数量。"""
    counter = Counter(r["language"] for r in snapshot["repos"] if r.get("language"))
    return [{"name": k, "count": v} for k, v in counter.most_common(20)]


def _api_display() -> dict:
    """给界面展示"当前走哪条线路"。这里只读缓存/配置，绝不触发网络探测。"""
    cfg = gt.load_config()
    custom = ((cfg.get("github") or {}).get("api_base") or "").strip()
    if custom:
        return {"base": custom, "label": "自定义线路", "custom": True,
                "latencyMs": None, "source": "config"}
    cached = gt._load_endpoint_cache()
    if cached and cached.get("base"):
        return {"base": cached.get("base"), "label": cached.get("label"),
                "custom": False, "latencyMs": cached.get("latency_ms"),
                "source": "cache"}
    return {"base": gt.GITHUB_API, "label": "尚未探测", "custom": False,
            "latencyMs": None, "source": "none"}


def _board_rows(state: AppState, top: int, language: str, min_delta: int):
    """统一的取数入口。全程 offline=True，保证不触发网络抓取。"""
    cfg = gt.load_config()
    ctx = gt.produce(state.data_dir, cfg, _Args(top, language, min_delta))
    ctx["rows"] = gt.enrich_summaries(ctx["rows"], cfg, state.data_dir,
                                      use_ai=True, show_progress=False)
    ctx["rows"] = gt.apply_plain_cached(ctx["rows"], cfg, state.data_dir)
    snapshot = gt.load_snapshot(gt.snapshot_path(state.data_dir, ctx["date"]))
    return cfg, ctx, snapshot


def get_board(state: AppState, top: int, language: str, min_delta: int) -> dict:
    day = gt.today_str()
    path = gt.snapshot_path(state.data_dir, day)

    if not path.exists():
        return {
            "ok": False,
            "reason": "no_snapshot",
            "date": day,
            "message": "今天还没有抓取数据。点右上角「抓取最新数据」开始，约需 1 分钟。",
        }

    try:
        cfg, ctx, snapshot = _board_rows(state, top, language, min_delta)
    except Exception as e:
        return {"ok": False, "reason": "error", "message": str(e), "date": day}

    items = []
    lib_items = state.lib.get("items", {})
    for i, r in enumerate(ctx["rows"], 1):
        saved = lib_items.get(r["full_name"]) or {}
        base = int(saved.get("starsAtAdd") or 0)
        items.append({
            "rank": i,
            "name": r["full_name"],
            "url": r.get("url") or "",
            "language": r.get("language") or "",
            "stars": r["stars"],
            "delta": r.get("delta"),
            "perDay": r.get("per_day"),
            "velocity": r.get("velocity"),
            "summary": r.get("summary") or "",
            "plainTitle": r.get("plain_title") or "",
            "plainDetail": r.get("plain_detail") or "",
            "plainSrc": r.get("plain_src") or "rule",
            "plainPending": bool(r.get("plain_pending")),
            "topics": (r.get("topics") or [])[:5],
            "createdAt": (r.get("created_at") or "")[:10],
            "prevStars": r.get("prev_stars"),
            "starred": r["full_name"] in lib_items,
            "starsAtAdd": base or None,
            "deltaSinceAdd": (r["stars"] - base) if base > 0 else None,
        })

    total_delta = sum(x["delta"] for x in items if x.get("delta") is not None)
    llm = cfg.get("llm") or {}
    pending = sum(1 for x in items if x["plainPending"])

    return {
        "ok": True,
        "date": ctx["date"],
        "mode": ctx["mode"],
        "baseline": ctx.get("baseline"),
        "daysGap": ctx.get("days_gap", 1),
        "poolSize": ctx["pool_size"],
        "sample": ctx.get("sample", 0),
        "snapshotDays": ctx.get("snapshot_days", 0),
        "note": gt.velocity_note(ctx) if ctx["mode"] == "velocity" else "",
        "generatedAt": ctx["generated_at"],
        "filters": {"top": top, "language": language, "minDelta": min_delta},
        "stats": {"totalDelta": total_delta, "count": len(items)},
        "languages": _languages_of(snapshot),
        "linkModes": cfg.get("web", {}).get("link_modes", []),
        "defaultLinkMode": cfg.get("web", {}).get("default_link_mode", "direct"),
        "api": _api_display(),
        "llmEnabled": bool(llm.get("enabled") and llm.get("api_key")),
        "summaryPending": pending,
        "summaryJob": state.sumjob,
        "libraryCount": len(lib_items),
        "items": items,
    }


def start_summarize(state: AppState, top: int, language: str, min_delta: int):
    """后台生成「人话总结」，逐条通过 SSE 推给前端。"""
    with state.sum_lock:
        if state.sumjob.get("state") == "running":
            return False, "正在生成中"
        state.sumjob = {"state": "running", "done": 0, "total": 0, "error": None}
    state.broadcast("summary-job", state.sumjob)

    def worker() -> None:
        cfg = gt.load_config()
        llm = cfg.get("llm") or {}
        if not (llm.get("enabled") and llm.get("api_key")):
            with state.sum_lock:
                state.sumjob = {"state": "error", "done": 0, "total": 0,
                                "error": "未配置 LLM，无法生成人话总结"}
            state.broadcast("summary-job", state.sumjob)
            return
        try:
            _, ctx, _ = _board_rows(state, top, language, min_delta)
            rows = [r for r in ctx["rows"] if r.get("plain_pending")]
            total = len(rows)

            with state.sum_lock:
                state.sumjob["total"] = total
            state.broadcast("summary-job", state.sumjob)

            counter = {"n": 0}

            def on_item(r: dict) -> None:
                counter["n"] += 1
                with state.sum_lock:
                    state.sumjob["done"] = counter["n"]
                # 每生成一条就推一条 —— 前端标题会一条条"长出来"
                state.broadcast("summary", {
                    "name": r["full_name"],
                    "title": r.get("plain_title"),
                    "detail": r.get("plain_detail"),
                    "src": r.get("plain_src"),
                })

            res = gt.generate_plain(rows, cfg, state.data_dir, on_item=on_item,
                                    max_calls=int(llm.get("max_calls_per_run", 30)))

            with state.sum_lock:
                state.sumjob = {"state": "done", "done": counter["n"],
                                "total": total, "error": None, "result": res}
            state.broadcast("summary-job", state.sumjob)
        except Exception as e:
            with state.sum_lock:
                state.sumjob = {"state": "error", "done": 0, "total": 0,
                                "error": str(e)}
            state.broadcast("summary-job", state.sumjob)

    threading.Thread(target=worker, daemon=True, name="ghtrend-summarize").start()
    return True, "开始生成人话总结"


def get_status(state: AppState) -> dict:
    day = gt.today_str()
    snaps = gt.list_snapshots(state.data_dir)
    today = gt.snapshot_path(state.data_dir, day)
    count = 0
    if today.exists():
        try:
            count = gt.load_snapshot(today).get("count", 0)
        except Exception:
            count = 0
    return {
        "ok": True,
        "date": day,
        "hasToday": today.exists(),
        "todayCount": count,
        "snapshotDays": len(snaps),
        "online": state.online,
        "job": state.job,
        "summaryJob": state.sumjob,
        "transport": gt.TRANSPORT,
        "api": _api_display(),
        "libraryCount": len(state.lib.get("items", {})),
        "lastAuto": _read_daily_last(state.data_dir),
    }


def _write_daily_last(data_dir: Path, result: dict) -> None:
    """记录最近一次抓取的结果。

    三条抓取路径（计划任务 / 手动点按钮 / 打开软件自动补抓）都要写，
    否则界面上的「上次自动抓取」会一直停在很早以前，用户以为没在跑；
    自动补抓的冷却判断也依赖这里的时间戳，漏写会变成每次开窗都抓一遍。
    """
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "daily_last.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        _logline(f"写入 daily_last.json 失败：{exc}")


def _read_daily_last(data_dir: Path) -> dict | None:
    """最近一次定时抓取的结果，没有就返回 None（界面据此提示去设置）。"""
    p = data_dir / "daily_last.json"
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def start_refresh(state: AppState, source: str = "manual") -> tuple[bool, str]:
    """启动后台抓取线程。同时只允许一个任务在跑。

    source 只用于记录（manual / auto / schedule），写进 daily_last.json，
    方便日后看日志时区分「人点的」和「程序自己补的」。
    """
    with state.job_lock:
        if state.job["state"] == "running":
            return False, "已有抓取任务在运行"
        state.job = {"state": "running", "step": "准备中…", "count": None, "error": None}

    state.broadcast("job", state.job)
    cfg = gt.load_config()
    t0 = time.time()
    started_at = datetime.now().isoformat(timespec="seconds")

    def worker() -> None:
        # 接管引擎的日志输出，把真实进度实时推给前端。
        # ghtrend 内部的 log() 查的是模块全局名，所以直接替换 gt.log 即可生效。
        original_log = gt.log

        def hooked(msg: str = "") -> None:
            original_log(msg)
            text = str(msg).strip()
            if not text:
                return
            with state.job_lock:
                state.job["step"] = text
            state.broadcast("progress", {"step": text})

        gt.log = hooked  # type: ignore[assignment]
        try:
            day = gt.today_str()
            repos = gt.fetch_pool(cfg)
            if not repos:
                raise RuntimeError("未抓取到任何仓库，请检查网络或 GitHub Token")
            path = gt.save_snapshot(state.data_dir, day, repos)
            with state.job_lock:
                state.job = {"state": "done", "step": f"完成，共 {len(repos)} 个仓库",
                             "count": len(repos), "error": None}
            state.broadcast("job", state.job)
            _write_daily_last(state.data_dir, {
                "date": day, "ok": True, "count": len(repos), "error": None,
                "startedAt": started_at,
                "finishedAt": datetime.now().isoformat(timespec="seconds"),
                "elapsedSec": round(time.time() - t0, 1),
                "source": source,
            })
            # 数据更新广播 —— 前端收到就自动重载榜单，无需手动刷新
            state.broadcast("board-updated", {"date": day, "count": len(repos), "file": path.name})
        except Exception as e:
            with state.job_lock:
                state.job = {"state": "error", "step": "抓取失败", "count": None,
                             "error": str(e)}
            state.broadcast("job", state.job)
            _write_daily_last(state.data_dir, {
                "date": gt.today_str(), "ok": False, "count": 0,
                "error": (str(e) or type(e).__name__)[:300],
                "startedAt": started_at,
                "finishedAt": datetime.now().isoformat(timespec="seconds"),
                "elapsedSec": round(time.time() - t0, 1),
                "source": source,
            })
        finally:
            gt.log = original_log  # type: ignore[assignment]

    threading.Thread(target=worker, daemon=True, name="ghtrend-refresh").start()
    return True, "已开始抓取"


# ---------------------------------------------------------------------------
# HTTP 处理
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "GHTrendDesktop/1.0"
    protocol_version = "HTTP/1.1"
    state: AppState = None  # type: ignore[assignment]

    # ---- 通用响应 ----
    def _send_json(self, obj: dict, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, name: str) -> None:
        if name not in ALLOWED_STATIC:          # 白名单，杜绝路径穿越
            self._send_json({"ok": False, "message": "not found"}, 404)
            return
        path = WEB_DIR / name
        if not path.exists():
            self._send_json({"ok": False, "message": f"缺少前端文件 web/{name}"}, 404)
            return
        body = path.read_bytes()
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
        }.get(path.suffix, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    # ---- SSE ----
    def _handle_events(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True

        q = self.state.subscribe()
        try:
            hello = {"online": self.state.online, "job": self.state.job,
                     "serverTime": time.strftime("%H:%M:%S")}
            self._sse_write(f"event: hello\ndata: {json.dumps(hello, ensure_ascii=False)}\n\n")
            while not self.state.stop_event.is_set():
                try:
                    self._sse_write(q.get(timeout=12))
                except queue.Empty:
                    self._sse_write(": ping\n\n")       # 心跳，防代理/浏览器断流
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.state.unsubscribe(q)

    def _sse_write(self, text: str) -> None:
        self.wfile.write(text.encode("utf-8"))
        self.wfile.flush()

    # ---- 路由 ----
    def do_GET(self) -> None:  # noqa: N802
        self.state.touch()
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            self._send_static("index.html")
        elif path in ("/app.css", "/app.js"):
            self._send_static(path.lstrip("/"))
        elif path == "/api/events":
            self._handle_events()
        elif path == "/api/status":
            self._send_json(get_status(self.state))
        elif path == "/api/library":
            self._send_json(get_library(self.state))
        elif path == "/api/library/export":
            with self.state.lib_lock:
                payload = json.loads(json.dumps(self.state.lib, ensure_ascii=False))
            payload["exportedAt"] = datetime.now().isoformat(timespec="seconds")
            self._send_json(payload)
        elif path == "/api/board":
            try:
                top = max(1, min(int(qs.get("top", ["20"])[0]), 200))
            except ValueError:
                top = 20
            try:
                min_delta = max(0, int(qs.get("min_delta", ["0"])[0] or 0))
            except ValueError:
                min_delta = 0
            language = (qs.get("language", [""])[0] or "").strip()
            self._send_json(get_board(self.state, top, language, min_delta))
        else:
            self._send_json({"ok": False, "message": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        self.state.touch()
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        raw = self._read_body()
        payload: dict = {}
        if raw:
            try:
                body = json.loads(raw.decode("utf-8"))
                if isinstance(body, dict):
                    payload = body
            except (ValueError, UnicodeDecodeError):
                payload = {}

        if path == "/api/refresh":
            started, msg = start_refresh(self.state)
            self._send_json({"ok": started, "message": msg}, 200 if started else 409)
        elif path == "/api/summarize":
            try:
                top = max(1, min(int(qs.get("top", ["20"])[0]), 200))
            except ValueError:
                top = 20
            try:
                min_delta = max(0, int(qs.get("min_delta", ["0"])[0] or 0))
            except ValueError:
                min_delta = 0
            language = (qs.get("language", [""])[0] or "").strip()
            started, msg = start_summarize(self.state, top, language, min_delta)
            self._send_json({"ok": started, "message": msg}, 200 if started else 409)
        elif path == "/api/library/toggle":
            self._send_json(toggle_star(self.state, payload))
        elif path == "/api/library/note":
            self._send_json(set_note(self.state, payload))
        elif path == "/api/library/import":
            self._send_json(import_library(self.state, payload))
        elif path == "/api/quit":
            self._send_json({"ok": True, "message": "正在退出…"})
            threading.Timer(0.3, lambda: self.state.stop_event.set()).start()
            threading.Timer(0.8, lambda: os._exit(0)).start()
        else:
            self._send_json({"ok": False, "message": "not found"}, 404)

    def log_message(self, fmt: str, *args) -> None:
        """静音访问日志。pythonw 下 sys.stderr 为 None，这里更要守住。"""
        if sys.stderr is None:
            return
        try:
            sys.stderr.write("[ghtrend] %s\n" % (fmt % args))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------

def make_server(port: int, handler, retries: int = 20):
    for offset in range(retries):
        try:
            return ThreadingHTTPServer(("127.0.0.1", port + offset), handler), port + offset
        except OSError:
            continue
    raise SystemExit(f"端口 {port}~{port + retries} 都被占用，请用 --port 指定其他端口")


def _port_serving(port: int) -> bool:
    """端口上是否已经有服务在应答。

    必须显式检测：Windows 下 SO_REUSEADDR 允许两个进程同时绑定同一端口，
    不像 Linux 会直接报错。不检测的话会出现两个实例并存，
    请求被随机分配到旧代码上，表现为"改了没生效"。
    """
    import socket

    s = socket.socket()
    s.settimeout(0.35)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def open_app_window(url: str) -> str:
    """优先用 Edge/Chrome 的应用模式打开 —— 没有地址栏和标签页，像个桌面应用。"""
    import subprocess

    candidates = [
        (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", "Edge"),
        (r"C:\Program Files\Microsoft\Edge\Application\msedge.exe", "Edge"),
        (r"C:\Program Files\Google\Chrome\Application\chrome.exe", "Chrome"),
        (r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe", "Chrome"),
    ]
    for exe, name in candidates:
        if os.path.exists(exe):
            try:
                subprocess.Popen(
                    [exe, f"--app={url}", "--window-size=1200,880"],
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                return f"{name} 应用窗口"
            except OSError:
                continue
    webbrowser.open(url)
    return "默认浏览器"


def idle_watchdog(state: AppState) -> None:
    """没有任何 SSE 连接、且长时间没有请求 → 自动退出，避免后台僵尸进程。"""
    while not state.stop_event.wait(30):
        if state.online == 0 and (time.time() - state.last_activity) > IDLE_TIMEOUT:
            if sys.stderr:
                sys.stderr.write("[ghtrend] 空闲超时，退出。\n")
            os._exit(0)


def _write_ready_file(path: str, port: int) -> None:
    """把「服务已在哪个端口就绪」告诉外壳进程（Electron）。

    为什么需要这个文件而不是让外壳去猜端口：
    make_server() 在端口被占时会自动顺延，实际端口可能不是传进来的那个。
    外壳拿错端口就会一直白屏等下去。写文件是唯一可靠的握手方式。

    用 临时文件 + os.replace 做原子替换：否则外壳可能读到写了一半的 JSON。
    """
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "port": port,
                    "url": f"http://127.0.0.1:{port}/",
                    "pid": os.getpid(),
                    "time": time.time(),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        os.replace(tmp, p)
    except OSError as exc:
        _logline(f"写入 ready 文件失败：{exc}")


def run_refresh_once(data_dir: Path) -> int:
    """静默抓取一次，供 Windows 计划任务每天调用。

    不启服务、不开窗口、不监听端口，所以用户正开着软件时也不会冲突。
    结果写到 data/daily_last.json，界面据此显示「上次自动抓取」。
    这个函数存在的理由：日增榜的准确性完全依赖「每天有基线快照」，
    靠用户主动打开软件是拿不到连续数据的。
    """
    gt.set_data_dir(data_dir)
    day = gt.today_str()
    started = time.time()
    started_at = datetime.now().isoformat(timespec="seconds")

    lines: list[str] = []
    original_log = gt.log

    def capture(msg: str = "") -> None:
        text = str(msg).rstrip()
        if text:
            lines.append(text)

    gt.log = capture  # type: ignore[assignment]
    result = {
        "date": day, "ok": False, "count": 0, "error": None,
        "startedAt": started_at, "finishedAt": None, "elapsedSec": None,
        "source": "schedule",
    }
    try:
        repos = gt.fetch_pool(gt.load_config())
        if not repos:
            raise RuntimeError("未抓取到任何仓库，请检查网络或 GitHub Token")
        gt.save_snapshot(data_dir, day, repos)
        result.update(ok=True, count=len(repos))
    except Exception as e:
        result["error"] = (str(e) or type(e).__name__)[:300]
    finally:
        gt.log = original_log  # type: ignore[assignment]

    result["finishedAt"] = datetime.now().isoformat(timespec="seconds")
    result["elapsedSec"] = round(time.time() - started, 1)

    _write_daily_last(data_dir, result)

    # 计划任务没有控制台，抓取过程必须单独落盘，否则失败时完全无从排查
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"\n=== 定时抓取 {started_at} ===\n")
            if lines:
                f.write("\n".join(lines[-80:]) + "\n")
            state_txt = "成功" if result["ok"] else "失败"
            f.write(f"结果：{state_txt}，{result['count']} 个，用时 {result['elapsedSec']}s\n")
            if result["error"]:
                f.write(f"错误：{result['error']}\n")
    except OSError:
        pass

    return 0 if result["ok"] else 1


AUTO_REFRESH_GRACE = 8        # 秒：等窗口把榜单渲染完再动，别一开就抢带宽
AUTO_REFRESH_COOLDOWN = 600  # 秒：上次抓取尝试之后的冷却期，避免开窗/关窗连环抓


def auto_refresh_on_launch(state: AppState) -> None:
    """打开软件时若今天还没有快照，后台自动补抓一次。

    「让数据自己长」只靠计划任务是不够的：早上 9 点没开机，那天就断档了。
    这里补一层兜底 —— 用户打开软件的那一刻，缺的那份数据自己补上，
    不需要再手动点「抓取最新数据」。
    """
    def worker() -> None:
        time.sleep(AUTO_REFRESH_GRACE)
        day = gt.today_str()
        if gt.snapshot_path(state.data_dir, day).exists():
            return                                   # 今天已经有了，什么都不做
        last = _read_daily_last(state.data_dir) or {}
        try:
            started = datetime.fromisoformat(str(last.get("startedAt") or ""))
            if (datetime.now() - started).total_seconds() < AUTO_REFRESH_COOLDOWN:
                _logline("今天还没有快照，但刚尝试过抓取，本次不自动重试")
                return
        except ValueError:
            pass                                     # 没记录 / 格式不对，照常抓
        _logline(f"今天（{day}）还没有快照，自动补抓中…")
        ok, msg = start_refresh(state, source="auto")
        if not ok:
            _logline(f"自动补抓未启动：{msg}")

    threading.Thread(target=worker, daemon=True, name="ghtrend-auto").start()


def main() -> int:
    parser = argparse.ArgumentParser(description="GHTrend 桌面版 · 本地服务")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--no-browser", action="store_true", help="不自动打开窗口（调试用）")
    parser.add_argument("--ready-file", default=None,
                        help="启动成功后把实际端口写入该文件（供桌面外壳读取）")
    parser.add_argument("--no-idle-exit", action="store_true",
                        help="禁用空闲自动退出（桌面外壳托管时由外壳负责退出）")
    parser.add_argument("--refresh-once", action="store_true",
                        help="静默抓取一次后退出（供每日定时任务调用，不启服务、不开窗口）")
    args = parser.parse_args()

    # 后端 stdout 被桌面外壳接管时，Python 默认用系统编码（Windows 是 GBK）输出，
    # 外壳按 UTF-8 读就会把中文全变成 ï¿½，日志彻底没法用来排错。
    # 强制统一成 UTF-8。pythonw 下 stdout 可能是 None，所以要 try。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    if args.refresh_once:
        once_dir = Path(args.data_dir) if args.data_dir else gt.DEFAULT_DATA_DIR
        return run_refresh_once(once_dir)

    _logline("=== 启动 ===")

    # 已有实例在跑？直接复用，不重复启动（避免 Windows 下的双绑定）
    if _port_serving(args.port):
        _logline(f"端口 {args.port} 已有实例在运行，本次不重复启动")
        if args.ready_file:
            _write_ready_file(args.ready_file, args.port)
        if not args.no_browser:
            open_app_window(f"http://127.0.0.1:{args.port}/")
        return 0

    data_dir = Path(args.data_dir) if args.data_dir else gt.DEFAULT_DATA_DIR
    gt.set_data_dir(data_dir)      # 页缓存与线路缓存要跟着同一个数据目录走
    seeded = gt.ensure_seed_snapshots(data_dir)
    if seeded:
        _logline(f"已注入种子快照：{', '.join(seeded)}（首开就有日增榜可用）")
    state = AppState(data_dir)
    Handler.state = state

    httpd, port = make_server(args.port, Handler)
    url = f"http://127.0.0.1:{port}/"

    threading.Thread(target=httpd.serve_forever, daemon=True, name="http").start()
    # 外壳托管时不启用空闲自杀：窗口最小化 / 断网时无人连接，
    # 30 分钟后服务自己退出，用户再点界面就全废了。退出交给外壳负责。
    if not args.no_idle_exit:
        threading.Thread(target=idle_watchdog, args=(state,), daemon=True).start()

    _logline(f"服务已启动：{url}")
    if args.ready_file:
        _write_ready_file(args.ready_file, port)
        _logline(f"已写入 ready 文件：{args.ready_file}")

    if not args.no_browser:
        how = open_app_window(url)
        _logline(f"打开窗口方式：{how}")

    # 缺今天的快照就在后台自己补 —— 「让数据自己长」的兜底
    auto_refresh_on_launch(state)

    try:
        while not state.stop_event.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        _logline("=== 服务结束 ===")
        try:
            httpd.shutdown()
        except Exception:
            pass
    return 0


def _show_error(msg: str) -> None:
    """pythonw 没有控制台，启动失败时用系统消息框告知用户，否则就是"点了没反应"。"""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, msg, "GHTrend 启动失败", 0x10)
    except Exception:
        pass


if __name__ == "__main__":
    try:
        code = main()
    except SystemExit as e:
        detail = str(e) or "启动失败"
        _logline(f"退出：{detail}")
        _show_error(detail)
        code = 1
    except BaseException:
        tb = traceback.format_exc()
        _logline("!! 启动失败\n" + tb)
        _show_error("GHTrend 启动失败：\n\n" + tb[-700:] + "\n\n详见 logs\\server.log")
        code = 1
    sys.exit(code)
