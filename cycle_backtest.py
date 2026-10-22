#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按“熊底 -> 牛顶 -> 熊底”分段回测估值止盈规则，并支持中证800。

数据：
  data/cache/zz500_history.json   中证500真实 PE/PB（乐咕口径）
  data/cache/hs300_history.json   沪深300真实 PE/PB（乐咕口径）
  data/cache/market_history.json  全市场平均 PE/PB
  data/cache/nav_161017.cache     富国中证500增强(LOF)A 累计净值

中证800没有现成接口，用“75% 沪深300 + 25% 中证500”按市值权重合成
（PE/PB 用调和加权，价格用日收益加权）。它是近似值，不是官方口径。

用法：
  # 看中证800当前估值和指数星级
  python cycle_backtest.py --latest --index zz800

  # 看中证500 + 161017 净值
  python cycle_backtest.py --latest --index zz500 --code 161017

  # 看全市场估值
  python cycle_backtest.py --latest --index market

  # 用合成中证800价格回测第2轮
  python cycle_backtest.py --start 2024-02-05 --end 2026-10-21 \
      --index zz800 --window all --t1 0.60 --t2 0.68 \
      --trail 0.12 --price-stop 0.20

  # 先联网刷新估值历史
  python cycle_backtest.py --refresh --index zz800

规则（2026-10 定稿，指数自己的口径，不是全市场星级）：
  修正星级 = min(估值星级, 价格星级)
    估值星级：全历史 PE/PB 综合分位 10/20/40/60/80/100%
              -> 5.0/4.8/4.0/3.0/2.0/1.0
    价格星级：相对过去 3 年最高点的回撤 <=5/10/20/30%
              -> 3.8/4.2/4.5/5.0
  买入：修正星级 >=4.8 买 1/3；价格再跌 10% 买 1/3；再跌 20% 买最后 1/3
  止盈：全历史分位 >=60% 卖 1/3；>=63% 卖剩余 2/3
  保险：浮盈最高 >=45% 后，从本轮高点回撤 10% 清仓（只在 S1 前生效）
