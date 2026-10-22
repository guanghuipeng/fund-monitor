#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""中证800 / 中证500 牛熊周期策略回测（可复现）。

这不是模拟盘，也不是未来的收益承诺。它只做一件事：
用 data/cache 里的真实历史 PE/PB 和指数点位，按下面这套规则逐日跑一遍，
把每一次买入、卖出、投入金额、期末资金、最大回撤都打印出来。

规则（2026-10 定稿）
--------------------
买入（20 万分成 3 份）：
  T1  修正星级 >= 4.8              -> 买入 1/3
  T2  价格 <= T1 价 × 0.90         -> 买入 1/3
  T3  价格 <= T1 价 × 0.80         -> 买入 1/3
  没触发的档位不追，现金留着。

修正星级：
  估值星级（全历史 PE/PB 综合分位 p）：
    <=10% -> 5.0; 20% -> 4.8; 40% -> 4.0; 60% -> 3.0; 80% -> 2.0; 100% -> 1.0
  价格星级（相对过去 3 年最高点的回撤 dd）：
    回撤 <=5% -> 3.8; 10% -> 4.2; 20% -> 4.5; >=30% -> 5.0
  综合星级 = min(估值星级, 价格星级)
  这样做的目的：2021 年中证500 价格在 3 年新高附近时，估值分位可能只有
  19%，但价格星级会把星级压到 3.8，避免在顶部买入。

卖出：
  S1  全历史分位 >= 60%            -> 卖出 1/3
  S2  全历史分位 >= 63%            -> 卖出剩余 2/3
  保险层（只在中证500这种“估值一直不贵、价格却到顶”的情况下兜底）：
    买入后最大浮盈 >= 45%，且价格从本轮最高点回撤 >= 10% -> 清仓
  保险层只在 S1 之前生效；S1 之后不再用回撤止盈，避免卖在半山腰。

数据口径
--------
- 中证500：乐咕真实 PE/PB + 收盘点位。
- 中证800：没有现成历史接口，用 75% 沪深300 + 25% 中证500 合成：
    PE800 = 1 / (0.75/PE300 + 0.25/PE500)，PB 同理；
    点位用 75/25 的日收益加权。它是近似值，不是官方中证800历史。
- 分位是“当时可见的历史”滚动计算，没有偷看未来。
- 信号按当日收盘价成交，未计手续费、管理费、分红。指数价格不含分红，
  基金净值含分红，两者大体抵消；实际收益会有 1%-2%/年的偏差。

用法
----
  python strategy_backtest.py                 # 跑全部周期
  python strategy_backtest.py --detail        # 附每笔交易
  python strategy_backtest.py --cycle 2018    # 只跑 2018-2021
  python strategy_backtest.py --index zz800   # 只跑中证800
  python strategy_backtest.py --insurance dd15
  python strategy_backtest.py --json          # 机器可读
