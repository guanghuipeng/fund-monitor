#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用真实指数历史数据验证当前买卖规则是否达到目标。

数据
----
- 中证500：真实收盘价 + 真实 PE/PB（乐咕口径）。
- 中证800：75% 沪深300 + 25% 中证500 按日收益合成的点位，
  PE/PB 同样按市值权重合成。它是近似值，不是官方 000906 口径。

规则
----
默认（current）保留旧规则，便于对照：
  买入：修正星级 >= 4.8 买 1/3；较 T1 再跌 10% 买 1/3；
        再跌 20% 买最后 1/3。
  卖出：全历史分位 >= 60% 卖 1/3；>= 63% 卖剩余 2/3。
  保险：最大浮盈 >= 45% 后，从本轮高点回撤 10% 清仓（S1 前）。

--v3 使用线上 v3.2（与 v3_rule.py 一致）：
  T1 4.8★ 按 1 年回撤深度买 1/3、1/2 或 2/3；T2/T3 在跌 10%/20%
  或趋势确认时加仓；若 T2 价格始终不到，T1 后 60 天且年线向上、
  现价高于年线 8% 时补第二笔（V 型底）；1 年回撤 <=-30% 且现价
  低于年线 15% 时暂停价格加仓（崩盘过滤）；卖出仍为 60%/63%，
  强趋势中剩余仓位改用 12% 移动止盈；保险改为最大浮盈 >= 40%
  + 回撤 10% + 年线向上。

