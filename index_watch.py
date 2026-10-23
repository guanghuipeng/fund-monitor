#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""指数星级日报：中证800 / 中证500 / 沪深300 / 全市场。

星级只看指数自己的历史，不是螺丝钉的全市场星级：
  修正星级 = min(估值星级, 价格星级)
  估值星级：全历史 PE/PB 综合分位 10/20/40/60/80/100%
            -> 5.0/4.8/4.0/3.0/2.0/1.0
  价格星级：相对过去 3 年最高点的回撤 <=5/10/20/30%
            -> 3.8/4.2/4.5/5.0
  4.8 星 = 估值分位 <=20% 且价格距 3 年高点 <= -26%
  全市场没有统一价格指数，所以全市场只算估值星级，作参考。

买卖动作使用 v3.2 规则（见 v3_rule.py）：深度首笔 + 趋势加仓 +
V 型底补仓 + 崩盘过滤 + 60%/63% 卖出 + 强趋势 12% 移动止盈 + 40% 浮盈保险。

用法：
  python index_watch.py                     # 终端打印
  python index_watch.py --json              # JSON
  python index_watch.py --push              # 通过 config.json 的 Server酱/PushPlus 推送
  python index_watch.py --html docs/index.html   # 生成 GitHub Pages 静态页
"""
import argparse
import bisect
import json
import os
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone

import cycle_backtest as cb
import v3_rule

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
ALERT_STATE_PATH = os.path.join(BASE, "data", "alert_state.json")
HISTORY_PATH = os.path.join(BASE, "data", "star_history.jsonl")
PAGE_URL = os.environ.get("INDEX_PAGE_URL") or ""
HISTORY_YEARS = 3
HISTORY_KEY_ORDER = {k: i for i, k in enumerate(
    ("zz800", "zz500", "hs300", "market"))}

DEFAULT_KEYS = [("zz800", "中证800"), ("zz500", "中证500"),
                ("hs300", "沪深300"), ("market", "全市场")]

QUOTE_SECIDS = {"zz800": "1.000906", "zz500": "1.000905",
                "hs300": "1.000300"}
QUOTE_TENCENT = {"zz800": "sh000906", "zz500": "sh000905",
                 "hs300": "sh000300"}
CN_TZ = timezone(timedelta(hours=8))
# 腾讯/东财都是国内行情，直连即可；空 ProxyHandler 可避免走本机代理时
# urllib 做反向 DNS（proxy_bypass）拖慢几秒。
_QUOTE_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _scaled(value, digits=2):
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    if x != x:
        return None
    return x / (10.0 ** int(digits))


def _quote_tencent(key):
    """腾讯行情，字段简单且盘中/收盘都稳定，作为主数据源。"""
    code = QUOTE_TENCENT.get(key)
    if not code:
        return None
    url = "https://qt.gtimg.cn/q=%s" % code
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://gu.qq.com/",
    })
    try:
        with _QUOTE_OPENER.open(req, timeout=10) as resp:
            text = resp.read().decode("gbk", "ignore")
    except Exception:  # noqa: BLE001
        return None
    body = text.split('"', 2)
    if len(body) < 2:
        return None
    parts = body[1].split("~")
    if len(parts) < 33:
        return None
    try:
        price = float(parts[3])
        prev = float(parts[4])
    except ValueError:
        return None
    if price <= 0 or prev <= 0:
        return None
    try:
        chg_pct = float(parts[32]) / 100.0
    except (TypeError, ValueError):
        chg_pct = price / prev - 1.0
    try:
        qdt = datetime.strptime(parts[30], "%Y%m%d%H%M%S").replace(tzinfo=CN_TZ)
    except (TypeError, ValueError):
        qdt = datetime.now(CN_TZ)
    return {"price": price, "prev": prev, "chg_pct": chg_pct,
            "date": qdt.date(), "time": qdt.strftime("%m-%d %H:%M"),
            "name": parts[1] or ""}


def _quote_eastmoney(key):
    """东方财富行情，作为备用源。"""
    secid = QUOTE_SECIDS.get(key)
    if not secid:
        return None
    url = ("https://push2.eastmoney.com/api/qt/stock/get?secid=%s"
           "&fields=f43,f57,f58,f59,f60,f86,f170" % secid)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://quote.eastmoney.com/",
    })
    try:
        with _QUOTE_OPENER.open(req, timeout=10) as resp:
            obj = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    d = obj.get("data") or {}
    digits = int(d.get("f59") or 2)
    price = _scaled(d.get("f43"), digits)
    prev = _scaled(d.get("f60"), digits)
    if price is None or not prev:
        return None
    try:
        chg_pct = float(d.get("f170")) / 10000.0
    except (TypeError, ValueError):
        chg_pct = price / prev - 1.0
    try:
        qdt = datetime.fromtimestamp(int(d.get("f86")), tz=CN_TZ)
    except (TypeError, ValueError, OSError):
        qdt = datetime.now(CN_TZ)
    return {"price": price, "prev": prev, "chg_pct": chg_pct,
            "date": qdt.date(), "time": qdt.strftime("%m-%d %H:%M"),
            "name": d.get("f58") or ""}


_QUOTE_CACHE = {}


def fetch_quote(key):
    """实时/收盘行情：腾讯优先，失败再试东方财富；都失败返回 None。

    同一次运行里同一指数只抓一次，避免估值/距离/历史各算一遍时重复请求。
    """
    if key in _QUOTE_CACHE:
        return _QUOTE_CACHE[key]
    _QUOTE_CACHE[key] = _quote_tencent(key) or _quote_eastmoney(key)
    return _QUOTE_CACHE[key]


def _valuation_percentile(pe, pb, key, weight_hs300, rows=None):
    rows = rows if rows is not None else cb._load_raw_rows(key, weight_hs300)
    pes = sorted(float(r["pe_ttm"]) for r in rows)
    pbs = sorted(float(r["pb"]) for r in rows)
    return (0.6 * bisect.bisect_right(pes, pe) / float(len(pes))
            + 0.4 * bisect.bisect_right(pbs, pb) / float(len(pbs)))


def _live_pe_pb(key, weight_hs300):
    """最新 PE/PB：估值缓存 + 今日行情（缓存比行情旧时按涨跌缩放）。"""
    rows = cb._load_raw_rows(key, weight_hs300)
    if not rows:
        return None, None, []
    last = rows[-1]
    s_date = date.fromisoformat(last["date_k"])
    pe, pb = float(last["pe_ttm"]), float(last["pb"])
    quote = fetch_quote(key)
    if quote and quote["date"] > s_date:
        pe *= 1.0 + quote["chg_pct"]
        pb *= 1.0 + quote["chg_pct"]
    return pe, pb, rows


def _live_val_star(key, weight_hs300):
    """成分指数最新估值星级。"""
    pe, pb, rows = _live_pe_pb(key, weight_hs300)
    if not rows:
        return None
    return cb.valuation_star(
        _valuation_percentile(pe, pb, key, weight_hs300, rows))


def _valuation_price_t1(key, weight_hs300):
    """估值星级达到 4.8 时对应的指数点位（按当前 PE/PB 同比例缩放）。

    中证800 的估值星级是 75% 沪深300 + 25% 中证500 的加权，
    所以这里也按成分各自的估值星级加权求解。
    """
    if key == "market":
        return None
    if key == "zz800":
        comp = []
        for k in ("hs300", "zz500"):
            pe, pb, rows = _live_pe_pb(k, weight_hs300)
            if not rows:
                return None
            comp.append((pe, pb, rows))
        w = weight_hs300

        def star_at(f):
            a = cb.valuation_star(_valuation_percentile(
                comp[0][0] * f, comp[0][1] * f, "hs300", w, comp[0][2]))
            b = cb.valuation_star(_valuation_percentile(
                comp[1][0] * f, comp[1][1] * f, "zz500", w, comp[1][2]))
            return w * a + (1.0 - w) * b
    else:
        pe, pb, rows = _live_pe_pb(key, weight_hs300)
        if not rows:
            return None

        def star_at(f):
            return cb.valuation_star(_valuation_percentile(
                pe * f, pb * f, key, weight_hs300, rows))

    if star_at(1.0) >= 4.8:
        return 1.0
    lo, hi = 0.2, 1.0
    for _ in range(50):
        mid = (lo + hi) / 2.0
        if star_at(mid) >= 4.8:
            lo = mid
        else:
            hi = mid
    return lo


def _zone(pall, star):
    if pall >= 0.63:
        return "第二档止盈区（≥63%）"
    if pall >= 0.60:
        return "第一档止盈区（≥60%）"
    if star >= 4.8:
        return "建仓区（≥4.8星）"
    if star >= 4.5:
        return "观察区（接近4.8星）"
    return "等待区（新资金不买）"


def _apply_quote(row, key, quote, weight_hs300):
    """用实时行情更新今日涨跌；必要时按价格变动估算星级。"""
    row["chg_pct"] = None
    row["quote_time"] = None
    row["estimate"] = False
    row["estimate_label"] = ""
    row["point_note"] = "合成点位" if key == "zz800" else ""
    row["px_display"] = row["star_px"]
    row["high_display"] = row["star_high"]
    if not quote:
        return
    s_date = date.fromisoformat(row["data_date"])
    if quote["date"] < s_date:
        return

    row["chg_pct"] = quote["chg_pct"]
    row["quote_time"] = quote["time"]
    row["quote_date"] = quote["date"].isoformat()
    if quote["date"] == s_date:
        # 官方数据已包含当天；只把中证800的合成点位换算成真实点位
        if row["star_px"]:
            scale = quote["price"] / row["star_px"]
            row["px_display"] = quote["price"]
            row["high_display"] = (row["star_high"] or 0) * scale
            row["point_note"] = ""
        return

    # 行情比估值缓存新：按价格变动同比例缩放 PE/PB，估算今天的星级
    chg = quote["chg_pct"]
    pe = row["pe"] * (1.0 + chg)
    pb = row["pb"] * (1.0 + chg)
    pall = _valuation_percentile(pe, pb, key, weight_hs300)
    live_level = row["star_px"] * (1.0 + chg) if row["star_px"] else None
    if live_level is None:
        return
    high = max(row["star_high"] or live_level, live_level)
    dd = live_level / high - 1.0
    val_star = cb.valuation_star(pall)
    p_star = cb.price_star(dd)
    star = min(val_star, p_star)
    scale = quote["price"] / live_level
    row.update({
        "pe": pe, "pb": pb, "pall": pall,
        "val_star": val_star, "price_star": p_star, "star": star,
        "star_dd": dd, "star_high": high, "star_px": live_level,
        "px_display": quote["price"], "high_display": high * scale,
        "point_note": "", "estimate": True, "zone": _zone(pall, star),
        "estimate_label": ("收盘估算" if quote["time"][-5:] >= "15:00"
                           else "盘中估算"),
    })


def _period_stats(series, days):
    if len(series) < 2:
        return None
    end_d, end_v = series[-1]
    dates_only = [d for d, _ in series]
    i = bisect.bisect_left(dates_only, end_d - timedelta(days=days))
    if i >= len(series) - 1:
        i = max(0, len(series) - 2)
    start_v = series[i][1]
    if start_v <= 0:
        return None
    pts = series[i:]
    if len(pts) > 60:
        step = len(pts) / 60.0
        pts = [pts[int(j * step)] for j in range(60)]
        pts[-1] = (end_d, end_v)
    return {"chg": end_v / start_v - 1.0,
            "start": series[i][0].isoformat(),
            "points": [round(v, 4) for _, v in pts]}


def _attach_trend(row, key, quote, weight_hs300):
    if key == "market":
        row["trend"] = {}
        return
    nav, dates = cb.load_index_nav(key, weight_hs300)
    if not dates:
        row["trend"] = {}
        return
    series = [(d, nav[d]) for d in dates]
    if quote and quote["date"] > dates[-1]:
        series.append((quote["date"], series[-1][1] * (1.0 + quote["chg_pct"])))
    row["trend"] = {str(d): _period_stats(series, d) for d in (7, 30, 365)}


def _row(key, name, weight_hs300=0.75):
    s = cb.latest_snapshot(code=None, key=key, weight_hs300=weight_hs300)
    row = {
        "key": key,
        "name": name,
        "data_date": s["date"].isoformat(),
        "pe": s["pe"],
        "pb": s["pb"],
        "p10": s["p10"],
        "pall": s["pall"],
        "star": s["star"],
        "val_star": s["val_star"],
        "price_star": s["price_star"],
        "star_dd": s["star_dd"],
        "star_high": s["star_high"],
        "star_px": s["star_px"],
        "zone": s["zone"] if key != "market" else "参考（不可直接买）",
        "market_pall": s["market_pall"],
        "market_star": s["market_star"],
        "tradeable": key != "market",
    }
    quote = fetch_quote(key) if key != "market" else None
    _apply_quote(row, key, quote, weight_hs300)
    if key == "zz800":
        # 800 的估值星级 = 75% 沪深300 + 25% 中证500 的估值星级，
        # 再与 800 自己的价格星级取 min；卖出仍看原来的全历史分位。
        va = _live_val_star("hs300", weight_hs300)
        vb = _live_val_star("zz500", weight_hs300)
        if va is not None and vb is not None:
            val_star = weight_hs300 * va + (1.0 - weight_hs300) * vb
            row["val_star"] = val_star
            row["star"] = (min(val_star, row["price_star"])
                           if row.get("price_star") is not None else val_star)
            row["zone"] = _zone(row["pall"], row["star"])
    if key != "market" and row.get("high_display"):
        row["price_t1"] = row["high_display"] * 0.74  # price_star 4.8 的位置
        row["price_gap"] = row["px_display"] / row["price_t1"] - 1.0
        row["val_gap"] = row["pall"] - 0.20
        vf = _valuation_price_t1(key, weight_hs300)
        row["val_price_t1"] = (row["px_display"] * vf) if vf else None
        if row["val_price_t1"] and row.get("px_display"):
            row["val_px_gap"] = row["val_price_t1"] / row["px_display"] - 1.0
        else:
            row["val_px_gap"] = None
        levels = [x for x in (row["price_t1"], row["val_price_t1"]) if x]
        if levels and row.get("px_display"):
            row["drop_to_t1"] = min(levels) / row["px_display"] - 1.0
        else:
            row["drop_to_t1"] = None
    else:
        row["price_t1"] = None
        row["price_gap"] = None
        row["val_gap"] = None
        row["val_price_t1"] = None
        row["val_px_gap"] = None
        row["drop_to_t1"] = None
    _attach_trend(row, key, quote, weight_hs300)
    v3_rule.attach(row, key, weight_hs300)
    return row


def refresh_cache(keys=None):
    """联网刷新指数估值历史；失败时保留旧缓存，不中断日报。"""
    for key in keys or ["hs300", "zz500", "market"]:
        try:
            n = cb.refresh_history(key)
            print("已刷新 %s：%d 条" % (key, n), file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print("刷新 %s 失败：%s（继续用缓存）" % (key, exc), file=sys.stderr)


def collect(keys=None, weight_hs300=0.75, refresh=False):
    wanted = set(keys or [k for k, _ in DEFAULT_KEYS])
    if refresh:
        needed = set()
        for key in wanted:
            if key == "zz800":
                needed.update(["hs300", "zz500"])
            elif key == "market":
                needed.add("market")
            else:
                needed.add(key)
        refresh_cache(sorted(needed))
    rows = []
    for key, name in DEFAULT_KEYS:
        if key not in wanted:
            continue
        rows.append(_row(key, name, weight_hs300))
    return rows


def _fmt_pct(x, digits=1, sign=False):
    if x is None:
        return "--"
    pattern = ("%+." if sign else "%.") + str(digits) + "f%%"
    return pattern % (x * 100.0)


def _fmt_num(x, digits=2):
    if x is None:
        return "--"
    return ("%." + str(digits) + "f") % x


# 本轮资金分配：默认 20 万，中证500 / 中证800 各一半，两个子账户各自
# 按自己的 4.8★ 和 T1/T2/T3 时间分批，互不等待、互不挪用。
DEFAULT_CAPITAL = 200000.0
DEFAULT_ALLOCATION = (("zz500", 0.5), ("zz800", 0.5))
ALLOC_NAMES = {"zz500": "中证500", "zz800": "中证800", "hs300": "沪深300"}


def _alloc_name(key):
    return ALLOC_NAMES.get(key, key)


def _wan(x):
    if x is None:
        return "--"
    return "%.1f万" % (x / 10000.0)


def _allocation_plan():
    """返回 (总资金, [(指数, 子账户金额, 权重)])；config 可覆盖。"""
    config = load_config()
    raw = config.get("allocation") or {}
    try:
        total = float(raw.get("capital", DEFAULT_CAPITAL))
    except (TypeError, ValueError):
        total = DEFAULT_CAPITAL
    if total <= 0:
        total = DEFAULT_CAPITAL
    weights = []
    for key in ("zz500", "zz800", "hs300"):
        try:
            w = float(raw.get(key, 0) or 0)
        except (TypeError, ValueError):
            w = 0.0
        if w > 0:
            weights.append((key, w))
    if not weights:
        weights = list(DEFAULT_ALLOCATION)
    norm = sum(w for _, w in weights)
    return total, [(k, total * w / norm, w / norm) for k, w in weights]


def _sleeve_plan(row, amount):
    """一个独立子账户的金额计划（元）。"""
    frac = row.get("v3_first_frac")
    if frac is None or frac <= 0 or frac > 1:
        frac = 1.0 / 3.0
    stage = int(row.get("v3_stage") or 0)
    t1 = amount * frac
    add = amount * (1.0 - frac) / 2.0
    code = row.get("v3_today_code")
    today = ""
    if code == "T1":
        today = "今日T1买 %s" % _wan(t1)
    elif code in ("T2", "T3"):
        today = "今日%s加 %s" % (code, _wan(add))
    elif code == "S1":
        today = "今日卖 1/3"
    elif code == "S2":
        today = "今日卖剩余 2/3"
    elif code in ("保险", "TR"):
        today = "今日清仓"
    if stage <= 0:
        status = row.get("v3_action") or "空仓等待 4.8★"
        if row.get("star") is not None and row["star"] >= 4.8:
            nxt = "T1 买 %s（首笔 %.0f%%），T2/T3 各约 %s" % (
                _wan(t1), frac * 100, _wan(add))
        else:
            nxt = "T1 约 3.3/5.0/6.7万（按触发时 1 年回撤分档）"
        return {"status": status, "today": today, "invested": 0.0,
                "stage": 0, "next": nxt}
    pos = row.get("v3_position")
    if pos is None:
        pos = frac + (stage - 1) * (1.0 - frac) / 2.0
    invested = amount * pos
    if row.get("v3_s1_done"):
        nxt = ("移动止盈中" if row.get("v3_trail_on")
               else "已卖 1/3，剩余等 ≥63%")
    elif stage >= 3:
        nxt = "等分位 ≥60% 卖 1/3"
    elif stage == 1:
        nxt = "T2 约 %s" % _wan(add)
    else:
        nxt = "T3 约 %s" % _wan(add)
    return {"status": row.get("v3_action") or "持有", "today": today,
            "invested": invested, "stage": stage, "next": nxt}


def _sleeve_brief(row, amount):
    """推送里一行子账户说明，尽量短。"""
    frac = row.get("v3_first_frac")
    if frac is None or frac <= 0 or frac > 1:
        frac = 1.0 / 3.0
    code = row.get("v3_today_code")
    if code == "T1":
        buy = amount * frac
        return "子账户%s：今日T1买%s（首笔%.0f%%），剩%s" % (
            _wan(amount), _wan(buy), frac * 100, _wan(amount - buy))
    if code in ("T2", "T3"):
        return "子账户%s：今日%s加%s" % (
            _wan(amount), code, _wan(amount * (1.0 - frac) / 2.0))
    if code == "S1":
        return "子账户%s：今日卖1/3" % _wan(amount)
    if code == "S2":
        return "子账户%s：今日卖剩余2/3" % _wan(amount)
    if code in ("保险", "TR"):
        return "子账户%s：今日清仓" % _wan(amount)
    plan = _sleeve_plan(row, amount)
    if not row.get("v3_stage"):
        return "子账户%s：空仓，等4.8★" % _wan(amount)
    return "子账户%s：累计买入%s（%d/3笔），%s" % (
        _wan(amount), _wan(plan["invested"]), plan["stage"], plan["next"])


def _allocation_text():
    total, plan = _allocation_plan()
    return ("本轮总资金 %s：%s；每个子账户按自己的 4.8★ 和 "
            "T1/T2/T3 信号独立建仓，互不等待、互不挪用。" % (
                _wan(total),
                " + ".join("%s %s（%.0f%%）" % (
                    _alloc_name(k), _wan(a), w * 100)
                    for k, a, w in plan)))


def distance_text(row):
    if not row["tradeable"]:
        return "全市场只作参考，不可直接买"
    suffix = ("（%s）" % row["estimate_label"]) if row.get("estimate") else ""
    if row["star"] >= 4.8:
        return "已到 4.8 星买点" + suffix
    head = ""
    if row.get("drop_to_t1") is not None and row["drop_to_t1"] < 0:
        head = "还需跌 %.1f%%；" % (-row["drop_to_t1"] * 100)
    parts = []
    if row.get("val_star") is not None and row["val_star"] >= 4.8:
        parts.append("估值条件已满足")
    elif row.get("val_price_t1") is not None:
        parts.append("估值条件需 ≤%.0f（现 %.0f，%+.1f%%）" % (
            row["val_price_t1"], row["px_display"], row["val_px_gap"] * 100))
    elif row.get("key") == "zz800":
        parts.append("成分估值星级需 ≥4.8（现在 %.2f）" % row["val_star"])
    else:
        parts.append("估值分位需 ≤20%%（现在 %.1f%%）" % (row["pall"] * 100))
    if row.get("price_t1") is not None:
        if row.get("price_star") is not None and row["price_star"] >= 4.8:
            parts.append("价格星级已满足")
        else:
            gap = row["price_t1"] / row["px_display"] - 1.0
            parts.append("价格条件需 ≤%.0f（现 %.0f，%+.1f%%）" % (
                row["price_t1"], row["px_display"], gap * 100))
    return head + "；".join(parts) + suffix


def markdown(rows, title_date=None):
    d = title_date or datetime.now(CN_TZ).date().isoformat()
    lines = ["## 指数星级日报｜%s" % d, ""]
    verdict_bits = []
    for r in rows:
        if not r["tradeable"]:
            continue
        action = r.get("v3_action") or r.get("v3_current_action") or "等待"
        verdict_bits.append("%s **%.2f★** %s" % (r["name"], r["star"], action))
    if verdict_bits:
        lines.append("**结论**：%s。" % "；".join(verdict_bits))
        lines.append("")
    if any(r.get("estimate") for r in rows):
        lines.append("> 今日涨跌为实时/收盘行情；盘中或收盘估算按价格变动缩放 PE/PB，"
                     "晚间官方数据更新后以官方值为准。")
        lines.append("")

    lines.append("| 指数 | 今日 | 修正星级 | 估值星级 | 价格星级 | 全历史分位 | 3年回撤 | PE | PB | 状态 |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---|")
    for r in rows:
        today = _fmt_pct(r.get("chg_pct"), 2, sign=True)
        lines.append("| %s | %s | **%.2f** | %.2f | %s | %.1f%% | %s | %.2f | %.2f | %s |" % (
            r["name"], today, r["star"], r["val_star"],
            "--" if r["price_star"] is None else "%.2f" % r["price_star"],
            r["pall"] * 100,
            "--" if r["star_dd"] is None else "%.1f%%" % (r["star_dd"] * 100),
            r["pe"], r["pb"], r["zone"]))
    lines.append("")
    lines.append("**怎么读**：修正星级 = min(估值星级, 价格星级)，只看该指数自己的历史，"
                 "不是全市场星级。4.8 星才是第一档买点；全市场那一行只作参考。"
                 "中证800 的估值星级 = 75% 沪深300 + 25% 中证500 的估值星级加权；"
                 "全历史分位仍只用于卖出。")
    lines.append("")
    lines.append("### 走势")
    lines.append("| 指数 | 近1周 | 近1月 | 近1年 |")
    lines.append("|---|---:|---:|---:|")
    for r in rows:
        cells = []
        for key in ("7", "30", "365"):
            t = (r.get("trend") or {}).get(key)
            cells.append(_fmt_pct(t["chg"], 1, sign=True) if t else "--")
        lines.append("| %s | %s | %s | %s |" % (r["name"], cells[0], cells[1], cells[2]))
    lines.append("")
    lines.append("### 距离 4.8 星买点")
    for r in rows:
        lines.append("- **%s**：%.2f 星。%s。" % (r["name"], r["star"], distance_text(r)))
    lines.append("")
    _, alloc_plan = _allocation_plan()
    lines.append("### 本轮资金分配（各自分批）")
    lines.append(_allocation_text())
    lines.append("")
    for key, amount, weight in alloc_plan:
        r = next((x for x in rows if x.get("key") == key), None)
        if r is None:
            continue
        p = _sleeve_plan(r, amount)
        status = p["status"] + ("；" + p["today"] if p["today"] else "")
        if p["stage"]:
            lines.append("- **%s（%s，%.0f%%）**：%s；累计买入 %s（%d/3 笔）；"
                         "下一笔：%s。" % (
                             _alloc_name(key), _wan(amount), weight * 100,
                             status, _wan(p["invested"]), p["stage"],
                             p["next"]))
        else:
            lines.append("- **%s（%s，%.0f%%）**：%s；下一笔：%s。" % (
                _alloc_name(key), _wan(amount), weight * 100,
                status, p["next"]))
    lines.append("")
    lines.append("### v3.2 规则（已启用）")
    for r in rows:
        if not r["tradeable"] or not r.get("v3_action"):
            continue
        lines.append("- **%s**：%s。%s" % (
            r["name"], r["v3_action"], r.get("v3_detail") or ""))
    lines.append("")
    lines.append("### 现行规则对照")
    for r in rows:
        if not r["tradeable"]:
            continue
        lines.append("- **%s**：现行规则说“%s”；v3.2 说“%s”。" % (
            r["name"], r.get("v3_current_action") or "不买",
            r.get("v3_action") or "等待"))
    lines.append("")
    lines.append("### 规则")
    lines.append("- 买入：4.8★ 首笔按 1 年回撤加深：≤-60% 投 2/3，"
                 "≤-40% 投 1/2，否则 1/3；剩余现金分给 T2/T3。")
    lines.append("- 加仓：T2 较 T1 跌 10%，或 T1 一年回撤≤-40% 且 120 日线转上；"
                 "若价格始终不到，T1 后 60 天且年线向上、现价高于年线 8% 时补第二笔；"
                 "T3 较 T1 跌 20%，或 250 日线转上。")
    lines.append("- 崩盘过滤：1 年回撤 ≤-30% 且现价低于年线 15% 时，"
                 "暂停按价格加仓；回到年线 85% 以上或趋势确认后恢复。")
    lines.append("- 卖出：全历史分位 ≥60% 卖 1/3；≥63% 卖剩余 2/3；"
                 "若点位高于年线 30%，剩余改 12% 移动止盈。")
    lines.append("- 保险：浮盈最高 ≥40% 后，从本轮高点回撤 10% 且年线向上，清仓；"
                 "强趋势中不触发。")
    lines.append("- 空仓资金：放货币基金；回测按年化 1.5% 计息，"
                 "不改变任何买卖信号。")
    lines.append("")
    lines.append("数据：乐咕 PE/PB + 腾讯/东方财富行情；中证800 的 PE/PB 为 75% 沪深300 + "
                 "25% 中证500 合成，价格优先用中证800 实际点位。仅为估值记录，不构成投资建议。")
    return "\n".join(lines)


def _esc(s):
    import html
    return html.escape("" if s is None else str(s))


# ------------------------------------------------------------ 星级历史
def _history_cutoff(years=HISTORY_YEARS):
    today = datetime.now(CN_TZ).date()
    try:
        return today.replace(year=today.year - years)
    except ValueError:  # 2 月 29 日
        return today.replace(year=today.year - years, day=28)


def _pe_pb_map(key, weight_hs300=0.75):
    rows = cb._load_raw_rows(key, weight_hs300)
    return {r["date_k"]: (float(r["pe_ttm"]), float(r["pb"])) for r in rows}


def _hist_record(key, dk, star, val_star, price_star, pall, dd,
                 pe, pb, px=None, flag="o"):
    return {
        "d": dk, "k": key, "s": round(star, 3),
        "v": None if val_star is None else round(val_star, 3),
        "p": None if price_star is None else round(price_star, 3),
        "q": None if pall is None else round(pall, 4),
        "r": None if dd is None else round(dd, 4),
        "e": None if pe is None else round(pe, 2),
        "b": None if pb is None else round(pb, 2),
        "x": None if px is None else round(px, 2),
        "f": flag,
    }


def backfill_star_history(years=HISTORY_YEARS, weight_hs300=0.75):
    """用官方估值历史回算最近 N 年的每日星级。

    口径和线上页面一致：中证800 的估值星级 = 75% 沪深300 + 25% 中证500
    的估值星级加权，再和自己的价格星级取 min。
    """
    import strategy_backtest as sb

    cutoff = _history_cutoff(years)
    out = {}
    comp = {}
    for key in ("zz500", "hs300"):
        pe_map = _pe_pb_map(key, weight_hs300)
        recs = {}
        for d, p, px, dd, _star in sb.build_series(key, weight_hs300):
            if d < cutoff:
                continue
            dk = d.isoformat()
            if dk not in pe_map:
                continue
            pe, pb = pe_map[dk]
            val_star = cb.valuation_star(p)
            price_star = cb.price_star(dd)
            recs[dk] = _hist_record(
                key, dk, min(val_star, price_star), val_star, price_star,
                p, dd, pe, pb, px)
        comp[key] = recs
        out[key] = recs

    pe_map = _pe_pb_map("zz800", weight_hs300)
    recs = {}
    for d, p, _px, dd, _star in sb.build_series("zz800", weight_hs300):
        dk = d.isoformat()
        if d < cutoff or dk not in pe_map:
            continue
        va = (comp["hs300"].get(dk) or {}).get("v")
        vb = (comp["zz500"].get(dk) or {}).get("v")
        if va is None or vb is None:
            val_star = cb.valuation_star(p)
        else:
            val_star = weight_hs300 * va + (1.0 - weight_hs300) * vb
        price_star = cb.price_star(dd)
        pe, pb = pe_map[dk]
        # 800 的历史点位是合成点位，和真实 000906 不同，历史里不存点位。
        recs[dk] = _hist_record(
            "zz800", dk, min(val_star, price_star), val_star, price_star,
            p, dd, pe, pb, None)
    out["zz800"] = recs

    pe_map = _pe_pb_map("market", weight_hs300)
    recs = {}
    for d, p in cb.load_valuation("all", "market", weight_hs300):
        dk = d.isoformat()
        if d < cutoff or dk not in pe_map:
            continue
        pe, pb = pe_map[dk]
        star = cb.valuation_star(p)
        recs[dk] = _hist_record(
            "market", dk, star, star, None, p, None, pe, pb, None)
    out["market"] = recs
    return out


def _official_records_for_dates(dates_iso, weight_hs300=0.75):
    """只回算指定日期（通常只有缓存新出现的 1-2 天）。

    全量 build_series 要跑几十秒，日常补齐没必要；这里对每个日期直接算
    “截至当天的分位 + 过去 3 年回撤”，结果与全量回算一致。
    """
    wanted = set(dates_iso)
    if not wanted:
        return {}
    raw = {k: cb._load_raw_rows(k, weight_hs300)
           for k in ("zz500", "hs300", "zz800", "market")}
    idx = {k: {r["date_k"]: i for i, r in enumerate(v)}
           for k, v in raw.items()}
    navs = {}
    for key in ("zz500", "hs300", "zz800"):
        try:
            navs[key] = cb.load_index_nav(key, weight_hs300)
        except Exception:  # noqa: BLE001
            navs[key] = (None, None)

    def pct(key, i):
        rows = raw[key]
        pe = float(rows[i]["pe_ttm"])
        pb = float(rows[i]["pb"])
        pes = sorted(float(r["pe_ttm"]) for r in rows[:i + 1])
        pbs = sorted(float(r["pb"]) for r in rows[:i + 1])
        return (0.6 * bisect.bisect_right(pes, pe) / len(pes)
                + 0.4 * bisect.bisect_right(pbs, pb) / len(pbs))

    def price_dd(key, dk):
        nav, nav_dates = navs.get(key) or (None, None)
        if not nav:
            return None, None
        d = date.fromisoformat(dk)
        j = bisect.bisect_right(nav_dates, d) - 1
        if j < 0:
            return None, None
        px = nav[nav_dates[j]]
        try:
            c = d.replace(year=d.year - 3)
        except ValueError:
            c = d.replace(year=d.year - 3, day=28)
        k0 = bisect.bisect_left(nav_dates, c)
        high = max(nav[x] for x in nav_dates[k0:j + 1])
        return px, px / high - 1.0

    out = {}
    comp = {}
    for key in ("zz500", "hs300"):
        rows = raw[key]
        for dk in wanted:
            i = idx[key].get(dk)
            if i is None:
                continue
            pe, pb = float(rows[i]["pe_ttm"]), float(rows[i]["pb"])
            p = pct(key, i)
            val_star = cb.valuation_star(p)
            px, dd = price_dd(key, dk)
            price_star = cb.price_star(dd) if dd is not None else None
            star = min(val_star, price_star) if price_star is not None else val_star
            out[(dk, key)] = _hist_record(
                key, dk, star, val_star, price_star, p, dd, pe, pb, px)
            comp.setdefault(dk, {})[key] = val_star

    rows = raw["zz800"]
    for dk in wanted:
        i = idx["zz800"].get(dk)
        if i is None:
            continue
        pe, pb = float(rows[i]["pe_ttm"]), float(rows[i]["pb"])
        p = pct("zz800", i)
        vals = comp.get(dk) or {}
        va, vb = vals.get("hs300"), vals.get("zz500")
        if va is None or vb is None:
            val_star = cb.valuation_star(p)
        else:
            val_star = weight_hs300 * va + (1.0 - weight_hs300) * vb
        _px, dd = price_dd("zz800", dk)
        price_star = cb.price_star(dd) if dd is not None else None
        star = min(val_star, price_star) if price_star is not None else val_star
        out[(dk, "zz800")] = _hist_record(
            "zz800", dk, star, val_star, price_star, p, dd, pe, pb, None)

    rows = raw["market"]
    for dk in wanted:
        i = idx["market"].get(dk)
        if i is None:
            continue
        pe, pb = float(rows[i]["pe_ttm"]), float(rows[i]["pb"])
        p = pct("market", i)
        star = cb.valuation_star(p)
        out[(dk, "market")] = _hist_record(
            "market", dk, star, star, None, p, None, pe, pb, None)
    return out


def _live_history_records(rows):
    """把本次实时/收盘快照转成历史记录（只覆盖当天）。"""
    recs = []
    for r in rows:
        if r.get("star") is None:
            continue
        dk = r.get("quote_date") or r.get("data_date")
        if not dk:
            continue
        label = r.get("estimate_label") or ""
        flag = "i" if label == "盘中估算" else ("c" if label == "收盘估算" else "o")
        recs.append(_hist_record(
            r["key"], dk, r["star"], r.get("val_star"), r.get("price_star"),
            r.get("pall"), r.get("star_dd"), r.get("pe"), r.get("pb"),
            r.get("px_display") if r.get("tradeable") else None, flag))
    return recs


def load_star_history():
    if not os.path.exists(HISTORY_PATH):
        return []
    out = []
    with open(HISTORY_PATH, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("d") and rec.get("k") and rec.get("s") is not None:
                out.append(rec)
    return out


def _save_star_history(recs):
    parent = os.path.dirname(HISTORY_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(HISTORY_PATH, "w", encoding="utf-8") as fh:
        for rec in recs:
            fh.write(json.dumps(rec, ensure_ascii=False,
                                separators=(",", ":")) + "\n")


def update_star_history(rows, years=HISTORY_YEARS, weight_hs300=0.75,
                        force=False):
    """回算历史 + 写入今日快照，返回按日期排序的记录列表。

    同一天：官方估值回算优先于盘中/收盘估算；实时记录只补今天及缓存还没
    更新到的日期。
    """
    cutoff = _history_cutoff(years)
    merged = {}
    for rec in load_star_history() if not force else []:
        try:
            d = date.fromisoformat(rec["d"])
        except (KeyError, TypeError, ValueError):
            continue
        if d >= cutoff:
            merged[(rec["d"], rec["k"])] = rec

    # 每个指数只补“自己的官方数据里存在、但历史里还没有官方值”的日期。
    # 这样既能在晚间把盘中估算改成官方值，也不会因为全市场缺周五而反复重算。
    expected = {}
    for key in ("zz500", "hs300", "zz800", "market"):
        expected[key] = {r["date_k"] for r in cb._load_raw_rows(key, weight_hs300)
                         if r["date_k"] >= cutoff.isoformat()}
    needs = set()
    for key, day_set in expected.items():
        for dk in day_set:
            rec = merged.get((dk, key))
            if rec is None or rec.get("f") != "o":
                needs.add(dk)
    needs = sorted(needs)

    try:
        if force or not merged or len(needs) > 60:
            official = {}
            for recs in backfill_star_history(years, weight_hs300).values():
                official.update(recs)
        else:
            official = _official_records_for_dates(needs, weight_hs300)
    except Exception as exc:  # noqa: BLE001
        print("回算历史星级失败：%s（只记录今日）" % exc, file=sys.stderr)
        official = {}
    for rec in official.values():
        merged[(rec["d"], rec["k"])] = rec

    for rec in _live_history_records(rows):
        prev = merged.get((rec["d"], rec["k"]))
        if prev and prev.get("f") == "o" and rec.get("f") != "o":
            continue
        merged[(rec["d"], rec["k"])] = rec

    out = sorted(merged.values(),
                 key=lambda r: (r["d"], HISTORY_KEY_ORDER.get(r["k"], 99)))
    _save_star_history(out)
    return out


def _spark(points, up, width=112, height=32):
    vals = [float(v) for v in (points or []) if v is not None]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    span = hi - lo or 1.0
    pts = []
    for i, v in enumerate(vals):
        x = i * width / float(len(vals) - 1)
        y = height - 2 - (v - lo) / span * (height - 4)
        pts.append("%.1f,%.1f" % (x, y))
    cls = "up" if up else "down"
    return ('<svg class="trend-spark %s" viewBox="0 0 %d %d" '
            'preserveAspectRatio="none"><polyline points="%s" fill="none" '
            'stroke="currentColor" stroke-width="1.7" stroke-linejoin="round" '
            'stroke-linecap="round"/></svg>') % (cls, width, height, " ".join(pts))


def _history_html(history):
    """历史星级板块：走势图 + 分页表格 + 单日明细（纯前端，无外部依赖）。"""
    payload = json.dumps(history or [], ensure_ascii=False,
                         separators=(",", ":")).replace("</", "<\\/")
    return """<style>
