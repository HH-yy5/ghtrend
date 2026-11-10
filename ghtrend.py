#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GHTrend · GitHub 星速榜  v1.0
================================
每日追踪 GitHub 上 star 增速最快的项目。

设计原则：
  1. 零第三方依赖 —— 只用 Python 标准库，clone 下来就能跑
  2. 离线可演示 —— demo 模式不需要网络、Token、配置
  3. 诚实降级   —— 没基线就出新星榜，没 LLM 就出规则中文摘要，不报错、不空转
  4. 分层解耦   —— 抓取 / 存储 / 计算 / 增强 / 展现 五层分离，便于将来接 Web 端

用法：
  python ghtrend.py demo                       # 零配置体验（不联网）
  python ghtrend.py top                        # 看今天的日增榜
  python ghtrend.py daily --report --notify    # 一键日报（定时任务用这个）
  python ghtrend.py init                       # 生成配置模板
  python ghtrend.py config                     # 查看当前配置状态
  python ghtrend.py show torvalds/linux        # 看某仓库的本地 star 历史
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import smtplib
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from email.header import Header
from email.mime.text import MIMEText
from pathlib import Path
from urllib import error, parse, request

# ---------------------------------------------------------------------------
# 0. 基础常量
# ---------------------------------------------------------------------------

def _resolve_app_dir() -> Path:
    """数据根目录（config.json / data / logs 都挂在它下面）。

    默认就是脚本所在目录 —— CLI 和 .bat 启动方式的行为完全不变。

    桌面版（Electron）会通过环境变量 GHTREND_HOME 指定另一个位置。
    原因：PyInstaller 打包后 __file__ 指向的是临时解压目录，
    配置和历史快照写进去，程序一关就随临时目录一起被删掉。
    这是"打包成软件"最容易踩、也最致命的坑。
    """
    raw = os.environ.get("GHTREND_HOME")
    if raw:
        try:
            p = Path(raw).expanduser().resolve()
            p.mkdir(parents=True, exist_ok=True)
            return p
        except OSError:
            pass  # 目录不可写就老实退回脚本目录，不因为一个环境变量起不来
    return Path(__file__).resolve().parent


APP_DIR = _resolve_app_dir()
DEFAULT_DATA_DIR = APP_DIR / "data"
CONFIG_PATH = APP_DIR / "config.json"
EXAMPLE_CONFIG_PATH = APP_DIR / "config.example.json"

VERSION = "1.0.0"
USER_AGENT = f"ghtrend/{VERSION} (local-cli)"
GITHUB_API = "https://api.github.com"

# ---------------------------------------------------------------------------
# API 线路候选：应对「国内直连 api.github.com 不通」
# ---------------------------------------------------------------------------
# 设计取舍（重要）：
#   1. 不写死任何一条镜像。公共镜像随时可能失效，而且一台机器能通的线路，
#      在另一台机器/另一个运营商上未必能通。所以改为「运行时现场探测」，
#      选第一条真正能返回 GitHub 数据的线路。
#   2. 顺序即优先级：先试官方直连（最稳、不经过第三方），失败再依次试镜像。
#   3. 请求会经过第三方。介意隐私/稳定性的话，请在 config.json 的
#      github.api_base 填自己的加速器地址或自建反代，填了就不再自动探测。
ENDPOINT_CANDIDATES: list[dict] = [
    {"id": "direct", "label": "GitHub 官方直连", "base": "https://api.github.com"},
    {"id": "ghproxy", "label": "gh-proxy 加速",
     "base": "https://gh-proxy.com/https://api.github.com"},
    {"id": "ghfast", "label": "ghfast 加速",
     "base": "https://ghfast.top/https://api.github.com"},
    {"id": "kkgithub", "label": "kkgithub 镜像", "base": "https://api.kkgithub.com"},
    {"id": "llkk", "label": "llkk 加速",
     "base": "https://gh.llkk.cc/https://api.github.com"},
]

# 运行时生效的线路（由 ensure_api_base 决定）
API_BASE = GITHUB_API
API_INFO: dict = {"id": "direct", "label": "GitHub 官方直连", "base": GITHUB_API,
                  "latency_ms": None, "probed": False, "custom": False}

ENDPOINT_CACHE_PATH = DEFAULT_DATA_DIR / "api_endpoint.json"

# 抓取页缓存。每成功抓取一页就立刻落盘 —— 慢线路（公共镜像单页 10~25s）
# 经常抓两页就断，若不落盘则每一轮都从头再来，永远凑不齐数据。
PAGE_CACHE_DIR = DEFAULT_DATA_DIR / "page_cache"
PAGE_CACHE_TTL = 600       # 秒。10 分钟内重复刷新直接复用，避免每次都等一分多钟


def set_data_dir(p) -> None:
    """统一数据目录。打包后是 exe 同级的 data/，必须由调用方显式设定。"""
    global PAGE_CACHE_DIR, ENDPOINT_CACHE_PATH
    root = Path(p)
    PAGE_CACHE_DIR = root / "page_cache"
    ENDPOINT_CACHE_PATH = root / "api_endpoint.json"

# 本工具只做「增速」的定性排序，因此 star 增量口径允许存在快照时间差带来的偏差。
# 该偏差水平已在 PRD §2 (G5) 中被定义为 ≤5%，不影响 Top10 名单。

DEFAULT_CONFIG: dict = {
    # ---- GitHub 抓取 ----
    "github": {
        # 强烈建议配置：未认证 10 次/分钟，认证后 30 次/分钟（搜索接口）
        "token": "",
        # 手动指定 API 线路。填了就不再自动探测 —— 适合有加速器/自建反代的人。
        # 例："https://gh.你的域名.com"（反代 api.github.com）
        "api_base": "",
        # 走本机代理（用户自己的 Clash / v2rayN 等）。留空则直连或走镜像。
        # 例："http://127.0.0.1:7890"。注意：填了但代理没开会导致全部请求失败。
        "proxy": "",
        # 单次请求超时。国内走公共镜像时单页常要 10~25s，30s 太紧会频繁截断。
        "timeout": 60,
        # 额外备用线路，自动探测时会在内置候选之后一起尝试
        "extra_endpoints": [],
        # 核心池：star 多 + 近期活跃 → 覆盖"大项目持续增长"
        "core_min_stars": 500,
        "core_pages": 3,          # 每页 100 条，3 页 = 300 个候选（够看，且国内镜像能在超时内跑完）
        "push_window_days": 30,   # 近 30 天内有提交
        # 新星池：新建不久但涨得快 → 覆盖"新项目爆发"
        "new_min_stars": 100,
        "new_pages": 2,           # 2 页 = 200 个候选
        "created_window_days": 60,
    },
    # ---- 榜单默认值（可被命令行覆盖）----
    "report": {
        "top_n": 20,
        "language": "",           # 例如 "python"，留空为全部语言
        "min_delta": 0,
    },
    # ---- 界面：点项目名时打开哪个站点 ----
    # 有了数据却点不开 github.com，是小白的典型卡点，所以链接也做镜像。
    # {url} = 完整地址，{path} = owner/repo
    "web": {
        "link_modes": [
            {"id": "direct", "label": "github.com 官方", "template": "{url}"},
            {"id": "ghproxy", "label": "gh-proxy 加速（直连打不开时选）",
             "template": "https://gh-proxy.com/{url}"},
        ],
        # 默认 direct：点开就是 github.com 官方页面。
        # 曾经默认 auto（跟着抓取线路走），结果抓取走镜像时，"在 GitHub 查看"
        # 会把用户带到第三方加速站 —— 那不是用户要的 GitHub。
        # 抓取走哪条线（为了能拿到数据）和用户点开看哪里（为了看官方页面）是两件事，
        # 不能耦合。直连较慢时由用户在右上角下拉里手动切加速。
        "default_link_mode": "direct",
    },
    # ---- AI 中文摘要（可选；不配置则用规则引擎）----
    "llm": {
        "enabled": False,
        "base_url": "https://api.openai.com/v1",   # 任何 OpenAI 兼容端点
        "api_key": "",
        "model": "gpt-4o-mini",
        "timeout": 40,
        "max_calls_per_run": 30,   # 单次运行最多调用次数（成本护栏）
        # 并发数：串行时 6 秒/条（读 README 3.9s + LLM 1.9s），20 条要 2 分钟。
        # 并发 4 之后约 40 秒。再高容易触发端点限流。
        "concurrency": 4,
    },
    # ---- 推送（可选；都不配置则只出本地报告）----
    "notify": {
        "wecom_webhook": "",       # 企业微信群机器人 Webhook 完整地址
        "email": {
            "enabled": False,
            "host": "smtp.qq.com",
            "port": 465,
            "use_ssl": True,
            "user": "",
            "password": "",        # QQ 邮箱填「授权码」而非登录密码
            "to": "",
        },
    },
    # ---- 数据保留 ----
    "retention_days": 90,
}

# 环境变量覆盖映射：ENV 名 -> (配置路径, 类型)
ENV_OVERRIDES = {
    "GITHUB_TOKEN": (("github", "token"), str),
    "GHTREND_API_BASE": (("github", "api_base"), str),
    "GHTREND_LLM_BASE_URL": (("llm", "base_url"), str),
    "GHTREND_LLM_API_KEY": (("llm", "api_key"), str),
    "GHTREND_LLM_MODEL": (("llm", "model"), str),
    "GHTREND_LLM_ENABLED": (("llm", "enabled"), bool),
    "GHTREND_WECOM_WEBHOOK": (("notify", "wecom_webhook"), str),
    "GHTREND_SMTP_HOST": (("notify", "email", "host"), str),
    "GHTREND_SMTP_PORT": (("notify", "email", "port"), int),
    "GHTREND_SMTP_USER": (("notify", "email", "user"), str),
    "GHTREND_SMTP_PASSWORD": (("notify", "email", "password"), str),
    "GHTREND_SMTP_TO": (("notify", "email", "to"), str),
}


# ---------------------------------------------------------------------------
# 1. 终端与文本工具
# ---------------------------------------------------------------------------