输出
----
每个周期、每笔交易、大顶/大底检查、最大回撤、买入持有对照。
只用于研究，不构成投资建议。
"""
import argparse
import json
import os
import statistics
import sys
from datetime import date

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import cycle_backtest as cb
import strategy_backtest as sb

CASH0 = 200000.0
DROPS = (0.10, 0.20)
INSURANCE_GAIN = 0.45
INSURANCE_DD = 0.10


def _avg(xs):
    return sum(xs) / len(xs) if xs else None


def build_series(key, weight_hs300=0.75):
    """逐日：估值、点位、3年回撤、星级、均线、动量、趋势。"""
    raw = sb.build_series(key, weight_hs300)
    p10 = {d: q for d, q in cb.load_valuation("10", key, weight_hs300)}
    prices = [x[2] for x in raw]
    out = []
    for i, (d, p, px, dd, star) in enumerate(raw):
        row = {
            "date": d, "pall": p, "p10": p10.get(d), "px": px,
            "dd3": dd, "star": star,
            "val_star": sb.valuation_star(p) if p is not None else None,
            "price_star": sb.price_star(dd) if dd is not None else None,
        }
        for w in (20, 60, 120, 250):
            row["ma%d" % w] = (
                _avg(prices[max(0, i - w + 1):i + 1]) if i + 1 >= w else None
            )
        # 均线斜率（20 个交易日前），用来区分“下跌中的反弹”和“转势”。
        for w in (60, 120, 250):
            a = row.get("ma%d" % w)
            j = i - 20
            b = _avg(prices[max(0, j - w + 1):j + 1]) if j + 1 >= w else None
            row["ma%d_slope20" % w] = (a / b - 1.0) if (a and b) else None
        for n in (20, 60, 120, 250):
            row["mom%d" % n] = (
                px / prices[i - n] - 1 if i >= n and prices[i - n] else None
            )
        row["dd1y"] = (
            px / max(prices[max(0, i - 249):i + 1]) - 1
            if i + 1 >= 60 else None
        )
        if all(row.get("ma%d" % w) for w in (20, 60, 120, 250)):
            score = (
                int(px > row["ma20"])
                + int(row["ma20"] > row["ma60"])
                + int(row["ma60"] > row["ma120"])
                + int(row["ma120"] > row["ma250"])
            )
            row["trend_score"] = score
            row["trend"] = "强" if score >= 3 else ("弱" if score <= 1 else "中")
        else:
            row["trend_score"] = None
            row["trend"] = "?"
        if i >= 20:
            rets = [prices[j] / prices[j - 1] - 1 for j in range(i - 19, i + 1)]
            row["vol20"] = statistics.pstdev(rets) * (252 ** 0.5)
        else:
            row["vol20"] = None
        out.append(row)

    # 中证800 的估值星级不能先混合 PE/PB 水平再取分位：分位不是线性的，
    # 混合后的分位会被权重更大、但当时并不便宜的一侧带偏。改为先分别
    # 算沪深300/中证500 的估值星级，再按市值权重合成，最后与 800 自己
    # 的价格星级取 min。卖出仍用原来的全历史分位，不受影响。
    if key == "zz800":
        hs = {r["date"]: r.get("val_star")
              for r in build_series("hs300", weight_hs300)}
        zz = {r["date"]: r.get("val_star")
              for r in build_series("zz500", weight_hs300)}
        for row in out:
            a, b = hs.get(row["date"]), zz.get(row["date"])
            if a is None or b is None:
                continue
            val_star = weight_hs300 * a + (1.0 - weight_hs300) * b
            row["val_star"] = val_star
            row["star"] = (min(val_star, row["price_star"])
                           if row["price_star"] is not None else val_star)
    return out


def find_extremes(series):
    """局部极值 + 后续 25% 反向运动，用来标出主要牛顶/熊底。"""
    n = len(series)
    tops, bottoms = [], []
    i = 60
    while i < n - 1:
        lo, hi = max(0, i - 60), min(n, i + 61)
        px = series[i]["px"]
        if px == max(x["px"] for x in series[lo:hi]):
            fut = series[i:min(n, i + 250)]
            if fut and min(x["px"] for x in fut) <= px * 0.75:
                tops.append(i)
                i += 80
                continue
        if px == min(x["px"] for x in series[lo:hi]):
            fut = series[i:min(n, i + 250)]
            if fut and max(x["px"] for x in fut) >= px * 1.25:
                bottoms.append(i)
                i += 80
                continue
        i += 1
    return tops, bottoms


def simulate(series, rule="current", trail=0.10, bull=0.30,
             fixed_cash=False, entry_mode="current",
             insurance_mode="current", insurance_gain=INSURANCE_GAIN,
             insurance_dd=INSURANCE_DD, deep_frac=2.0 / 3.0,
             mid_frac=0.5, time_fill_days=365, cash_rate=0.0,
             catchup_days=0, catchup_max_p=1.0,
             catchup_above_ma250=0.0, s1_p10=None, s2_p10=None,
             price_adds_need_ma250_up=False, crash_guard_dd1y=None,
             crash_guard_below_ma250=None, crash_guard_t3_only=False):
    """从第一笔 4.8 星开始，按当前规则滚动运行；清仓后等待下一轮。

    rule="current"   : 分位 >= 60% 卖 1/3，>= 63% 卖剩余。
    rule="bull-trail": 分位 >= 60% 卖 1/3；到 63% 时若仍处于强趋势
                       （点位高于 250 日均线 bull 以上），剩余 2/3
                       不卖，改用从本轮高点回撤 trail 清仓。
    fixed_cash=True  : 每一轮重新投入 CASH0（不复利），用于逐轮对比。
    entry_mode="depth": 4.8★ 首笔按一年回撤加深：<=-60% 投 deep_frac，
                       <=-40% 投 mid_frac，其余 1/3；剩余现金平分给 T2/T3。
    entry_mode="depth_trend": 在 depth 基础上，T2 允许在“建仓时一年回撤
                       <=-40% 且 120 日线转向上”时确认加仓；T3 允许在
                       250 日线转向上时确认加仓。用来解决 V 型底只买 1/3，
                       同时用 250 日线过滤 2022 年级别的熊市反弹。
    entry_mode="trend_fill": T2/T3 除了“较 T1 跌 10%/20%”，也可以在
                       60/120 日均线转向上时加仓（较激进，仅研究用）。
    insurance_mode="trend": 保险层只在 250 日均线仍向上时触发；均线下行
                       的初期反弹不因 10% 回调清仓。
    catchup_days>0 : T2 价格未跌 10% 时，若从 T1 起已过 N 天、250 日线
                       向上且现价站上年线，则补第二笔；默认 0（关闭）。
                       用于 V 型底只买到 T1 的情况，需先回测确认。
    catchup_max_p  : 补仓时允许的最高全历史分位，默认 1.0（不限制）。
    catchup_above_ma250: 补仓时要求现价高于年线的最小幅度，默认 0。
                       用来区分“有效反转”和“贴着年线的熊市反弹”。
    s1_p10 / s2_p10: 双分位卖出。除全历史分位 60%/63% 外，10 年分位达到
                       该值也触发 S1/S2；默认 None（只用全历史分位）。
    price_adds_need_ma250_up: T2/T3 的“较 T1 跌 10%/20%”价格触发也必须
                       等 250 日线转上，避免在单边下跌中过早打光资金。
    crash_guard_dd1y: 只在该 1 年回撤阈值以内（如 -0.35）启用上面的
                       价格加仓熔断；默认 None，不启用。
    crash_guard_below_ma250: 崩盘过滤带。启用熔断后，若现价低于年线的
                       幅度达到该值（如 -0.15 表示低于年线 15%），暂停
                       价格加仓；价格回到带内后恢复。默认 None 不限制。
    crash_guard_t3_only: 熔断只作用于 T3，T2 仍按价格触发。
    """
    cash = CASH0
    shares = 0.0
    invested = 0.0
    avg_cost = 0.0
    stage = 0
    t1_px = None
    t1_dd1y = None
    last_buy_date = None
    last_buy_px = None
    per_buy = 0.0
    s1_done = False
    cycle_high = 0.0
    max_gain = 0.0
    trail_on = False
    trail_high = 0.0
    cycle_no = 0
    cycle_start = None
    cycle_start_cash = 0.0
    cycle_invested = 0.0
    cycle_trades = []
    trades = []
    cycles = []
    equity = []
    prev_date = None
    cash_days = 0
    n_days = 0
    cash_ratio_sum = 0.0

    for row in series:
        d, px, p = row["date"], row["px"], row["pall"]
        # 空仓现金按年化 cash_rate 计息（货币基金/活期）。默认 0，便于和旧结果对比。
        if prev_date is not None and cash > 0 and cash_rate:
            days = (d - prev_date).days
            if days > 0:
                cash *= (1.0 + cash_rate) ** (days / 365.0)
        prev_date = d
        if shares > 0:
            cycle_high = max(cycle_high, px)
            avg_cost = invested / shares
            max_gain = max(max_gain, px / avg_cost - 1.0)

        # ---- 买入 ----
        if stage == 0 and row["star"] is not None and row["star"] >= 4.8:
            if fixed_cash and cycle_no > 0:
                cash = CASH0
            # 跌得越深，第一笔越大：一年回撤 -60% 投 2/3，-40% 投 1/2，
            # 其余 1/3。剩余现金平均留给后面两次加仓。
            frac = 1.0 / 3.0
            if entry_mode in ("depth", "depth_trend", "depth_trend_time"):
                d1 = row.get("dd1y")
                if d1 is not None:
                    if d1 <= -0.60:
                        frac = deep_frac
                    elif d1 <= -0.40:
                        frac = mid_frac
            amt = min(cash * frac, cash)
            per_buy = (cash - amt) / 2.0
            sh = amt / px
            cash -= amt
            shares += sh
            invested += amt
            stage = 1
            t1_px = px
            t1_dd1y = row.get("dd1y")
            last_buy_date = d
            last_buy_px = px
            cycle_high = px
            trail_on = False
            trail_high = 0.0
            cycle_start = d
            cycle_start_cash = cash + amt
            cycle_invested = amt
            cycle_trades = []
            cycle_no += 1
            tr = dict(date=d, action="T1", px=px, pct=p, star=row["star"],
                      trend=row["trend"], amt=amt, sh=sh,
                      note="4.8星建仓")
            trades.append(tr)
            cycle_trades.append(tr)
        elif stage >= 1 and not s1_done and t1_px is not None and stage < 3:
            drop = DROPS[stage - 1]
            trig = t1_px * (1.0 - drop)
            trend_ok = False
            time_ok = False
            ma250_up = bool(
                row.get("ma250") and row.get("ma250_slope20") is not None
                and row["ma250_slope20"] > 0 and px > row["ma250"]
            )
            if entry_mode == "trend_fill":
                if stage == 1:
                    trend_ok = (
                        row.get("ma60") and row.get("ma60_slope20") is not None
                        and row["ma60_slope20"] > 0 and px > row["ma60"]
                    )
                else:
                    trend_ok = (
                        row.get("ma120") and row.get("ma120_slope20") is not None
                        and row["ma120_slope20"] > 0 and px > row["ma120"]
                    )
            elif entry_mode in ("depth_trend", "depth_trend_time"):
                # T2 只在“建仓时确实处于深度熊市”+120 日线转向上时确认；
                # T3 要求 250 日线也转向上，避免 2022 年级别的熊市反弹。
                if stage == 1:
                    trend_ok = bool(
                        t1_dd1y is not None and t1_dd1y <= -0.40
                        and row.get("ma120") and row.get("ma120_slope20") is not None
                        and row["ma120_slope20"] > 0 and px > row["ma120"]
                    )
                else:
                    trend_ok = ma250_up
                    if entry_mode == "depth_trend_time" and last_buy_date:
                        trend_ok = trend_ok and (
                            d - last_buy_date).days >= 120
            if entry_mode == "depth_trend_time" and last_buy_date is not None:
                # 一年后仍明显高于上次买入：熊市没有继续，按趋势把剩余
                # 资金补上，避免“只买了第一笔就一直等更低价”的踏空。
                time_ok = bool(
                    (d - last_buy_date).days >= time_fill_days
                    and last_buy_px and px > last_buy_px * 1.05
                    and ma250_up
                )
            catchup_ok = bool(
                catchup_days > 0 and stage == 1 and last_buy_date is not None
                and (d - last_buy_date).days >= catchup_days
                and ma250_up and p <= catchup_max_p
                and (px / row["ma250"] - 1.0) >= catchup_above_ma250
            )
            price_ok = px <= trig
            crash_now = bool(
                crash_guard_dd1y is not None
                and (not crash_guard_t3_only or stage == 2)
                and row.get("dd1y") is not None
                and row["dd1y"] <= crash_guard_dd1y
            )
            if crash_now and crash_guard_below_ma250 is not None:
                # 崩盘过滤带：现价仍低于年线一定幅度时，不按价格越跌越买；
                # 回到带内后恢复。这比“必须站上年线”更早恢复加仓。
                if (row.get("ma250")
                        and px / row["ma250"] - 1.0
                        <= crash_guard_below_ma250):
                    price_ok = False
            elif price_adds_need_ma250_up or crash_now:
                price_ok = price_ok and ma250_up
            if price_ok or trend_ok or time_ok or catchup_ok:
                amt = min(per_buy, cash)
                if amt > 1e-6:
                    sh = amt / px
                    cash -= amt
                    shares += sh
                    invested += amt
                    cycle_invested += amt
                    stage += 1
                    last_buy_date = d
                    last_buy_px = px
                    if price_ok:
                        note = "较T1跌%.0f%%" % (drop * 100)
                    elif trend_ok:
                        note = "趋势确认加仓"
                    elif catchup_ok:
                        note = "V型底趋势补仓"
                    else:
                        note = "一年后仍高于上次买入，趋势补仓"
                    tr = dict(date=d, action="T%d" % stage, px=px, pct=p,
                              star=row["star"], trend=row["trend"],
                              amt=amt, sh=sh, note=note)
                    trades.append(tr)
                    cycle_trades.append(tr)

        # ---- 保险层（S1 前）----
        strong_now = (row.get("ma250") is not None
                      and px / row["ma250"] - 1.0 >= bull)
        insurance_ok = (
            shares > 1e-9 and not s1_done and max_gain >= insurance_gain
            and cycle_high > 0
            and px / cycle_high - 1.0 <= -insurance_dd
        )
        if insurance_mode == "trend":
            insurance_ok = insurance_ok and (
                row.get("ma250_slope20") is not None
                and row["ma250_slope20"] > 0
            )
        if insurance_ok and not (rule == "bull-trail" and strong_now):
            proceeds = shares * px
            tr = dict(date=d, action="保险", px=px, pct=p, star=row["star"],
                      trend=row["trend"], amt=proceeds, sh=shares,
                      note="浮盈最高%.0f%%，高点回撤%.0f%%清仓"
                           % (max_gain * 100, insurance_dd * 100))
            cash += proceeds
            shares = 0.0
            trades.append(tr)
            cycle_trades.append(tr)
            cycles.append(dict(
                no=cycle_no, start=cycle_start, end=d,
                start_cash=cycle_start_cash,
                invested=cycle_invested,
                pnl=cash - cycle_start_cash,
                trades=cycle_trades, reason="保险",
            ))
            stage = 0
            t1_px = None
            s1_done = False
            cycle_high = 0.0
            max_gain = 0.0
            invested = 0.0
            avg_cost = 0.0
            per_buy = 0.0
            trail_on = False
            trail_high = 0.0

        # ---- 估值止盈 ----
        p10 = row.get("p10")
        s1_by_p10 = bool(s1_p10 is not None and p10 is not None
                         and p10 >= s1_p10)
        if shares > 1e-9 and not s1_done and (p >= 0.60 or s1_by_p10):
            sh = shares / 3.0
            proceeds = sh * px
            note = ("全历史分位≥60%卖1/3" if p >= 0.60
                    else "10年分位≥%.0f%%卖1/3" % (s1_p10 * 100))
            tr = dict(date=d, action="S1", px=px, pct=p, star=row["star"],
                      trend=row["trend"], amt=proceeds, sh=sh, note=note)
            cash += proceeds
            shares -= sh
            s1_done = True
            trades.append(tr)
            cycle_trades.append(tr)

        s2_by_p10 = bool(s2_p10 is not None and p10 is not None
                         and p10 >= s2_p10)
        if shares > 1e-9 and s1_done and (p >= 0.63 or s2_by_p10) and not trail_on:
            strong = (row.get("ma250") is not None
                      and px / row["ma250"] - 1.0 >= bull)
            if rule == "bull-trail" and strong:
                trail_on = True
                trail_high = max(cycle_high, px)
                tr = dict(date=d, action="HOLD", px=px, pct=p,
                          star=row["star"], trend=row["trend"], amt=0.0,
                          sh=0.0,
                          note="强趋势(高于年线%.0f%%)，剩余改移动止盈"
                               % ((px / row["ma250"] - 1.0) * 100))
                trades.append(tr)
                cycle_trades.append(tr)
            else:
                proceeds = shares * px
                note = ("全历史分位≥63%卖剩余" if p >= 0.63
                        else "10年分位≥%.0f%%卖剩余" % (s2_p10 * 100))
                tr = dict(date=d, action="S2", px=px, pct=p,
                          star=row["star"], trend=row["trend"],
                          amt=proceeds, sh=shares, note=note)
                cash += proceeds
                shares = 0.0
                trades.append(tr)
                cycle_trades.append(tr)
                cycles.append(dict(
                    no=cycle_no, start=cycle_start, end=d,
                    start_cash=cycle_start_cash,
                    invested=cycle_invested,
                    pnl=cash - cycle_start_cash,
                    trades=cycle_trades, reason="S2",
                ))
                stage = 0
                t1_px = None
                s1_done = False
                cycle_high = 0.0
                max_gain = 0.0
                invested = 0.0
                avg_cost = 0.0
                per_buy = 0.0

        # 强趋势移动止盈：从本轮高点回撤 trail 清仓
        if shares > 1e-9 and trail_on:
            trail_high = max(trail_high, px)
            if px <= trail_high * (1.0 - trail):
                proceeds = shares * px
                tr = dict(date=d, action="TR", px=px, pct=p,
                          star=row["star"], trend=row["trend"],
                          amt=proceeds, sh=shares,
                          note="移动止盈：高点回撤%.0f%%清仓"
                               % (trail * 100))
                cash += proceeds
                shares = 0.0
                trades.append(tr)
                cycle_trades.append(tr)
                cycles.append(dict(
                    no=cycle_no, start=cycle_start, end=d,
                    start_cash=cycle_start_cash,
                    invested=cycle_invested,
                    pnl=cash - cycle_start_cash,
                    trades=cycle_trades, reason="TR",
                ))
                stage = 0
                t1_px = None
                s1_done = False
                cycle_high = 0.0
                max_gain = 0.0
                trail_on = False
                trail_high = 0.0
                invested = 0.0
                avg_cost = 0.0
                per_buy = 0.0

        total_val = cash + shares * px
        if total_val > 0:
            n_days += 1
            cash_ratio_sum += cash / total_val
            if shares <= 1e-9 and cash > 0:
                cash_days += 1
        equity.append(total_val)

    final = cash + shares * series[-1]["px"]
    if shares > 1e-9:
        cycles.append(dict(
            no=cycle_no, start=cycle_start, end=series[-1]["date"],
            start_cash=cycle_start_cash,
            invested=cycle_invested,
            pnl=cash + shares * series[-1]["px"] - cycle_start_cash,
            trades=cycle_trades, reason="未清仓",
        ))
    return dict(final=final, cash=cash, shares=shares, trades=trades,
                cycles=cycles, equity=equity, cash_days=cash_days,
                n_days=n_days,
                avg_cash_ratio=(cash_ratio_sum / n_days if n_days else 0.0))


def analyze(series, res):
    eq = res["equity"]
    peak = eq[0]
    mdd = 0.0
    mdd_i = 0
    for i, v in enumerate(eq):
        if v > peak:
            peak = v
        if v / peak - 1.0 < mdd:
            mdd = v / peak - 1.0
            mdd_i = i
    peak = eq[0]
    start = 0
    longest = 0
    for i, v in enumerate(eq):
        if v >= peak:
            peak = v
            start = i
        longest = max(longest, i - start)

    # 买入持有的最大回撤，作为对照
    bh_peak = series[0]["px"]
    bh_mdd = 0.0
    for r in series:
        bh_peak = max(bh_peak, r["px"])
        bh_mdd = min(bh_mdd, r["px"] / bh_peak - 1.0)

    for tr in res["trades"]:
        idx = next((j for j, r in enumerate(series) if r["date"] == tr["date"]),
                   None)
        if idx is None:
            continue
        fut = series[idx:min(len(series), idx + 250)]
        tr["fut_low"] = (min(x["px"] for x in fut) / tr["px"] - 1.0
                         if fut else None)
        tr["fut_high"] = (max(x["px"] for x in fut) / tr["px"] - 1.0
                          if fut else None)
    tops, bottoms = find_extremes(series)
    days = (series[-1]["date"] - series[0]["date"]).days
    years = days / 365.25 if days else 1.0
    cagr = (res["final"] / CASH0) ** (1.0 / years) - 1.0
    bh_cagr = (series[-1]["px"] / series[0]["px"]) ** (1.0 / years) - 1.0
    return dict(mdd=mdd, mdd_date=series[mdd_i]["date"], longest=longest,
                bh=series[-1]["px"] / series[0]["px"], bh_mdd=bh_mdd,
                years=years, cagr=cagr, bh_cagr=bh_cagr,
                tops=tops, bottoms=bottoms)


def sale_stats(res):
    """卖出后一年：继续涨多少（卖飞）、随后跌多少（躲过）。"""
    rows = []
    for t in res["trades"]:
        if t["action"] not in ("S1", "S2", "保险", "TR"):
            continue
        rows.append((t["action"], t.get("fut_high"), t.get("fut_low")))
    return rows


def pct(x, d=1):
    return "--" if x is None else ("%+." + str(d) + "f%%") % (x * 100)


def top_review(series, res, i):
    """看每个大顶发生时：有没有仓位、卖没卖、卖价离顶多远。"""
    r = series[i]
    future = series[i:min(len(series), i + 250)]
    drop_d = next((x["date"] for x in future if x["px"] <= r["px"] * 0.85),
                  None)
    end_d = drop_d or (future[-1]["date"] if future else r["date"])
    buys = [t for t in res["trades"]
            if t["action"] in ("T1", "T2", "T3") and t["date"] <= r["date"]]
    if not buys:
        return "顶前无持仓"
    last_buy = buys[-1]
    full = [t for t in res["trades"]
            if t["action"] in ("S2", "保险", "TR")
            and last_buy["date"] < t["date"] <= r["date"]]
    if full:
        ex = full[0]
        return "顶前已清仓 %s %s@%.0f（卖价距顶 %s）" % (
            ex["date"], ex["action"], ex["px"], pct(ex["px"] / r["px"] - 1.0))
    sales = [t for t in res["trades"]
             if t["action"] in ("S1", "S2", "保险", "TR")
             and last_buy["date"] < t["date"] <= end_d]
    if not sales:
        return "顶前有仓位，未卖，直到%s" % (drop_d or "期末")
    return "；".join("%s %s@%.0f（距顶 %s）" % (
        t["date"], t["action"], t["px"], pct(t["px"] / r["px"] - 1.0))
        for t in sales)


def print_report(key, series, res, m):
    print("\n" + "=" * 100)
    print("%s  %s ~ %s  天数=%d" % (
        key, series[0]["date"], series[-1]["date"], len(series)))
    print("期末资金 %.0f  总收益 %s  年化 %s"
          % (res["final"], pct(res["final"] / CASH0 - 1.0), pct(m["cagr"])))
    print("空仓天数 %d/%d（%.0f%%）  平均现金比例 %.0f%%"
          % (res["cash_days"], res["n_days"],
             100.0 * res["cash_days"] / res["n_days"] if res["n_days"] else 0.0,
             100.0 * res["avg_cash_ratio"]))
    if res["cycles"]:
        first = res["cycles"][0]["start"]
        idx = next((j for j, r in enumerate(series) if r["date"] >= first), 0)
        yrs = (series[-1]["date"] - series[idx]["date"]).days / 365.25
        if yrs > 0:
            start_cap = res["cycles"][0]["start_cash"] or CASH0
            cagr_fb = (res["final"] / start_cap) ** (1.0 / yrs) - 1.0
            bh_fb = series[-1]["px"] / series[idx]["px"]
            bh_cagr_fb = bh_fb ** (1.0 / yrs) - 1.0
            print("首笔买入 %s 起：年化 %s  买入持有 %s（年化 %s）"
                  % (first, pct(cagr_fb), pct(bh_fb - 1.0), pct(bh_cagr_fb)))
    print("买入持有 %s（年化 %s）  策略最大回撤 %s (%s)  持有最大回撤 %s  "
          "最长水下 %d 天"
          % (pct(m["bh"] - 1.0), pct(m["bh_cagr"]), pct(m["mdd"]),
             m["mdd_date"], pct(m["bh_mdd"]), m["longest"]))
    sells = [t for t in res["trades"]
             if t["action"] in ("S1", "S2", "保险", "TR")]
    if sells:
        his = [t["fut_high"] for t in sells if t.get("fut_high") is not None]
        los = [t["fut_low"] for t in sells if t.get("fut_low") is not None]
        print("卖出后1年：平均最高 %s / 最大 %s（卖飞空间）；平均最低 %s / 最大 %s（躲过下跌）"
              % (pct(sum(his) / len(his)), pct(max(his)),
                 pct(sum(los) / len(los)), pct(min(los))))
    print("\n周期交易:")
    for c in res["cycles"]:
        ret = c["pnl"] / c["start_cash"] if c["start_cash"] else 0.0
        ret_inv = c["pnl"] / c["invested"] if c["invested"] else 0.0
        print("  #%d %s -> %s  本轮资金 %.0f  投入 %.0f  盈亏 %+.0f  "
              "账户收益 %s  投入收益 %s  结束 %s"
              % (c["no"], c["start"], c["end"], c["start_cash"],
                 c["invested"], c["pnl"], pct(ret), pct(ret_inv),
                 c["reason"]))
        for t in c["trades"]:
            print("     %s %-4s px=%8.1f 分位=%5.1f%% 星级=%.2f 趋势=%s %s"
                  % (t["date"], t["action"], t["px"], (t["pct"] or 0) * 100,
                     t["star"], t["trend"], t["note"]))
            print("        后1年最低 %s / 最高 %s"
                  % (pct(t.get("fut_low")), pct(t.get("fut_high"))))
    print("\n大顶检查:")
    for i in m["tops"]:
        r = series[i]
        future = series[i:min(len(series), i + 250)]
        drop_date = next((x["date"] for x in future
                          if x["px"] <= r["px"] * 0.85), None)
        print("  顶 %s px=%.0f pall=%.1f%% p10=%.1f%% 趋势=%s | -15%%日期=%s | %s"
              % (r["date"], r["px"], (r["pall"] or 0) * 100,
                 (r["p10"] or 0) * 100, r["trend"],
                 drop_date or "未跌15%", top_review(series, res, i)))
    print("\n大底检查:")
    for i in m["bottoms"]:
        r = series[i]
        buys = [t for t in res["trades"]
                if t["action"].startswith("T")
                and abs((t["date"] - r["date"]).days) <= 120]
        print("  底 %s px=%.0f pall=%.1f%% p10=%.1f%% 趋势=%s | 前后120天买入=%s"
              % (r["date"], r["px"], (r["pall"] or 0) * 100,
                 (r["p10"] or 0) * 100, r["trend"],
                 ",".join("%s@%.0f(%s)" % (t["date"], t["px"],
                                           pct(t["px"] / r["px"] - 1.0))
                          for t in buys) or "无"))


def main(argv=None):
    ap = argparse.ArgumentParser(description="用真实指数历史验证买卖规则")
    ap.add_argument("--index", default="all",
                    choices=["all", "zz500", "zz800", "hs300"])
    ap.add_argument("--rule", default="current",
                    choices=["current", "bull-trail"],
                    help="current=现行规则；bull-trail=强趋势时剩余仓位改移动止盈")
    ap.add_argument("--trail", type=float, default=0.10,
                    help="移动止盈回撤比例，默认 0.10")
    ap.add_argument("--bull", type=float, default=0.30,
                    help="判定强趋势的点位高于年线幅度，默认 0.30")
    ap.add_argument("--entry", default="current",
                    choices=["current", "depth", "depth_trend"],
                    help="买入方式：current 现行；depth 深度首笔；"
                         "depth_trend 深度首笔+趋势确认加仓")
    ap.add_argument("--insurance-mode", default="current",
                    choices=["current", "trend"],
                    help="保险层是否要求 250 日线向上")
    ap.add_argument("--insurance-gain", type=float, default=0.45,
                    help="保险层触发所需最大浮盈，默认 0.45")
    ap.add_argument("--cash-rate", type=float, default=0.0,
                    help="空仓现金年化收益，默认 0；如 0.015 表示 1.5%%")
    ap.add_argument("--catchup-days", type=int, default=None,
                    help="V 型底趋势补仓：T1 后 N 天、年线向上且站上年线时"
                         "补第二笔；--v3 默认 60，其他模式默认 0 关闭")
    ap.add_argument("--catchup-max-p", type=float, default=1.0,
                    help="趋势补仓允许的最高全历史分位，默认 1.0 不限制")
    ap.add_argument("--catchup-above-ma250", type=float, default=None,
                    help="趋势补仓要求现价高于年线的最小幅度，如 0.10 表示 10%%；"
                         "--v3 默认 0.08")
    ap.add_argument("--s1-p10", type=float, default=None,
                    help="双分位卖出：10 年分位达到该值也触发 S1（如 0.65）")
    ap.add_argument("--s2-p10", type=float, default=None,
                    help="双分位卖出：10 年分位达到该值也触发 S2（如 0.68）")
    ap.add_argument("--crash-guard-dd1y", type=float, default=None,
                    help="崩盘过滤：1 年回撤达到该值（如 -0.30）时，"
                         "价格加仓需等年线转上")
    ap.add_argument("--crash-guard-below-ma250", type=float, default=None,
                    help="崩盘过滤带：现价低于年线的幅度达到该值（如 -0.20）"
                         "时暂停价格加仓，回到带内恢复")
    ap.add_argument("--crash-guard-t3-only", action="store_true",
                    help="崩盘过滤只作用于 T3")
    ap.add_argument("--no-crash-guard", action="store_true",
                    help="关闭 v3.2 崩盘过滤，仅用于和 v3.1 对照")
    ap.add_argument("--v3", action="store_true",
                    help="使用线上 v3.2 预设：12%% 移动止盈 + 深度首笔 + "
                         "趋势加仓 + 崩盘过滤 + 40%%/年线保险")
    ap.add_argument("--json", action="store_true", help="输出 JSON 摘要")
    args = ap.parse_args(argv)

    if args.v3:
        # 与 v3_rule.py 线上逻辑保持一致。
        args.rule = "bull-trail"
        args.trail = 0.12
        args.bull = 0.30
        args.entry = "depth_trend"
        args.insurance_mode = "trend"
        args.insurance_gain = 0.40
        if args.catchup_days is None:
            args.catchup_days = 60
        if args.catchup_above_ma250 is None:
            args.catchup_above_ma250 = 0.08
        if args.crash_guard_dd1y is None:
            args.crash_guard_dd1y = -0.30
        if args.crash_guard_below_ma250 is None:
            args.crash_guard_below_ma250 = -0.15

    if args.no_crash_guard:
        args.crash_guard_dd1y = None
        args.crash_guard_below_ma250 = None

    if args.catchup_days is None:
        args.catchup_days = 0
    if args.catchup_above_ma250 is None:
        args.catchup_above_ma250 = 0.0

    keys = (["zz500", "zz800", "hs300"]
            if args.index == "all" else [args.index])
    out = []
    for key in keys:
        series = [r for r in build_series(key)
                  if r["pall"] is not None and r["px"]]
        res = simulate(series, rule=args.rule, trail=args.trail,
                       bull=args.bull, entry_mode=args.entry,
                       insurance_mode=args.insurance_mode,
                       insurance_gain=args.insurance_gain,
                       cash_rate=args.cash_rate,
                       catchup_days=args.catchup_days,
                       catchup_max_p=args.catchup_max_p,
                       catchup_above_ma250=args.catchup_above_ma250,
                       s1_p10=args.s1_p10, s2_p10=args.s2_p10,
                       crash_guard_dd1y=args.crash_guard_dd1y,
                       crash_guard_below_ma250=args.crash_guard_below_ma250,
                       crash_guard_t3_only=args.crash_guard_t3_only)
        m = analyze(series, res)
        print_report(key, series, res, m)
        out.append(dict(key=key, final=res["final"],
                        return_=res["final"] / CASH0 - 1.0,
                        buy_hold=m["bh"] - 1.0, mdd=m["mdd"],
                        mdd_date=m["mdd_date"].isoformat(),
                        longest=m["longest"],
                        cycles=[dict(no=c["no"],
                                     start=c["start"].isoformat(),
                                     end=c["end"].isoformat(),
                                     invested=c["invested"], pnl=c["pnl"],
                                     reason=c["reason"],
                                     trades=[dict(date=t["date"].isoformat(),
                                                  action=t["action"],
                                                  px=t["px"], pct=t["pct"])
                                             for t in c["trades"]])
                                for c in res["cycles"]]))
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