"""
import argparse
import bisect
import json
import os
import sys
import unicodedata
from collections import deque
from datetime import date

import cycle_backtest as cb

BASE = os.path.dirname(os.path.abspath(__file__))

# 每个周期：(指数, 策略开始日, 策略结束日, 买入持有开始日, 买入持有结束日)
# 策略结束日故意留到下一轮熊市，才能看到“峰值之后才触发”的保险卖出。
CYCLES = {
    "2008": {
        "label": "2008 金融危机",
        "cases": [
            ("zz800", "2007-10-16", "2015-06-12", "2007-10-16", "2015-06-12"),
            ("zz500", "2007-10-16", "2015-06-12", "2007-10-16", "2015-06-12"),
        ],
    },
    "2015": {
        "label": "2015 杠杆牛 → 2016 熊",
        "cases": [
            ("zz800", "2015-06-12", "2018-01-24", "2015-06-12", "2018-01-24"),
            ("zz500", "2015-06-12", "2018-01-24", "2015-06-12", "2018-01-24"),
        ],
    },
    "2018": {
        "label": "2018 熊 → 2021 牛",
        "cases": [
            ("zz800", "2018-01-24", "2024-02-05", "2018-01-24", "2021-12-31"),
            ("zz500", "2018-01-24", "2024-02-05", "2018-01-24", "2021-12-31"),
        ],
    },
    "2021": {
        "label": "2021 牛 → 2024 熊 → 2026 牛",
        "cases": [
            ("zz800", "2021-02-19", "2026-10-21", "2021-02-19", "2026-10-21"),
            ("zz500", "2021-09-13", "2026-10-21", "2021-09-13", "2026-10-21"),
        ],
    },
}

INDEX_LABEL = {"zz800": "中证800", "zz500": "中证500"}


# ---------------------------------------------------------------- 星级
def valuation_star(p):
    """全历史综合分位 -> 估值星级（分段线性）。"""
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
    """相对过去 3 年最高点的回撤 -> 价格星级。"""
    if drawdown >= -0.05:
        return 3.8
    if drawdown >= -0.10:
        return 3.8 + (-drawdown - 0.05) / 0.05 * 0.4
    if drawdown >= -0.20:
        return 4.2 + (-drawdown - 0.10) / 0.10 * 0.3
    if drawdown >= -0.30:
        return 4.5 + (-drawdown - 0.20) / 0.10 * 0.5
    return 5.0


def combined_star(p, drawdown):
    return min(valuation_star(p), price_star(drawdown))


# ---------------------------------------------------------------- 数据
def _three_years_ago(d):
    try:
        return d.replace(year=d.year - 3)
    except ValueError:  # 2 月 29 日
        return d.replace(year=d.year - 3, day=28)


_SERIES_CACHE = {}


def build_series(key, weight_hs300=0.75):
    """逐日序列：(日期, 全历史分位, 点位, 3年回撤, 综合星级)。

    分位来自 cycle_backtest.load_valuation("all")，它本身是滚动计算的；
    3 年高点是过去 3 年点位的最大值，同样只用当天及以前的数据。
    """
    cache_key = (key, weight_hs300)
    if cache_key in _SERIES_CACHE:
        return _SERIES_CACHE[cache_key]

    val = cb.load_valuation("all", key, weight_hs300)
    nav, nav_dates = cb.load_index_nav(key, weight_hs300)

    out = []
    window = deque()  # (date, px)，保留最近 3 年
    for d, p in val:
        px = cb.nav_on(nav_dates, nav, d)
        if px is None:
            continue
        window.append((d, px))
        cutoff = _three_years_ago(d)
        while window and window[0][0] < cutoff:
            window.popleft()
        high = max(x[1] for x in window)
        drawdown = px / high - 1.0
        out.append((d, p, px, drawdown, combined_star(p, drawdown)))

    _SERIES_CACHE[cache_key] = out
    return out


def _last_on_or_before(dates, nav, d):
    i = bisect.bisect_right(dates, d) - 1
    return (dates[i], nav[dates[i]]) if i >= 0 else (None, None)


def buy_and_hold(key, start, end, cash=200000.0, weight_hs300=0.75):
    """起点买入、终点卖出的对照。"""
    nav, nav_dates = cb.load_index_nav(key, weight_hs300)
    d0, px0 = _last_on_or_before(nav_dates, nav, date.fromisoformat(start))
    d1, px1 = _last_on_or_before(nav_dates, nav, date.fromisoformat(end))
    if d0 is None or px0 is None or d1 is None or px1 is None or d1 <= d0:
        return None
    final = cash * px1 / px0
    yrs = (d1 - d0).days / 365.25
    cagr = (final / cash) ** (1.0 / yrs) - 1.0 if yrs > 0 else 0.0
    # 买入持有的最大回撤
    peak = cash
    mdd = 0.0
    for d in nav_dates:
        if d < d0 or d > d1:
            continue
        eq = cash * nav[d] / px0
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1.0)
    return {
        "start": d0,
        "end": d1,
        "final": final,
        "return": final / cash - 1.0,
        "cagr": cagr,
        "mdd": mdd,
    }


# ---------------------------------------------------------------- 回测
def simulate(key, start, end, cash=200000.0, tranches=3,
             drops=(0.10, 0.20, 0.30), t1_star=4.8, t2=0.63,
             insurance="dd10", weight_hs300=0.75):
    """按规则逐日回测一个周期。

    insurance: dd10 / dd15 / none
    返回 dict，包含 trades / 期末资金 / 年化 / 最大回撤 / 是否清仓。
    """
    series = build_series(key, weight_hs300)
    start_d = date.fromisoformat(start)
    end_d = date.fromisoformat(end)

    ins_dd = {"dd10": 0.10, "dd15": 0.15, "none": None}[insurance]
    per_buy = cash / tranches

    cash_left = cash
    shares = 0.0
    invested = 0.0
    trades = []
    t1_px = None
    stage = 0          # 0=未买, 1=已T1, 2=已T2, 3=已T3 ...
    s1_done = False
    done = False
    cycle_high = 0.0
    max_gain = 0.0
    last_px = None
    eq_peak = cash
    mdd = 0.0

    for d, p, px, dd, star in series:
        if d < start_d:
            continue
        if d > end_d:
            break
        last_px = px

        # 记录本轮最高点和最大浮盈（用当时的平均成本）
        if shares > 0:
            cycle_high = max(cycle_high, px)
            avg = invested / shares
            max_gain = max(max_gain, px / avg - 1.0)

        # ---- 买入 ----
        if stage == 0 and star >= t1_star:
            amt = per_buy
            sh = amt / px
            cash_left -= amt
            shares += sh
            invested += amt
            t1_px = px
            stage = 1
            cycle_high = max(cycle_high, px)
            trades.append({
                "date": d, "action": "T1", "px": px, "pct": p, "star": star,
                "amount": amt, "shares": sh,
            })
        elif stage >= 1 and stage < tranches and t1_px is not None:
            trigger = t1_px * (1.0 - drops[stage - 1])
            if px <= trigger:
                amt = min(per_buy, cash_left)
                sh = amt / px
                cash_left -= amt
                shares += sh
                invested += amt
                stage += 1
                cycle_high = max(cycle_high, px)
                trades.append({
                    "date": d, "action": "T%d" % stage, "px": px, "pct": p,
                    "star": star, "amount": amt, "shares": sh,
                })

        # ---- 保险层（只在 S1 之前）----
        if (shares > 0 and not s1_done and ins_dd is not None
                and max_gain >= 0.45 and cycle_high > 0
                and px / cycle_high - 1.0 <= -ins_dd):
            proceeds = shares * px
            cash_left += proceeds
            trades.append({
                "date": d, "action": "保险", "px": px, "pct": p, "star": star,
                "amount": proceeds, "shares": shares,
            })
            shares = 0.0
            done = True

        # ---- 估值止盈 ----
        if shares > 0 and not s1_done and not done and p >= 0.60:
            sh = shares / 3.0
            proceeds = sh * px
            cash_left += proceeds
            shares -= sh
            s1_done = True
            trades.append({
                "date": d, "action": "S1", "px": px, "pct": p, "star": star,
                "amount": proceeds, "shares": sh,
            })

        if shares > 1e-9 and s1_done and not done and p >= t2:
            proceeds = shares * px
            cash_left += proceeds
            trades.append({
                "date": d, "action": "S2", "px": px, "pct": p, "star": star,
                "amount": proceeds, "shares": shares,
            })
            shares = 0.0
            done = True

        eq = cash_left + shares * px
        eq_peak = max(eq_peak, eq)
        mdd = min(mdd, eq / eq_peak - 1.0)

        if done:
            break

    final = cash_left + shares * (last_px or 0.0)
    buys = [t for t in trades if t["action"].startswith("T")]
    sells = [t for t in trades if t["action"] in ("S1", "S2", "保险")]
    if not buys:
        return {
            "key": key, "start": start_d, "end": end_d,
            "traded": False, "final": final, "return": 0.0, "cagr": 0.0,
            "mdd": 0.0, "invested": 0.0, "avg_cost": 0.0, "shares_left": 0.0,
            "trades": [], "first_buy": None, "last_sell": None,
            "fully_sold": True, "status": "未触发买入",
        }

    avg_cost = invested / sum(t["shares"] for t in buys)
    fully_sold = shares < 1e-9
    last_sell = sells[-1]["date"] if sells else end_d
    yrs = (last_sell - buys[0]["date"]).days / 365.25
    cagr = (final / cash) ** (1.0 / yrs) - 1.0 if yrs > 0 else 0.0
    profit_on_invested = final - (cash - invested)
    return {
        "key": key, "start": start_d, "end": end_d,
        "traded": True, "final": final, "return": final / cash - 1.0,
        "cagr": cagr, "mdd": mdd, "invested": invested,
        "avg_cost": avg_cost,
        "return_on_invested": profit_on_invested / invested if invested else 0.0,
        "shares_left": shares, "trades": trades,
        "first_buy": buys[0]["date"], "last_sell": last_sell,
        "fully_sold": fully_sold,
        "status": "已清仓" if fully_sold else "未卖完",
    }


# ---------------------------------------------------------------- 输出
def _width(s):
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
               for ch in str(s))


def _pad(s, width, align="left"):
    s = str(s)
    gap = max(0, width - _width(s))
    if align == "right":
        return " " * gap + s
    return s + " " * gap


def _pct(x):
    return "%.1f%%" % (x * 100.0)


def _money(x):
    return "{:,.0f}".format(x)


def _print_table(headers, all_rows, aligns):
    """按显示宽度对齐（中文算 2 格），宽度同时考虑表头和单元格。"""
    widths = [_width(h) for h in headers]
    for cells in all_rows:
        for i, c in enumerate(cells):
            widths[i] = max(widths[i], _width(c))

    def line(cells):
        return "  ".join(_pad(c, widths[i], aligns[i])
                        for i, c in enumerate(cells))

    print(line(headers))
    print("  ".join("-" * w for w in widths))
    for cells in all_rows:
        print(line(cells))


def print_summary(rows):
    headers = ["周期", "指数", "首次买入", "均价", "实际投入", "最后卖出",
               "期末资金", "总收益", "年化", "最大回撤", "状态"]
    aligns = ["left", "left", "left", "right", "right", "left",
              "right", "right", "right", "right", "left"]
    all_rows = []
    for r in rows:
        if not r["traded"]:
            all_rows.append([
                r["cycle_label"], INDEX_LABEL[r["key"]], "未触发", "-", "0",
                "未卖出", _money(r["final"]), "0.0%", "-", "0.0%",
                r["status"],
            ])
        else:
            all_rows.append([
                r["cycle_label"], INDEX_LABEL[r["key"]],
                r["first_buy"].isoformat(), _money(r["avg_cost"]),
                _money(r["invested"]), r["last_sell"].isoformat(),
                _money(r["final"]), _pct(r["return"]), _pct(r["cagr"]),
                _pct(r["mdd"]), r["status"],
            ])
    _print_table(headers, all_rows, aligns)


def print_compare(rows):
    headers = ["周期", "指数", "策略收益", "同期买入持有", "差额",
               "策略回撤", "持有回撤"]
    aligns = ["left", "left", "right", "right", "right", "right", "right"]
    all_rows = []
    for r in rows:
        bh = r.get("bh")
        if not bh:
            continue
        diff = r["return"] - bh["return"]
        all_rows.append([
            r["cycle_label"], INDEX_LABEL[r["key"]],
            _pct(r["return"]), _pct(bh["return"]),
            ("%+.1fpp" % (diff * 100.0)), _pct(r["mdd"]),
            _pct(bh["mdd"]),
        ])
    _print_table(headers, all_rows, aligns)


def print_trades(rows):
    for r in rows:
        if not r["traded"]:
            continue
        print("\n%s / %s" % (r["cycle_label"], INDEX_LABEL[r["key"]]))
        for t in r["trades"]:
            print("  %s  %-3s  px=%8.0f  分位=%5.1f%%  星级=%.2f  "
                  "金额=%10s  份额=%8.3f" % (
                      t["date"].isoformat(), t["action"], t["px"],
                      t["pct"] * 100.0, t["star"],
                      _money(t["amount"]), t["shares"]))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="中证800/中证500 牛熊周期策略回测（修正星级 + 价格分档 + 保险层）")
    ap.add_argument("--cycle", default="all",
                    choices=["all"] + sorted(CYCLES.keys()),
                    help="跑哪个周期，默认全部")
    ap.add_argument("--index", default="all", choices=["all", "zz800", "zz500"])
    ap.add_argument("--insurance", default="dd10",
                    choices=["dd10", "dd15", "none"],
                    help="保险层：最大浮盈>=45% 后，从高点回撤多少清仓")
    ap.add_argument("--tranches", type=int, default=3, choices=[3, 4])
    ap.add_argument("--cash", type=float, default=200000.0)
    ap.add_argument("--detail", action="store_true", help="打印每笔交易")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    cycle_keys = sorted(CYCLES.keys()) if args.cycle == "all" else [args.cycle]
    drops = (0.10, 0.20, 0.30)[:args.tranches]
    rows = []

    for ck in cycle_keys:
        spec = CYCLES[ck]
        for key, s_start, s_end, bh_start, bh_end in spec["cases"]:
            if args.index != "all" and key != args.index:
                continue
            r = simulate(key, s_start, s_end, cash=args.cash,
                         tranches=args.tranches, drops=drops,
                         insurance=args.insurance)
            r["cycle"] = ck
            r["cycle_label"] = spec["label"]
            r["bh"] = buy_and_hold(key, bh_start, bh_end, cash=args.cash)
            rows.append(r)

    if args.json:
        out = []
        for r in rows:
            x = dict(r)
            for k in ("start", "end", "first_buy", "last_sell"):
                if x.get(k) is not None:
                    x[k] = x[k].isoformat()
            x["trades"] = [
                {kk: (vv.isoformat() if isinstance(vv, date) else vv)
                 for kk, vv in t.items()}
                for t in x["trades"]
            ]
            if x.get("bh"):
                x["bh"] = {kk: (vv.isoformat() if isinstance(vv, date) else vv)
                           for kk, vv in x["bh"].items()}
            out.append(x)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    print("=" * 118)
    print("中证800 / 中证500 牛熊周期策略回测  "
          "（20万本金，修正星级，价格分档，保险层=%s）" % args.insurance)
    print("=" * 118)
    print_summary(rows)
    print("\n" + "=" * 118)
    print("对照：同期买入持有（周期起点一次性买入，拿到周期终点）")
    print("=" * 118)
    print_compare(rows)
    if args.detail:
        print("\n" + "=" * 118)
        print("逐笔交易")
        print("=" * 118)
        print_trades(rows)
    print("\n说明：指数价格回测，未计手续费/管理费/分红；信号按当日收盘价成交。")
    print("中证800 为 75% 沪深300 + 25% 中证500 的合成近似，不是官方历史。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
