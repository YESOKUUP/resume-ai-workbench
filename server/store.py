# -*- coding: utf-8 -*-
"""
存储层 —— 配置 / 日志 / 用量。纯文件（JSON），不入数据库。

为什么不入库：用户清盘、备份、迁移都容易；也不会踩到 SQLite 的沙箱写权限问题。
所有文件都在本机工作目录下，不联网。
"""

import json
import os
import sys
import time

HOME = os.environ.get("RW_HOME") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # resume-workbench-mvp/（RW_HOME 可覆盖，便于隔离测试）
CONFIG_PATH = os.path.join(HOME, "config.json")
LOGS_DIR = os.path.join(HOME, "logs")
LOGS_PATH = os.path.join(LOGS_DIR, "last-30-actions.json")
USAGE_PATH = os.path.join(HOME, "usage.json")
SNAPSHOTS_DIR = os.path.join(HOME, "snapshots")

DEFAULT_CONFIG = {
    "llm": {
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "api_key": "",
        "temperature": 0.20,          # 归一化 0–1，按服务商量程折算
        "ai_level": "heavy",          # 兼容旧配置；新配置用 rewrite_style
        "rewrite_style": "standard",  # faithful | standard | bold | aggressive（全局改写力度）
        "pure_rule": False,           # 评估时仅纯规则体检（不调 AI）
        "timeout": 120,
        "price_in": 0,                # 元 / 百万 token（用户自填，留空则只显示 token）
        "price_out": 0,
        "currency": "¥",
    },
    "privacy": {
        "strip_phone": False,
        "strip_email": False,
    },
    "prefs": {
        "infer_outputs": True,
        "name_mechanism": True,
        "upgrade_verb": False,
    },
    "blacklist": [],
    "budget": {"session_warn_tokens": 200000},
}