def setup_stdout() -> None:
    """Windows 控制台默认 GBK，中文会乱码。强制 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def log(msg: str = "") -> None:
    """进度信息走 stderr，保证 stdout 的 JSON 可以被管道消费。

    注意：用 pythonw 启动（无控制台）时 sys.stderr 是 None，
    这里必须提前返回，否则 print(file=None) 会抛 AttributeError。
    """
    stream = sys.stderr
    if stream is None:
        return
    print(msg, file=stream)


def _char_width(ch: str) -> int:
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def disp_width(s: str) -> int:
    return sum(_char_width(c) for c in s)


def truncate(s: str, width: int) -> str:
    """按显示宽度截断（中文算 2 宽），超出补省略号。"""
    if disp_width(s) <= width:
        return s
    out, cur = [], 0
    for ch in s:
        w = _char_width(ch)
        if cur + w > width - 1:
            break
        out.append(ch)
        cur += w
    return "".join(out) + "…"


def pad(s: str, width: int, align: str = "left") -> str:
    s = truncate(s, width)
    space = max(width - disp_width(s), 0)
    return (s + " " * space) if align == "left" else (" " * space + s)


def now_local() -> datetime:
    return datetime.now()


def today_str() -> str:
    return now_local().strftime("%Y-%m-%d")


def utc_date_minus(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")


def parse_iso(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 2. 配置层
# ---------------------------------------------------------------------------

def deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _set_path(cfg: dict, path: tuple, value) -> None:
    node = cfg
    for key in path[:-1]:
        node = node.setdefault(key, {})
    node[path[-1]] = value


def _coerce(raw: str, typ):
    if typ is bool:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if typ is int:
        return int(raw)
    return raw


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # 深拷贝

    if CONFIG_PATH.exists():
        try:
            user_cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            cfg = deep_merge(cfg, user_cfg)
        except json.JSONDecodeError as e:
            log(f"⚠ config.json 格式有误，已忽略（{e}）")

    for env_name, (path, typ) in ENV_OVERRIDES.items():
        raw = os.environ.get(env_name)
        if raw:
            try:
                _set_path(cfg, path, _coerce(raw, typ))
            except (TypeError, ValueError):
                log(f"⚠ 环境变量 {env_name} 取值异常，已忽略")
    return cfg


def cmd_init(args) -> int:
    if EXAMPLE_CONFIG_PATH.exists() and not args.force:
        log(f"ℹ 配置模板已存在：{EXAMPLE_CONFIG_PATH}（加 --force 覆盖）")
    else:
        EXAMPLE_CONFIG_PATH.write_text(
            json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log(f"✔ 已生成配置模板：{EXAMPLE_CONFIG_PATH}")

    print()
    print("下一步（三步走，全程可跳过）：")
    print()
    print("  1) 想从 10 次/分钟 提到 30 次/分钟？配置 GitHub Token")
    print("     · 打开 https://github.com/settings/tokens → Generate new token (classic)")
    print("     · 只勾选 public_repo 权限即可（本工具只读公开数据）")
    print("     · 复制 config.example.json 为 config.json，填入 github.token")
    print("       或设置环境变量 GITHUB_TOKEN")
    print()
    print("  2) 想要真正的中文摘要？配置 LLM（OpenAI 兼容接口均可）")
    print("     · 不配置也能用 —— 会有规则引擎生成的中文说明，只是不含语义理解")
    print()
    print("  3) 想每天自动收到推送？配置企业微信机器人或邮箱")
    print("     · 企业微信：群设置 → 群机器人 → 添加 → 复制 Webhook 地址")
    print("       填到 config.json 的 notify.wecom_webhook")
    print()
    print("现在就可以先跑一下不联网的演示：  python ghtrend.py demo")
    return 0


def cmd_config(args) -> int:
    cfg = load_config()
    mask = lambda v: ("已配置" if v else "未配置") if isinstance(v, str) else v

    print()
    print("当前生效配置")
    print("=" * 62)
    gh = cfg["github"]
    print(f"  GitHub Token          : {mask(gh['token'])}"
          f"{'（未认证：搜索接口 10 次/分钟）' if not gh['token'] else '（认证：30 次/分钟）'}")
    api_base = (gh.get("api_base") or "").strip()
    cached = _load_endpoint_cache()
    if api_base:
        line = f"{api_base}（手动指定，不自动探测）"
    elif cached and cached.get("base"):
        line = f"{cached.get('label', '?')} · {cached.get('latency_ms')}ms（上次探测结果）"
    else:
        line = "自动探测（尚未探测，抓取时自动选线）"
    print(f"  API 线路             : {line}")
    print(f"  核心池               : star>={gh['core_min_stars']} / push {gh['push_window_days']}天内 / {gh['core_pages']} 页")
    print(f"  新星池               : star>={gh['new_min_stars']} / created {gh['created_window_days']}天内 / {gh['new_pages']} 页")
    print(f"  预计 API 调用        : {gh['core_pages'] + gh['new_pages']} 次")
    print("-" * 62)
    llm = cfg["llm"]
    llm_on = bool(llm.get("enabled")) and bool(llm.get("api_key"))
    print(f"  LLM 中文摘要         : {'已启用 (' + llm['model'] + ')' if llm_on else '未启用 → 使用规则引擎（仍输出中文）'}")
    print(f"  LLM 单次调用上限     : {llm['max_calls_per_run']} 次（成本护栏）")
    print("-" * 62)
    nt = cfg["notify"]
    print(f"  企业微信 Webhook     : {mask(nt['wecom_webhook'])}")
    print(f"  邮件推送             : {'已启用 → ' + nt['email']['to'] if nt['email'].get('enabled') and nt['email'].get('user') else '未启用'}")
    print("-" * 62)
    print(f"  数据保留             : {cfg['retention_days']} 天")
    print(f"  配置文件             : {CONFIG_PATH if CONFIG_PATH.exists() else '（未创建，使用默认值 + 环境变量）'}")
    print("=" * 62)
    print()
    print("提示：环境变量优先级高于 config.json。")
    print("     若 config.example.json 不存在，先执行： python ghtrend.py init")
    print()
    return 0


# ---------------------------------------------------------------------------
# 2.5 LLM 配置向导
# ---------------------------------------------------------------------------

LLM_PROVIDERS: list[dict] = [
    {
        "id": "deepseek",
        "label": "DeepSeek（推荐：便宜、中文好）",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "signup": "https://platform.deepseek.com/api_keys",
        "note": "注册后进「API keys」页面 → 创建 → 复制 sk- 开头那串",
    },
    {
        "id": "dashscope",
        "label": "阿里通义千问（新用户有免费额度）",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "signup": "https://bailian.console.aliyun.com/",
        "note": "百炼控制台 → API-KEY 管理 → 创建",
    },
    {
        "id": "zhipu",
        "label": "智谱 GLM（有免费的 glm-4-flash）",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
        "signup": "https://open.bigmodel.cn/usercenter/apikeys",
        "note": "个人中心 → API Keys → 创建",
    },
    {
        "id": "ollama",
        "label": "本地 Ollama（完全免费、数据不出门，但要先装软件）",
        "base_url": "http://127.0.0.1:11434/v1",
        "model": "qwen2.5:7b",
        "signup": "https://ollama.com/download",
        "no_key": True,
        "note": "装好后在命令行跑一次：ollama pull qwen2.5:7b（约 4.7GB，只需一次）",
    },
    {
        "id": "custom",
        "label": "自定义（任何 OpenAI 兼容接口，例如公司内部网关）",
        "base_url": "",
        "model": "",
        "signup": "",
        "note": "",
    },
]


def _write_config_patch(patch: dict) -> None:
    """把改动合并进 config.json，不动里面已有的其它设置。"""
    raw: dict = {}
    if CONFIG_PATH.exists():
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            raw = {}
    raw = deep_merge(raw, patch)
    CONFIG_PATH.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")


def test_llm_connection(llm_cfg: dict) -> tuple[bool, str]:
    """发一句最短的请求验证密钥是否可用 —— 比让用户自己去猜有效得多。"""
    base = (llm_cfg.get("base_url") or "").rstrip("/")
    model = llm_cfg.get("model") or ""
    key = llm_cfg.get("api_key") or ""
    if not base:
        return False, "接口地址（base_url）是空的"
    if not model:
        return False, "模型名（model）是空的"

    payload = {
        "model": model,
        "max_tokens": 24,
        "messages": [{"role": "user", "content": "只回复两个字：正常"}],
    }
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        data = _http(f"{base}/chat/completions", method="POST", payload=payload,
                     headers=headers, timeout=30)
    except Exception as e:
        return False, str(e)[:200]

    try:
        text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "")
    except Exception:
        text = ""
    if not text:
        return False, f"接口有响应但没内容：{json.dumps(data, ensure_ascii=False)[:180]}"
    return True, str(text).strip()[:40]


def cmd_setup_llm(args) -> int:
    print()
    print("配置「人话总结」用的 AI")
    print("=" * 68)
    print("  程序自己不会写人话。这一步是给它请个「写手」：")
    print("  把项目的英文说明书读一遍，再用大白话讲给你听。")
    print()
    print("  不配也能用，只是每行标题只会是「一个用 XX 写的开源项目」。")
    print("=" * 68)

    provider = None
    if getattr(args, "provider", None):
        provider = next((p for p in LLM_PROVIDERS if p["id"] == args.provider), None)
        if not provider:
            print(f"✘ 没有这个服务商：{args.provider}")
            return 1
    else:
        print()
        for i, p in enumerate(LLM_PROVIDERS, 1):
            print(f"  {i}  {p['label']}")
        print("  0  取消")
        print()
        raw = input("  请选择 [1-5]：").strip()
        if raw in ("0", "", "q", "Q"):
            print("\n  已取消，什么都没改。\n")
            return 0
        try:
            provider = LLM_PROVIDERS[int(raw) - 1]
        except (ValueError, IndexError):
            print("\n✘ 输入无效。\n")
            return 1

    base_url = getattr(args, "base_url", None) or provider["base_url"]
    model = getattr(args, "model", None) or provider["model"]
    api_key = getattr(args, "key", None) or ""

    if provider["id"] == "custom":
        print()
        base_url = input("  接口地址 base_url（例：https://api.xxx.com/v1）：").strip()
        model = input("  模型名 model（例：gpt-4o-mini）：").strip()

    if provider.get("no_key"):
        api_key = "ollama"           # 本地服务不校验密钥，占位即可
    elif not api_key:
        print()
        if provider["signup"]:
            print(f"  ① 打开这个页面注册 / 登录：")
            print(f"     {provider['signup']}")
            if provider.get("note"):
                print(f"     {provider['note']}")
            print("  ② 复制那串密钥（通常只显示一次，先存好）")
            print()
        api_key = input("  ③ 把密钥粘贴到这里，回车确认：").strip()

    if not api_key:
        print("\n✘ 没拿到密钥，已取消，什么都没改。\n")
        return 1

    print()
    print("  正在测试能不能连通…")
    ok, msg = test_llm_connection({"base_url": base_url, "model": model, "api_key": api_key})

    if not ok:
        print(f"  ✘ 没连上：{msg}")
        print()
        print("  常见原因：")
        print("    · 密钥复制不完整（多了空格、或少了几位）")
        print("    · 模型名写错（例如 deepseek-chat 写成 deepseek-reasoner）")
        print("    · 账户没余额 / 没实名认证")
        print("    · 本地 Ollama 没启动，或还没 pull 模型")
        print()
        print("  配置没有写入，什么都没改。改好后重跑：python ghtrend.py setup")
        print()
        return 1

    print(f"  ✔ 通了（模型回复：{msg}）")

    _write_config_patch({"llm": {"enabled": True, "base_url": base_url,
                                 "api_key": api_key, "model": model}})
    print()
    print(f"  ✔ 已写入：{CONFIG_PATH}")
    print()
    print("  下一步：回到界面刷新一下（或重新打开窗口），")
    print("  右上角会出现「人话总结 3/20」，标题会一条条变成大白话。")
    print()
    return 0


def cmd_pack(args) -> int:
    """打包成可以发给别人的压缩包。

    必须剔除：config.json（含密钥）、data/（本地数据）、logs/、__pycache__。
    """
    import zipfile

    out = Path(args.out) if args.out else (APP_DIR.parent / "GHTrend-可发给他人的版本.zip")
    exclude_dirs = {"data", "logs", "__pycache__", ".git"}
    exclude_files = {"config.json"}
    exclude_suffix = {".pyc", ".pyo"}

    files = []
    for p in APP_DIR.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(APP_DIR)
        if any(part in exclude_dirs for part in rel.parts):
            continue
        if p.name in exclude_files or p.suffix in exclude_suffix:
            continue
        files.append((p, rel))

    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p, rel in sorted(files, key=lambda x: x[1].as_posix()):
            # 必须用正斜杠：zip 内部用反斜杠解压后不会形成目录
            z.write(p, arcname="GHTrend/" + rel.as_posix())

    # 自检：确认没把密钥或本地数据打进去
    leaks = []
    with zipfile.ZipFile(out) as z:
        for n in z.namelist():
            if Path(n).name == "config.json":
                leaks.append(n)
            if n.endswith((".py", ".js", ".html", ".md", ".json", ".txt", ".bat")):
                try:
                    txt = z.read(n).decode("utf-8", "ignore")
                except Exception:
                    continue
                if re.search(r"sk-[a-f0-9]{20,}", txt):
                    leaks.append(f"{n}（含疑似密钥）")

    print()
    print(f"✔ 已生成：{out}")
    print(f"  体积：{out.stat().st_size / 1024:.0f} KB ｜ 文件数：{len(files)}")
    print(f"  已剔除：config.json / data/ / logs/ / __pycache__")
    if leaks:
        print()
        print("✘ 安全检查未通过，压缩包里出现了不该有的东西：")
        for x in leaks:
            print(f"    {x}")
        print("  请检查后再发送！")
        return 1
    print("  ✔ 安全检查通过：不含密钥、不含本地数据")
    print()
    print("  对方解压后双击「打开界面.bat」即可。需要 Python 3.9+。")
    print()
    return 0


def cmd_netcheck(args) -> int:
    """逐条探测 GitHub API 线路，给出可读结论与下一步建议。"""
    cfg = load_config()
    print()
    print("GitHub 线路探测（国内直连常常不通，会自动尝试镜像）")
    print("=" * 70)
    print(f"  {'线路':<18}{'状态':<8}结果")
    print("-" * 70)

    results = probe_endpoints(cfg, timeout=getattr(args, "timeout", 8),
                             show_progress=False)
    winner = None
    for r in results:
        if r.get("ok"):
            mark, status, detail = "✔", "可用", f"{r['latency_ms']} ms"
            winner = winner or r
        else:
            mark, status = "✘", "不通"
            detail = (r.get("error") or "")[:40]
        print(f"  {mark} {r['label']:<16}{status:<8}{detail}")

    print("=" * 70)

    if winner:
        _save_endpoint_cache({**winner, "probed": True})
        print(f"  本次将使用：{winner['label']}")
        print(f"  线路地址　：{winner['base']}")
        print()
        print("  已记录该结果，24 小时内直接复用，不会重复探测。")
        print("  相关命令：python ghtrend.py fetch   抓取最新数据")
    else:
        print("  ✘ 所有线路都不通，本次无法抓取。")
        print()
        print("  按顺序试试：")
        print("    1) 打开加速器 / 代理，然后重新运行本命令")
        print("    2) 填自己的线路：config.json → github.api_base")
        print("       例：\"https://gh.你的域名.com\"（反代 api.github.com）")
        print("    3) 没有加速器也没有反代 → 看 README「无加速器怎么用」")
        print()
        print("  界面仍可打开，查看本地已保存的历史快照。")
    print()
    return 0 if winner else 1


# ---------------------------------------------------------------------------
# 3. 网络层（含限流与重试）
# ---------------------------------------------------------------------------

class ApiError(Exception):
    pass


class RateLimited(ApiError):
    def __init__(self, message: str, reset_epoch: int | None = None):
        super().__init__(message)
        self.reset_epoch = reset_epoch


class HttpResponse:
    __slots__ = ("status", "headers", "text")

    def __init__(self, status: int, headers: dict, text: str):
        self.status = status
        self.headers = headers      # 键统一为小写，便于跨传输层查找
        self.text = text


# 传输层：默认 auto —— 先用 Python 标准库，遇到 TLS/网络层故障再自动回退 curl。
# 这不是过度设计：企业网络的 SSL 解密代理、缺失的证书链，都会让 Python 的
# TLS 握手失败而 curl 正常（本工具的开发环境正是如此）。
TRANSPORT = "auto"       # auto | urllib | curl
_CURL_CACHE: str | None = None
# 用户自备代理（如 Clash 的 http://127.0.0.1:7890）。空 = 不用代理。
PROXY_URL = ""
# 线路探测时静默掉传输层切换日志：并发探测会同时触发多次 TLS→curl 回退，
# 那些警告对用户没有意义，只会让"正在探测"看起来像出错了。
_QUIET_TRANSPORT = False


def curl_path() -> str:
    global _CURL_CACHE
    if _CURL_CACHE is None:
        _CURL_CACHE = shutil.which("curl") or ""
    return _CURL_CACHE


def _req_urllib(url, method, payload, headers, timeout) -> HttpResponse:
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        hdrs["Content-Type"] = "application/json"

    req = request.Request(url, data=data, headers=hdrs, method=method)
    if PROXY_URL:
        opener = request.build_opener(
            request.ProxyHandler({"http": PROXY_URL, "https": PROXY_URL}))
        do_open = opener.open
    else:
        do_open = request.urlopen
    try:
        with do_open(req, timeout=timeout) as resp:
            return HttpResponse(resp.status, {k.lower(): v for k, v in resp.headers.items()},
                                resp.read().decode("utf-8", "replace"))
    except error.HTTPError as e:
        # HTTP 错误码是「有效响应」，不是传输故障 —— 交给上层决定重试还是报错
        return HttpResponse(e.code, {k.lower(): v for k, v in e.headers.items()},
                            e.read().decode("utf-8", "replace"))


def _hidden_subprocess_kwargs() -> dict:
    """让子进程完全静默，不弹控制台窗口。

    Windows 上有个坑：父进程是 pythonw（无控制台）时，spawn 一个控制台程序
    （curl.exe）系统会给它**新分配一个控制台窗口**，于是每发一个请求就闪一次黑框。
    把 stdout/stderr 重定向也拦不住 —— 必须显式 CREATE_NO_WINDOW。
    """
    kw: dict = {}
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if flags:
        kw["creationflags"] = flags
    elif hasattr(subprocess, "STARTUPINFO"):      # 老版本 Python 的兜底
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
        kw["startupinfo"] = si
    return kw


def _req_curl(url, method, payload, headers, timeout) -> HttpResponse:
    exe = curl_path()
    if not exe:
        raise ApiError("未找到 curl，无法使用 curl 传输层")

    fd, header_file = tempfile.mkstemp(prefix="ghtrend_hdr_")
    os.close(fd)
    cmd = [exe, "-sS", "--max-time", str(int(timeout)),
           "-X", method, "-D", header_file,
           "-w", "\n__GHTREND_STATUS__%{http_code}", url]
    if PROXY_URL:
        cmd += ["--proxy", PROXY_URL]
    for k, v in (headers or {}).items():
        cmd += ["-H", f"{k}: {v}"]
    cmd += ["-H", f"User-Agent: {USER_AGENT}", "-H", "Accept: application/json"]

    stdin = None
    if payload is not None:
        stdin = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        cmd += ["--data-binary", "@-", "-H", "Content-Type: application/json"]

    try:
        proc = subprocess.run(cmd, input=stdin, capture_output=True,
                              timeout=timeout + 20, **_hidden_subprocess_kwargs())
    except subprocess.TimeoutExpired:
        raise ApiError(f"curl 请求超时（{timeout}s）")
    except OSError as e:
        raise ApiError(f"无法执行 curl：{e}")

    # 必须检查 curl 退出码。踩过的坑：--max-time 到点时 curl 已经收到响应头，
    # -w 照样写出 200，但 body 是**被截断的半截 JSON**。不查退出码就会把半截
    # 内容当成成功响应交给 json.loads，最后报「返回的不是 JSON」——错误信息
    # 完全指向错误的方向，让人以为是镜像挂了或接口变了。
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()[:200]
        if proc.returncode == 28:
            raise ApiError(f"请求超时（超过 {int(timeout)}s，线路太慢或网络拥塞）")
        if proc.returncode in (6, 7):
            raise ApiError(f"无法解析或连接主机：{err or '域名解析失败'}")
        if proc.returncode in (18, 52, 55, 56):
            raise ApiError(f"传输中断（curl {proc.returncode}）：{err or '连接被中断'}")
        raise ApiError(f"curl 失败（退出码 {proc.returncode}）：{err or '未知错误'}")

    out = proc.stdout.decode("utf-8", "replace")
    body, sep, status = out.rpartition("__GHTREND_STATUS__")

    hdrs: dict = {}
    try:
        for line in Path(header_file).read_text(encoding="utf-8", errors="replace").splitlines():
            if ":" in line and not line.upper().startswith("HTTP/"):
                k, v = line.split(":", 1)
                hdrs[k.strip().lower()] = v.strip()
    except OSError:
        pass
    finally:
        try:
            os.unlink(header_file)
        except OSError:
            pass

    if not sep:
        err = proc.stderr.decode("utf-8", "replace").strip()[:200]
        raise ApiError(f"curl 未返回状态码：{err or '未知错误'}")
    return HttpResponse(int(status), hdrs, body)


def _request_raw(url, method="GET", payload=None, headers=None, timeout=30) -> HttpResponse:
    global TRANSPORT

    if TRANSPORT == "curl":
        return _req_curl(url, method, payload, headers, timeout)

    try:
        return _req_urllib(url, method, payload, headers, timeout)
    except (ssl.SSLError, socket.timeout, TimeoutError, error.URLError, OSError) as e:
        if TRANSPORT == "urllib":
            raise ApiError(f"网络不可达：{_net_hint(e)}")
        if not curl_path():
            raise ApiError(f"网络不可达：{_net_hint(e)}（且未检测到 curl，无法自动回退）")
        if not _QUIET_TRANSPORT:
            log(f"  ⚠ Python 网络层失败（{_net_hint(e)}），自动切换 curl 传输层…")
        TRANSPORT = "curl"
        return _req_curl(url, method, payload, headers, timeout)


def _net_hint(e: Exception) -> str:
    msg = str(e)
    if "handshake" in msg.lower() or isinstance(e, ssl.SSLError):
        return "TLS 握手失败（常见于企业 SSL 解密代理或证书链缺失）"
    if isinstance(e, (socket.timeout, TimeoutError)) or "timed out" in msg.lower():
        return "连接超时"
    return msg or type(e).__name__


def _http(url: str, *, method: str = "GET", payload=None, headers=None, timeout: int = 30):
    resp = _request_raw(url, method=method, payload=payload, headers=headers, timeout=timeout)
    if resp.status >= 400:
        raise ApiError(f"HTTP {resp.status}：{resp.text[:200]}")
    return json.loads(resp.text) if resp.text.strip() else {}


class RateLimiter:
    """未认证搜索接口只有 10 次/分钟，必须主动控速。"""

    def __init__(self, interval: float):
        self.interval = interval
        self._last = 0.0

    def wait(self) -> None:
        gap = time.time() - self._last
        if gap < self.interval:
            time.sleep(self.interval - gap)
        self._last = time.time()


def gh_get(url: str, token: str = "", timeout: int = 60, retries: int = 4,
           show_progress: bool = True):
    headers = {"X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    for attempt in range(1, retries + 1):
        try:
            resp = _request_raw(url, headers=headers, timeout=timeout)
        except ApiError:
            if attempt < retries:
                if show_progress:
                    log(f"  ⚠ 请求失败，{2 * attempt}s 后重试（{attempt}/{retries}）…")
                time.sleep(2 * attempt)
                continue
            raise

        if resp.status == 200:
            try:
                return json.loads(resp.text) if resp.text.strip() else {}
            except json.JSONDecodeError:
                # 公共镜像偶发返回 200 + HTML 限流页，或传输被截断。重试一次往往就好了。
                if attempt < retries:
                    if show_progress:
                        log(f"  ⚠ 响应不是有效 JSON（长度 {len(resp.text)}），{2 * attempt}s 后重试（{attempt}/{retries}）…")
                    time.sleep(2 * attempt)
                    continue
                raise ApiError(
                    f"GitHub 返回了非 JSON 内容（长度 {len(resp.text)}）。"
                    "常见原因：镜像线路返回了限流页、传输被截断，或该镜像不支持 API 代理")

        if resp.status in (403, 429):
            remaining = resp.headers.get("x-ratelimit-remaining")
            reset = resp.headers.get("x-ratelimit-reset")
            reset_epoch = int(reset) if reset and reset.isdigit() else None

            if remaining == "0" and reset_epoch:
                wait = max(reset_epoch - int(time.time()), 0) + 3
                if attempt < retries and wait <= 120:
                    if show_progress:
                        log(f"  ⚠ 触发 GitHub 限流，等待 {wait}s 后重试（{attempt}/{retries}）…")
                    time.sleep(wait)
                    continue
                when = datetime.fromtimestamp(reset_epoch).strftime("%H:%M:%S")
                hint = ("配置 GITHUB_TOKEN 可把搜索配额从 10 次/分钟提到 30 次/分钟"
                        if not token else "请稍后再试，或调小 core_pages / new_pages")
                raise RateLimited(
                    f"GitHub 配额已用尽（将于 {when} 重置）。{hint}", reset_epoch)

        raise ApiError(f"GitHub 接口返回 {resp.status}：{resp.text[:300]}")

    raise ApiError("请求失败")


# --- API 线路探测：判断哪条线路真的能返回 GitHub 数据 --------------------

def _probe_one(base: str, timeout: int = 8) -> int:
    """用 /rate_limit 做探针：轻量、不消耗搜索配额。成功返回延迟（毫秒）。

    必须校验返回内容，而不能只看 HTTP 状态码 —— 镜像站点在不可用时
    常常返回 200 + 一个 HTML 错误页，光看状态码会误判为"可用"。
    """
    url = base.rstrip("/") + "/rate_limit"
    t0 = time.perf_counter()
    resp = _request_raw(url, headers={"Accept": "application/vnd.github+json"},
                        timeout=timeout)
    if resp.status != 200:
        raise ApiError(f"HTTP {resp.status}")
    try:
        data = json.loads(resp.text)
    except json.JSONDecodeError:
        raise ApiError("返回的不是 JSON（可能被劫持，或镜像不支持 API 代理）")
    if "resources" not in data:
        raise ApiError("响应缺少 GitHub API 特征字段（镜像可能只代理网页/下载）")
    return max(int((time.perf_counter() - t0) * 1000), 1)


def build_candidates(cfg: dict) -> list[dict]:
    """自定义线路优先，其次内置候选，再其次用户追加的备用线路。"""
    gh = cfg.get("github") or {}
    custom = (gh.get("api_base") or "").strip().rstrip("/")
    extras = [str(x).strip().rstrip("/") for x in (gh.get("extra_endpoints") or [])
              if str(x).strip()]

    out: list[dict] = []
    seen: set[str] = set()
    if custom:
        out.append({"id": "custom", "label": "自定义线路", "base": custom, "custom": True})
        seen.add(custom)
    for item in ENDPOINT_CANDIDATES:
        if item["base"] in seen:
            continue
        out.append(dict(item))
        seen.add(item["base"])
    for i, base in enumerate(extras, 1):
        if base in seen:
            continue
        out.append({"id": f"extra{i}", "label": f"自定义备用 {i}", "base": base})
        seen.add(base)
    return out


def probe_endpoints(cfg: dict, timeout: int = 8, show_progress: bool = True) -> list[dict]:
    """并发探测所有线路，返回值仍按候选优先级排序。

    串行探测的代价太大：七八条候选 × 8s 超时，最坏要等近一分钟才出结果，
    用户会以为程序卡死。并发之后最坏只等一个超时周期。
    返回值顺序不变，调用方取第一个 ok 的，语义完全一样（直连能用就不走第三方）。
    """
    cands = build_candidates(cfg)
    if not cands:
        return []

    global _QUIET_TRANSPORT
    prev_quiet = _QUIET_TRANSPORT
    _QUIET_TRANSPORT = True      # 探测期间不刷传输层回退日志，避免看起来像报错

    results: dict[str, dict] = {}
    try:
        with ThreadPoolExecutor(max_workers=min(len(cands), 6)) as pool:
            futures = {pool.submit(_probe_one, c["base"], timeout): c for c in cands}
            for fut in as_completed(futures):
                cand = futures[fut]
                try:
                    ms = fut.result()
                    results[cand["base"]] = {**cand, "ok": True, "latency_ms": ms}
                    if show_progress:
                        log(f"  ✔ {cand['label']}  可用（{ms}ms）")
                except Exception as e:
                    msg = str(e) or type(e).__name__
                    results[cand["base"]] = {**cand, "ok": False, "error": msg[:160]}
                    if show_progress:
                        log(f"  ✘ {cand['label']}  {msg[:90]}")
    finally:
        _QUIET_TRANSPORT = prev_quiet

    return [results[c["base"]] for c in cands if c["base"] in results]


def _load_endpoint_cache(max_age_hours: int = 24) -> dict | None:
    try:
        data = json.loads(ENDPOINT_CACHE_PATH.read_text(encoding="utf-8"))
        ts = datetime.fromisoformat(data["checked_at"])
        if (now_local() - ts).total_seconds() > max_age_hours * 3600:
            return None
        return data
    except Exception:
        return None


def _save_endpoint_cache(info: dict) -> None:
    try:
        ENDPOINT_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        ENDPOINT_CACHE_PATH.write_text(
            json.dumps({**info, "checked_at": now_local().isoformat(timespec="seconds")},
                       ensure_ascii=False, indent=1),
            encoding="utf-8")
    except OSError:
        pass


def ensure_api_base(cfg: dict, force: bool = False, show_progress: bool = False) -> str:
    """确定本次使用的 API 线路。全部不通时抛出带处理建议的 ApiError。"""
    global API_BASE, API_INFO

    gh = cfg.get("github") or {}
    custom = (gh.get("api_base") or "").strip().rstrip("/")

    # 用户手动指定了线路 —— 尊重它，不探测（他大概率有自己的加速器）
    if custom and not force:
        API_BASE = custom
        API_INFO = {"id": "custom", "label": "自定义线路", "base": custom,
                    "latency_ms": None, "probed": False, "custom": True}
        return API_BASE

    # 24 小时内的探测结果可以直接复用，省掉每次启动的等待
    if not force:
        cached = _load_endpoint_cache()
        if cached and cached.get("base"):
            API_BASE = cached["base"]
            API_INFO = {**cached, "probed": True}
            if show_progress:
                log(f"  线路：{API_INFO.get('label', '?')}"
                    f"（{API_INFO.get('latency_ms')}ms，缓存结果）")
            return API_BASE

    if show_progress:
        log("  正在探测可用线路（国内直连 GitHub 常常不通，会自动换镜像）…")
    results = probe_endpoints(cfg, show_progress=show_progress)
    winner = next((r for r in results if r.get("ok")), None)

    if not winner:
        detail = "\n".join(
            f"    · {r['label']}：{r.get('error', '失败')}" for r in results[:4])
        raise ApiError(
            "所有 GitHub 线路都不通，本次无法抓取新数据。\n"
            f"{detail}\n"
            "  可以这样处理：\n"
            "    1) 打开加速器/代理后重试；\n"
            "    2) 在 config.json 的 github.api_base 填一个能访问的地址；\n"
            "    3) 运行 python ghtrend.py netcheck 查看各线路探测详情。\n"
            "  （界面仍可查看本地已保存的历史快照）"
        )

    API_BASE = winner["base"]
    API_INFO = {**winner, "probed": True}
    _save_endpoint_cache(API_INFO)
    if show_progress:
        log(f"  线路已选定：{API_INFO['label']}（{API_INFO.get('latency_ms')}ms）")
    return API_BASE


def _page_cache_file(query: str, page: int, per_page: int) -> Path:
    import hashlib
    key = hashlib.md5(f"{API_BASE}|{query}|{page}|{per_page}".encode("utf-8")).hexdigest()[:20]
    return PAGE_CACHE_DIR / f"{key}.json"


def _page_cache_get(query: str, page: int, per_page: int, show_progress: bool = True):
    """返回缓存的这一页数据；无缓存或已过期返回 None。"""
    path = _page_cache_file(query, page, per_page)
    try:
        if time.time() - path.stat().st_mtime > PAGE_CACHE_TTL:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        items = data.get("items")
        if isinstance(items, list) and items:
            if show_progress:
                log(f"  · 复用缓存 page {page}（{len(items)} 条，线路相同时有效）")
            return items
    except (OSError, ValueError, KeyError):
        pass
    return None


def _page_cache_set(query: str, page: int, per_page: int, items: list[dict]) -> None:
    """立刻落盘。失败也不影响抓取主流程。"""
    if not items:
        return
    try:
        path = _page_cache_file(query, page, per_page)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"query": query, "page": page, "base": API_BASE,
                                    "items": items}, ensure_ascii=False),
                        encoding="utf-8")
    except OSError:
        pass


def clear_page_cache() -> int:
    """清空页缓存（换线或用户主动强制刷新时用）。"""
    n = 0
    try:
        for f in PAGE_CACHE_DIR.glob("*.json"):
            try:
                f.unlink()
                n += 1
            except OSError:
                pass
    except OSError:
        pass
    return n


def search_repos(query: str, pages: int, token: str, limiter: RateLimiter,
                 per_page: int = 100, show_progress: bool = True,
                 timeout: int = 60) -> list[dict]:
    """GitHub Search API。单查询最多返回 1000 条，超出部分物理上拿不到。"""
    items: list[dict] = []
    for page in range(1, pages + 1):
        cached = _page_cache_get(query, page, per_page, show_progress=show_progress)
        if cached is not None:
            items.extend(cached)
            if len(cached) < per_page:
                break
            continue

        url = (f"{API_BASE}/search/repositories?q={parse.quote(query)}"
               f"&sort=stars&order=desc&per_page={per_page}&page={page}")
        limiter.wait()
        if show_progress:
            log(f"  · 抓取中 page {page}/{pages}  →  {query}")
        data = gh_get(url, token=token, timeout=timeout, show_progress=show_progress)
        batch = data.get("items") or []
        _page_cache_set(query, page, per_page, batch)      # 先落盘再往下走
        items.extend(batch)
        if len(batch) < per_page:
            break
    return items


# ---------------------------------------------------------------------------
# 4. 仓库数据结构
# ---------------------------------------------------------------------------

def normalize(item: dict) -> dict:
    return {
        "full_name": item.get("full_name", ""),
        "stars": int(item.get("stargazers_count") or 0),
        "forks": int(item.get("forks_count") or 0),
        "issues": int(item.get("open_issues_count") or 0),
        "language": item.get("language") or "",
        "description": (item.get("description") or "").strip(),
        "topics": list(item.get("topics") or []),
        "created_at": item.get("created_at") or "",
        "pushed_at": item.get("pushed_at") or "",
        "url": item.get("html_url") or "",
        "archived": bool(item.get("archived")),
    }


def fetch_pool(cfg: dict, show_progress: bool = True) -> list[dict]:
    """抓取候选池。自动选择可用线路；整条线路失败时换线重试一次。"""
    global PROXY_URL
    gh = cfg["github"]
    PROXY_URL = (gh.get("proxy") or "").strip()
    if PROXY_URL and show_progress:
        log(f"  使用代理：{PROXY_URL}")
    if show_progress:
        log(f"① 抓取候选池（预计 {gh['core_pages'] + gh['new_pages']} 次 API 调用）")

    ensure_api_base(cfg, show_progress=show_progress)

    try:
        return _fetch_pool_once(cfg, show_progress)
    except ApiError as e:
        if (cfg.get("github") or {}).get("api_base"):
            raise            # 用户手动指定了线路，不要偷偷换成第三方
        if show_progress:
            log(f"  ⚠ 当前线路抓取失败：{e}")
            log("  → 重新探测线路，再试一次…")
        ensure_api_base(cfg, force=True, show_progress=show_progress)
        return _fetch_pool_once(cfg, show_progress)


def _fetch_pool_once(cfg: dict, show_progress: bool = True) -> list[dict]:
    gh = cfg["github"]
    tmo = int(gh.get("timeout") or 60)
    # 未认证时搜索接口只有 10 次/分钟。原来的 6.5s 间隔等于贴着上限跑，
    # 稍微重试一下就撞 403；放宽到 8s 反而更稳。
    limiter = RateLimiter(2.5 if gh["token"] else 8.0)

    core_q = (f"stars:>={gh['core_min_stars']} "
              f"pushed:>={utc_date_minus(gh['push_window_days'])}")
    new_q = (f"stars:>={gh['new_min_stars']} "
             f"created:>={utc_date_minus(gh['created_window_days'])}")

    merged: dict[str, dict] = {}

    for q, pages, label in ((core_q, gh["core_pages"], "核心池"),
                            (new_q, gh["new_pages"], "新星池")):
        try:
            for raw in search_repos(q, pages, gh["token"], limiter,
                                    show_progress=show_progress, timeout=tmo):
                rec = normalize(raw)
                if rec["full_name"]:
                    merged[rec["full_name"]] = rec
        except RateLimited as e:
            log(f"  ⚠ {label} 抓取被限流中断：{e}")
            if not merged:
                raise
        except ApiError as e:
            log(f"  ⚠ {label} 抓取失败：{e}")
            if not merged:
                raise

    if show_progress:
        log(f"   ✔ 候选池去重后 {len(merged)} 个仓库")
    return sorted(merged.values(), key=lambda r: r["stars"], reverse=True)


# ---------------------------------------------------------------------------
# 5. 存储层
# ---------------------------------------------------------------------------

def snapshot_path(data_dir: Path, day: str) -> Path:
    return data_dir / "snapshots" / f"{day}.json"


def save_snapshot(data_dir: Path, day: str, repos: list[dict], source: str = "github") -> Path:
    path = snapshot_path(data_dir, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "date": day,
        "generated_at": now_local().isoformat(timespec="seconds"),
        "source": source,
        "count": len(repos),
        "repos": repos,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def load_snapshot(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def list_snapshots(data_dir: Path) -> list[Path]:
    d = data_dir / "snapshots"
    if not d.exists():
        return []
    return sorted(d.glob("*.json"), key=lambda p: p.stem)


def find_baseline(data_dir: Path, day: str) -> Path | None:
    """找最近一个「严格早于」今天的快照作为基准。"""
    earlier = [p for p in list_snapshots(data_dir) if p.stem < day]
    return earlier[-1] if earlier else None


SEED_DIR_NAME = "seed"
SEED_SNAPSHOT_LIMIT = 3


def ensure_seed_snapshots(data_dir: Path) -> list[str]:
    """首次运行时把随包分发的种子快照拷进 data/snapshots。

    为什么需要：日增榜要两份快照才能算。没有种子的话，别人第一次打开
    看到的是「总 star 排行榜」——一个跟 Trending 完全无关的东西，
    要等到第二天抓了第二份数据才像样。第一印象直接决定他还开不开第二次。

    约定：
      · 只在 data/snapshots 为空时复制，绝不覆盖任何真实数据。
      · 种子数据的日期保持原样，不篡改。界面上会照常标注基准日和
        「中间缺 N 天，已按日均折算」，宁可说清也不假装是当天的。
    """
    seed_dir = APP_DIR / SEED_DIR_NAME
    if not seed_dir.is_dir():
        return []
    snap_dir = data_dir / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    if any(snap_dir.glob("*.json")):
        return []                      # 已有真实数据，一个字节都不动

    copied: list[str] = []
    for p in sorted(seed_dir.glob("*.json"))[-SEED_SNAPSHOT_LIMIT:]:
        try:
            shutil.copy2(p, snap_dir / p.name)
            copied.append(p.stem)
        except OSError as exc:
            log(f"  ⚠ 复制种子快照失败（{p.name}）：{exc}")
    return copied


def cleanup_old(data_dir: Path, retention_days: int) -> int:
    """清理超期快照与报告，避免磁盘无限增长（PRD R12）。"""
    if retention_days <= 0:
        return 0
    cutoff = (now_local() - timedelta(days=retention_days)).strftime("%Y-%m-%d")
    removed = 0
    for sub in ("snapshots", "reports"):
        d = data_dir / sub
        if not d.exists():
            continue
        for p in d.glob("*"):
            if p.suffix in (".json", ".md") and p.stem < cutoff:
                try:
                    p.unlink()
                    removed += 1
                except OSError:
                    pass
    return removed


def load_summary_cache(data_dir: Path) -> dict:
    p = data_dir / "summaries.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_summary_cache(data_dir: Path, cache: dict) -> None:
    p = data_dir / "summaries.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------------------
# 6. 计算层
# ---------------------------------------------------------------------------

def compute_daily_delta(today: list[dict], baseline: list[dict], days_gap: int) -> list[dict]:
    """Δ = 今日 star − 基准快照 star。

    只有当仓库在两次快照中都出现时才能算增量 —— 这正是「候选池」策略的
    天然优势：池子按 star 总量排序，短期波动不会让高增速项目掉出池外。

    基准间隔超过 1 天时归一化为「日均增量」，并在报告中显式标注实际间隔，
    避免用 7 天的累计增长去和 1 天的增长做不公平比较。
    """
    base = {r["full_name"]: r for r in baseline}
    out = []
    for r in today:
        b = base.get(r["full_name"])
        if not b:
            continue
        delta = r["stars"] - b["stars"]
        out.append({
            **r,
            "delta": delta,
            "per_day": round(delta / days_gap, 2) if days_gap > 0 else float(delta),
            "prev_stars": b["stars"],
            "delta_ratio": round(delta / b["stars"] * 100, 2) if b["stars"] else 0.0,
        })
    return out


def compute_velocity(repos: list[dict]) -> list[dict]:
    """无基线降级方案：用 star ÷ 项目年龄 衡量「爆发速度」。

    这不是真正的日增，但是一个诚实且有信息量的替代 —— 一个 3 个月前创建
    却已有 2 万星的项目，其增长必然极快。
    """
    now = datetime.now(timezone.utc)
    out = []
    for r in repos:
        created = parse_iso(r.get("created_at", ""))
        age = max((now - created).days, 1) if created else 1
        out.append({**r, "age_days": age,
                    "velocity": round(r["stars"] / age, 2),
                    "delta": None})
    return out


def apply_filters(rows: list[dict], language: str = "", min_delta: int = 0) -> list[dict]:
    res = rows
    if language:
        lang = language.strip().lower()
        res = [r for r in res if (r.get("language") or "").lower() == lang]
    if min_delta > 0:
        res = [r for r in res if (r.get("delta") or 0) >= min_delta]
    return res


def sort_rows(rows: list[dict], mode: str) -> list[dict]:
    if mode == "delta":
        return sorted(rows, key=lambda r: (r.get("delta") or 0), reverse=True)
    return sorted(rows, key=lambda r: r.get("velocity") or 0, reverse=True)


# ---------------------------------------------------------------------------
# 7. 增强层：中文摘要（LLM 优先 → 规则引擎降级）
# ---------------------------------------------------------------------------

# 摘要缓存逻辑版本：任何影响摘要生成结果的改动都要 +1，否则会命中旧缓存
SUMMARY_CACHE_VERSION = "3"

LANG_ZH = {
    "Python": "Python", "JavaScript": "JavaScript", "TypeScript": "TypeScript",
    "Rust": "Rust", "Go": "Go", "Java": "Java", "C": "C", "C++": "C++",
    "C#": "C#", "Ruby": "Ruby", "PHP": "PHP", "Swift": "Swift", "Kotlin": "Kotlin",
    "Shell": "Shell", "Jupyter Notebook": "Jupyter Notebook", "Vue": "Vue",
    "HTML": "HTML", "CSS": "CSS", "Dart": "Dart", "Zig": "Zig", "Lua": "Lua",
    "Scala": "Scala", "Haskell": "Haskell", "Elixir": "Elixir", "Nim": "Nim",
}

# 常见 topic → 中文。覆盖 GitHub 高频主题词，让离线摘要也具备可读性。
TOPIC_ZH = {
    "ai": "人工智能", "artificial-intelligence": "人工智能", "machine-learning": "机器学习",
    "deep-learning": "深度学习", "llm": "大语言模型", "llms": "大语言模型",
    "gpt": "GPT", "chatgpt": "ChatGPT", "openai": "OpenAI", "rag": "检索增强生成(RAG)",
    "agent": "智能体(Agent)", "agents": "智能体(Agent)", "ai-agents": "AI 智能体",
    "mcp": "MCP 协议", "transformer": "Transformer", "nlp": "自然语言处理",
    "computer-vision": "计算机视觉", "diffusion": "扩散模型", "embeddings": "向量嵌入",
    "vector-database": "向量数据库", "prompt-engineering": "提示词工程",
    "fine-tuning": "模型微调", "inference": "推理引擎", "pytorch": "PyTorch",
    "tensorflow": "TensorFlow", "huggingface": "HuggingFace",
    "cli": "命令行工具", "command-line": "命令行工具", "terminal": "终端",
    "developer-tools": "开发者工具", "devtools": "开发者工具", "tooling": "工具链",
    "framework": "框架", "library": "软件库", "api": "API", "sdk": "SDK",
    "web": "Web 开发", "frontend": "前端", "backend": "后端", "fullstack": "全栈",
    "react": "React", "vue": "Vue", "nextjs": "Next.js", "nodejs": "Node.js",
    "database": "数据库", "sql": "SQL", "nosql": "NoSQL", "postgres": "PostgreSQL",
    "redis": "Redis", "sqlite": "SQLite", "orm": "ORM",
    "docker": "Docker", "kubernetes": "Kubernetes", "devops": "DevOps",
    "serverless": "Serverless", "cloud": "云计算", "self-hosted": "自托管",
    "security": "安全", "cybersecurity": "网络安全", "privacy": "隐私保护",
    "hacking": "渗透测试", "pentesting": "渗透测试", "malware": "恶意软件分析",
    "automation": "自动化", "workflow": "工作流", "productivity": "效率工具",
    "monitoring": "监控", "observability": "可观测性", "logging": "日志",
    "data-science": "数据科学", "data-analysis": "数据分析", "visualization": "数据可视化",
    "analytics": "数据分析", "etl": "数据管道(ETL)", "big-data": "大数据",
    "awesome": "精选资源合集", "awesome-list": "精选资源合集", "tutorial": "教程",
    "tutorials": "教程", "guide": "指南", "book": "书籍", "course": "课程",
    "learning": "学习资料", "cheatsheet": "速查表", "interview": "面试准备",
    "boilerplate": "项目脚手架", "template": "模板", "starter": "起步模板",
    "mobile": "移动端", "android": "Android", "ios": "iOS", "flutter": "Flutter",
    "game": "游戏", "gamedev": "游戏开发", "graphics": "图形渲染",
    "video": "视频处理", "audio": "音频处理", "image": "图像处理",
    "blockchain": "区块链", "crypto": "加密货币", "web3": "Web3",
    "browser": "浏览器", "chrome-extension": "浏览器扩展", "vscode": "VS Code 插件",
    "neovim": "Neovim 插件", "plugin": "插件", "emulator": "模拟器",
    "operating-system": "操作系统", "kernel": "内核", "embedded": "嵌入式",
    "iot": "物联网", "robotics": "机器人", "3d": "3D", "ar": "AR", "vr": "VR",
    "language": "编程语言", "compiler": "编译器", "interpreter": "解释器",
    "regex": "正则表达式", "parser": "解析器", "serialization": "序列化",
    "http": "HTTP", "websocket": "WebSocket", "proxy": "代理", "vpn": "VPN",
    "search": "搜索", "elasticsearch": "Elasticsearch", "crawler": "爬虫",
    "testing": "测试", "debugging": "调试", "profiling": "性能分析",
    "performance": "性能优化", "concurrency": "并发", "distributed": "分布式",
    "microservices": "微服务", "queue": "消息队列", "caching": "缓存",
    "pdf": "PDF 处理", "markdown": "Markdown", "notes": "笔记", "wiki": "知识库",
    "knowledge-base": "知识库", "documentation": "文档工具",
    "finance": "金融科技", "trading": "量化交易", "education": "教育",
    "ecommerce": "电商", "cms": "内容管理系统", "chatbot": "聊天机器人",
}

# 描述关键词 → 中文分类（命中即用，优先级从高到低）
# 注意：必须按「词边界」匹配。早期版本用子串匹配，结果 "video" 里的 "ide"
# 把图片管理项目误判成「编辑器/IDE」，"engineering" 里的 "engine" 误判成「引擎」。
CATEGORY_RULES: list[tuple[tuple[str, ...], str]] = [
    (("awesome", "curated list", "collection of"), "精选资源合集"),
    (("cheat sheet", "cheatsheet"), "速查表"),
    (("tutorial", "tutorials", "a book", "guide to", "course", "courses"), "教程 / 学习资料"),
    (("boilerplate", "starter", "template for"), "项目脚手架"),
    (("framework", "frameworks"), "开发框架"),
    (("toolkit", "toolbox", "tool suite"), "工具集"),
    (("command line", "command-line", "cli tool", "cli for", "cli to", "terminal"), "命令行工具"),
    (("library", "libraries", "package for", "packages for"), "软件库"),
    (("platform", "platforms"), "平台"),
    (("engine", "engines", "runtime"), "引擎"),
    (("database", "databases", "dbms", "key-value store"), "数据库"),
    (("server", "servers", "backend service"), "服务端"),
    (("browser extension", "chrome extension"), "浏览器扩展"),
    # 「照片/图像工具」必须排在通用「编辑器/IDE」之前，否则 "photo editor"
    # 会被判成「编辑器」，把一个图片工具误导成代码编辑器。
    (("photo", "photos", "image editor", "photo editor", "image processing"), "图像 / 照片工具"),
    (("editor", "editors", "ide", "ides"), "编辑器 / IDE"),
    (("proxy", "gateway"), "代理 / 网关"),
    (("protocol", "protocols", "specification"), "协议 / 规范"),
    (("llm", "llms", "language model", "language models", "gpt", "genai",
      "generative ai"), "大语言模型应用"),
    (("ai agent", "ai agents", "agentic", "autonomous agent"), "AI 智能体"),
    (("rag", "retrieval-augmented"), "检索增强生成(RAG)"),
    (("machine learning", "deep learning", "neural network", "neural networks"), "机器学习"),
    (("text to speech", "speech recognition", "voice"), "语音处理"),
    (("image generation", "text-to-image", "diffusion model"), "图像生成"),
    (("video", "ffmpeg", "streaming"), "视频处理"),
    (("scraper", "crawler", "spider"), "爬虫 / 抓取"),
    (("monitoring", "observability", "metrics"), "可观测性"),
    (("automation", "automate"), "自动化工具"),
    (("alternative to", "drop-in replacement"), "替代方案"),
    (("notes", "note-taking", "knowledge base"), "知识管理"),
    (("operating system", "kernel"), "操作系统"),
    (("game", "games", "gaming"), "游戏"),
    (("emulator",), "模拟器"),
    (("compiler", "interpreter", "transpiler"), "编译器"),
    (("language server", "linter", "formatter"), "代码检查 / 格式化"),
    (("test", "tests", "testing", "test runner"), "测试工具"),
]


def _has_keyword(text: str, keys: tuple[str, ...]) -> bool:
    """按词边界匹配，避免 'video' → 'ide'、'engineering' → 'engine' 这类误判。"""
    for k in keys:
        if re.search(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])", text):
            return True
    return False


def rule_summary(repo: dict) -> str:
    """规则引擎：语言 + 分类 + 主题 → 中文说明。

    这是「没有 LLM 也不能给用户看英文」的兜底。不假装能做语义翻译，
    但把可确定的信息（技术栈、领域、项目类型）转成中文骨架，并保留原描述。
    """
    parts: list[str] = []
    lang = repo.get("language") or ""
    if lang:
        parts.append(f"{LANG_ZH.get(lang, lang)} 项目")

    desc_l = (repo.get("description") or "").lower()
    name_l = (repo.get("full_name") or "").lower()

    for keys, label in CATEGORY_RULES:
        if _has_keyword(desc_l, keys):
            parts.append(label)
            break
    else:
        if "awesome" in name_l:
            parts.append("精选资源合集")

    topics_zh: list[str] = []
    for t in repo.get("topics") or []:
        zh = TOPIC_ZH.get(t.lower())
        if zh and zh not in topics_zh and zh not in parts:
            topics_zh.append(zh)
        if len(topics_zh) >= 3:
            break
    if not topics_zh and parts:
        pass
    head = "，".join(parts) if parts else "开源项目"
    if topics_zh:
        head += f"，涉及「{'、'.join(topics_zh)}」"

    desc = repo.get("description") or ""
    if desc:
        desc = truncate(desc, 150)
        return f"{head}。官方描述：{desc}"
    return f"{head}。（该项目未填写描述）"


def llm_summary(repo: dict, llm_cfg: dict) -> str | None:
    base = (llm_cfg.get("base_url") or "").rstrip("/")
    key = llm_cfg.get("api_key") or ""
    if not base or not key:
        return None

    topics = "、".join((repo.get("topics") or [])[:8])
    user_prompt = (
        f"仓库全名：{repo.get('full_name')}\n"
        f"编程语言：{repo.get('language') or '未知'}\n"
        f"官方描述：{repo.get('description') or '（无）'}\n"
        f"主题标签：{topics or '（无）'}"
    )
    payload = {
        "model": llm_cfg.get("model") or "gpt-4o-mini",
        "temperature": 0.3,
        "max_tokens": 160,
        "messages": [
            {"role": "system",
             "content": ("你是技术资讯编辑，为中文开发者写 GitHub 项目速览。"
                         "请用一句话（40~70 个汉字）说明：这个项目是什么、解决什么问题、"
                         "有什么亮点。直接输出这句话，不要任何前缀、引号或 Markdown 标记。"
                         "不要臆造描述中没有的功能。")},
            {"role": "user", "content": user_prompt},
        ],
    }
    try:
        data = _http(f"{base}/chat/completions", method="POST", payload=payload,
                     headers={"Authorization": f"Bearer {key}"},
                     timeout=int(llm_cfg.get("timeout", 40)))
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        text = (text or "").strip().strip('"').replace("\n", " ")
        return text or None
    except Exception as e:  # 任何 LLM 故障都不应阻断主流程
        log(f"  ⚠ LLM 摘要失败（{e}），本条降级为规则引擎")
        return None


# --- 人话总结：给圈外人的标题 + 详述 ---------------------------------------

PLAIN_CACHE_VERSION = "5"      # 改了标题/总结的生成逻辑就必须 +1，否则命中旧缓存
PLAIN_CACHE_FILE = "plain_summaries.json"
README_MAX_CHARS = 6000


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save_json(path: Path, data) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def clean_readme(text: str) -> str:
    """把 README 洗成纯文字：去徽章、图片、HTML 标签、代码块，然后截断。

    README 里大量内容是给开发者看的（安装命令、徽章墙、架构图），
    对"圈外人看得懂"没有任何帮助，反而会干扰模型判断这项目是干什么的。
    """
    t = text or ""
    t = re.sub(r"```.*?```", " ", t, flags=re.S)                   # 代码块
    t = re.sub(r"\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)", " ", t)        # 徽章 [![a](b)](c)
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", t)                    # 图片
    t = re.sub(r"<!--.*?-->", " ", t, flags=re.S)                  # HTML 注释
    t = re.sub(r"<[^>]+>", " ", t)                                 # HTML 标签
    t = re.sub(r"^\s*[-=*_]{3,}\s*$", " ", t, flags=re.M)          # 分隔线
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()[:README_MAX_CHARS]


def fetch_readme(full_name: str, timeout: int = 15) -> str:
    """优先走 raw 域名（不消耗 API 配额），失败再退回官方 API。"""
    urls = [
        f"https://raw.githubusercontent.com/{full_name}/HEAD/README.md",
        f"https://gh-proxy.com/https://raw.githubusercontent.com/{full_name}/HEAD/README.md",
    ]
    for u in urls:
        try:
            resp = _request_raw(u, timeout=timeout)
            if resp.status == 200 and len(resp.text.strip()) > 40:
                return clean_readme(resp.text)
        except Exception:
            continue
    try:
        resp = _request_raw(f"{API_BASE}/repos/{full_name}/readme",
                            headers={"Accept": "application/vnd.github.raw"},
                            timeout=timeout)
        if resp.status == 200 and len(resp.text.strip()) > 40:
            return clean_readme(resp.text)
    except Exception:
        pass
    return ""


def rule_title(repo: dict) -> str:
    """没有 LLM 时的标题降级。

    理想标题是"它能帮你干什么"的一句话，那必须读懂 README，规则引擎做不到。
    这里只保证：至少是一句人话（「一个用 Rust 写的图像工具」），
    而不是一个光秃秃的名词，也不假装自己是 AI 生成的。
    """
    cat = ""
    desc_l = (repo.get("description") or "").lower()
    for keys, label in CATEGORY_RULES:
        if _has_keyword(desc_l, keys):
            cat = label
            break
    if not cat:
        for t in repo.get("topics") or []:
            zh = TOPIC_ZH.get(t.lower())
            if zh:
                cat = zh
                break

    lang = repo.get("language") or ""
    lang_zh = LANG_ZH.get(lang, lang)

    if cat and lang_zh:
        return f"一个用 {lang_zh} 写的{cat}"
    if cat:
        return f"一个{cat}"
    if lang_zh:
        return f"一个用 {lang_zh} 写的开源项目"
    return "一个开源项目"


PLAIN_SYSTEM_PROMPT = """【任务一：给这一行起一个标题】
用一句 25–45 个字的中文，说清「这是一个什么」或者「它能帮你干什么」。
- 不要只写分类名词（如「图像处理工具」「AI 智能体」「命令行工具」），那等于没说。
- 可以用比喻，让完全不懂技术的人一眼有画面感。
- 必须是一句完整的话，不要压缩成名词短语。
- 同样只依据 README，不编造。
正例：像请了个随叫随到的私人助理，把电脑里散落的文件自动归位
反例：文件管理工具

