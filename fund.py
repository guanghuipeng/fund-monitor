# -*- coding: utf-8 -*-
"""基金估值监控与止盈提醒工具。

用法：
  python fund.py init --demo
  python fund.py add 161725 --kind index
  python fund.py check --dry-run
  python fund.py check
  python fund.py report --open
  python fund.py backtest 161725 --start 2020-01-01 --end 2022-12-31
  python fund.py article <公众号文章链接>
  python fund.py push-test

只提醒，不自动交易。规则见 README.md。
"""
import argparse
import copy
import json
import os
import shutil
import sys
import traceback
from datetime import date, datetime, timedelta

import datasource as ds
import index_watch
import render

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
CACHE_DIR = os.path.join(DATA_DIR, "cache")
ARTICLE_CACHE = os.path.join(CACHE_DIR, "articles")
REPORT_DIR = os.path.join(ROOT, "reports")
ARTICLE_DIR = os.path.join(ROOT, "articles")
LOG_DIR = os.path.join(ROOT, "logs")
CONFIG_PATH = os.path.join(ROOT, "config.json")
EXAMPLE_PATH = os.path.join(ROOT, "config.example.json")
STATE_PATH = os.path.join(DATA_DIR, "state.json")
OVERRIDES_PATH = os.path.join(DATA_DIR, "valuation_overrides.json")
LAST_RUN_PATH = os.path.join(DATA_DIR, "last_run.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")
LOG_PATH = os.path.join(LOG_DIR, "fund.log")
INBOX_PATH = os.path.join(ARTICLE_DIR, "inbox.txt")

DEFAULT_CONFIG = {
    "push": {
        "enabled": False,
        "provider": "serverchan",
        "serverchan_key": "",
        "pushplus_token": "",
    },
    "run": {
        "weekly_summary_weekday": 5,
        "push_on_signal_only": True,
        "buy_cooldown_days": 10,
        "cooldown_days": 5,
        "rearm_days": 5,
        "article_max_age_days": 7,
        "index_watch_push": False,
    },
    "thresholds": {
        "buy": {"low_pct": 0.20, "deep_low_pct": 0.10, "pb_guard": 0.30,
                "active_low_nav_pct": 0.15, "active_deep_nav_pct": 0.05},
        "sell": {"high_pct": 0.80, "extreme_pct": 0.90,
                 "active_high_nav_pct": 0.90, "active_extreme_nav_pct": 0.97},
        "trailing": {"broad_index": 0.08, "sector_index": 0.10, "active": 0.12},
        "rearm_val_pct": 0.70,
        "rearm_drawdown": -0.08,
    },
    "network": {"proxy": ""},
    "funds": [],
}

DEMO_FUNDS = [
    {"code": "161725", "alias": "白酒", "kind": "index", "index_code": "SZ399997",
     "enabled": True, "cost": None, "shares": None, "trailing_drawdown": None,
     "notes": "演示：高波动行业指数"},
    {"code": "000961", "alias": "沪深300联接", "kind": "index", "index_code": "SH000300",
     "enabled": True, "cost": None, "shares": None, "trailing_drawdown": None,
     "notes": "演示：宽基指数"},
    {"code": "110022", "alias": "易方达消费", "kind": "active", "index_code": None,
     "enabled": True, "cost": None, "shares": None, "trailing_drawdown": None,
     "notes": "演示：主动权益"},
]

BROAD_KEYWORDS = ("沪深300", "中证500", "中证1000", "中证800", "中证A500",
                  "上证50", "上证180", "创业板", "科创50", "全指", "红利",
                  "恒生", "纳斯达克", "标普", "msci", "道琼斯")


# ---------------------------------------------------------------------------
# 基础 IO
# ---------------------------------------------------------------------------

def ensure_dirs():
    for path in (DATA_DIR, CACHE_DIR, ARTICLE_CACHE, REPORT_DIR, ARTICLE_DIR, LOG_DIR):
        os.makedirs(path, exist_ok=True)


def log(message, quiet=False):
    ensure_dirs()
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), message)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    if not quiet:
        print(message)


def _atomic_json(path, obj):
    ensure_dirs()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _read_json(path, default):
    if not os.path.exists(path):
        return copy.deepcopy(default)
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return copy.deepcopy(default)


def deep_merge(base, override):
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config():
    raw = _read_json(CONFIG_PATH, {})
    return deep_merge(DEFAULT_CONFIG, raw)


def save_config(config):
    _atomic_json(CONFIG_PATH, config)


def default_state():
    return {
        "zone": "normal",
        "peak_nav": None,
        "peak_nav_date": None,
        "peak_index": None,
        "peak_index_date": None,
        "sold_tiers": [],
        "last_alert": {},
        "normal_days": 0,
        "history": [],
        "updated": "",
    }


def load_state():
    return _read_json(STATE_PATH, {})


def save_state(state):
    _atomic_json(STATE_PATH, state)


def ensure_fund_state(state, code):
    st = state.get(code)
    if not isinstance(st, dict):
        st = default_state()
    for key, value in default_state().items():
        st.setdefault(key, value)
    # 兼容旧字段
    if st.get("peak") is not None and st.get("peak_nav") is None:
        st["peak_nav"] = st["peak"]
        st["peak_nav_date"] = st.get("peak_date")
    state[code] = st
    return st


def load_overrides():
    return _read_json(OVERRIDES_PATH, {
        "updated": None, "market_star": None, "indices": {}, "articles": [],
    })


def save_overrides(overrides):
    overrides["updated"] = datetime.now().isoformat(timespec="seconds")
    _atomic_json(OVERRIDES_PATH, overrides)


def fresh_market_star(overrides, config):
    entry = (overrides or {}).get("market_star") or {}
    value = entry.get("value")
    day = entry.get("date")
    if value is None or not day:
        return None
    try:
        age = (date.today() - date.fromisoformat(day)).days
    except ValueError:
        return None
    max_age = config.get("run", {}).get("article_max_age_days", 7)
    if age > max_age:
        return None
    return value


# ---------------------------------------------------------------------------
# 指标计算
# ---------------------------------------------------------------------------

def pct_rank(values, value):
    if not values:
        return None
    return sum(1 for v in values if v <= value) / float(len(values))