PROVIDER_PRESETS = {
    "deepseek": {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
    "claude": {"base_url": "https://api.anthropic.com/v1", "model": "claude-3-5-sonnet-latest"},
    "moonshot": {"base_url": "https://api.moonshot.cn/v1", "model": "moonshot-v1-8k"},
    "zhipu": {"base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-flash"},
    "qwen": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus"},
    "custom": {"base_url": "", "model": ""},
}


def _deep_merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _atomic_write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


# ------------------------------------------------------------------ 配置

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = _deep_merge(DEFAULT_CONFIG, json.load(f))
        except Exception:
            pass
    return cfg


def save_config(patch):
    """合并保存。api_key 传 "__KEEP__" 表示不修改。"""
    cfg = load_config()
    patch = dict(patch or {})
    llm = patch.get("llm")
    if isinstance(llm, dict) and llm.get("api_key") == "__KEEP__":
        llm = dict(llm)
        llm["api_key"] = cfg["llm"].get("api_key", "")
        patch["llm"] = llm
    cfg = _deep_merge(cfg, patch)
    _atomic_write(CONFIG_PATH, json.dumps(cfg, ensure_ascii=False, indent=2))
    return cfg


def public_config():
    """回给前端的配置 —— 永不包含 API Key 明文，只告知是否已设置。"""
    cfg = load_config()
    c = json.loads(json.dumps(cfg, ensure_ascii=False))
    key = cfg["llm"].get("api_key") or ""
    c["llm"]["api_key"] = ""
    c["llm"]["api_key_set"] = bool(key)
    c["llm"]["api_key_hint"] = (key[:3] + "…" + key[-4:]) if len(key) >= 8 else ("已设置" if key else "")
    return c


# ------------------------------------------------------------------ 日志

def _read_logs():
    if not os.path.exists(LOGS_PATH):
        return []
    try:
        with open(LOGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def log(action, detail=None, scope="本机", extra=None):
    """写一条脱敏日志。绝不记录简历正文或 API Key。"""
    items = _read_logs()
    entry = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "action": action,
        "scope": scope,
        "detail": detail or "",
    }
    if extra:
        entry.update(extra)
    items.append(entry)
    items = items[-30:]
    _atomic_write(LOGS_PATH, json.dumps(items, ensure_ascii=False, indent=2))
    return entry


def read_logs():
    return list(reversed(_read_logs()))


def clear_logs():
    _atomic_write(LOGS_PATH, "[]")


# ------------------------------------------------------------------ 用量

def _read_usage():
    if not os.path.exists(USAGE_PATH):
        return {"session": {"in": 0, "out": 0, "calls": 0}, "total": {"in": 0, "out": 0, "calls": 0},
                "by_step": {}, "history": []}
    try:
        with open(USAGE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"session": {"in": 0, "out": 0, "calls": 0}, "total": {"in": 0, "out": 0, "calls": 0},
                "by_step": {}, "history": []}


def add_usage(step, model, usage, saved_tokens=0):
    """记一次调用。usage 为 OpenAI 格式的 usage 对象（可能为 None）。"""
    u = _read_usage()
    pt = int((usage or {}).get("prompt_tokens") or 0)
    ct = int((usage or {}).get("completion_tokens") or 0)
    for k in ("session", "total"):
        u[k]["in"] = u[k].get("in", 0) + pt
        u[k]["out"] = u[k].get("out", 0) + ct
        u[k]["calls"] = u[k].get("calls", 0) + 1
    bs = u.setdefault("by_step", {}).setdefault(step, {"in": 0, "out": 0, "calls": 0})
    bs["in"] += pt
    bs["out"] += ct
    bs["calls"] += 1
    u.setdefault("history", []).append({
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "step": step, "model": model, "in": pt, "out": ct, "saved": int(saved_tokens or 0),
    })
    u["history"] = u["history"][-50:]
    _atomic_write(USAGE_PATH, json.dumps(u, ensure_ascii=False, indent=2))
    return u


def estimate_cost(usage, cfg=None):
    """按用户自填单价换算费用；单价留空则返回 None（前端只显示 token）。"""
    cfg = cfg or load_config()
    pin = float(cfg["llm"].get("price_in") or 0)
    pout = float(cfg["llm"].get("price_out") or 0)
    if not pin and not pout:
        return None
    pt = int((usage or {}).get("in") or 0)
    ct = int((usage or {}).get("out") or 0)
    return round(pt / 1e6 * pin + ct / 1e6 * pout, 6)


def read_usage_with_cost():
    cfg = load_config()
    u = _read_usage()
    out = json.loads(json.dumps(u, ensure_ascii=False))
    for k in ("session", "total"):
        out[k]["cost"] = estimate_cost(u.get(k), cfg)
    out["currency"] = cfg["llm"].get("currency") or "¥"
    out["price_set"] = bool(cfg["llm"].get("price_in") or cfg["llm"].get("price_out"))
    out["budget"] = cfg.get("budget", {})
    return out


def reset_session_usage():
    u = _read_usage()
    u["session"] = {"in": 0, "out": 0, "calls": 0}
    _atomic_write(USAGE_PATH, json.dumps(u, ensure_ascii=False, indent=2))
    return u


# ------------------------------------------------------------------ 快照

def save_snapshot(payload):
    os.makedirs(SNAPSHOTS_DIR, exist_ok=True)
    name = "V_%s.json" % time.strftime("%Y%m%d-%H%M%S")
    _atomic_write(os.path.join(SNAPSHOTS_DIR, name), json.dumps(payload, ensure_ascii=False, indent=2))
    return name


def list_snapshots():
    if not os.path.isdir(SNAPSHOTS_DIR):
        return []
    out = []
    for n in sorted(os.listdir(SNAPSHOTS_DIR), reverse=True):
        p = os.path.join(SNAPSHOTS_DIR, n)
        out.append({"name": n, "size": os.path.getsize(p), "mtime": os.path.getmtime(p)})
    return out


def data_overview():
    def _dir_size(p):
        if not os.path.isdir(p):
            return 0, 0
        tot = cnt = 0
        for n in os.listdir(p):
            fp = os.path.join(p, n)
            if os.path.isfile(fp):
                tot += os.path.getsize(fp)
                cnt += 1
        return tot, cnt

    snap_size, snap_cnt = _dir_size(SNAPSHOTS_DIR)
    log_size = os.path.getsize(LOGS_PATH) if os.path.exists(LOGS_PATH) else 0
    usage_size = os.path.getsize(USAGE_PATH) if os.path.exists(USAGE_PATH) else 0
    cfg_size = os.path.getsize(CONFIG_PATH) if os.path.exists(CONFIG_PATH) else 0
    return {
        "home": HOME,
        "config": {"path": "config.json", "size": cfg_size, "has_key": bool(load_config()["llm"].get("api_key"))},
        "snapshots": {"path": "snapshots/", "size": snap_size, "count": snap_cnt},
        "logs": {"path": "logs/", "size": log_size, "count": len(_read_logs())},
        "usage": {"path": "usage.json", "size": usage_size},
        "total": snap_size + log_size + usage_size + cfg_size,
    }