【任务二：写详细总结】
你是一位资深开源项目分析者。请阅读下面的 GitHub 项目 README，写一段中文总结，200–300 字。
## 输出结构（四段，连成一段通顺文字，不要小标题）
1. 一句话定位：这个项目是什么、给谁用。
2. 解决的问题：它替代或简化了哪类原有痛点。
3. 核心用法：最主要的 1–2 个使用方式或命令（README 有才写，没有就跳过）。
4. 适用场景与门槛：什么情况下值得用，有什么前置条件（语言、依赖、平台、License）。
## 硬性要求
- 只使用 README 中出现的信息，不补充你的先验知识；README 没写的一律不编。
- 不吹不黑，禁用「强大」「革命性」「一站式」「赋能」这类营销词。
- 技术名词保留英文原文（Docker、CLI、API、SDK）。
- 版本号、数字、命令必须与原文完全一致。
- 总字数严格控制在 200–300 字；README 信息不够时宁可写短，不要凑字灌水。
- 结尾不要加「总之/综上所述」的总结句。

【字数自检（输出前必做，不要跳过）】
detail 必须落在 200–300 字之间，这是硬门槛。按下面分配篇幅，加起来正好约 240 字：
- 第 1 段（是什么、给谁用）：约 50 字
- 第 2 段（解决什么痛点）：约 60 字
- 第 3 段（核心用法，README 没写就并入其他段）：约 60 字
- 第 4 段（适用场景与前置条件）：约 70 字
写完逐字数一遍（中文标点各算 1 字，英文按字符数算）：
- 超过 300 字 → 删掉功能清单、实现细节这类次要内容，压到 300 以内再输出
- 不足 200 字 → 只补充 README 里确实写了的必要信息
- 超过 300 字的回答视为不合格