def infer_kind(fund_cfg, basic):
    kind = (fund_cfg.get("kind") or "").lower()
    if kind in ("index", "active"):
        return kind
    if basic.get("index_code"):
        return "index"
    ftype = basic.get("ftype") or ""
    if "指数" in ftype or "ETF" in ftype.upper():
        return "index"
    return "active"


def is_broad(index_name, fund_name):
    text = ("%s %s" % (index_name or "", fund_name or "")).lower()
    return any(keyword.lower() in text for keyword in BROAD_KEYWORDS)


def trailing_for(fund_cfg, kind, broad, config):
    override = fund_cfg.get("trailing_drawdown")
    if override is not None:
        try:
            return float(override)
        except (TypeError, ValueError):
            pass
    trailing = config.get("thresholds", {}).get("trailing", {})
    if kind == "active":
        return float(trailing.get("active", 0.12))
    return float(trailing.get("broad_index", 0.08) if broad
                 else trailing.get("sector_index", 0.10))


def calc_metrics(fund_cfg, basic, nav_data, ratings, val_item, mapped_index,
                 overrides, config, intraday=False, cache_dir=None, offline=False):
    rows = nav_data.get("rows") or []
    if len(rows) < 30:
        raise ds.FetchError("净值数据太少（%d 条）" % len(rows))

    values = []
    valid_rows = []
    for row in rows:
        value = row.get("acc") if row.get("acc") is not None else row.get("nav")
        if value is None or value <= 0:
            continue
        values.append(float(value))
        valid_rows.append(row)
    if len(values) < 30:
        raise ds.FetchError("有效净值数据太少")

    last_row = valid_rows[-1]
    level = values[-1]

    def window(n):
        return values[-n:] if len(values) >= n else values

    nav_pct_3y = pct_rank(window(750), level)
    nav_pct_5y = pct_rank(window(1250), level)

    peak_250 = max(window(250))
    dd_250 = level / peak_250 - 1 if peak_250 else None

    ma_window = window(120)
    ma120 = sum(ma_window) / len(ma_window) if ma_window else None
    ma_dev_120 = level / ma120 - 1 if ma120 else None

    def ret(n):
        if len(values) > n and values[-n - 1]:
            return level / values[-n - 1] - 1
        return None

    prev_nav = valid_rows[-2].get("nav") if len(valid_rows) >= 2 else None
    day_change = last_row.get("change")
    if day_change is None and prev_nav:
        day_change = (last_row.get("nav", level) / prev_nav - 1) * 100

    kind = infer_kind(fund_cfg, basic)
    index_name = basic.get("index_name") or fund_cfg.get("index_name") or ""
    broad = is_broad(index_name, basic.get("name"))

    val_source = "蛋卷"
    val_date = None
    pe_pct = pb_pct = val_pct = None
    eva_type = ""
    pe = pb = roe = None
    if val_item:
        pe_pct = val_item.get("pe_pct")
        pb_pct = val_item.get("pb_pct")
        pe = val_item.get("pe")
        pb = val_item.get("pb")
        roe = val_item.get("roe")
        eva_type = val_item.get("eva_type") or ""
        val_date = val_item.get("date")

    override = (overrides.get("indices") or {}).get(mapped_index) if mapped_index else None
    if override:
        entered = override.get("date")
        try:
            age = (date.today() - date.fromisoformat(entered)).days if entered else 999
        except ValueError:
            age = 999
        if age <= config.get("run", {}).get("article_max_age_days", 7):
            if override.get("pe_pct") is not None:
                pe_pct = override["pe_pct"]
            if override.get("pb_pct") is not None:
                pb_pct = override["pb_pct"]
            if override.get("pct") is not None:
                pe_pct = pe_pct if pe_pct is not None else override["pct"]
            if override.get("level"):
                eva_type = override["level"]
            if override.get("pe") is not None:
                pe = override["pe"]
            if override.get("pb") is not None:
                pb = override["pb"]
            val_source = "公众号"
            val_date = entered

    if pe_pct is not None and pb_pct is not None:
        val_pct = 0.6 * pe_pct + 0.4 * pb_pct
    elif pe_pct is not None:
        val_pct = pe_pct
    elif pb_pct is not None:
        val_pct = pb_pct

    index_quote = None
    if intraday and mapped_index and kind == "index":
        try:
            index_quote = ds.fetch_index_quote(mapped_index, cache_dir=cache_dir, offline=offline)
        except Exception:
            index_quote = None

    return {
        "code": fund_cfg.get("code"),
        "alias": fund_cfg.get("alias") or basic.get("name") or fund_cfg.get("code"),
        "name": basic.get("name") or nav_data.get("name") or fund_cfg.get("code"),
        "ftype": basic.get("ftype") or "",
        "kind": kind,
        "index_name": index_name,
        "index_code": mapped_index,
        "is_broad": broad,
        "nav": last_row.get("nav"),
        "acc_nav": last_row.get("acc"),
        "nav_date": last_row.get("date"),
        "day_change": day_change,
        "nav_pct_3y": nav_pct_3y,
        "nav_pct_5y": nav_pct_5y,
        "dd_250": dd_250,
        "ma_dev_120": ma_dev_120,
        "ret_5": ret(5),
        "ret_20": ret(20),
        "ret_60": ret(60),
        "level_nav": level,
        "spark": [round(v, 6) for v in window(250)][::max(1, len(window(250)) // 160)],
        "level_index": (index_quote or {}).get("level"),
        "index_change_pct": (index_quote or {}).get("change_pct"),
        "pe": pe,
        "pb": pb,
        "roe": roe,
        "pe_pct": pe_pct,
        "pb_pct": pb_pct,
        "val_pct": val_pct,
        "eva_type": eva_type,
        "val_source": val_source if val_item or override else None,
        "val_date": val_date,
        "morningstar": ratings.get("morningstar"),
        "zhaoshang": ratings.get("zhaoshang"),
        "rating_date": ratings.get("date"),
        "trailing_drawdown": trailing_for(fund_cfg, kind, broad, config),
        "cost": fund_cfg.get("cost"),
        "shares": fund_cfg.get("shares"),
        "notes": fund_cfg.get("notes") or "",
        "nav_points": len(values),
        "data_start": valid_rows[0].get("date"),
    }


# ---------------------------------------------------------------------------
# 信号状态机
# ---------------------------------------------------------------------------

def _recent(st, key, today, days):
    last = (st.get("last_alert") or {}).get(key)
    if not last:
        return False
    try:
        return (today - date.fromisoformat(last)).days < days
    except ValueError:
        return False


def _signal(fund_cfg, m, kind, severity, action, reason, tier=None):
    return {
        "code": fund_cfg.get("code"),
        "alias": m.get("alias"),
        "name": m.get("name"),
        "type": kind,
        "severity": severity,
        "action": action,
        "reason": reason,
        "tier": tier,
        "date": date.today().isoformat(),
        "nav": m.get("nav"),
        "nav_date": m.get("nav_date"),
        "val_pct": m.get("val_pct"),
        "nav_pct_3y": m.get("nav_pct_3y"),
        "dd_250": m.get("dd_250"),
        "morningstar": m.get("morningstar"),
    }


def evaluate_fund(fund_cfg, m, st, config):
    """返回 (signals, updated_state)。只在新状态迁移时产生提醒。"""
    today = date.today()
    th = config.get("thresholds", {})
    buy_th = th.get("buy", {})
    sell_th = th.get("sell", {})
    run_th = config.get("run", {})
    cooldown = int(run_th.get("cooldown_days", 5))
    buy_cooldown = int(run_th.get("buy_cooldown_days", 10))
    rearm_days = int(run_th.get("rearm_days", 5))
    rearm_val = float(th.get("rearm_val_pct", 0.70))
    rearm_dd = float(th.get("rearm_drawdown", -0.08))

    market_star = fresh_market_star(load_overrides(), config)
    overlay_star = bool(run_th.get("market_star_overlay", True))
    signals = []

    val_pct = m.get("val_pct")
    nav_pct = m.get("nav_pct_3y")
    dd250 = m.get("dd_250")
    kind = m.get("kind")
    morningstar = m.get("morningstar")

    # 实际生效的高估阈值：市场整体越贵（market_star 越低），越早提示止盈
    high_pct = float(sell_th.get("high_pct", 0.80))
    extreme_pct = float(sell_th.get("extreme_pct", 0.90))
    if overlay_star and market_star is not None:
        if market_star < 3.5:
            high_pct = max(0.05, high_pct - 0.05)
        elif market_star > 4.5:
            high_pct = min(0.99, high_pct + 0.05)

    use_eva = bool(sell_th.get("use_eva_type_fallback", True))
    if kind == "index" and val_pct is not None:
        high_zone = val_pct >= high_pct or (
            use_eva and m.get("eva_type") == "high" and val_pct >= high_pct - 0.05)
        extreme = val_pct >= extreme_pct or (
            use_eva and m.get("eva_type") == "high" and val_pct >= extreme_pct - 0.05)
        buy_low = val_pct <= float(buy_th.get("low_pct", 0.20))
        buy_deep = val_pct <= float(buy_th.get("deep_low_pct", 0.10))
        pb_guard = m.get("pb_pct")
        if pb_guard is not None and pb_guard > float(buy_th.get("pb_guard", 0.30)):
            buy_low = False
        val_desc = "指数估值分位 %.0f%%" % (val_pct * 100)
    elif kind == "index":
        # 有指数代码但没匹配到估值：退化到净值位置
        high_zone = nav_pct is not None and nav_pct >= 0.90 and (dd250 is None or dd250 >= -0.05)
        extreme = nav_pct is not None and nav_pct >= 0.97
        buy_low = nav_pct is not None and nav_pct <= 0.15 and dd250 is not None and dd250 <= -0.20
        buy_deep = nav_pct is not None and nav_pct <= 0.05 and dd250 is not None and dd250 <= -0.30
        val_desc = "无估值数据，净值 3 年分位 %.0f%%" % ((nav_pct or 0) * 100)
    else:
        high_zone = (nav_pct is not None and nav_pct >= float(sell_th.get("active_high_nav_pct", 0.90))
                     and (dd250 is None or dd250 >= -0.05))
        extreme = nav_pct is not None and nav_pct >= float(sell_th.get("active_extreme_nav_pct", 0.97))
        buy_low = (nav_pct is not None and nav_pct <= float(buy_th.get("active_low_nav_pct", 0.15))
                   and dd250 is not None and dd250 <= -0.20)
        buy_deep = (nav_pct is not None and nav_pct <= float(buy_th.get("active_deep_nav_pct", 0.05))
                    and dd250 is not None and dd250 <= -0.30)
        val_desc = "净值 3 年分位 %.0f%%" % ((nav_pct or 0) * 100)

    # ---------------- 买入提醒 ----------------
    if st.get("zone") == "normal" and not high_zone and not st.get("is_dormant"):
        buy_type = None
        if buy_deep:
            buy_type = "BUY_DEEP"
        elif buy_low:
            buy_type = "BUY_LOW"
        if buy_type and not _recent(st, buy_type, today, buy_cooldown):
            if kind == "active":
                action = "分档建仓 / 加大定投" if buy_type == "BUY_DEEP" else "加入观察，可小额定投"
                reason = "%s，距 250 日高点 %.1f%%，主动基金无实时估值，按净值位置判断" % (
                    val_desc, (dd250 or 0) * 100)
            else:
                action = "加大定投额" if buy_type == "BUY_DEEP" else "分批买入 / 定投"
                reason = "%s，PE 分位 %s，PB 分位 %s，距 250 日高点 %.1f%%" % (
                    val_desc,
                    "--" if m.get("pe_pct") is None else "%.0f%%" % (m["pe_pct"] * 100),
                    "--" if m.get("pb_pct") is None else "%.0f%%" % (m["pb_pct"] * 100),
                    (dd250 or 0) * 100)

            severity = "action"
            if morningstar is not None and morningstar <= 2:
                severity = "warn"
                action = "低估，但评级偏低：控制仓位，或换同指数更优产品"
                reason += "；晨星评级 %d 星，质量门槛未通过（指数基金可对比跟踪误差/费率）" % morningstar
            if overlay_star and market_star is not None and market_star < 3.5:
                severity = "info"
                action = "暂缓买入：全市场高估，等待更好位置"
                reason += "；市场星级 %.1f（低于 3.5 视为整体高估）" % market_star
            signals.append(_signal(fund_cfg, m, buy_type, severity, action, reason))
            st.setdefault("last_alert", {})[buy_type] = today.isoformat()

    # ---------------- 回撤止盈状态机 ----------------
    trailing = float(m.get("trailing_drawdown") or 0.10)

    def update_peaks():
        level_nav = m.get("level_nav")
        if level_nav is not None:
            if st.get("peak_nav") is None or level_nav > st["peak_nav"]:
                st["peak_nav"] = level_nav
                st["peak_nav_date"] = m.get("nav_date")
        level_index = m.get("level_index")
        if level_index is not None:
            if st.get("peak_index") is None or level_index > st["peak_index"]:
                st["peak_index"] = level_index
                st["peak_index_date"] = date.today().isoformat()

    def peak_drawdown():
        values = []
        if st.get("peak_nav") and m.get("level_nav"):
            values.append(m["level_nav"] / st["peak_nav"] - 1)
        if st.get("peak_index") and m.get("level_index"):
            values.append(m["level_index"] / st["peak_index"] - 1)
        return min(values) if values else None

    # dormant：等估值/净值回落到正常区，允许下一轮
    if st.get("zone") == "dormant":
        normal_now = ((val_pct is not None and val_pct < rearm_val)
                      or (val_pct is None and nav_pct is not None and nav_pct < rearm_val))
        dd_peak = peak_drawdown()
        if normal_now and (dd_peak is None or dd_peak <= rearm_dd):
            st["normal_days"] = int(st.get("normal_days", 0)) + 1
        else:
            st["normal_days"] = 0
        if st["normal_days"] >= rearm_days:
            st["zone"] = "normal"
            st["sold_tiers"] = []
            st["peak_nav"] = None
            st["peak_nav_date"] = None
            st["peak_index"] = None
            st["peak_index_date"] = None
            st["last_alert"] = {}
            st["normal_days"] = 0
        st["updated"] = today.isoformat()
        return signals, st

    if st.get("zone") == "normal":
        if high_zone:
            update_peaks()
            # 一进入就极度高估：T1+T2 一起给，避免分两天才卖出 2/3
            also_extreme = bool(extreme)
            st["zone"] = "extreme" if also_extreme else "high"
            st["sold_tiers"] = ["T1", "T2"] if also_extreme else ["T1"]
            st["last_alert"]["T1"] = today.isoformat()
            if also_extreme:
                st["last_alert"]["T2"] = today.isoformat()
            action = "减仓约 2/3（T1+T2，已深度止盈区）" if also_extreme else "减仓约 1/3（进入止盈区）"
            reason = "%s；距 250 日高点 %.1f%%；晨星 %s 星；回撤止盈阈值 %.0f%%" % (
                val_desc, (dd250 or 0) * 100,
                "--" if morningstar is None else morningstar, trailing * 100)
            if market_star is not None:
                reason += "；市场星级 %.1f（越高越便宜）" % market_star
            signals.append(_signal(fund_cfg, m, "SELL_T1", "action", action, reason, "T1"))
        st["updated"] = today.isoformat()
        return signals, st

    # 已在 high / extreme
    update_peaks()
    dd_peak = peak_drawdown()

    # T3 优先：跌破峰值，先止盈离场，避免继续坐过山车
    if dd_peak is not None and dd_peak <= -trailing:
        st["zone"] = "dormant"
        st["sold_tiers"] = list(set(st.get("sold_tiers", []) + ["T3"]))
        st["normal_days"] = 0
        st["last_alert"]["T3"] = today.isoformat()
        action = "卖出剩余仓位（回撤止盈 T3）"
        reason = ("从止盈区峰值 %.4f 回撤 %.1f%%，达到 %.0f%% 阈值；峰值日 %s；"
                  "当前 %s") % (
            st.get("peak_nav") or st.get("peak_index") or 0,
            dd_peak * 100, trailing * 100,
            st.get("peak_nav_date") or st.get("peak_index_date") or "--",
            val_desc)
        signals.append(_signal(fund_cfg, m, "SELL_T3", "action", action, reason, "T3"))
        st["updated"] = today.isoformat()
        return signals, st

    if extreme and "T2" not in (st.get("sold_tiers") or []):
        if not _recent(st, "T2", today, cooldown):
            st["sold_tiers"] = list(set(st.get("sold_tiers", []) + ["T2"]))
            st["zone"] = "extreme"
            st["last_alert"]["T2"] = today.isoformat()
            signals.append(_signal(
                fund_cfg, m, "SELL_T2", "action", "再减仓约 1/3（深度止盈区 T2）",
                "%s；距峰值回撤 %.1f%%" % (val_desc, (dd_peak or 0) * 100), "T2"))
    elif high_zone and st.get("zone") != "extreme":
        st["zone"] = "high"

    st["updated"] = today.isoformat()
    return signals, st


# ---------------------------------------------------------------------------
# 运行一次检查
# ---------------------------------------------------------------------------

def _load_all(config, offline=False, intraday=False, quiet=False):
    ensure_dirs()
    proxy = (config.get("network") or {}).get("proxy") or None
    valuations = ds.fetch_index_valuations(cache_dir=CACHE_DIR, offline=offline)
    overrides = load_overrides()
    results = []
    all_signals = []

    for fund_cfg in config.get("funds") or []:
        if not fund_cfg.get("enabled", True):
            continue
        code = str(fund_cfg.get("code") or "").strip()
        if not code:
            continue
        try:
            basic = ds.fetch_fund_basic(code, cache_dir=CACHE_DIR, offline=offline)
            nav_data = ds.fetch_nav_history(code, cache_dir=CACHE_DIR, offline=offline)
            ratings = ds.fetch_ratings(code, cache_dir=CACHE_DIR, offline=offline)

            cfg_index = fund_cfg.get("index_code")
            index_name = basic.get("index_name") or fund_cfg.get("index_name")
            if not index_name:
                index_name = ds.fetch_tracking_index(code, cache_dir=CACHE_DIR, offline=offline)

            if cfg_index:
                if cfg_index in (valuations.get("items") or {}):
                    mapped_index = cfg_index
                else:
                    mapped_index = ds.map_index_code(cfg_index, index_name, valuations)
            else:
                mapped_index = ds.map_index_code(basic.get("index_code"), index_name, valuations)

            val_item = (valuations.get("items") or {}).get(mapped_index) if mapped_index else None
            m = calc_metrics(fund_cfg, basic, nav_data, ratings, val_item, mapped_index,
                             overrides, config, intraday=intraday,
                             cache_dir=CACHE_DIR, offline=offline)
            results.append({"metrics": m, "fund": fund_cfg, "error": None})
            if not quiet:
                log("  %s %s：净值 %s（%s），估值分位 %s" % (
                    code, m["name"], m["nav"], m["nav_date"],
                    "--" if m["val_pct"] is None else "%.0f%%" % (m["val_pct"] * 100)))
        except Exception as exc:  # noqa: BLE001
            log("  %s 抓取失败：%s" % (code, exc))
            results.append({
                "metrics": None, "fund": fund_cfg, "error": str(exc),
                "code": code, "alias": fund_cfg.get("alias") or code,
            })
    return results, all_signals, valuations, overrides


def run_check(config, dry_run=False, offline=False, intraday=False,
              no_push=False, simulate_high=False, quiet=False):
    ensure_dirs()
    today = date.today()
    state = load_state()
    results, _, valuations, overrides = _load_all(config, offline=offline,
                                                  intraday=intraday, quiet=quiet)
    all_signals = []

    for result in results:
        if result.get("error"):
            continue
        m = result["metrics"]
        fund_cfg = result["fund"]
        code = str(fund_cfg.get("code"))
        st = ensure_fund_state(state, code)

        if simulate_high:
            m = copy.deepcopy(m)
            m["val_pct"] = 0.95
            m["pe_pct"] = 0.95
            m["pb_pct"] = 0.90
            m["nav_pct_3y"] = 0.95
            m["dd_250"] = 0.0
            m["eva_type"] = "high"
            st = copy.deepcopy(st)
            st["zone"] = "normal"
            st["sold_tiers"] = []
            st["last_alert"] = {}

        signals, st = evaluate_fund(fund_cfg, m, st, config)
        if not simulate_high:
            state[code] = st
        result["signals"] = signals
        result["state"] = st
        all_signals.extend(signals)

    market_star = fresh_market_star(overrides, config)
    try:
        index_rows = index_watch.collect()
    except Exception as exc:  # noqa: BLE001
        index_rows = []
        log("  指数星级计算失败：%s" % exc)
    meta = {
        "date": today.isoformat(),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "market_star": market_star,
        "index_watch": index_rows,
        "market_star_raw": (overrides or {}).get("market_star"),
        "signal_count": len(all_signals),
        "fund_count": len([r for r in results if not r.get("error")]),
        "error_count": len([r for r in results if r.get("error")]),
        "data_source": "天天基金 / 蛋卷 / 晨星",
        "overrides_updated": (overrides or {}).get("updated"),
        "thresholds": {
            "buy_low_pct": float(((config.get("thresholds") or {}).get("buy") or {}).get("low_pct", 0.20)),
            "buy_deep_pct": float(((config.get("thresholds") or {}).get("buy") or {}).get("deep_low_pct", 0.10)),
            "sell_high_pct": float(((config.get("thresholds") or {}).get("sell") or {}).get("high_pct", 0.80)),
            "sell_extreme_pct": float(((config.get("thresholds") or {}).get("sell") or {}).get("extreme_pct", 0.90)),
        },
    }

    if not simulate_high:
        state["_meta"] = {"updated": meta["generated_at"], "date": meta["date"]}
        save_state(state)
        _append_history(all_signals)
        _atomic_json(LAST_RUN_PATH, {
            "meta": meta,
            "results": [_jsonable_result(r) for r in results],
            "signals": all_signals,
        })

    # 模拟运行只用于验证状态机，不能污染真实日报/面板/周报。
    if not simulate_high:
        md = render.daily_markdown(results, meta)
        with open(os.path.join(REPORT_DIR, "latest.md"), "w", encoding="utf-8") as f:
            f.write(md)
        html_text = render.dashboard_html(results, meta)
        with open(os.path.join(REPORT_DIR, "dashboard.html"), "w", encoding="utf-8") as f:
            f.write(html_text)

        if all_signals:
            title = render.notification_title(all_signals)
            content = render.notification_markdown(all_signals, results, meta)
            if index_rows:
                content += "\n\n" + index_watch.markdown(index_rows)
            _push(config, title, content, dry_run=dry_run, no_push=no_push)
        else:
            if not quiet:
                log("  没有新的买卖信号。")
            if index_rows and config.get("run", {}).get("index_watch_push"):
                row800 = next((r for r in index_rows if r["key"] == "zz800"), None)
                title = "指数星级｜%s%s" % (
                    today.isoformat(),
                    (" 中证800 %.2f★" % row800["star"]) if row800 else "")
                _push(config, title, index_watch.markdown(index_rows),
                      dry_run=dry_run, no_push=no_push)

        # 周报：默认每周五（isoweekday=5）
        weekly_day = int(config.get("run", {}).get("weekly_summary_weekday", 5))
        if today.isoweekday() == weekly_day:
            weekly = render.weekly_markdown(results, meta)
            week_no = today.isocalendar()[1]
            path = os.path.join(REPORT_DIR, "weekly-%s-W%02d.md" % (today.isocalendar()[0], week_no))
            with open(path, "w", encoding="utf-8") as f:
                f.write(weekly)
            if not quiet:
                log("  周报已生成：%s" % path)
            _push(config, "基金周报｜%s" % today.isoformat(), weekly,
                  dry_run=dry_run, no_push=no_push)

    if simulate_high:
        print("\n=== 模拟高估信号（不保存状态、不推送、不改写日报/面板）===")
        for sig in all_signals:
            print("  %s %s：%s —— %s" % (sig["code"], sig["alias"], sig["action"], sig["reason"]))

    if not quiet:
        if simulate_high:
            log("模拟完成：%d 只基金，%d 条模拟信号" % (meta["fund_count"], meta["signal_count"]))
        else:
            log("完成：%d 只基金，%d 条信号，面板 %s" % (
                meta["fund_count"], meta["signal_count"],
                os.path.join(REPORT_DIR, "dashboard.html")))
    return 0 if meta["error_count"] == 0 else 1


def _jsonable_result(result):
    out = {"fund": result.get("fund"), "error": result.get("error"),
           "metrics": result.get("metrics"), "signals": result.get("signals") or [],
           "state": result.get("state")}
    return out


def _append_history(signals):
    if not signals:
        return
    try:
        with open(HISTORY_PATH, "a", encoding="utf-8") as f:
            for sig in signals:
                f.write(json.dumps(sig, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _push(config, title, content, dry_run=False, no_push=False):
    if dry_run:
        print("\n--- [dry-run] 推送内容 ---")
        print(title)
        print(content)
        print("--- [dry-run] 结束 ---")
        return
    if no_push:
        return
    push_cfg = config.get("push") or {}
    if not push_cfg.get("enabled"):
        log("  推送未启用（push.enabled=false），内容仅写入报告。")
        return
    ok, msg = ds.send_push(config, title, content)
    if ok:
        log("  微信推送成功：%s" % title)
    else:
        log("  微信推送失败：%s" % msg)
        try:
            with open(os.path.join(LOG_DIR, "push_failures.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps({"time": datetime.now().isoformat(), "title": title,
                                    "error": msg}, ensure_ascii=False) + "\n")
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 回测：用历史净值回放同一套止盈纪律
# ---------------------------------------------------------------------------

def cmd_backtest(args):
    config = load_config()
    code = args.code
    nav_data = ds.fetch_nav_history(code, cache_dir=CACHE_DIR, offline=args.offline)
    rows = nav_data["rows"]
    if not rows:
        print("没有净值数据")
        return 1

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    trailing = args.trailing
    if trailing is None:
        trailing = 0.10

    series = [(date.fromisoformat(r["date"]),
               (r["acc"] if r["acc"] is not None else r["nav"])) for r in rows]
    series = [(d, v) for d, v in series if v and v > 0]

    zone = "normal"
    peak = None
    peak_date = None
    sold_tiers = []
    dormant_days = 0
    cash = 0.0
    shares = 1.0
    original = 1.0
    signals = []
    first_value = None
    end_value = None
    end_day = None
    equity_peak = 0.0
    buyhold_peak = 0.0
    max_dd_strategy = 0.0
    max_dd_buyhold = 0.0

    for i, (day, value) in enumerate(series):
        if day < start:
            continue
        if first_value is None:
            first_value = value
        if day > end:
            break
        end_value = value
        end_day = day
        if i < 250:
            continue

        hist = [v for _, v in series[:i + 1]]
        window750 = hist[-750:]
        window250 = hist[-250:]
        nav_pct = pct_rank(window750, value)
        dd250 = value / max(window250) - 1
        high_zone = nav_pct >= 0.90 and dd250 >= -0.05
        extreme = nav_pct >= 0.97

        if zone == "dormant":
            dd_peak = value / peak - 1 if peak else 0
            if nav_pct <= 0.70 and dd_peak <= -0.08:
                dormant_days += 1
            else:
                dormant_days = 0
            if dormant_days >= 5:
                zone = "normal"
                peak = None
                peak_date = None
                sold_tiers = []
                dormant_days = 0

        if zone == "normal" and high_zone:
            zone = "high"
            peak = value
            peak_date = day
            sold_tiers = ["T1"]
            shares -= original / 3.0
            cash += (original / 3.0) * value
            signals.append((day, "T1", value, "进入高估区，减仓 1/3", nav_pct, dd250))
        elif zone in ("high", "extreme"):
            if peak is None or value > peak:
                peak = value
                peak_date = day
            dd_peak = value / peak - 1 if peak else 0
            if dd_peak <= -trailing:
                cash += shares * value
                signals.append((day, "T3", value,
                                "从峰值回撤 %.1f%%，卖出剩余" % (dd_peak * 100), nav_pct, dd250))
                shares = 0.0
                zone = "dormant"
            elif extreme and "T2" not in sold_tiers:
                sold_tiers.append("T2")
                zone = "extreme"
                shares -= original / 3.0
                cash += (original / 3.0) * value
                signals.append((day, "T2", value, "极度高估，再减 1/3", nav_pct, dd250))

        equity = cash + shares * value
        equity_peak = max(equity_peak, equity)
        if equity_peak:
            max_dd_strategy = min(max_dd_strategy, equity / equity_peak - 1)
        buyhold = value / first_value
        buyhold_peak = max(buyhold_peak, buyhold)
        if buyhold_peak:
            max_dd_buyhold = min(max_dd_buyhold, buyhold / buyhold_peak - 1)

    if end_value is None:
        print("区间内没有净值数据。")
        return 1
    final_strategy = cash + shares * end_value
    final_buyhold = end_value / first_value if first_value else 1
    print("回测：%s %s  %s ~ %s（最后净值日 %s）" % (
        code, nav_data["name"], args.start, args.end, end_day.isoformat()))
    print("说明：历史指数估值不可得，本回测用净值位置（3 年分位≥90% 且距 250 日高点≤5%）")
    print("      模拟进入高估区，再用 %.0f%% 回撤止盈；收益按累计净值计算。" % (trailing * 100))
    print("")
    if signals:
        print("触发记录：")
        for day, tier, value, note, nav_pct, dd250 in signals:
            print("  %s  %s  净值 %.4f  3年分位 %.0f%%  距250日高点 %.1f%%  %s" % (
                day.isoformat(), tier, value, nav_pct * 100, dd250 * 100, note))
    else:
        print("该区间没有触发任何止盈信号。")
    print("")
    print("策略最终净值：%.4f（买入持有 %.4f）" % (final_strategy, final_buyhold))
    print("策略最大回撤：%.1f%%（买入持有 %.1f%%）" % (
        max_dd_strategy * 100, max_dd_buyhold * 100))
    if signals:
        print("纪律效果：策略最大回撤比买入持有少 %.1f 个百分点" % (
            abs(max_dd_buyhold - max_dd_strategy) * 100))
    return 0


# ---------------------------------------------------------------------------
# 公众号文章
# ---------------------------------------------------------------------------

def _slug(text, limit=40):
    text = "".join(ch for ch in (text or "") if ch not in '\\/:*?"<>|')
    text = text.strip().replace(" ", "-")
    return text[:limit] or "article"


def process_article(url, config, overrides, valuations):
    art = ds.fetch_article(url, cache_dir=ARTICLE_CACHE, offline=False)
    day = art.get("publish") or date.today().isoformat()

    # 归档 Markdown
    fname = "%s-%s.md" % (day, _slug(art.get("title")))
    path = os.path.join(ARTICLE_DIR, fname)
    with open(path, "w", encoding="utf-8") as f:
        f.write("# %s\n\n" % art["title"])
        f.write("- 链接：%s\n" % url)
        f.write("- 发布时间：%s\n" % day)
        f.write("- 市场星级：%s（5 星最便宜）\n" % (art.get("market_star") or "未识别"))
        f.write("- 图片数：%d，文字表格数：%d\n\n" % (
            art.get("images", 0), len(art.get("tables") or [])))
        for table in art.get("tables") or []:
            f.write("\n## 表格\n\n")
            for row in table:
                f.write("| " + " | ".join(row) + " |\n")
                if row is table[0]:
                    f.write("|" + "---|" * len(row) + "\n")
        f.write("\n## 正文\n\n")
        f.write(art.get("text", "")[:12000])
        f.write("\n")

    if art.get("market_star") is not None:
        overrides["market_star"] = {
            "value": art["market_star"],
            "date": day,
            "source": url,
            "title": art["title"],
        }

    extracted = ds.extract_index_overrides(art.get("tables") or [], valuations)
    for idx_code, entry in extracted.items():
        entry["date"] = day
        entry["source"] = url
        overrides.setdefault("indices", {})[idx_code] = entry

    # 同一篇文章重复解析时替换旧记录，避免 inbox 反复粘贴导致重复。
    articles = [a for a in overrides.get("articles", []) if a.get("url") != url]
    articles.append({
        "url": url, "title": art["title"], "date": day,
        "market_star": art.get("market_star"),
        "tables": len(art.get("tables") or []),
        "images": art.get("images", 0),
        "text_length": art.get("text_length", 0),
    })
    overrides["articles"] = articles[-30:]
    save_overrides(overrides)

    print("文章：%s" % art["title"])
    print("  发布时间：%s  市场星级：%s" % (
        day, "--" if art.get("market_star") is None else art["market_star"]))
    print("  正文 %d 字，表格 %d 个，图片 %d 张" % (
        art.get("text_length", 0), len(art.get("tables") or []), art.get("images", 0)))
    print("  已归档：%s" % path)
    if extracted:
        print("  解析到 %d 个指数估值覆盖：%s" % (
            len(extracted), ", ".join(extracted.keys())))
    if art.get("text_length", 0) < 300 or (art.get("images", 0) > 0 and not art.get("tables")):
        print("  [注意] 正文/表格可能是图片，无法 OCR。请在 data/valuation_overrides.json 手动补录，")
        print("         或直接把估值表截图里的数字发给我，我帮你写进去。")
    return art


def cmd_article(args):
    ensure_dirs()
    config = load_config()
    overrides = load_overrides()
    valuations = ds.fetch_index_valuations(cache_dir=CACHE_DIR)

    urls = []
    if args.url and not args.inbox:
        urls = [args.url]
    else:
        if not os.path.exists(INBOX_PATH):
            print("没有找到 inbox：%s" % INBOX_PATH)
            return 1
        with open(INBOX_PATH, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    urls.append(line)
    if not urls:
        print("inbox.txt 里没有链接。")
        return 1

    failed = 0
    for url in urls:
        if not (url.startswith("https://mp.weixin.qq.com/")
                or url.startswith("http://mp.weixin.qq.com/")):
            failed += 1
            print("跳过（不是公众号链接）：%s" % url)
            continue
        try:
            process_article(url, config, overrides, valuations)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print("解析失败：%s\n  %s" % (url, exc))
    return 0 if failed == 0 else 1


# ---------------------------------------------------------------------------
# 配置管理
# ---------------------------------------------------------------------------

def _write_bat():
    bat = os.path.join(ROOT, "run_daily.bat")
    python_exe = sys.executable
    content = (
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        "set PYTHONIOENCODING=utf-8\r\n"
        'cd /d "%~dp0"\r\n'
        '"' + python_exe + '" fund.py check >> logs\\run.log 2>&1\r\n'
    )
    with open(bat, "w", encoding="utf-8", newline="") as f:
        f.write(content)
    return bat


def cmd_init(args):
    ensure_dirs()
    if os.path.exists(CONFIG_PATH) and not args.force:
        print("config.json 已存在，未覆盖。需要重置请加 --force。")
    else:
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["funds"] = copy.deepcopy(DEMO_FUNDS) if args.demo else []
        save_config(config)
        print("已生成 config.json%s" % ("（含 3 只演示基金）" if args.demo else ""))

    with open(EXAMPLE_PATH, "w", encoding="utf-8") as f:
        example = copy.deepcopy(DEFAULT_CONFIG)
        example["funds"] = copy.deepcopy(DEMO_FUNDS)
        json.dump(example, f, ensure_ascii=False, indent=2)

    if not os.path.exists(INBOX_PATH):
        with open(INBOX_PATH, "w", encoding="utf-8") as f:
            f.write("# 把公众号文章链接粘到下面，每行一条，然后运行：\n")
            f.write("# python fund.py article --inbox\n")

    bat = _write_bat()
    print("定时任务入口：%s" % bat)
    print("")
    print("下一步：")
    print("  1) python fund.py check --dry-run   # 抓数据、算信号、不推送")
    print("  2) python fund.py report --open     # 打开 HTML 面板")
    print("  3) 在 config.json 填 push 配置后，python fund.py push-test")
    print("  4) 注册定时任务：python fund.py schedule")
    return 0


def cmd_add(args):
    config = load_config()
    code = str(args.code).strip()
    if any(str(f.get("code")) == code for f in config.get("funds") or []):
        print("基金 %s 已在配置里。" % code)
        return 1
    basic = ds.fetch_fund_basic(code, cache_dir=CACHE_DIR)
    kind = args.kind or infer_kind({}, basic)
    index_name = basic.get("index_name")
    if not index_name:
        try:
            index_name = ds.fetch_tracking_index(code, cache_dir=CACHE_DIR)
        except Exception:
            index_name = None
    mapped = None
    try:
        valuations = ds.fetch_index_valuations(cache_dir=CACHE_DIR)
        mapped = ds.map_index_code(basic.get("index_code"), index_name, valuations)
    except Exception:
        pass

    fund = {
        "code": code,
        "alias": args.alias or basic["name"],
        "kind": kind,
        "index_code": args.index_code or mapped,
        "index_name": index_name,
        "enabled": True,
        "cost": None,
        "shares": None,
        "trailing_drawdown": None,
        "notes": "",
    }
    config.setdefault("funds", []).append(fund)
    save_config(config)
    print("已添加：%s %s（%s）" % (code, basic["name"], kind))
    if index_name:
        print("  跟踪标的：%s；估值映射：%s" % (index_name, mapped or "未匹配"))
    if kind == "active":
        print("  主动基金没有实时估值，将使用净值位置指标。")
    return 0


def cmd_remove(args):
    config = load_config()
    before = len(config.get("funds") or [])
    config["funds"] = [f for f in config.get("funds") or []
                       if str(f.get("code")) != str(args.code)]
    save_config(config)
    print("已移除 %s（%d -> %d）" % (args.code, before, len(config["funds"])))
    return 0


def cmd_list(args):
    config = load_config()
    funds = config.get("funds") or []
    if not funds:
        print("配置里还没有基金。用 python fund.py add <代码> 添加。")
        return 0
    print("%-8s %-22s %-8s %-14s %s" % ("代码", "名称", "类型", "估值映射", "状态"))
    for f in funds:
        print("%-8s %-22s %-8s %-14s %s" % (
            f.get("code"), (f.get("alias") or "")[:20], f.get("kind") or "-",
            f.get("index_code") or "-", "启用" if f.get("enabled", True) else "停用"))
    return 0


def cmd_report(args):
    if os.path.exists(LAST_RUN_PATH):
        payload = _read_json(LAST_RUN_PATH, {})
        meta = payload.get("meta") or {}
        results = payload.get("results") or []
        md = render.daily_markdown(results, meta)
        html_text = render.dashboard_html(results, meta)
    else:
        print("还没有运行记录，先跑一次 check（不推送）。")
        run_check(load_config(), dry_run=True, quiet=True)
        payload = _read_json(LAST_RUN_PATH, {})
        meta = payload.get("meta") or {}
        results = payload.get("results") or []
        md = render.daily_markdown(results, meta)
        html_text = render.dashboard_html(results, meta)

    with open(os.path.join(REPORT_DIR, "latest.md"), "w", encoding="utf-8") as f:
        f.write(md)
    html_path = os.path.join(REPORT_DIR, "dashboard.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_text)
    print("面板：%s" % html_path)
    if args.open:
        try:
            os.startfile(html_path)  # Windows
        except Exception:
            print("（无法自动打开，请手动双击）")
    return 0


def cmd_push_test(args):
    config = load_config()
    if not (config.get("push") or {}).get("enabled"):
        print("push.enabled 还是 false。先在 config.json 里填 token 并改成 true。")
        return 1
    content = ("## 测试消息\n\n这是一条来自 fund-monitor 的测试推送。\n\n"
               "- 如果你收到，说明 Server酱 / PushPlus 配置正确。\n"
               "- 日常只在触发买/卖信号或周报时推送。\n")
    ok, msg = ds.send_push(config, "基金监控｜推送测试", content)
    print("成功" if ok else "失败：%s" % msg)
    return 0 if ok else 1


def cmd_schedule(args):
    bat = os.path.join(ROOT, "run_daily.bat")
    cmd = ('schtasks /Create /TN "FundMonitor" /SC WEEKLY /D MON-FRI /ST 21:30 '
           '/TR "%s" /F' % bat)
    print("注册每个交易日 21:30 运行（当前用户，无需管理员）：")
    print("")
    print(cmd)
    print("")
    print("如果要当天 15:00 前行动，可以再加一个 14:20 的任务：")
    print('schtasks /Create /TN "FundMonitorIntraday" /SC WEEKLY /D MON-FRI /ST 14:20 '
          '/TR "%s fund.py check --intraday" /F' % sys.executable)
    print("")
    print("注意：任务默认只在用户登录时运行。删除任务用 schtasks /Delete /TN FundMonitor /F")
    if args.install:
        os.system(cmd)
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(description="基金估值监控与止盈提醒")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("init", help="初始化目录和配置")
    p.add_argument("--demo", action="store_true", help="写入 3 只演示基金")
    p.add_argument("--force", action="store_true", help="覆盖已有 config.json")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("add", help="添加基金")
    p.add_argument("code")
    p.add_argument("--kind", choices=["index", "active"])
    p.add_argument("--alias")
    p.add_argument("--index-code", help="手动指定蛋卷指数代码，如 SZ399997")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("remove", help="移除基金")
    p.add_argument("code")
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser("list", help="列出配置里的基金")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("check", help="抓数据、算信号、按需推送")
    p.add_argument("--dry-run", action="store_true", help="只打印，不推送")
    p.add_argument("--offline", action="store_true", help="只用缓存")
    p.add_argument("--intraday", action="store_true", help="指数基金使用实时点位跟踪峰值")
    p.add_argument("--no-push", action="store_true")
    p.add_argument("--simulate-high", action="store_true", help="模拟高估，验证状态机，不保存")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=lambda a: run_check(load_config(), dry_run=a.dry_run,
                                            offline=a.offline, intraday=a.intraday,
                                            no_push=a.no_push,
                                            simulate_high=a.simulate_high,
                                            quiet=a.quiet))

    p = sub.add_parser("report", help="生成 HTML 面板")
    p.add_argument("--open", action="store_true")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("backtest", help="用历史净值回放止盈纪律")
    p.add_argument("code")
    p.add_argument("--start", default="2019-01-01")
    p.add_argument("--end", default=date.today().isoformat())
    p.add_argument("--trailing", type=float, default=None)
    p.add_argument("--offline", action="store_true")
    p.set_defaults(func=cmd_backtest)

    p = sub.add_parser("article", help="解析公众号文章链接")
    p.add_argument("url", nargs="?", help="文章链接")
    p.add_argument("--inbox", action="store_true", help="解析 articles/inbox.txt 里的所有链接")
    p.set_defaults(func=cmd_article)

    p = sub.add_parser("push-test", help="发送测试推送")
    p.set_defaults(func=cmd_push_test)

    p = sub.add_parser("schedule", help="显示/注册 Windows 定时任务")
    p.add_argument("--install", action="store_true", help="直接执行 schtasks")
    p.set_defaults(func=cmd_schedule)

    return parser


def main():
    ensure_dirs()
    parser = build_parser()
    args = parser.parse_args()
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    if not os.path.exists(CONFIG_PATH) and args.command not in ("init",):
        print("还没有 config.json，先运行：python fund.py init --demo")
        return 1
    try:
        return args.func(args) or 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        log("运行出错：%s" % exc)
        if os.environ.get("FUND_DEBUG"):
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