#history .hist-controls{display:flex;justify-content:space-between;align-items:center;
gap:12px;flex-wrap:wrap;margin:0 0 10px}
.hist-ranges{display:flex;gap:6px;flex-wrap:wrap}
.hist-ranges button{border:1px solid var(--line);background:#fff;color:var(--muted);
border-radius:999px;padding:4px 11px;font-size:12px;cursor:pointer}
.hist-ranges button.on{background:var(--accent);border-color:var(--accent);color:#fff}
.hist-legend{display:flex;gap:12px;flex-wrap:wrap;font-size:12px;color:var(--muted)}
.hist-legend .hl{display:inline-flex;align-items:center;gap:5px}
.hist-legend i{width:14px;height:3px;border-radius:2px;display:inline-block}
.hist-chart-wrap{position:relative;margin:4px 0 12px}
#histChart{width:100%;height:auto;display:block}
.hist-tip{position:absolute;display:none;background:#111827;color:#fff;font-size:12px;
padding:4px 8px;border-radius:6px;pointer-events:none;white-space:nowrap;z-index:5}
.hist-table-wrap{overflow-x:auto}
.hist-table td{white-space:nowrap}
.hist-table tbody tr{cursor:pointer}
.hist-table tbody tr:hover{background:#f3f6fb}
.hist-table td.st-buy{color:var(--green);font-weight:700}
.hist-table td.st-watch{color:var(--amber);font-weight:700}
.hist-table td.na{color:#b6bec9}
.hist-pager{display:flex;align-items:center;gap:10px;flex-wrap:wrap;
margin:12px 0 0;font-size:13px;color:var(--muted)}
.hist-pager button{border:1px solid var(--line);background:#fff;color:var(--text);
border-radius:8px;padding:4px 10px;font-size:13px;cursor:pointer}
.hist-pager button:disabled{opacity:.45;cursor:default}
.hist-jump{margin-left:auto;display:inline-flex;align-items:center;gap:6px}
.hist-jump input{border:1px solid var(--line);border-radius:8px;padding:3px 6px;
font-size:13px;color:var(--text)}
.hist-detail{display:none;background:#f8fafc;border:1px solid var(--line);
border-radius:12px;padding:12px;margin:12px 0}
.hd-date{font-size:13px;font-weight:700;color:var(--muted);margin:0 0 8px}
.hd-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px}
.hd-card{background:#fff;border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.hd-top{display:flex;justify-content:space-between;align-items:center;font-size:12px}
.hd-name{font-weight:700}
.hd-flag{color:var(--muted);font-size:11px}
.hd-star{font-size:24px;font-weight:700;line-height:1.2}
.hd-star span{font-size:12px;color:var(--accent);margin-left:2px}
.hd-star.st-buy{color:var(--green)}
.hd-star.st-watch{color:var(--amber)}
.hd-metrics{display:flex;flex-wrap:wrap;gap:4px 12px;font-size:11px;color:var(--muted)}
.hd-metrics b{color:var(--text);font-weight:600}
@media(max-width:640px){
.hist-table{font-size:11px}
.hist-jump{margin-left:0}
.hd-grid{grid-template-columns:1fr 1fr}
}
</style>
<section class="card" id="history">
<h2>历史星级</h2>
<p class="rule">每个交易日一行，点日期看当天明细。早期数据由官方估值回算，
当天盘中为估算值，晚间以官方数据为准。</p>
<div class="hist-controls">
  <div class="hist-ranges" id="histRanges">
    <button data-days="30">30天</button>
    <button data-days="90" class="on">90天</button>
    <button data-days="180">180天</button>
    <button data-days="365">1年</button>
    <button data-days="0">全部</button>
  </div>
  <div class="hist-legend" id="histLegend"></div>
</div>
<div class="hist-chart-wrap">
  <svg id="histChart" role="img" aria-label="星级历史走势"></svg>
  <div class="hist-tip" id="histTip"></div>
</div>
<div class="hist-detail" id="histDetail"></div>
<table class="hist-table">
<thead><tr><th>日期</th><th>中证800</th><th>中证500</th><th>沪深300</th><th>全市场</th></tr></thead>
<tbody id="histBody"></tbody>
</table>
<div class="hist-pager">
  <button id="histPrev">上一页</button>
  <span id="histPage"></span>
  <button id="histNext">下一页</button>
  <label class="hist-jump">跳到 <input id="histDate" type="date"> <button id="histGo">查看</button></label>
</div>
</section>
<script>
(function(){
var HIST = __HIST_JSON__;
var KEYS = ["zz800","zz500","hs300","market"];
var NAMES = {zz800:"中证800",zz500:"中证500",hs300:"沪深300",market:"全市场"};
var COLORS = {zz800:"#2563eb",zz500:"#dc2626",hs300:"#059669",market:"#9ca3af"};
var FLAGS = {o:"官方",i:"盘中估算",c:"收盘估算"};
var PAGE = 20;
var byDate = {}, dates = [], page = 1;
for (var i = 0; i < HIST.length; i++) {
  var r = HIST[i];
  if (!byDate[r.d]) { byDate[r.d] = {}; dates.push(r.d); }
  byDate[r.d][r.k] = r;
}
dates.sort();
if (!dates.length) {
  var box = document.getElementById("history");
  if (box) box.insertAdjacentHTML("beforeend",
    '<p class="rule">还没有历史记录，下一次生成页面时会写入。</p>');
  return;
}
var W = 900, H = 300, PAD = {l:40, r:16, t:18, b:30}, chartState = {ds:[]};
function el(tag, attrs, text) {
  var e = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (var k in attrs) e.setAttribute(k, attrs[k]);
  if (text != null) e.textContent = text;
  return e;
}
function rangeDates(days) {
  if (!days) return dates.slice();
  var last = new Date(dates[dates.length-1] + "T00:00:00");
  var start = new Date(last.getTime());
  start.setDate(start.getDate() - days);
  return dates.filter(function(d){ return new Date(d+"T00:00:00") >= start; });
}
function drawChart(days) {
  var svg = document.getElementById("histChart");
  if (!svg) return;
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  svg.setAttribute("viewBox", "0 0 " + W + " " + H);
  var ds = rangeDates(days);
  chartState.ds = ds;
  if (ds.length < 2) {
    svg.appendChild(el("text", {x:W/2, y:H/2, "text-anchor":"middle",
      fill:"#9ca3af", "font-size":"14"}, "历史数据积累中"));
    return;
  }
  var minv = 5.0;
  ds.forEach(function(d){
    var rec = byDate[d] || {};
    KEYS.forEach(function(k){
      var r = rec[k];
      if (r && r.s != null) minv = Math.min(minv, r.s);
    });
  });
  var lo = Math.min(3.5, Math.floor((minv - 0.1) * 10) / 10), hi = 5.0;
  function x(i) { return PAD.l + i * (W - PAD.l - PAD.r) / (ds.length - 1); }
  function y(v) { v = Math.max(lo, Math.min(hi, v));
    return PAD.t + (hi - v) / (hi - lo) * (H - PAD.t - PAD.b); }
  var ticks = [lo, 4.0, 4.5, 4.8, 5.0].filter(function(v, i, a){
    return v >= lo - 1e-9 && a.indexOf(v) === i; });
  ticks.forEach(function(v){
    var key = Math.abs(v - 4.8) < 1e-9;
    svg.appendChild(el("line", {x1:PAD.l, x2:W-PAD.r, y1:y(v), y2:y(v),
      stroke:key ? "#b45309" : "#e5e7eb", "stroke-width":key ? 1.4 : 1,
      "stroke-dasharray":key ? "5 4" : ""}));
    svg.appendChild(el("text", {x:PAD.l-6, y:y(v)+4, "text-anchor":"end",
      fill:key ? "#b45309" : "#9ca3af", "font-size":"11"}, v.toFixed(1)));
  });
  var n = Math.min(6, ds.length);
  var longRange = !days || days >= 365;
  for (var i = 0; i < n; i++) {
    var idx = Math.round(i * (ds.length - 1) / (n - 1));
    svg.appendChild(el("text", {x:x(idx), y:H-10,
      "text-anchor":i === 0 ? "start" : (i === n-1 ? "end" : "middle"),
      fill:"#9ca3af", "font-size":"11"},
      longRange ? ds[idx].slice(2) : ds[idx].slice(5)));
  }
  KEYS.forEach(function(k){
    var pts = [];
    ds.forEach(function(d, i){
      var rec = byDate[d] || {}, r = rec[k];
      if (r && r.s != null) pts.push(x(i) + "," + y(r.s));
    });
    if (pts.length > 1) {
      svg.appendChild(el("polyline", {points:pts.join(" "), fill:"none",
        stroke:COLORS[k], "stroke-width":1.8, "stroke-linejoin":"round",
        "stroke-linecap":"round", opacity:k === "market" ? 0.7 : 1}));
    }
  });
  var hg = el("g", {});
  svg.appendChild(hg);
  svg.onmousemove = function(ev) {
    var rect = svg.getBoundingClientRect();
    var mx = (ev.clientX - rect.left) * (W / rect.width);
    var idx = Math.round((mx - PAD.l) / ((W - PAD.l - PAD.r) / (ds.length - 1)));
    idx = Math.max(0, Math.min(ds.length - 1, idx));
    while (hg.firstChild) hg.removeChild(hg.firstChild);
    hg.appendChild(el("line", {x1:x(idx), x2:x(idx), y1:PAD.t, y2:H-PAD.b,
      stroke:"#9ca3af", "stroke-width":1, "stroke-dasharray":"3 3"}));
    var rec = byDate[ds[idx]] || {};
    KEYS.forEach(function(k){
      var r = rec[k];
      if (r && r.s != null) hg.appendChild(el("circle",
        {cx:x(idx), cy:y(r.s), r:3, fill:COLORS[k]}));
    });
    var tip = document.getElementById("histTip");
    if (tip) {
      var lines = [ds[idx]];
      KEYS.forEach(function(k){
        if (rec[k]) lines.push(NAMES[k] + " " + rec[k].s.toFixed(2));
      });
      tip.textContent = lines.join(" · ");
      tip.style.display = "block";
      var left = x(idx) / W * rect.width;
      tip.style.left = Math.max(0, Math.min(rect.width - 150, left - 70)) + "px";
      tip.style.top = "2px";
    }
  };
  svg.onmouseleave = function() {
    while (hg.firstChild) hg.removeChild(hg.firstChild);
    var tip = document.getElementById("histTip");
    if (tip) tip.style.display = "none";
  };
}
function fmt(v, d) { return v == null ? "--" : v.toFixed(d == null ? 2 : d); }
function pct(v) { return v == null ? "--" : (v * 100).toFixed(1) + "%"; }
function starCls(v) { return v >= 4.8 ? "st-buy" : (v >= 4.5 ? "st-watch" : ""); }
function starCell(r) {
  if (!r) return '<td class="na">--</td>';
  return '<td class="' + starCls(r.s) + '">' + r.s.toFixed(2) + '</td>';
}
function renderTable() {
  var desc = dates.slice().reverse();
  var pages = Math.max(1, Math.ceil(desc.length / PAGE));
  page = Math.max(1, Math.min(page, pages));
  var rows = desc.slice((page-1)*PAGE, page*PAGE).map(function(d){
    var rec = byDate[d] || {};
    return '<tr data-date="' + d + '"><td class="name">' + d + '</td>' +
      KEYS.map(function(k){ return starCell(rec[k]); }).join("") + '</tr>';
  }).join("");
  document.getElementById("histBody").innerHTML = rows;
  document.getElementById("histPage").textContent =
    "第 " + page + " / " + pages + " 页 · 共 " + desc.length + " 个交易日";
  document.getElementById("histPrev").disabled = page <= 1;
  document.getElementById("histNext").disabled = page >= pages;
}
function showDetail(d) {
  var rec = byDate[d];
  if (!rec) return;
  var cards = KEYS.filter(function(k){ return rec[k]; }).map(function(k){
    var r = rec[k];
    return '<div class="hd-card"><div class="hd-top">' +
      '<span class="hd-name" style="color:' + COLORS[k] + '">' + NAMES[k] + '</span>' +
      '<span class="hd-flag">' + (FLAGS[r.f] || "") + '</span></div>' +
      '<div class="hd-star ' + starCls(r.s) + '">' + r.s.toFixed(2) +
      '<span>★</span></div><div class="hd-metrics">' +
      '<span>估值 <b>' + fmt(r.v) + '</b></span>' +
      '<span>价格 <b>' + fmt(r.p) + '</b></span>' +
      '<span>分位 <b>' + pct(r.q) + '</b></span>' +
      '<span>回撤 <b>' + pct(r.r) + '</b></span>' +
      '<span>PE <b>' + fmt(r.e) + '</b></span>' +
      '<span>PB <b>' + fmt(r.b) + '</b></span>' +
      (r.x != null ? '<span>点位 <b>' + Math.round(r.x) + '</b></span>' : '') +
      '</div></div>';
  }).join("");
  var box = document.getElementById("histDetail");
  box.innerHTML = '<div class="hd-date">' + d + ' 明细</div>' +
    '<div class="hd-grid">' + cards + '</div>';
  box.style.display = "block";
}
document.getElementById("histLegend").innerHTML = KEYS.map(function(k){
  return '<span class="hl"><i style="background:' + COLORS[k] + '"></i>' +
    NAMES[k] + '</span>';
}).join("");
document.getElementById("histRanges").addEventListener("click", function(ev){
  var b = ev.target;
  if (b.tagName !== "BUTTON") return;
  var days = parseInt(b.getAttribute("data-days"), 10);
  var all = this.querySelectorAll("button");
  for (var i = 0; i < all.length; i++) all[i].className = "";
  b.className = "on";
  drawChart(days);
});
document.getElementById("histBody").addEventListener("click", function(ev){
  var tr = ev.target.closest ? ev.target.closest("tr") : null;
  if (tr && tr.getAttribute("data-date")) showDetail(tr.getAttribute("data-date"));
});
document.getElementById("histPrev").onclick = function(){ page--; renderTable(); };
document.getElementById("histNext").onclick = function(){ page++; renderTable(); };
document.getElementById("histGo").onclick = function(){
  var v = document.getElementById("histDate").value;
  if (!v || dates.indexOf(v) < 0) { alert("这一天没有记录"); return; }
  var desc = dates.slice().reverse();
  page = Math.floor(desc.indexOf(v) / PAGE) + 1;
  renderTable();
  showDetail(v);
};
document.getElementById("histDate").addEventListener("keydown", function(ev){
  if (ev.key === "Enter") document.getElementById("histGo").click();
});
drawChart(90);
renderTable();
showDetail(dates[dates.length-1]);
})();
</script>""".replace("__HIST_JSON__", payload)


def html(rows, title_date=None, history=None):
    d = title_date or datetime.now(CN_TZ).date().isoformat()
    if history is None:
        history = load_star_history()
    history_section = _history_html(history)
    generated = datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M")
    data_dates = sorted({r["data_date"] for r in rows})
    data_date_text = data_dates[-1] if data_dates else d
    quote_times = sorted({r["quote_time"] for r in rows if r.get("quote_time")})
    quote_text = (" · 行情 %s" % quote_times[-1]) if quote_times else ""
    estimate_text = (" · 含盘中/收盘估算，晚间以官方数据为准"
                     if any(r.get("estimate") for r in rows) else "")

    trs = []
    trend_rows = []
    for r in rows:
        cls = "market" if not r["tradeable"] else ""
        price_star = "--" if r["price_star"] is None else "%.2f" % r["price_star"]
        dd = "--" if r["star_dd"] is None else "%.1f%%" % (r["star_dd"] * 100)
        chg = r.get("chg_pct")
        if chg is None:
            today_td = '<td class="chg">--</td>'
            trend_today = '<td class="trend-cell">--</td>'
        else:
            c = "up" if chg >= 0 else "down"
            today_td = '<td class="chg %s">%+.2f%%</td>' % (c, chg * 100)
            trend_today = ('<td class="trend-cell"><span class="chg %s">'
                           '%+.2f%%</span></td>' % (c, chg * 100))
        trs.append(
            '<tr class="%s"><td class="name">%s</td>%s'
            '<td class="star">%.2f</td><td>%.2f</td><td>%s</td>'
            '<td>%.1f%%</td><td>%s</td><td>%.2f</td><td>%.2f</td>'
            '<td class="zone">%s</td></tr>' % (
                cls, _esc(r["name"]), today_td, r["star"], r["val_star"],
                price_star, r["pall"] * 100, dd, r["pe"], r["pb"],
                _esc(r["zone"])))
        if r["tradeable"]:
            cells = []
            for key in ("7", "30", "365"):
                t = (r.get("trend") or {}).get(key)
                if not t:
                    cells.append('<td class="trend-cell">--</td>')
                    continue
                up = t["chg"] >= 0
                cells.append(
                    '<td class="trend-cell">%s<span class="chg %s">%+.1f%%</span></td>' % (
                        _spark(t["points"], up), "up" if up else "down",
                        t["chg"] * 100))
            trend_rows.append('<tr><td class="name">%s</td>%s%s</tr>' % (
                _esc(r["name"]), trend_today, "".join(cells)))

    distance = []
    for r in rows:
        distance.append('<li><b>%s</b>：%.2f 星。%s。</li>' % (
            _esc(r["name"]), r["star"], _esc(distance_text(r))))

    v3_items = []
    for r in rows:
        if not r["tradeable"] or not r.get("v3_action"):
            continue
        v3_items.append('<li><b>%s</b>：%s。%s</li>' % (
            _esc(r["name"]), _esc(r["v3_action"]),
            _esc(r.get("v3_detail") or "")))

    verdicts = []
    for r in rows:
        if not r["tradeable"]:
            continue
        action = r.get("v3_action") or r.get("v3_current_action") or "等待"
        if "清仓" in action or "卖出" in action:
            cls = "sell"
        elif "建仓" in action or "加第" in action:
            cls = "buy"
        elif ("持有" in action or "已卖" in action or "强趋势" in action
              or "满仓" in action or "三笔已完成" in action
              or "等分位" in action):
            cls = "watch"
        else:
            cls = "wait"
        sub = r.get("v3_detail") or ""
        if r.get("v3_current_action"):
            sub += " · 现行：%s" % r["v3_current_action"]
        if r.get("estimate"):
            sub += " · " + r["estimate_label"]
        verdicts.append(
            '<div class="verdict %s"><div class="v-name">%s</div>'
            '<div class="v-star">%.2f<span>★</span></div>'
            '<div class="v-text">%s</div><div class="v-sub">%s</div></div>' % (
                cls, _esc(r["name"]), r["star"], _esc(action), _esc(sub)))

    alloc_total, alloc_plan = _allocation_plan()
    alloc_rows = []
    for key, amount, weight in alloc_plan:
        r = next((x for x in rows if x.get("key") == key), None)
        if r is None:
            continue
        p = _sleeve_plan(r, amount)
        status = p["status"] + ("；" + p["today"] if p["today"] else "")
        invested = ("%s（%d/3）" % (_wan(p["invested"]), p["stage"])
                    if p["stage"] else "0")
        alloc_rows.append(
            '<tr><td class="name">%s</td><td>%s（%.0f%%）</td>'
            '<td>%s</td><td>%s</td><td>%s</td></tr>' % (
                _esc(_alloc_name(key)), _wan(amount), weight * 100,
                _esc(status), invested, _esc(p["next"])))
    alloc_card = ""
    if alloc_rows:
        alloc_desc = " + ".join("%s %s（%.0f%%）" % (
            _alloc_name(k), _wan(a), w * 100) for k, a, w in alloc_plan)
        alloc_card = (
            '<section class="card">'
            '<h2>本轮资金分配（各自分批）</h2>'
            '<p class="rule">本轮总资金 <b>%s</b>：%s。每个子账户按自己的 '
            '4.8★ 和 T1/T2/T3 信号独立建仓，互不等待、互不挪用。</p>'
            '<table class="alloc-table"><thead><tr><th>指数</th><th>子账户</th>'
            '<th>当前动作</th><th>累计买入</th><th>下一笔</th></tr></thead>'
            '<tbody>%s</tbody></table>'
            '<p class="rule">回测口径（2007-01 至 2026-10，20 万，含 1.5%% 活期）：'
            '期末 162.2 万，年化 11.17%%，最大回撤 -21.6%%。'
            '金额按设定资金计算，实盘以子账户当前市值为准。</p>'
            '</section>'
        ) % (_esc(_wan(alloc_total)), _esc(alloc_desc), "".join(alloc_rows))

    return """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>指数星级日报｜%(d)s</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--text:#1f2937;--muted:#6b7280;
--line:#e5e7eb;--accent:#2563eb;--green:#15803d;--amber:#b45309;--red:#b91c1c}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;
line-height:1.65}
main{max-width:980px;margin:0 auto;padding:28px 18px 48px}
h1{font-size:26px;margin:0 0 6px}
.meta{color:var(--muted);font-size:13px;margin:0 0 22px}
.verdicts{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));
gap:12px;margin:0 0 18px}
.verdict{background:var(--card);border:1px solid var(--line);border-left:6px solid var(--muted);
border-radius:12px;padding:14px 16px;box-shadow:0 1px 2px rgba(0,0,0,.03)}
.verdict.buy{border-left-color:var(--green)}
.verdict.watch{border-left-color:var(--amber)}
.verdict.wait{border-left-color:#9aa7b6}
.verdict.sell{border-left-color:var(--red)}
.v-name{font-size:13px;color:var(--muted)}
.v-star{font-size:32px;font-weight:700;line-height:1.15;color:var(--text)}
.v-star span{font-size:16px;color:var(--accent);margin-left:2px}
.v-text{font-size:15px;font-weight:700;margin-top:2px}
.verdict.buy .v-text{color:var(--green)}
.verdict.watch .v-text{color:var(--amber)}
.verdict.wait .v-text{color:#475467}
.verdict.sell .v-text{color:var(--red)}
.v-sub{font-size:12px;color:var(--muted);margin-top:6px;line-height:1.5}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:18px;margin:0 0 18px;box-shadow:0 1px 2px rgba(0,0,0,.03)}
table{width:100%%;border-collapse:collapse;font-size:14px}
th,td{padding:9px 7px;border-bottom:1px solid var(--line);text-align:right;
white-space:nowrap}
th:first-child,td:first-child{text-align:left}
th{color:var(--muted);font-weight:600;font-size:12px}
td.name{font-weight:600}
td.star{font-size:18px;font-weight:700;color:var(--accent)}
tr.market td{color:var(--muted)}
tr.market td.star{color:var(--muted)}
td.zone{text-align:left;white-space:normal}
.alloc-table td{white-space:normal}
.alloc-table td:first-child{white-space:nowrap}
td.chg{font-weight:700}
td.chg.up,.trend-table .chg.up{color:#c94a4a}
td.chg.down,.trend-table .chg.down{color:#15803d}
.trend-table{width:100%%;border-collapse:collapse;font-size:13px}
.trend-table th,.trend-table td{padding:8px 6px;border-bottom:1px solid var(--line);
text-align:right;white-space:nowrap}
.trend-table th:first-child,.trend-table td:first-child{text-align:left}
.trend-table th{color:var(--muted);font-weight:600;font-size:12px}
.trend-spark{width:112px;height:32px;display:block;margin:0 0 2px auto}
.trend-spark.up{color:#c94a4a}
.trend-spark.down{color:#15803d}
.trend-table .chg{font-size:12px;font-weight:700}
h2{font-size:16px;margin:0 0 10px}
ul{margin:0;padding-left:20px}
li{margin:6px 0}
.rule{color:var(--muted);font-size:13px}
.footer{color:var(--muted);font-size:12px;margin-top:22px}
@media(max-width:640px){
main{padding:18px 10px 36px}
.card{padding:12px;overflow-x:auto}
table{font-size:12px}
th,td{padding:7px 5px}
.trend-table{font-size:11px}
.trend-spark{width:82px;height:26px}
h1{font-size:21px}
}
</style>
</head>
<body>
<main>
<h1>指数星级日报</h1>
<p class="meta">估值数据截至 %(data_date)s%(quote_text)s%(estimate_text)s · 生成于 %(generated)s · 中证800 / 中证500 / 沪深300 / 全市场</p>
<section class="verdicts">%(verdicts)s</section>
%(alloc_card)s
<section class="card">
<table>
<thead><tr><th>指数</th><th>今日</th><th>修正星级</th><th>估值星级</th><th>价格星级</th>
<th>全历史分位</th><th>3年回撤</th><th>PE</th><th>PB</th><th>状态</th></tr></thead>
<tbody>%(trs)s</tbody>
</table>
</section>
<section class="card">
<h2>走势</h2>
<table class="trend-table">
<thead><tr><th>指数</th><th>今日</th><th>近1周</th><th>近1月</th><th>近1年</th></tr></thead>
<tbody>%(trends)s</tbody>
</table>
</section>
%(history_section)s
<section class="card">
<h2>距离 4.8 星买点</h2>
<ul>%(distance)s</ul>
</section>
<section class="card">
<h2>v3.2 规则（已启用）</h2>
<ul>%(v3_items)s</ul>
<p class="rule">v3.2 与现行规则的差别：首笔按 1 年回撤加深；T2/T3 增加趋势确认，
浅 V 底在 T1 后 60 天站上年线 8%% 时补第二笔；1 年回撤 ≤-30%% 且现价低于年线 15%% 时
暂停按价格加仓；保险改为 40%% 浮盈 + 年线向上过滤；强趋势中 63%% 不卖剩余 2/3，
改用高点回撤 12%%。网页和推送现在都按 v3.2 给出动作，下面仍保留现行规则对照。</p>
</section>
<section class="card rule">
<h2>怎么读</h2>
<p>修正星级 = min(估值星级, 价格星级)，只看该指数自己的历史，<b>不是全市场星级</b>。
4.8 星才是第一档买点：估值分位 ≤20%%，且价格距 3 年高点 ≤-26%%。
中证800 的估值星级 = 75%% 沪深300 + 25%% 中证500 的估值星级加权；
全历史分位仍只用于卖出。</p>
<p>买入：4.8★ 首笔按 1 年回撤加深（≤-60%% 投 2/3，≤-40%% 投 1/2，否则 1/3），
剩余现金分给 T2/T3。<br>
加仓：T2 较 T1 跌 10%%，或 T1 一年回撤≤-40%% 且 120 日线转上；
若价格始终不到，T1 后 60 天且年线向上、现价高于年线 8%% 时补第二笔；
T3 较 T1 跌 20%%，或 250 日线转上。<br>
崩盘过滤：1 年回撤 ≤-30%% 且现价低于年线 15%% 时，暂停按价格加仓；
回到年线 85%% 以上或趋势确认后恢复。<br>
卖出：全历史分位 ≥60%% 卖 1/3；≥63%% 卖剩余 2/3；若点位高于年线 30%%，剩余改 12%% 移动止盈。<br>
保险：浮盈最高 ≥40%% 后，从本轮高点回撤 10%% 且年线向上，清仓；强趋势中不触发。<br>
空仓资金：放货币基金；回测按年化 1.5%% 计息，不改变任何买卖信号。</p>
<p>全市场没有统一价格指数，所以只算估值星级，作参考。</p>
</section>
<p class="footer">数据：乐咕 PE/PB + 腾讯/东方财富行情；中证800 的 PE/PB 为 75%% 沪深300 + 25%% 中证500 合成，
价格优先用中证800 实际点位。盘中/收盘估算按价格变动缩放 PE/PB，晚间官方数据更新后以官方值为准。
仅为估值记录，不构成投资建议。</p>
</main>
</body>
</html>
""" % {
        "d": _esc(d),
        "data_date": _esc(data_date_text),
        "quote_text": _esc(quote_text),
        "estimate_text": _esc(estimate_text),
        "generated": _esc(generated),
        "verdicts": "".join(verdicts),
        "alloc_card": alloc_card,
        "trs": "".join(trs),
        "trends": "".join(trend_rows),
        "history_section": history_section,
        "distance": "".join(distance),
        "v3_items": "".join(v3_items),
    }


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}  # GitHub Actions 里没有 config.json，推送配置走环境变量


def _alert_signals(rows):
    """当天 v3 实际触发的买/卖动作。信号消失后自动从状态里清掉。"""
    out = {}
    for r in rows:
        key = r.get("key")
        code = r.get("v3_today_code")
        if key == "market" or not code:
            continue
        label = r.get("v3_today_action") or code
        if code == "T1":
            out[key + ":buy48"] = label
        elif code == "T2":
            out[key + ":buyT2"] = label
        elif code == "T3":
            out[key + ":buyT3"] = label
        elif code == "S1":
            out[key + ":sell60"] = label
        elif code == "S2":
            out[key + ":sell63"] = label
        elif code == "保险":
            out[key + ":insurance"] = label
        elif code == "TR":
            out[key + ":trail"] = label
        elif code == "HOLD":
            out[key + ":hold"] = label
    return out


def _load_alert_state():
    try:
        with open(ALERT_STATE_PATH, encoding="utf-8") as fh:
            obj = json.load(fh)
        return obj if isinstance(obj, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_alert_state(signals):
    parent = os.path.dirname(ALERT_STATE_PATH)
    os.makedirs(parent, exist_ok=True)
    with open(ALERT_STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump({"date": datetime.now(CN_TZ).date().isoformat(),
                   "signals": sorted(signals)}, fh, ensure_ascii=False, indent=1)


def _push_configured(config):
    push_cfg = config.get("push") or {}
    return bool(push_cfg.get("enabled")
                or os.environ.get("SERVERCHAN_KEY")
                or os.environ.get("PUSHPLUS_TOKEN")
                or os.environ.get("SMTP_HOST")
                or (os.environ.get("GITHUB_TOKEN")
                    and os.environ.get("GITHUB_REPOSITORY")))


def _fmt_px(x):
    return "--" if x is None else "%.0f" % x


def _brief_items(rows):
    items = []
    for r in rows:
        if r.get("key") == "market":
            continue
        action = r.get("v3_short") or r.get("v3_action") or "等待"
        items.append((r, action))
    return items


def _fresh_desc(fresh):
    names = {"zz800": "中证800", "zz500": "中证500", "hs300": "沪深300"}
    return "；".join("%s %s" % (names.get(k.split(":")[0], k), v)
                    for k, v in sorted(fresh.items()))


def push_brief(rows, fresh=None):
    """推送只保留关键信息，详细走势让用户点链接看网页。"""
    today = datetime.now(CN_TZ).strftime("%Y-%m-%d")
    lines = ["**指数星级｜%s**" % today]
    if fresh:
        lines.append("新增信号：%s" % _fresh_desc(fresh))
    lines.append("**资金分配**：" + _allocation_text())
    lines.append("")
    alloc = {k: amt for k, amt, _ in _allocation_plan()[1]}
    for r, action in _brief_items(rows):
        sleeve = ""
        if r.get("key") in alloc:
            sleeve = "｜" + _sleeve_brief(r, alloc[r["key"]])
        pall = r.get("pall") or 0.0
        vtxt = "估值分位 %s（%s）" % (
            _fmt_pct(pall), "卖点≥60%" if pall >= 0.60 else "买点≤20%")
        price = "点位 %s" % _fmt_px(r.get("px_display"))
        if r.get("v3_t1_px") is not None:
            price += "（本轮T1 %s）" % _fmt_px(
                v3_rule._display_px(r, r["v3_t1_px"]))
        elif r.get("price_t1"):
            price += "（T1 %s）" % _fmt_px(r["price_t1"])
        est = "｜%s" % r["estimate_label"] if r.get("estimate_label") else ""
        current = r.get("v3_current_action")
        cand = ("｜现行：%s" % current
                if current and current != action else "")
        lines.append("- **%s %.2f★**｜%s%s｜今日 %s｜%s｜%s%s%s" % (
            r["name"], r["star"], action, sleeve,
            _fmt_pct(r.get("chg_pct"), 2, sign=True),
            vtxt, price, est, cand))
    if PAGE_URL:
        lines += ["", "完整走势图：[打开网页](%s)" % PAGE_URL]
    return "\n".join(lines)


def push_brief_html(rows, fresh=None):
    import html as htmlmod
    today = datetime.now(CN_TZ).strftime("%Y-%m-%d")
    parts = ["<h3>指数星级｜%s</h3>" % today]
    if fresh:
        parts.append("<p><b>新增信号</b>：%s</p>" % htmlmod.escape(_fresh_desc(fresh)))
    parts.append("<p><b>资金分配</b>：%s</p>" % htmlmod.escape(_allocation_text()))
    parts.append("<ul>")
    alloc = {k: amt for k, amt, _ in _allocation_plan()[1]}
    for r, action in _brief_items(rows):
        sleeve = ""
        if r.get("key") in alloc:
            sleeve = "｜" + _sleeve_brief(r, alloc[r["key"]])
        pall = r.get("pall") or 0.0
        vtxt = "估值分位 %s（%s）" % (
            _fmt_pct(pall), "卖点≥60%" if pall >= 0.60 else "买点≤20%")
        price = "点位 %s" % _fmt_px(r.get("px_display"))
        if r.get("v3_t1_px") is not None:
            price += "（本轮T1 %s）" % _fmt_px(
                v3_rule._display_px(r, r["v3_t1_px"]))
        elif r.get("price_t1"):
            price += "（T1 %s）" % _fmt_px(r["price_t1"])
        est = "｜%s" % r["estimate_label"] if r.get("estimate_label") else ""
        current = r.get("v3_current_action")
        cand = ("｜现行：%s" % current
                if current and current != action else "")
        parts.append("<li><b>%s %.2f★</b>｜%s%s｜今日 %s｜%s｜%s%s%s</li>" % (
            htmlmod.escape(r["name"]), r["star"], action,
            htmlmod.escape(sleeve),
            _fmt_pct(r.get("chg_pct"), 2, sign=True), vtxt, price, est,
            htmlmod.escape(cand)))
    parts.append("</ul>")
    if PAGE_URL:
        parts.append('<p><a href="%s">打开网页看完整走势</a></p>' % PAGE_URL)
    return "".join(parts)


def push(rows, signal_only=False, daily=False):
    import datasource as ds
    config = load_config()
    if not _push_configured(config):
        print("推送未配置：GitHub Actions 里加 SERVERCHAN_KEY（或 SMTP_*）Secret；"
              "本地则在 config.json 填 push 配置。")
        return 0 if (signal_only or daily) else 1

    state = _load_alert_state()
    previous = set(state.get("signals") or [])
    current = _alert_signals(rows)
    fresh = {k: v for k, v in current.items() if k not in previous}
    now = datetime.now(CN_TZ)

    if signal_only and not fresh and not daily:
        _save_alert_state(current)
        print("今天没有新的买卖信号，跳过推送。")
        return 0

    if fresh:
        desc = _fresh_desc(fresh)
        title = "指数信号｜%s %s" % (now.strftime("%m-%d"), desc)
    else:
        row800 = next((r for r in rows if r["key"] == "zz800"), None)
        title = "指数日报｜%s %s" % (
            now.strftime("%m-%d"),
            ("中证800 %.2f★" % row800["star"]) if row800 else "星级更新")

    content = push_brief(rows, fresh)
    ok, msg = ds.send_push(config, title, content,
                           html_body=push_brief_html(rows, fresh))
    if ok:
        _save_alert_state(current)
        print("推送成功：%s" % title)
        return 0
    # 发送失败：保留仍未消失的旧信号，下次继续尝试，不把新信号记成已提醒
    _save_alert_state(previous & set(current))
    print("推送失败：%s" % msg, file=sys.stderr)
    return 1


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="中证800/中证500/沪深300/全市场 指数星级日报")
    ap.add_argument("--index", default="all",
                    choices=["all", "zz800", "zz500", "hs300", "market"])
    ap.add_argument("--weight-hs300", type=float, default=0.75,
                    help="合成中证800时沪深300的市值权重，默认 0.75")
    ap.add_argument("--refresh", action="store_true",
                    help="先联网刷新指数估值历史（失败则继续用缓存）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--rebuild-history", action="store_true",
                    help="丢弃已有星级历史，用官方估值全量回算（规则改动后用）")
    ap.add_argument("--push", action="store_true",
                    help="通过 Server酱/PushPlus/邮件推送")
    ap.add_argument("--signal-only", action="store_true",
                    help="只在出现新的买/卖档位时推送（配合 --push）")
    ap.add_argument("--daily", action="store_true",
                    help="每天收盘后推一次日报（有新信号则盘中立即推）")
    ap.add_argument("--md", default=None, help="把 Markdown 写到指定文件")
    ap.add_argument("--html", default=None,
                    help="生成静态 HTML（可直接放 GitHub Pages）")
    args = ap.parse_args(argv)

    keys = None if args.index == "all" else [args.index]
    rows = collect(keys, args.weight_hs300, refresh=args.refresh)

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        print(markdown(rows))

    if args.md:
        parent = os.path.dirname(os.path.abspath(args.md))
        os.makedirs(parent, exist_ok=True)
        with open(args.md, "w", encoding="utf-8") as fh:
            fh.write(markdown(rows))
        print("\nMarkdown 已写入：%s" % args.md)

    if args.html:
        history = update_star_history(
            rows, weight_hs300=args.weight_hs300,
            force=args.rebuild_history)
        parent = os.path.dirname(os.path.abspath(args.html))
        os.makedirs(parent, exist_ok=True)
        with open(args.html, "w", encoding="utf-8") as fh:
            fh.write(html(rows, history=history))
        print("HTML 已写入：%s" % args.html)
        print("星级历史：%d 条 → %s" % (len(history), HISTORY_PATH),
              file=sys.stderr)

    if args.push:
        return push(rows, signal_only=args.signal_only, daily=args.daily)
    return 0


if __name__ == "__main__":
    sys.exit(main())