【输出格式】
只输出 JSON，不要任何多余文字：
{"title": "任务一的标题", "detail": "任务二的详细总结"}"""


COMPRESS_SYSTEM_PROMPT = "你是中文编辑。只输出改写后的正文，不要任何解释、小标题或「总之」式的收尾。"
# 踩过的坑（实测数据，别改回去）：
#   「压缩到 250 字」        → 359 字，几乎没压
#   「改写成 250 字以内」    → 362 字，没压
#   「重新表述，不超过 250 字」→ 269 字 ✔
#   「总结核心内容，250 字以内」→ 259 字 ✔
# 结论：用「重新表述 / 总结」而不是「压缩」，并且给单一上限而不是区间。
# 模型还爱奔着区间上沿写，所以上限要比目标低 50 字左右。


def _compress_detail(detail: str, llm_cfg: dict, ceiling: int = 240,
                     rounds: int = 2) -> str:
    base = (llm_cfg.get("base_url") or "").rstrip("/")
    key = llm_cfg.get("api_key") or ""
    current = detail
    if not base:
        return current

    for _ in range(rounds):
        n = len(current)
        if n <= 305:
            break
        user = (
            f"请用不超过 {ceiling} 字重新表述下面这段内容，保留关键信息。\n"
            "必须保住这四类信息：是什么、给谁用；解决了什么痛点；核心用法；适用场景与前置条件。\n"
            "软件名、网址、版本号、命令不允许改动，也不许新增原文没有的信息。\n\n"
            f"{current}"
        )
        payload = {
            "model": llm_cfg.get("model") or "gpt-4o-mini",
            "temperature": 0.5,
            "max_tokens": 700,
            "messages": [
                {"role": "system", "content": COMPRESS_SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
        }
        try:
            data = _http(f"{base}/chat/completions", method="POST", payload=payload,
                         headers={"Authorization": f"Bearer {key}"},
                         timeout=int(llm_cfg.get("timeout", 40)))
            got = ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "")
        except Exception as e:
            log(f"  ⚠ 压缩失败（{e}），保留原长度")
            break

        got = (got or "").strip()
        # 没明显变短 = 模型没执行，再试也是空转
        if len(got) < 120 or len(got) >= n * 0.95:
            break
        current = got

    return current


def _parse_json_block(text: str) -> dict | None:
    """模型有时会把 JSON 包在 ``` 里，或前后加解释。宽松解析。"""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.M).strip()
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        obj = json.loads(t[i:j + 1])
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def llm_plain_summary(repo: dict, readme: str, llm_cfg: dict) -> dict | None:
    base = (llm_cfg.get("base_url") or "").rstrip("/")
    key = llm_cfg.get("api_key") or ""
    if not base or not key:
        return None

    user = (
        f"项目名：{repo.get('full_name')}\n"
        f"官方描述：{repo.get('description') or '（无）'}\n"
        f"主题标签：{'、'.join((repo.get('topics') or [])[:8]) or '（无）'}\n\n"
        f"README 节选（可能含无关内容，自行忽略）：\n{readme[:5200] or '（未取到）'}"
    )
    payload = {
        "model": llm_cfg.get("model") or "gpt-4o-mini",
        "temperature": 0.4,
        # 详细总结要求 200~300 字，加上标题，留足空间
        "max_tokens": 900,
        "messages": [
            {"role": "system", "content": PLAIN_SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
    }
    try:
        data = _http(f"{base}/chat/completions", method="POST", payload=payload,
                     headers={"Authorization": f"Bearer {key}"},
                     timeout=int(llm_cfg.get("timeout", 40)))
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
    except Exception as e:
        log(f"  ⚠ 人话总结调用失败（{e}）")
        return None

    obj = _parse_json_block(text)
    if not obj:
        return None
    title = str(obj.get("title") or "").strip()
    detail = str(obj.get("detail") or "").strip()
    if not title:
        return None

    # 模型无法精确数字数（实测会写到 370~460 字）。
    # 先用定向重述把长度拉回来（质量好），再用句子裁剪兜底（保证合规）。
    if len(detail) > 305:
        detail = _compress_detail(detail, llm_cfg)
    if len(detail) > 305:
        detail = _fit_length(detail, 300)

    return {"title": title[:90], "detail": detail[:900]}


def _plain_key(repo: dict) -> str:
    return f"{repo['full_name']}@{repo.get('pushed_at', '')}@v{PLAIN_CACHE_VERSION}"


def apply_plain_cached(rows: list[dict], cfg: dict, data_dir: Path) -> list[dict]:
    """只读缓存 + 规则降级，不发任何网络请求 —— 保证 /api/board 毫秒级返回。"""
    cache = _load_json(data_dir / PLAIN_CACHE_FILE, {})
    for r in rows:
        entry = cache.get(r["full_name"])
        key = _plain_key(r)
        if entry and entry.get("key") == key and entry.get("title"):
            r["plain_title"] = entry["title"]
            r["plain_detail"] = entry.get("detail", "")
            r["plain_src"] = entry.get("src", "cache")
            r["plain_pending"] = False
        else:
            r["plain_title"] = rule_title(r)
            r["plain_detail"] = rule_summary(r)
            r["plain_src"] = "rule"
            r["plain_pending"] = True
    return rows


def generate_plain(rows: list[dict], cfg: dict, data_dir: Path,
                   on_item=None, max_calls: int = 40) -> dict:
    """为待生成的行抓 README 并调用 LLM，逐条回调（用于 SSE 实时推送）。

    并发执行。单条的成本是：读 README 约 3.9s + 调 LLM 约 1.9s，串行 6 秒/条，
    20 条要 2 分钟 —— 用户盯着一个不动的进度条，会以为卡死了。
    改成线程池后总耗时约等于最慢的那一条，20 条约 40 秒。

    注意：并发后 on_item 的到达顺序不再等于榜单顺序，前端是按 full_name
    匹配更新的，不依赖顺序；写缓存也只在主线程最后做一次，避免并发写坏文件。
    """
    llm = cfg.get("llm") or {}
    if not (llm.get("enabled") and llm.get("api_key")):
        return {"generated": 0, "failed": 0, "reason": "llm_disabled"}

    cache = _load_json(data_dir / PLAIN_CACHE_FILE, {})
    targets = [r for r in rows if r.get("plain_pending")][:max_calls]
    if not targets:
        return {"generated": 0, "failed": 0}

    workers = int(llm.get("concurrency") or 4)
    workers = max(1, min(workers, 8, len(targets)))
    generated = failed = 0

    def one(r: dict) -> dict | None:
        readme = fetch_readme(r["full_name"])
        return llm_plain_summary(r, readme, llm)

    # 按完成顺序处理：先好的先推给前端，标题一条条"长出来"
    results: list[tuple[dict, dict | None]] = []
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="plain") as pool:
        fut_map = {pool.submit(one, r): r for r in targets}
        for fut in as_completed(fut_map):
            r = fut_map[fut]
            try:
                res = fut.result()
            except Exception:
                res = None
            results.append((r, res))

    for r, res in results:
        if res:
            r["plain_title"] = res["title"]
            r["plain_detail"] = res["detail"]
            r["plain_src"] = "llm"
            r["plain_pending"] = False
            cache[r["full_name"]] = {
                "key": _plain_key(r),
                "title": res["title"],
                "detail": res["detail"],
                "src": "llm",
            }
            generated += 1
        else:
            # 失败不写缓存，下次还能重试；本轮先用规则结果顶着
            r["plain_src"] = "rule(fallback)"
            failed += 1

        if on_item:
            try:
                on_item(r)
            except Exception:
                pass   # 前端推送失败不能影响后面的条目

    _save_json(data_dir / PLAIN_CACHE_FILE, cache)
    return {"generated": generated, "failed": failed}


def _cap(text: str, limit: int) -> str:
    """截到 limit 字以内，尽量收在句末。注意别在截断后又补句号把长度撑超。"""
    cut = text[:limit].rstrip("，、,;： ")
    if cut and cut[-1] not in "。！？":
        cut = cut[:limit - 1].rstrip("，、,;： ") + "。"
    return cut[:limit]


def _fit_length(text: str, limit: int = 300) -> str:
    """最后一道保险：按句子边界裁到 limit 字以内。

    优先保住第一句（这是什么、给谁用）和最后一句（适用场景与门槛）——
    这两头对"我要不要用"最关键；中间的痛点/用法按顺序能塞多少塞多少。
    """
    if len(text) <= limit:
        return text

    sents = [s for s in re.split(r"(?<=[。！？])", text) if s.strip()]
    if len(sents) <= 2:
        return _cap(text, limit)

    head, tail = sents[0], sents[-1]
    if len(head) + len(tail) > limit:
        return _cap(tail, limit)

    kept, used = [head], len(head) + len(tail)
    for s in sents[1:-1]:
        if used + len(s) <= limit:
            kept.append(s)
            used += len(s)
    kept.append(tail)
    return "".join(kept)[:limit]


def enrich_summaries(rows: list[dict], cfg: dict, data_dir: Path,
                     use_ai: bool = True, show_progress: bool = True) -> list[dict]:
    cache = load_summary_cache(data_dir)
    llm_cfg = cfg.get("llm", {})
    ai_on = bool(use_ai and llm_cfg.get("enabled") and llm_cfg.get("api_key"))
    budget = int(llm_cfg.get("max_calls_per_run", 30))
    calls = 0

    if show_progress:
        log(f"③ 生成中文摘要（{'LLM: ' + str(llm_cfg.get('model')) if ai_on else '规则引擎'}）")

    for r in rows:
        name = r["full_name"]
        # 缓存键包含：仓库名 + 最后提交时间 + 摘要逻辑版本
        # 加入版本号是为了防止「改了摘要逻辑却命中旧缓存」这种沉默的 bug
        cache_key = f"{name}@{r.get('pushed_at', '')}@v{SUMMARY_CACHE_VERSION}"
        entry = cache.get(name)
        if entry and entry.get("key") == cache_key and entry.get("text"):
            r["summary"] = entry["text"]
            r["summary_src"] = entry.get("src", "cache")
            continue

        text, src = None, "rule"
        if ai_on and calls < budget:
            text = llm_summary(r, llm_cfg)
            if text:
                src = "llm"
                calls += 1
        if not text:
            text = rule_summary(r)
            src = "rule" if not ai_on else "rule(fallback)"

        r["summary"] = text
        r["summary_src"] = src
        cache[name] = {"key": cache_key, "text": text, "src": src}

    save_summary_cache(data_dir, cache)
    if show_progress and ai_on:
        log(f"   ✔ 本次实际调用 LLM {calls} 次（缓存命中 {len(rows) - calls} 条）")
    elif show_progress:
        log("   ✔ 已生成规则中文摘要（配置 config.llm 可升级为语义摘要）")
    return rows


# ---------------------------------------------------------------------------
# 8. 展现层
# ---------------------------------------------------------------------------

def inc_label(gap: int) -> str:
    """增量列的表头。基准距今超过 1 天时必须换成「日均」，否则数字口径与文字不符。"""
    return "24h增量" if gap == 1 else "日均增量"


def caliber_note(gap: int) -> str:
    """口径描述，用于头部/报告/推送，保证各处措辞一致。"""
    return "过去 24 小时 star 增量" if gap == 1 else f"基准距今 {gap} 天，展示日均 star 增量"


def delta_cell(r: dict, gap: int) -> str:
    """增量单元格：1 天口径显示绝对增量，多天口径显示日均增量。"""
    if gap == 1:
        d = r.get("delta") or 0
        return f"+{d:,}" if d >= 0 else f"{d:,}"
    return f"{r.get('per_day') or 0:+,.1f}"


def velocity_note(ctx: dict) -> str:
    """无日增时给用户的解释。必须让人明白「不是我出错了，是还差一天」。"""
    n = ctx.get("snapshot_days", 1)
    if n <= 1:
        return ("本地只有今天的 1 份快照。日增 = 今天减昨天，需要两份才能相减。"
                "今天的数据已经存好了，明天再跑就能看到真正的日增榜。")
    return "本地快照不足两份，暂无法计算日增。"


def header_lines(ctx: dict) -> list[str]:
    mode = ctx["mode"]
    lines = []
    lines.append(f"GHTrend · GitHub 星速榜     {ctx['date']}")
    lines.append("─" * 78)

    if mode == "delta":
        lines.append(f"口径：{caliber_note(ctx['days_gap'])}   基准快照：{ctx['baseline']}")
        lines.append(f"候选池：{ctx['pool_size']} 个仓库 → 可比样本：{ctx['sample']} 个")
    else:
        lines.append("口径：新星榜（star ÷ 项目年龄）—— 日增榜将于明天自动生成")
        lines.append(f"候选池：{ctx['pool_size']} 个仓库")
        lines.append(f"说明：{velocity_note(ctx)}")

    f = ctx["filters"]
    bits = []
    if f.get("language"):
        bits.append(f"语言={f['language']}")
    if f.get("min_delta"):
        bits.append(f"最小增量≥{f['min_delta']}")
    if f.get("top"):
        bits.append(f"Top {f['top']}")
    if bits:
        lines.append("筛选：" + "  ".join(bits))
    lines.append(f"数据来源：{'演示数据（非真实）' if ctx.get('demo') else 'GitHub Search API'}"
                 f"    生成时间：{ctx['generated_at']}")
    lines.append("─" * 78)
    return lines


def render_table(ctx: dict) -> str:
    rows = ctx["rows"]
    out = header_lines(ctx)

    if not rows:
        out.append("")
        out.append("  没有符合条件的项目。")
        out.append("")
        out.append(f"  当前候选池 {ctx['pool_size']} 个仓库"
                   f"{'，可比样本 ' + str(ctx['sample']) + ' 个' if ctx['mode'] == 'delta' else ''}。")
        if ctx["filters"].get("language"):
            out.append(f"  可能是语言筛选「{ctx['filters']['language']}」过窄，试试去掉 --language。")
        if ctx["filters"].get("min_delta"):
            out.append(f"  可能是最小增量门槛「{ctx['filters']['min_delta']}」过高，试试调低 --min-delta。")
        out.append("")
        return "\n".join(out)

    mode = ctx["mode"]
    inc_hdr = inc_label(ctx["days_gap"]) if mode == "delta" else "爆发速度"

    widths = (4, 34, 12, 10, 9)
    head = (pad("#", widths[0]), pad("项目", widths[1]), pad("语言", widths[2]),
            pad(inc_hdr, widths[3], "right"), pad("总star", widths[4], "right"))
    out.append("  " + "  ".join(head))
    out.append("  " + "  ".join("─" * w for w in widths))

    for i, r in enumerate(rows, 1):
        inc = (delta_cell(r, ctx["days_gap"]) if mode == "delta"
               else f"{r.get('velocity', 0):,.1f}/天")
        out.append("  " + "  ".join((
            pad(str(i), widths[0]),
            pad(r["full_name"], widths[1]),
            pad(r.get("language") or "-", widths[2]),
            pad(inc, widths[3], "right"),
            pad(f"{r['stars']:,}", widths[4], "right"),
        )))

    out.append("")
    out.append("─" * 78)
    out.append("  项目速览")
    out.append("─" * 78)
    for i, r in enumerate(rows, 1):
        meta = r["full_name"] + (f"  [{r['language']}]" if r.get("language") else "")
        detail = r.get("plain_detail") or r.get("summary") or ""
        out.append(f"  {i:>2}. {r.get('plain_title') or r['full_name']}")
        out.append(f"      {meta}")
        if detail:
            out.append(f"      {truncate(detail, 74)}")
        out.append(f"      {r.get('url', '')}")
        out.append("")
    return "\n".join(out)


def render_markdown(ctx: dict, for_report: bool = False) -> str:
    rows = ctx["rows"]
    L: list[str] = []
    title = "GitHub 星速榜 · 每日日报" if for_report else "GitHub 星速榜"
    L.append(f"# {title}（{ctx['date']}）")
    L.append("")

    if ctx["mode"] == "delta":
        L.append(f"> **口径**：{caliber_note(ctx['days_gap'])} ｜ **基准快照**：{ctx['baseline']}")
        L.append(f"> **样本**：候选池 {ctx['pool_size']} 个仓库，其中 {ctx['sample']} 个可对比")
    else:
        L.append("> **口径**：新星榜（star ÷ 项目年龄）。日增榜将于明天自动生成。")
        L.append(f"> **样本**：候选池 {ctx['pool_size']} 个仓库")
        L.append(f"> **说明**：{velocity_note(ctx)}")

    f = ctx["filters"]
    bits = []
    if f.get("language"):
        bits.append(f"语言 `{f['language']}`")
    if f.get("min_delta"):
        bits.append(f"最小增量 ≥ {f['min_delta']}")
    if bits:
        L.append(f"> **筛选**：{'；'.join(bits)}")
    if ctx.get("demo"):
        L.append("> ⚠️ **本报告使用演示数据，并非真实 GitHub 数据**")
    L.append("")
    L.append(f"*生成时间：{ctx['generated_at']} ｜ 数据来源：{'演示数据' if ctx.get('demo') else 'GitHub Search API'}*")
    L.append("")

    if not rows:
        L.append("**本次没有符合条件的项目。**")
        L.append("")
        L.append(f"候选池共 {ctx['pool_size']} 个仓库，可能是筛选条件过窄，可尝试去掉 `--language` 或调低 `--min-delta`。")
        return "\n".join(L)

    if ctx["mode"] == "delta":
        hdr = inc_label(ctx["days_gap"])
    else:
        hdr = "爆发速度"

    L.append("## 榜单")
    L.append("")
    L.append(f"| # | 项目 | 语言 | {hdr} | 总 Star |")
    L.append("|---:|---|---|---:|---:|")
    for i, r in enumerate(rows, 1):
        if ctx["mode"] == "delta":
            inc = delta_cell(r, ctx["days_gap"])
        else:
            inc = f"{r.get('velocity', 0):,.1f}/天"
        name = f"[{r['full_name']}]({r.get('url', '')})"
        L.append(f"| {i} | {name} | {r.get('language') or '-'} | **{inc}** | {r['stars']:,} |")
    L.append("")

    L.append("## 项目速览")
    L.append("")
    for i, r in enumerate(rows, 1):
        # 以「人话标题」开头 —— 报告是给人看的，第一眼应该是"它能干什么"
        L.append(f"### {i}. {r.get('plain_title') or r['full_name']}")
        L.append("")
        L.append(f"[{r['full_name']}]({r.get('url', '')})"
                 f"  ·  {r.get('language') or '未知语言'}  ·  {r['stars']:,} stars")
        L.append("")
        detail = r.get("plain_detail") or r.get("summary") or ""
        if detail:
            L.append(detail)
            L.append("")
        if r.get("created_at"):
            created = r["created_at"][:10]
            extra = f"创建于 {created}"
            if ctx["mode"] == "delta" and r.get("prev_stars") is not None:
                extra += f" ｜ 昨日 {r['prev_stars']:,} → 今日 {r['stars']:,}"
            L.append(f"<sub>{extra}</sub>")
            L.append("")

    L.append("---")
    L.append("")
    L.append("**关于口径**：本榜单基于每日快照的 star 总量差值计算。由于两次快照的取数"
             "存在时间差，增量与真实 24 小时增量可能存在少量偏差，不影响高增速项目的排序判断。")
    return "\n".join(L)


def render_json(ctx: dict) -> str:
    payload = {
        "date": ctx["date"],
        "mode": ctx["mode"],
        "baseline": ctx.get("baseline"),
        "days_gap": ctx.get("days_gap"),
        "pool_size": ctx["pool_size"],
        "sample": ctx.get("sample"),
        "filters": ctx["filters"],
        "demo": bool(ctx.get("demo")),
        "generated_at": ctx["generated_at"],
        "items": [
            {
                "rank": i,
                "full_name": r["full_name"],
                "url": r.get("url"),
                "language": r.get("language"),
                "stars": r["stars"],
                "delta": r.get("delta"),
                "per_day": r.get("per_day"),
                "velocity": r.get("velocity"),
                "age_days": r.get("age_days"),
                "summary": r.get("summary"),
                "summary_src": r.get("summary_src"),
                "description": r.get("description"),
                "topics": r.get("topics"),
                "created_at": r.get("created_at"),
                "pushed_at": r.get("pushed_at"),
            }
            for i, r in enumerate(ctx["rows"], 1)
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def wecom_markdown(ctx: dict) -> str:
    """构造企业微信 markdown 消息。单条上限 4096 字节，超长优先砍条目、保留头部与尾部。"""
    mode = ctx["mode"]

    head = [f"## GitHub 星速榜 · {ctx['date']}"]
    if mode == "delta":
        head.append(f"> 口径：{caliber_note(ctx['days_gap'])} ｜ 样本 {ctx.get('sample', 0)} 个仓库")
    else:
        head.append("> 口径：新星榜（star ÷ 项目年龄），日增榜明天自动生成")
    if ctx.get("demo"):
        head.append("> ⚠️ 演示数据，非真实 GitHub 数据")
    head.append("")

    items: list[str] = []
    for i, r in enumerate(ctx["rows"][:10], 1):
        inc = (delta_cell(r, ctx["days_gap"]) if mode == "delta"
               else f"{r.get('velocity', 0):,.1f}/天")
        items.append(f"**{i}. [{r['full_name']}]({r.get('url', '')})**  `{inc}`  `{r['stars']:,}★`")
        items.append(f"> {truncate(r.get('summary', ''), 90)}")

    total = len(ctx["rows"])
    tail = ["", f"完整榜单共 {total} 条，详见本地报告 data/reports/{ctx['date']}.md"]

    def build(n: int) -> str:
        body = items[: n * 2] if n else []
        return "\n".join(head + body + tail)

    show = min(len(ctx["rows"]), 10)
    text = build(show)
    while len(text.encode("utf-8")) > 4000 and show > 1:
        show -= 1
        text = build(show)
    return text


def email_html(ctx: dict) -> str:
    rows = ctx["rows"][:20]
    mode = ctx["mode"]
    head = inc_label(ctx["days_gap"]) if mode == "delta" else "爆发速度"
    rows_html = []
    for i, r in enumerate(rows, 1):
        if mode == "delta":
            inc = delta_cell(r, ctx["days_gap"])
            color = "#c0392b" if (r.get("delta") or 0) >= 0 else "#27ae60"
        else:
            inc = f"{r.get('velocity', 0):,.1f}/天"
            color = "#c0392b"
        rows_html.append(
            f'<tr>'
            f'<td style="padding:10px 8px;border-bottom:1px solid #eee;color:#888">{i}</td>'
            f'<td style="padding:10px 8px;border-bottom:1px solid #eee">'
            f'<a href="{r.get("url","")}" style="color:#0366d6;text-decoration:none;font-weight:600">{r["full_name"]}</a>'
            f'<div style="color:#666;font-size:13px;margin-top:4px;line-height:1.5">{r.get("summary","")}</div>'
            f'<div style="color:#999;font-size:12px;margin-top:3px">{r.get("language") or "未知语言"}</div>'
            f'</td>'
            f'<td style="padding:10px 8px;border-bottom:1px solid #eee;text-align:right;'
            f'font-weight:700;color:{color}">{inc}</td>'
            f'<td style="padding:10px 8px;border-bottom:1px solid #eee;text-align:right;color:#555">{r["stars"]:,}</td>'
            f'</tr>'
        )
    mode_note = (caliber_note(ctx["days_gap"]) if mode == "delta"
                 else "新星榜（star ÷ 项目年龄）")
    demo_note = ('<p style="color:#c0392b;font-weight:600">⚠️ 本邮件使用演示数据，并非真实 GitHub 数据</p>'
                 if ctx.get("demo") else "")
    return f"""<div style="font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;
max-width:680px;margin:0 auto;color:#24292f">
  <h2 style="margin:0 0 6px">GitHub 星速榜 · {ctx['date']}</h2>
  <p style="color:#666;font-size:13px;margin:0 0 4px">口径：{mode_note}｜基准快照：{ctx.get('baseline') or '—'}</p>
  <p style="color:#666;font-size:13px;margin:0 0 14px">样本：候选池 {ctx['pool_size']} 个仓库，其中 {ctx.get('sample', 0)} 个可对比</p>
  {demo_note}
  <table style="width:100%;border-collapse:collapse;font-size:14px">
    <thead><tr style="background:#f6f8fa;text-align:left">
      <th style="padding:8px;color:#57606a;font-weight:600">#</th>
      <th style="padding:8px;color:#57606a;font-weight:600">项目</th>
      <th style="padding:8px;color:#57606a;font-weight:600;text-align:right">{head}</th>
      <th style="padding:8px;color:#57606a;font-weight:600;text-align:right">总 Star</th>
    </tr></thead>
    <tbody>{''.join(rows_html)}</tbody>
  </table>
  <p style="color:#999;font-size:12px;margin-top:16px;line-height:1.6">
    本榜单基于每日快照的 star 总量差值计算，与真实 24 小时增量可能存在少量偏差。
    生成时间：{ctx['generated_at']}
  </p>
</div>"""


def notify_wecom(webhook: str, content: str) -> tuple[bool, str]:
    try:
        payload = {"msgtype": "markdown", "markdown": {"content": content}}
        data = _http(webhook, method="POST", payload=payload, timeout=20)
        if data.get("errcode") == 0:
            return True, "企业微信推送成功"
        return False, f"企业微信返回：{data}"
    except Exception as e:
        return False, f"企业微信推送失败：{e}"


def notify_email(cfg: dict, subject: str, html: str) -> tuple[bool, str]:
    em = cfg
    if not (em.get("enabled") and em.get("user") and em.get("password") and em.get("to")):
        return False, "邮件未配置完整（需 enabled/user/password/to）"
    try:
        msg = MIMEText(html, "html", "utf-8")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = em["user"]
        msg["To"] = em["to"]
        host, port = em["host"], int(em["port"])
        if em.get("use_ssl", True):
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
                s.login(em["user"], em["password"])
                s.sendmail(em["user"], [x.strip() for x in em["to"].split(",")], msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.starttls(context=ssl.create_default_context())
                s.login(em["user"], em["password"])
                s.sendmail(em["user"], [x.strip() for x in em["to"].split(",")], msg.as_string())
        return True, f"邮件已发送至 {em['to']}"
    except Exception as e:
        return False, f"邮件发送失败：{e}"


def do_notify(ctx: dict, cfg: dict, markdown_text: str, quiet: bool = False) -> None:
    nt = cfg.get("notify", {})
    any_channel = False

    webhook = (nt.get("wecom_webhook") or "").strip()
    if webhook:
        any_channel = True
        ok, msg = notify_wecom(webhook, wecom_markdown(ctx))
        log(f"   {'✔' if ok else '✘'} {msg}")

    em = nt.get("email") or {}
    if em.get("enabled"):
        any_channel = True
        ok, msg = notify_email(em, f"GitHub 星速榜 · {ctx['date']}", email_html(ctx))
        log(f"   {'✔' if ok else '✘'} {msg}")

    if not any_channel and not quiet:
        log("   ℹ 未配置任何推送通道（企业微信 Webhook / 邮件），已跳过推送。")
        log("     配置方法：python ghtrend.py init")


# ---------------------------------------------------------------------------
# 9. 编排层
# ---------------------------------------------------------------------------

def build_context(rows: list[dict], mode: str, data_dir: Path, baseline: str | None,
                  days_gap: int, pool_size: int, sample: int, filters: dict,
                  demo: bool = False, snapshot_days: int = 0) -> dict:
    return {
        "date": today_str(),
        "mode": mode,
        "baseline": baseline,
        "days_gap": days_gap,
        "pool_size": pool_size,
        "sample": sample,
        "snapshot_days": snapshot_days,
        "rows": rows,
        "filters": filters,
        "demo": demo,
        "generated_at": now_local().strftime("%Y-%m-%d %H:%M:%S"),
    }


def produce(data_dir: Path, cfg: dict, args) -> dict:
    """核心链路：读取今日快照 → 与基准快照做差 → 排名。"""
    day = today_str()
    today_path = snapshot_path(data_dir, day)
    refresh = bool(getattr(args, "refresh", False))
    offline = bool(getattr(args, "offline", False))

    if not today_path.exists() or refresh:
        if offline:
            if not today_path.exists():
                raise ApiError(f"缺少今日快照 {today_path.name}，且已指定 --offline")
            log(f"① --offline 模式：使用已有快照 {today_path.name}")
            repos = load_snapshot(today_path)["repos"]
        else:
            why = "强制刷新" if refresh else "今日尚无快照"
            log(f"① {why}，开始抓取（{day}）")
            repos = fetch_pool(cfg)
            save_snapshot(data_dir, day, repos)
            log(f"   ✔ 快照已保存：{today_path}（{len(repos)} 个仓库）")
    else:
        repos = load_snapshot(today_path)["repos"]
        log(f"① 使用已有快照 {today_path.name}（{len(repos)} 个仓库）")
        log("   （想强制刷新请加 --refresh）")

    base_path = find_baseline(data_dir, day)
    report_cfg = cfg.get("report", {})
    top_n = args.top if args.top else int(report_cfg.get("top_n", 20))
    language = args.language if args.language is not None else report_cfg.get("language", "")
    min_delta = args.min_delta if args.min_delta is not None else int(report_cfg.get("min_delta", 0))

    if base_path:
        base = load_snapshot(base_path)
        days_gap = max((date.fromisoformat(day) - date.fromisoformat(base_path.stem)).days, 1)
        log(f"② 计算日增（基准：{base_path.stem}，间隔 {days_gap} 天）")
        rows = compute_daily_delta(repos, base["repos"], days_gap)
        mode, sample, baseline = "delta", len(rows), base_path.stem
        log(f"   ✔ 可比项目 {sample} 个")
    else:
        log("② 未找到历史快照，降级为「新星榜」（star ÷ 项目年龄）")
        log("   ℹ 这是首次运行的正常行为，不是错误。明天开始就能看到真正的日增榜。")
        rows = compute_velocity(repos)
        mode, sample, baseline, days_gap = "velocity", 0, None, 1

    rows = apply_filters(rows, language=language or "", min_delta=min_delta)
    rows = sort_rows(rows, mode)[:top_n]

    filters = {"top": top_n, "language": language or "", "min_delta": min_delta}
    return build_context(rows, mode, data_dir, baseline, days_gap,
                         len(repos), sample, filters,
                         snapshot_days=len(list_snapshots(data_dir)))


def finalize(ctx: dict, cfg: dict, data_dir: Path, args) -> int:
    """摘要 → 输出 → 报告 → 推送。"""
    ctx["rows"] = enrich_summaries(ctx["rows"], cfg, data_dir,
                                   use_ai=not args.no_ai)

    # 「人话总结」：先读缓存 + 规则降级（不发网络），需要真正的人话再按需生成
    ctx["rows"] = apply_plain_cached(ctx["rows"], cfg, data_dir)
    if getattr(args, "plain", False):
        log("· 生成「人话总结」（需要 LLM，逐条抓 README）…")
        res = generate_plain(ctx["rows"], cfg, data_dir)
        if res.get("reason") == "llm_disabled":
            log("   ⚠ 未配置 LLM，已跳过：先用规则标题顶上，配置 llm 后可升级成人话")
        else:
            log(f"   ✔ 生成 {res['generated']} 条，失败 {res['failed']} 条")

    fmt = args.format or "table"
    if fmt == "json":
        print(render_json(ctx))
    elif fmt == "markdown":
        print(render_markdown(ctx))
    else:
        print(render_table(ctx))

    if args.report:
        data_dir.joinpath("reports").mkdir(parents=True, exist_ok=True)
        rp = data_dir / "reports" / f"{ctx['date']}.md"
        rp.write_text(render_markdown(ctx, for_report=True), encoding="utf-8")
        log(f"④ 报告已保存：{rp}")

    if args.notify:
        do_notify(ctx, cfg, render_markdown(ctx, for_report=True))

    removed = cleanup_old(data_dir, int(cfg.get("retention_days", 90)))
    if removed:
        log(f"   ℹ 已清理 {removed} 个超期文件（保留 {cfg.get('retention_days')} 天）")

    return 0


# ---------------------------------------------------------------------------
# 10. 演示数据（demo：不联网、零配置）
# ---------------------------------------------------------------------------

DEMO_SNAPSHOT = [
    # (repo, 昨日star, 24h增量, 语言, 创建距今天数, 描述, topics)
    ("langchain-ai/langchain", 98210, 1180, "Python", 1120,
     "Build context-aware reasoning applications", ["llm", "ai", "agents", "rag"]),
    ("browser-use/browser-use", 61440, 940, "Python", 620,
     "Make websites accessible for AI agents", ["ai-agents", "automation", "browser"]),
    ("modelcontextprotocol/servers", 52380, 830, "TypeScript", 480,
     "Model Context Protocol Servers", ["mcp", "ai", "llm"]),
    ("astral-sh/uv", 47800, 760, "Rust", 900,
     "An extremely fast Python package and project manager, written in Rust",
     ["python", "packaging", "rust", "cli"]),
    ("ollama/ollama", 148200, 690, "Go", 1310,
     "Get up and running with large language models locally", ["llm", "go", "llama"]),
    ("deepseek-ai/DeepSeek-R1", 92150, 640, "Python", 260,
     "DeepSeek-R1: incentivizing reasoning capability in LLMs via RL",
     ["llm", "reasoning", "deep-learning"]),
    ("open-webui/open-webui", 88900, 585, "JavaScript", 940,
     "User-friendly AI Interface (Supports Ollama, OpenAI API)",
     ["llm", "web", "self-hosted"]),
    ("n8n-io/n8n", 118300, 520, "TypeScript", 2900,
     "Fair-code workflow automation platform with native AI capabilities",
     ["automation", "workflow", "low-code", "ai"]),
    ("ghostty-org/ghostty", 31400, 470, "Zig", 1050,
     "A fast, feature-rich, cross-platform terminal emulator", ["terminal", "zig"]),
    ("zed-industries/zed", 58700, 425, "Rust", 1360,
     "Code at the speed of thought - a high-performance multiplayer code editor",
     ["editor", "rust", "gpu"]),
    ("microsoft/markitdown", 68400, 390, "Python", 480,
     "Python tool for converting files and office documents to Markdown",
     ["markdown", "python", "documentation"]),
    ("unslothai/unsloth", 41200, 355, "Python", 1080,
     "Finetune Llama, Qwen, Gemma and other models 2x faster with less memory",
     ["fine-tuning", "llm", "machine-learning"]),
    ("anthropics/anthropic-cookbook", 19800, 320, "Jupyter Notebook", 620,
     "A collection of notebooks and recipes using Claude", ["llm", "tutorial"]),
    ("dbeaver/dbeaver", 43100, 268, "Java", 4200,
     "Free universal database tool and SQL client", ["database", "sql", "tooling"]),
    ("supabase/supabase", 89400, 245, "TypeScript", 2200,
     "The open source Firebase alternative", ["database", "postgres", "backend"]),
    ("tailscale/tailscale", 24800, 210, "Go", 2450,
     "The easiest, most secure way to use WireGuard and 2FA", ["vpn", "networking", "security"]),
    ("sharkdp/bat", 54600, 156, "Rust", 3450,
     "A cat clone with wings", ["cli", "rust", "terminal"]),
    ("httpie/cli", 35100, 88, "Python", 4900,
     "Modern, user-friendly command-line HTTP client for the API era",
     ["cli", "http", "api"]),
]


def cmd_demo(args) -> int:
    """演示模式：造两天的假快照，跑通全链路，明确标注非真实数据。"""
    from copy import deepcopy

    data_dir = Path(args.data_dir or DEFAULT_DATA_DIR) / "demo"
    day_today = today_str()
    day_yday = (now_local() - timedelta(days=1)).strftime("%Y-%m-%d")

    yday_repos, today_repos = [], []
    for name, y_stars, delta, lang, age_days, desc, topics in DEMO_SNAPSHOT:
        created = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
        pushed = datetime.now(timezone.utc).isoformat()
        base = {
            "stars": y_stars, "forks": max(y_stars // 12, 1), "issues": max(y_stars // 40, 1),
            "language": lang, "description": desc, "topics": topics,
            "created_at": created, "pushed_at": pushed, "archived": False,
        }
        yday_repos.append({"full_name": name, "url": f"https://github.com/{name}",
                           **deepcopy(base)})
        today_repos.append({"full_name": name, "url": f"https://github.com/{name}",
                            **deepcopy(base), "stars": y_stars + delta})

    save_snapshot(data_dir, day_yday, yday_repos, source="demo")
    save_snapshot(data_dir, day_today, today_repos, source="demo")

    cfg = load_config()
    cfg["llm"]["enabled"] = False        # 演示不调用 LLM，避免产生费用
    args.report = True                   # 演示模式顺带产出报告文件，展示完整链路
    ctx = produce(data_dir, cfg, args)
    ctx["demo"] = True
    ctx["pool_size"] = len(DEMO_SNAPSHOT)
    ctx["sample"] = len(DEMO_SNAPSHOT)

    log("")
    log("★ 演示模式：使用内置示例数据，不联网、不消耗任何 API 配额。")
    log("  真实数据请运行：  python ghtrend.py top")
    log("")
    return finalize(ctx, cfg, data_dir, args)


# ---------------------------------------------------------------------------
# 11. 子命令
# ---------------------------------------------------------------------------

def cmd_fetch(args) -> int:
    cfg = load_config()
    data_dir = Path(args.data_dir or DEFAULT_DATA_DIR)
    day = today_str()
    path = snapshot_path(data_dir, day)

    if path.exists() and not args.refresh:
        log(f"ℹ 今日快照已存在：{path}")
        log("  如需强制重新抓取，请加 --refresh")
        return 0

    repos = fetch_pool(cfg)
    if not repos:
        log("✘ 未抓取到任何仓库，请检查网络或 Token。")
        return 1
    save_snapshot(data_dir, day, repos)
    log(f"✔ 快照已保存：{path}（{len(repos)} 个仓库）")
    print(f"已抓取 {len(repos)} 个候选仓库 → {path}")
    return 0


def cmd_top(args) -> int:
    cfg = load_config()
    data_dir = Path(args.data_dir or DEFAULT_DATA_DIR)
    try:
        ctx = produce(data_dir, cfg, args)
    except (ApiError, RateLimited) as e:
        return _handle_fetch_failure(e, data_dir, args)
    return finalize(ctx, cfg, data_dir, args)


def cmd_daily(args) -> int:
    cfg = load_config()
    data_dir = Path(args.data_dir or DEFAULT_DATA_DIR)
    args.report = True          # daily 必定产出报告文件
    args.notify = True          # daily 默认尝试推送（未配置通道会静默跳过）
    try:
        ctx = produce(data_dir, cfg, args)
    except (ApiError, RateLimited) as e:
        return _handle_fetch_failure(e, data_dir, args)
    return finalize(ctx, cfg, data_dir, args)


def _handle_fetch_failure(e: Exception, data_dir: Path, args) -> int:
    """抓取失败不等于没东西可看 —— 用上一次快照兜底（PRD US-13）。"""
    log("")
    log(f"✘ 抓取失败：{e}")
    snaps = list_snapshots(data_dir)
    if not snaps:
        log("  本地也没有任何历史快照，本次无法产出榜单。")
        return 1

    last = snaps[-1]
    log(f"  已回退到最近一次快照：{last.stem}")
    cfg = load_config()
    repos = load_snapshot(last)["repos"]
    base_path = find_baseline(data_dir, last.stem)
    top_n = args.top or int(cfg["report"]["top_n"])

    if base_path:
        days_gap = max((date.fromisoformat(last.stem) - date.fromisoformat(base_path.stem)).days, 1)
        rows = compute_daily_delta(repos, load_snapshot(base_path)["repos"], days_gap)
        mode, baseline, sample = "delta", base_path.stem, len(rows)
    else:
        rows = compute_velocity(repos)
        mode, baseline, sample, days_gap = "velocity", None, 0, 1

    ctx = build_context(sort_rows(rows, mode)[:top_n], mode, data_dir, baseline,
                        days_gap, len(repos), sample,
                        {"top": top_n, "language": "", "min_delta": 0},
                        snapshot_days=len(list_snapshots(data_dir)))
    ctx["stale"] = last.stem
    print()
    print(f"⚠ 以下为 {last.stem} 的榜单（本次抓取失败，数据非最新）")
    return finalize(ctx, cfg, data_dir, args)


def cmd_show(args) -> int:
    cfg = load_config()
    data_dir = Path(args.data_dir or DEFAULT_DATA_DIR)
    target = args.repo.strip().lower()

    snaps = list_snapshots(data_dir)
    if not snaps:
        log("✘ 本地还没有任何快照。先运行：python ghtrend.py fetch")
        return 1

    series = []
    meta = None
    for p in snaps:
        snap = load_snapshot(p)
        for r in snap["repos"]:
            if r["full_name"].lower() == target:
                series.append((p.stem, r["stars"]))
                meta = meta or r
                break

    if not series:
        print(f"未在本地快照中找到「{args.repo}」。")
        print()
        print("可能原因：")
        print("  · 仓库不在候选池内（star 数或活跃度未达门槛）")
        print("  · 快照积累天数不足")
        print("  · 仓库名拼写有误（需为 owner/repo 格式）")
        print()
        print(f"本地共 {len(snaps)} 份快照，覆盖 {snaps[0].stem} ~ {snaps[-1].stem}")
        return 1

    print()
    print(f"{meta['full_name']}  ·  {meta.get('language') or '未知语言'}")
    print(f"{meta.get('url','')}")
    print("─" * 60)
    print(f"  {'日期':<14}{'Star':>10}{'变化':>12}")
    print("─" * 60)
    prev = None
    for d, s in series:
        if prev is None:
            chg = "—"
        else:
            diff = s - prev
            chg = f"+{diff:,}" if diff >= 0 else f"{diff:,}"
        print(f"  {d:<14}{s:>10,}{chg:>12}")
        prev = s
    print("─" * 60)
    if len(series) >= 2:
        total = series[-1][1] - series[0][1]
        days = max((date.fromisoformat(series[-1][0]) - date.fromisoformat(series[0][0])).days, 1)
        print(f"  区间累计：{total:+,} stars ／ {days} 天  →  日均 {total / days:+,.1f}")
    print()
    return 0


# ---------------------------------------------------------------------------
# 12. 入口
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ghtrend",
        description="GHTrend · GitHub 星速榜 —— 每日追踪 star 增速最快的开源项目",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""常用示例：
  python ghtrend.py demo                              零配置体验（不联网）
  python ghtrend.py top                               查看今日日增榜
  python ghtrend.py top --language python --top 10    只看 Python 项目的 Top 10
  python ghtrend.py top --format json > today.json    输出 JSON 便于二次加工
  python ghtrend.py daily --report --notify           一键日报（定时任务用这个）
  python ghtrend.py show ollama/ollama                查看某仓库的本地 star 历史

首次使用建议：
  1. python ghtrend.py demo      → 先看看长什么样
  2. python ghtrend.py init      → 生成配置模板
  3. python ghtrend.py fetch     → 抓取今天的快照（建立基线）
  4. python ghtrend.py daily     → 明天开始就有真正的日增榜
""")
    p.add_argument("--data-dir", dest="_data_dir_main", default=None, metavar="DIR",
                   help="数据目录（默认 ./data；也可写在子命令后面）")
    p.add_argument("--transport", dest="_transport_main", default="auto",
                   choices=["auto", "urllib", "curl"],
                   help="网络传输层：auto=优先 Python、失败自动回退 curl（默认）")
    p.add_argument("--version", action="version", version=f"ghtrend {VERSION}")

    sub = p.add_subparsers(dest="command")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data-dir", default=None, metavar="DIR", help="数据目录（默认 ./data）")
    common.add_argument("--transport", default=None, choices=["auto", "urllib", "curl"],
                        help="网络传输层：auto / urllib / curl")
    common.add_argument("--top", type=int, default=0, help="展示条数（默认取配置，20）")
    common.add_argument("--language", default=None, help="按语言过滤，如 python / rust / go")
    common.add_argument("--min-delta", type=int, default=None, help="最小增量门槛")
    common.add_argument("--format", choices=["table", "markdown", "json"], default="table",
                        help="输出格式（默认 table）")
    common.add_argument("--report", action="store_true", help="额外生成 Markdown 报告文件")
    common.add_argument("--notify", action="store_true", help="推送到企业微信 / 邮箱")
    common.add_argument("--no-ai", action="store_true", help="关闭 AI 摘要（用规则引擎）")
    common.add_argument("--plain", action="store_true",
                        help="额外生成「人话总结」标题（需配置 LLM）")
    common.add_argument("--refresh", action="store_true", help="忽略今日已有快照，强制重新抓取")
    common.add_argument("--offline", action="store_true", help="禁止联网（无今日快照则报错）")

    sp = sub.add_parser("demo", parents=[common], help="用内置示例数据演示全流程（不联网）")
    sp.set_defaults(func=cmd_demo)

    sp = sub.add_parser("fetch", parents=[common], help="抓取候选池并保存今日快照")
    sp.set_defaults(func=cmd_fetch)

    sp = sub.add_parser("top", parents=[common], help="展示日增榜（缺快照会自动抓取）")
    sp.set_defaults(func=cmd_top)

    sp = sub.add_parser("daily", parents=[common], help="一键日报：抓取 + 榜单 + 摘要 + 报告 + 推送")
    sp.set_defaults(func=cmd_daily)

    sp = sub.add_parser("show", help="查看某个仓库的本地 star 历史")
    sp.add_argument("repo", help="仓库全名，如 torvalds/linux")
    sp.add_argument("--data-dir", default=None, help="数据目录（默认 ./data）")
    sp.set_defaults(func=cmd_show)

    sp = sub.add_parser("init", help="生成配置模板 config.example.json")
    sp.add_argument("--force", action="store_true", help="覆盖已存在的模板")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("config", help="查看当前生效配置")
    sp.set_defaults(func=cmd_config)

    sp = sub.add_parser("netcheck", help="探测哪个 GitHub 线路能通（国内网络必备）")
    sp.add_argument("--timeout", type=int, default=8, help="单条线路探测超时秒数")
    sp.set_defaults(func=cmd_netcheck)

    sp = sub.add_parser("setup", help="配置「人话总结」用的 LLM（向导式，不用改 JSON）")
    sp.add_argument("--provider", help="deepseek / dashscope / zhipu / ollama / custom")
    sp.add_argument("--key", help="API Key；不填会交互询问")
    sp.add_argument("--base-url", help="接口地址")
    sp.add_argument("--model", help="模型名")
    sp.set_defaults(func=cmd_setup_llm)

    sp = sub.add_parser("pack", help="打包成可发给别人的压缩包（自动剔除密钥与本地数据）")
    sp.add_argument("--out", help="输出路径，默认放在项目上一级目录")
    sp.set_defaults(func=cmd_pack)

    return p


def _data_status() -> str:
    """菜单顶部的一行状态，告诉用户今天该点哪一项。"""
    data_dir = DEFAULT_DATA_DIR
    day = today_str()
    today_path = snapshot_path(data_dir, day)
    days_total = len(list_snapshots(data_dir))

    if today_path.exists():
        try:
            cnt = load_snapshot(today_path).get("count", "?")
        except Exception:
            cnt = "?"
        line = f"今日快照：已存在（{cnt} 个仓库）"
    else:
        line = "今日快照：尚未抓取"

    if days_total == 0:
        return line + " ｜ 本地没有任何历史快照"
    if days_total == 1:
        return line + f" ｜ 本地 {days_total} 天快照（明天起可算日增）"
    return line + f" ｜ 本地 {days_total} 天快照"


def interactive_menu() -> int:
    """双击 启动.bat 时进入的菜单。目的：让不会用命令行的人也能跑。"""
    options = {
        "1": (["top"], "查看今日榜单"),
        "2": (["demo"], "演示模式（不联网，先看效果）"),
        "3": (["fetch", "--refresh"], "抓取 / 更新今日数据"),
        "4": (["daily"], "生成日报并推送"),
        "5": (["config"], "查看当前配置"),
        "6": (["--help"], "查看全部命令说明"),
        "7": (["netcheck"], "检测网络线路（连不上 GitHub 时用）"),
        "8": (["setup"], "配置 AI 人话总结（需要 LLM）"),
    }

    while True:
        print()
        print("═" * 66)
        print("  GHTrend · GitHub 星速榜")
        print("  每天追踪 GitHub 上 star 增速最快的开源项目")
        print("═" * 66)
        print(f"  {_data_status()}")
        print("─" * 66)
        print("  1  查看今日榜单            ← 最常用，直接看结果")
        print("  2  演示模式 · 不联网        ← 第一次用，先看它长什么样")
        print("  3  抓取 / 更新今日数据      ← 约 1 分钟，建立今天的数据快照")
        print("  4  生成日报并推送")
        print("  5  查看当前配置")
        print("  6  查看全部命令说明")
        print("  7  检测网络线路            ← 抓不到数据时，先跑这个")
        print("  8  配置 AI 人话总结          ← 让标题变成能看懂的句子")
        print("  0  退出")
        print("═" * 66)

        try:
            raw = input("  请选择 [0-8]，直接回车退出：").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if raw in ("", "0", "q", "Q"):
            print("\n  再见。\n")
            return 0

        if raw not in options:
            print("\n  没有这个选项，请输入 0-8 之间的数字。")
            continue

        cmd, desc = options[raw]
        print(f"\n▶ {desc}\n")
        try:
            main(cmd)
        except SystemExit:
            pass
        except Exception as e:                      # 菜单绝不因单次失败而崩掉
            print(f"\n✘ 出错了：{e}")
            print("  可以先把这段提示发给我，我帮你看。")

        try:
            input("\n  按回车键返回菜单…")
        except (EOFError, KeyboardInterrupt):
            return 0


def main(argv=None) -> int:
    setup_stdout()

    if argv is None:
        argv = sys.argv[1:]

    parser = build_parser()

    # 不带任何参数、且在真实终端里运行 → 进入交互菜单（双击 启动.bat 就是这条路）
    # 如果被管道/脚本调用（非 TTY），仍然输出帮助文本，避免影响自动化
    if not argv and sys.stdin.isatty():
        try:
            return interactive_menu()
        except KeyboardInterrupt:
            print()
            return 0

    args = parser.parse_args(argv)

    if not getattr(args, "command", None):
        parser.print_help()
        return 0

    main_data_dir = getattr(args, "_data_dir_main", None)
    main_transport = getattr(args, "_transport_main", "auto")

    # 补全 common 参数默认值（子命令未使用 parents 的情况）
    for attr, default in (("top", 0), ("language", None), ("min_delta", None),
                          ("format", "table"), ("report", False), ("notify", False),
                          ("no_ai", False), ("refresh", False), ("offline", False),
                          ("data_dir", None), ("transport", None), ("plain", False)):
        if not hasattr(args, attr):
            setattr(args, attr, default)

    # --data-dir / --transport 允许写在子命令前或后，后面优先
    if not args.data_dir:
        args.data_dir = main_data_dir

    global TRANSPORT
    TRANSPORT = args.transport or main_transport or "auto"

    try:
        return args.func(args)
    except KeyboardInterrupt:
        log("\n已中断。")
        return 130
    except (ApiError, RateLimited) as e:
        log(f"✘ {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