"""
import argparse
import bisect
import json
import os
import sys
from datetime import date

BASE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE, "data", "cache")

INDEXES = {
    "zz500": {
        "name": "中证500",
        "url": "http://harryblog.site/api/zz500_data",
        "path": os.path.join(CACHE_DIR, "zz500_history.json"),
    },
    "hs300": {
        "name": "沪深300",
        "url": "http://harryblog.site/api/hs300_data",
        "path": os.path.join(CACHE_DIR, "hs300_history.json"),
    },
    "market": {
        "name": "全市场",
        "url": "http://harryblog.site/api/average_pe_data",
        "path": os.path.join(CACHE_DIR, "market_history.json"),
    },
}

INDEX_NAMES = {
    "zz500": "中证500",
    "hs300": "沪深300",
    "zz800": "中证800(合成)",
    "market": "全市场",
}


def refresh_history(key="zz500"):
    """刷新一个指数（或合成指数所需的两个指数）的估值历史。"""
    import urllib.request

    if key == "zz800":
        keys = ["hs300", "zz500"]
    elif key in INDEXES:
        keys = [key]
    else:
        raise ValueError("不支持的指数：%s" % key)

    total = 0
    for k in keys:
        cfg = INDEXES[k]
        req = urllib.request.Request(cfg["url"], headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            obj = json.loads(resp.read().decode("utf-8"))
        if not isinstance(obj, dict) or not obj.get("data"):
            raise ValueError("%s 接口返回格式不对" % cfg["name"])
        with open(cfg["path"], "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False)
        total += len(obj["data"])
    return total


def _load_cached(key):
    cfg = INDEXES[key]
    with open(cfg["path"], encoding="utf-8") as fh:
        obj = json.load(fh)
    rows = obj["data"] if isinstance(obj, dict) else obj
    rows = [r for r in rows
            if r.get("pe_ttm") is not None and r.get("pb") is not None]
    rows.sort(key=lambda r: r["date_k"])
    return rows


def _synth_zz800_rows(weight_hs300=0.75):
    """按市值权重合成中证800的 PE/PB。

    中证800 = 沪深300 + 中证500。给定市值权重 w（沪深300 占 800 的比例），
    盈利 E = MC/PE，净资产 BV = MC/PB：
        PE800 = 1 / (w/PE300 + (1-w)/PE500)
        PB800 = 1 / (w/PB300 + (1-w)/PB500)
    默认 w=0.75；可在命令行用 --weight-hs300 调整。
    """
    hs = _load_cached("hs300")
    zz = {r["date_k"]: r for r in _load_cached("zz500")}
    w = weight_hs300
    out = []
    for r in hs:
        z = zz.get(r["date_k"])
        if z is None:
            continue
        pe = 1.0 / (w / float(r["pe_ttm"]) + (1.0 - w) / float(z["pe_ttm"]))
        pb = 1.0 / (w / float(r["pb"]) + (1.0 - w) / float(z["pb"]))
        out.append({"date_k": r["date_k"], "pe_ttm": pe, "pb": pb})
    return out


def _load_raw_rows(key="zz500", weight_hs300=0.75):
    if key == "zz800":
        return _synth_zz800_rows(weight_hs300)
    if key not in INDEXES:
        raise ValueError("不支持的指数：%s" % key)
    return _load_cached(key)


def _window_start(dates_iso, d, years):
    if years is None:
        return 0
    try:
        cutoff = d.replace(year=d.year - years).isoformat()
    except ValueError:  # 2 月 29 日
        cutoff = d.replace(year=d.year - years, day=28).isoformat()
    return bisect.bisect_left(dates_iso, cutoff)


def load_valuation(window="10", key="zz500", weight_hs300=0.75):
    """按指定窗口就地计算 PE/PB 分位。

    window=10  : 每个交易日回看 10 年（和乐咕 q10y 口径一致）
    window=5   : 回看 5 年
    window=all : 从数据起点到当前（中证500/沪深300 约 2007/2005 年起）

    注意：分位是“相对当时可见的历史”算的，所以估值带整体上移时，
    同样 60% 分位对应的绝对 PE/PB 也会跟着上移——这不是固定 PE 阈值。
    """
    rows = _load_raw_rows(key, weight_hs300)

    out = []
    dates_iso = [r["date_k"] for r in rows]
    pe_all = [float(r["pe_ttm"]) for r in rows]
    pb_all = [float(r["pb"]) for r in rows]
    years = None if window == "all" else int(window)
    for i, r in enumerate(rows):
        d = date.fromisoformat(r["date_k"])
        start = _window_start(dates_iso, d, years)
        sub_pe = sorted(pe_all[start:i + 1])
        sub_pb = sorted(pb_all[start:i + 1])
        pe_pct = bisect.bisect_right(sub_pe, pe_all[i]) / len(sub_pe)
        pb_pct = bisect.bisect_right(sub_pb, pb_all[i]) / len(sub_pb)
        out.append((d, 0.6 * pe_pct + 0.4 * pb_pct))
    return out


def load_nav(code):
    import datasource as ds

    obj = ds.fetch_nav_history(code, cache_dir=CACHE_DIR, offline=True)
    nav = {}
    for r in obj.get("rows") or []:
        x = r.get("acc") if r.get("acc") is not None else r.get("nav")
        if x is None:
            continue
        nav[date.fromisoformat(r["date"])] = float(x)
    return nav, sorted(nav)


def load_index_nav(key, weight_hs300=0.75):
    """用指数点位构造一条“净值”曲线，供没有基金代码时回测。

    - zz500 / hs300：直接用接口里的 close。
    - zz800：用 75% 沪深300 + 25% 中证500 的日收益合成。
    - market：接口没有点位，不能回测。
    """
    if key == "market":
        raise ValueError("全市场估值没有点位数据，回测请用 --code 指定基金")
    if key == "zz800":
        hs = _load_cached("hs300")
        zz = {r["date_k"]: float(r["close"]) for r in _load_cached("zz500")
              if r.get("close")}
        w = weight_hs300
        nav = {}
        level = 1000.0
        prev = None
        for r in hs:
            d = r["date_k"]
            if d not in zz or not r.get("close"):
                continue
            px_hs = float(r["close"])
            px_zz = zz[d]
            if prev is not None:
                level *= 1.0 + w * (px_hs / prev[0] - 1.0) \
                    + (1.0 - w) * (px_zz / prev[1] - 1.0)
            nav[date.fromisoformat(d)] = level
            prev = (px_hs, px_zz)
        return nav, sorted(nav)

    rows = _load_cached(key)
    nav = {date.fromisoformat(r["date_k"]): float(r["close"])
           for r in rows if r.get("close")}
    return nav, sorted(nav)


def nav_on(dates, nav, d):
    i = bisect.bisect_right(dates, d) - 1
    return nav[dates[i]] if i >= 0 else None


def valuation_star(p):
    """全历史 PE/PB 综合分位 -> 估值星级（指数自己的口径）。

    锚点：
      10% -> 5.0; 20% -> 4.8; 40% -> 4.0; 60% -> 3.0; 80% -> 2.0; 100% -> 1.0
    """
    if p <= 0.10:
        return 5.0
    if p <= 0.20:
        return 5.0 - (p - 0.10) / 0.10 * 0.2
    if p <= 0.40:
        return 4.8 - (p - 0.20) / 0.20 * 0.8
    if p <= 0.60:
        return 4.0 - (p - 0.40) / 0.20 * 1.0
    if p <= 0.80:
        return 3.0 - (p - 0.60) / 0.20 * 1.0
    return max(1.0, 2.0 - (p - 0.80) / 0.20 * 1.0)


def price_star(drawdown):
    """相对过去 3 年最高点的回撤 -> 价格星级。

    锚点：
      回撤 <=5% -> 3.8; 10% -> 4.2; 20% -> 4.5; >=30% -> 5.0
    这是为了防止“估值分位看起来便宜、但价格还在 3 年高位”的顶部买入。
    """
    if drawdown >= -0.05:
        return 3.8
    if drawdown >= -0.10:
        return 3.8 + (-drawdown - 0.05) / 0.05 * 0.4
    if drawdown >= -0.20:
        return 4.2 + (-drawdown - 0.10) / 0.10 * 0.3
    if drawdown >= -0.30:
        return 4.5 + (-drawdown - 0.20) / 0.10 * 0.5
    return 5.0


def index_star(p, drawdown=None):
    """指数自己的修正星级 = min(估值星级, 价格星级)。

    drawdown 为相对过去 3 年最高点的回撤；不传时只返回估值星级。
    注意：这不是螺丝钉的全市场星级，也不是同一口径。
    """
    val = valuation_star(p)
    if drawdown is None:
        return val
    return min(val, price_star(drawdown))


def _three_years_before(d):
    try:
        return d.replace(year=d.year - 3)
    except ValueError:  # 2 月 29 日
        return d.replace(year=d.year - 3, day=28)


def three_year_price_info(key, weight_hs300=0.75, asof=None):
    """返回 (当前点位, 3年高点, 回撤, 截止日)；全市场没有点位，返回 None。"""
    if key == "market":
        return None
    nav, dates = load_index_nav(key, weight_hs300)
    if not dates:
        return None
    if asof is None:
        asof = dates[-1]
    cutoff = _three_years_before(asof)
    recent = [nav[x] for x in dates if cutoff <= x <= asof]
    if not recent:
        return None
    high = max(recent)
    px = nav_on(dates, nav, asof)
    if px is None or high <= 0:
        return None
    return px, high, px / high - 1.0, asof


def market_valuation_snapshot():
    """全市场估值星级（只看 PE/PB 分位，没有统一价格指数）。"""
    val = load_valuation("all", "market")
    d, p = val[-1]
    return {"date": d, "pall": p, "star": valuation_star(p)}


def latest_snapshot(code=None, key="zz500", weight_hs300=0.75):
    """最新估值快照：10 年/全历史分位 + 指数星级 + 可选基金净值位置。"""
    rows = _load_raw_rows(key, weight_hs300)
    i = len(rows) - 1
    d = date.fromisoformat(rows[i]["date_k"])
    dates_iso = [r["date_k"] for r in rows]
    pe_now = float(rows[i]["pe_ttm"])
    pb_now = float(rows[i]["pb"])

    def pct(years):
        start = _window_start(dates_iso, d, years)
        sub_pe = sorted(float(r["pe_ttm"]) for r in rows[start:i + 1])
        sub_pb = sorted(float(r["pb"]) for r in rows[start:i + 1])
        return (0.6 * bisect.bisect_right(sub_pe, pe_now) / len(sub_pe)
                + 0.4 * bisect.bisect_right(sub_pb, pb_now) / len(sub_pb))

    p10, pall = pct(10), pct(None)

    # 指数自己的 3 年价格回撤（不是基金净值；同一指数的所有基金共用）
    price_info = three_year_price_info(key, weight_hs300, asof=d)
    star_dd = price_info[2] if price_info else None
    star_high = price_info[1] if price_info else None
    star_px = price_info[0] if price_info else None
    val_star = valuation_star(pall)
    p_star = price_star(star_dd) if star_dd is not None else None
    star = min(val_star, p_star) if p_star is not None else val_star
    if key == "zz800":
        # 先分别取成分指数的估值星级，再按市值权重合成；不能先混合
        # PE/PB 水平再取分位（分位不是线性的）。卖出仍用上面的 pall。
        va = latest_snapshot(key="hs300", weight_hs300=weight_hs300)["val_star"]
        vb = latest_snapshot(key="zz500", weight_hs300=weight_hs300)["val_star"]
        val_star = weight_hs300 * va + (1.0 - weight_hs300) * vb
        star = min(val_star, p_star) if p_star is not None else val_star

    nav_info = None
    if code:
        nav, nav_dates = load_nav(code)
        nav_info = ("基金 %s" % code, nav, nav_dates)
    elif key in ("zz500", "hs300", "zz800"):
        nav, nav_dates = load_index_nav(key, weight_hs300)
        nav_info = ("%s指数" % INDEX_NAMES[key], nav, nav_dates)

    dd = high = last_nav = None
    nav_date = None
    if nav_info:
        label, nav, nav_dates = nav_info
        nav_date = nav_dates[-1]
        last_nav = nav[nav_date]
        year_ago = nav_date.replace(year=nav_date.year - 1)
        recent = [nav[x] for x in nav_dates if x >= year_ago]
        high = max(recent) if recent else last_nav
        dd = last_nav / high - 1

    # 止盈看全历史分位；买入看修正星级。两者不冲突：分位到 60% 时
    # 估值星级只有 3.0，不可能同时是 4.8 星买点。
    if pall >= 0.63:
        zone = "第二档止盈区（≥63%）"
    elif pall >= 0.60:
        zone = "第一档止盈区（≥60%）"
    elif star >= 4.8:
        zone = "建仓区（≥4.8星）"
    elif star >= 4.5:
        zone = "观察区（接近4.8星）"
    else:
        zone = "等待区（新资金不买）"

    if key == "market":
        market = {"date": d, "pall": pall, "star": val_star}
    else:
        market = market_valuation_snapshot()

    return {"name": INDEX_NAMES.get(key, key), "date": d, "pe": pe_now,
            "pb": pb_now, "p10": p10, "pall": pall,
            "star": star, "val_star": val_star, "price_star": p_star,
            "star_dd": star_dd, "star_high": star_high, "star_px": star_px,
            "market_pall": market["pall"], "market_star": market["star"],
            "market_date": market["date"],
            "zone": zone, "nav_label": nav_info[0] if nav_info else None,
            "nav_date": nav_date, "nav": last_nav, "high": high, "dd": dd}


def simulate(val, nav, nav_dates, start, end, t1, t2, trail, price_stop,
             buy_low=0.25, buy_mid=0.20, buy_deep=0.18,
             weights=(0.10, 0.20, 0.70)):
    """返回 (final, buy_hold, max_dd, buy_hold_dd, buys, sells, peak_date, peak_eq)。"""
    cash = 1.0
    shares = 0.0
    plan = 1.0                      # 本轮可投入的总资金
    flags = [False, False, False]
    zone = "normal"                 # normal / high / dormant
    t1_done = t2_done = False
    peak = None                     # T1 后的净值高点（算回撤止盈）
    high = None                     # 本轮持仓以来的净值高点（算价格保险）
    dormant_days = 0
    buys, sells = [], []
    eq_peak = 0.0
    max_dd = 0.0
    navs = []
    first = last = None
    eq_peak_date = None

    for d, v in val:
        if d < start:
            continue
        px = nav_on(nav_dates, nav, d)
        if px is None:
            continue
        if first is None:
            first = px
        if d > end:
            break
        last = px
        navs.append(px)

        if shares > 0:
            high = px if high is None else max(high, px)

        # 清仓后的再入场
        if zone == "dormant":
            dormant_days = dormant_days + 1 if v < 0.50 else 0
            if dormant_days >= 5:
                zone = "normal"
                t1_done = t2_done = False
                peak = high = None
                flags = [False, False, False]
                plan = cash + shares * px
                dormant_days = 0

        # 买入
        if zone == "normal":
            for k, (thr, w) in enumerate(
                    ((buy_low, weights[0]), (buy_mid, weights[1]),
                     (buy_deep, weights[2]))):
                if flags[k] or v > thr:
                    continue
                amt = min(plan * w, cash)
                if amt <= 1e-12:
                    continue
                shares += amt / px
                cash -= amt
                flags[k] = True
                high = px if high is None else max(high, px)
                buys.append((d, k + 1, px, v, amt / plan))

        # T1 前的价格保险：从本轮最高点回撤 price_stop 清仓
        if shares > 0 and price_stop and high and px / high - 1 <= -price_stop:
            cash += shares * px
            sells.append((d, "价停%.0f%%" % (price_stop * 100), px, v, shares))
            shares = 0.0
            zone, dormant_days = "dormant", 0
            t1_done = t2_done = False
            peak = high = None
        elif shares > 0:
            base = None
            if not t1_done and v >= t1:
                base = shares
                s = base / 3.0
                cash += s * px
                shares -= s
                t1_done = True
                zone = "high"
                peak = px
                sells.append((d, "T1", px, v, s))
                if not t2_done and v >= t2:
                    s2 = base / 3.0
                    cash += s2 * px
                    shares -= s2
                    t2_done = True
                    zone = "extreme"
                    sells.append((d, "T2", px, v, s2))
            elif t1_done:
                peak = px if peak is None else max(peak, px)
                if px / peak - 1 <= -trail:
                    cash += shares * px
                    sells.append((d, "T3", px, v, shares))
                    shares = 0.0
                    zone, dormant_days = "dormant", 0
                elif not t2_done and v >= t2:
                    # 按 T1 时总份额的 1/3 卖，保证三档大致等额
                    # 这里用当前份额的 1/2 近似剩余两份中的一份
                    s2 = shares / 2.0 if t2_done is False else 0.0
                    cash += s2 * px
                    shares -= s2
                    t2_done = True
                    zone = "extreme"
                    sells.append((d, "T2", px, v, s2))

        eq = cash + shares * px
        if eq > eq_peak:
            eq_peak, eq_peak_date = eq, d
        if eq_peak:
            max_dd = min(max_dd, eq / eq_peak - 1)

    final = cash + shares * last if last else cash
    buy_hold = last / first if first and last else 1.0

    # 买入持有的最大回撤
    bp = 0.0
    bdd = 0.0
    for x in navs:
        bp = max(bp, x)
        if bp:
            bdd = min(bdd, x / bp - 1)

    return final, buy_hold, max_dd, bdd, buys, sells, eq_peak_date, eq_peak


def print_one(title, start, end, res):
    final, bh, dd, bdd, buys, sells, peak_date, peak_eq = res
    print("=" * 78)
    print(title)
    print("周期 %s ~ %s" % (start, end))
    if buys:
        print("买入：")
        for d, k, px, v, w in buys:
            print("  %s  第%d档 %3.0f%%  分位 %5.1f%%  净值 %.4f"
                  % (d, k, w * 100, v * 100, px))
    else:
        print("买入：无（本轮没有跌进买入区）")
    if sells:
        print("卖出：")
        for d, tag, px, v, sh in sells:
            print("  %s  %-8s  分位 %5.1f%%  净值 %.4f"
                  % (d, tag, v * 100, px))
    else:
        print("卖出：无")
    print("最终 %.4fx  买入持有 %.4fx  超额 %+.1f%%"
          % (final, bh, (final / bh - 1) * 100))
    print("策略最大回撤 %.1f%%  买入持有最大回撤 %.1f%%"
          % (dd * 100, bdd * 100))
    if peak_date:
        print("策略权益最高点：%s  %.4fx（起点=1）" % (peak_date, peak_eq))


def grid(val, nav, nav_dates, start, end):
    print("参数网格：最终倍数 / 最大回撤")
    print("T1/T2/尾随/价停        最终    回撤")
    for t1, t2, trail, ps in [
            (0.60, 0.70, 0.08, None),
            (0.60, 0.70, 0.08, 0.20),
            (0.70, 0.80, 0.10, None),
            (0.70, 0.80, 0.10, 0.20),
            (0.80, 0.90, 0.10, None),
            (0.80, 0.90, 0.10, 0.20),
            (0.80, 0.90, 0.12, None),
            (0.80, 0.90, 0.12, 0.20),
            (0.85, 0.90, 0.10, None),
            (0.85, 0.90, 0.10, 0.20),
            (0.90, 0.95, 0.10, 0.20)]:
        r = simulate(val, nav, nav_dates, start, end, t1, t2, trail, ps)
        name = "%2.0f/%2.0f/%.0f%%%s" % (
            t1 * 100, t2 * 100, trail * 100,
            ("/%.0f%%" % (ps * 100)) if ps else "/--")
        print("%-20s %.4f  %6.1f%%" % (name, r[0], r[2] * 100))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="按完整牛熊周期回测估值止盈规则（支持中证500/沪深300/中证800/全市场）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", help="周期起点，如 2024-02-05")
    ap.add_argument("--end", help="周期终点，如 2026-10-21")
    ap.add_argument("--latest", action="store_true",
                    help="只看最新估值快照，不需要 --start/--end")
    ap.add_argument("--index", choices=["zz500", "hs300", "zz800", "market"],
                    default="zz500", help="指数，默认 zz500")
    ap.add_argument("--code", default=None, help="基金代码；不填则用指数点位")
    ap.add_argument("--weight-hs300", type=float, default=0.75,
                    help="合成中证800时沪深300的市值权重，默认 0.75")
    ap.add_argument("--window", choices=["5", "10", "all"], default="10",
                    help="分位回看窗口：5/10/全部，默认 10")
    ap.add_argument("--t1", type=float, default=0.60, help="第一档止盈分位，默认 0.60")
    ap.add_argument("--t2", type=float, default=0.70, help="第二档止盈分位，默认 0.70")
    ap.add_argument("--trail", type=float, default=0.08, help="T1 后回撤离场，默认 0.08")
    ap.add_argument("--price-stop", type=float, default=None,
                    help="T1 前的价格保险回撤，如 0.20；默认关闭")
    ap.add_argument("--grid", action="store_true", help="打印参数网格")
    ap.add_argument("--refresh", action="store_true", help="先联网刷新估值历史")
    args = ap.parse_args(argv)

    if args.refresh:
        try:
            n = refresh_history(args.index)
            print("已刷新%s估值历史：%d 行" % (INDEX_NAMES[args.index], n))
        except Exception as exc:  # noqa: BLE001
            print("刷新失败（%s），继续使用本地缓存" % exc, file=sys.stderr)
        if not args.latest and not (args.start and args.end):
            return 0

    if args.latest:
        code = args.code
        # 兼容旧用法：--index zz500 不填 --code 时看 161017
        if code is None and args.index == "zz500":
            code = "161017"
        s = latest_snapshot(code, args.index, args.weight_hs300)
        print("=" * 64)
        print("%s 最新估值快照（数据截至 %s）" % (s["name"], s["date"]))
        print("  PE %.2f   PB %.2f" % (s["pe"], s["pb"]))
        print("  10 年综合分位   %.1f%%" % (s["p10"] * 100))
        print("  全历史综合分位 %.1f%%" % (s["pall"] * 100))
        if s["price_star"] is None:
            print("  指数星级（修正） %.2f 星 = 估值 %.2f"
                  "（全市场没有统一价格指数）" % (s["star"], s["val_star"]))
        else:
            print("  指数星级（修正） %.2f 星 = min(估值 %.2f, 价格 %.2f)"
                  % (s["star"], s["val_star"], s["price_star"]))
            print("  3 年高点 %.0f，当前 %.0f，回撤 %.1f%%"
                  % (s["star_high"], s["star_px"], s["star_dd"] * 100))
        if args.index != "market":
            if s["market_date"] != s["date"]:
                print("  全市场估值星级 %.2f 星（截至 %s，仅估值，参考）"
                      % (s["market_star"], s["market_date"]))
            else:
                print("  全市场估值星级 %.2f 星（仅估值，参考）"
                      % s["market_star"])
        if s["nav_label"]:
            print("-" * 64)
            print("%s 最新 %s  %.4f" % (s["nav_label"], s["nav_date"], s["nav"]))
            print("  距近一年高点 %.1f%%（高点 %.4f）" % (s["dd"] * 100, s["high"]))
        print("-" * 64)
        print("当前区域：%s" % s["zone"])
        print("买入：修正星级 >=4.8（估值<=20% 且价格距3年高点<=-26%）买 1/3；")
        print("      价格再跌 10% 买 1/3；再跌 20% 买最后 1/3")
        print("止盈：全历史 >=60% 卖 1/3；>=63% 卖剩余 2/3")
        print("保险：浮盈最高>=45% 后，从本轮高点回撤 10% 清仓（S1 前）")
        return 0

    if not args.start or not args.end:
        ap.error("回测需要 --start 和 --end；只看最新快照用 --latest")

    val = load_valuation(args.window, args.index, args.weight_hs300)
    if args.code:
        nav, nav_dates = load_nav(args.code)
        nav_label = "基金 %s" % args.code
    else:
        nav, nav_dates = load_index_nav(args.index, args.weight_hs300)
        nav_label = "%s指数" % INDEX_NAMES[args.index]
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)

    if args.grid:
        grid(val, nav, nav_dates, start, end)
        return 0

    res = simulate(val, nav, nav_dates, start, end,
                   args.t1, args.t2, args.trail, args.price_stop)
    print_one("%s 估值止盈回测（%s）" % (INDEX_NAMES[args.index], nav_label),
              args.start, args.end, res)
    return 0


if __name__ == "__main__":
    sys.exit(main())
