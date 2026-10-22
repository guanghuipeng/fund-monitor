#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v3.1 规则的实时状态：深度首笔 + 趋势加仓 + V 型底补仓 + 40% 保险 + 12% 移动止盈。

直接复用 rule_validation.simulate，保证网页/推送和回测完全同一套逻辑。
"""
from datetime import date

import rule_validation as rv
import strategy_backtest as sb

TRAIL = 0.12
BULL = 0.30
INSURANCE_GAIN = 0.40
INSURANCE_DD = 0.10
DEEP_FRAC = 2.0 / 3.0
MID_FRAC = 0.5
# V 型底补仓：T1 后 60 天，年线向上且现价高于年线 8%，补第二笔。
# 用于沪深300 2018 那种只跌 8%、等不到 T1×0.90 的浅 V 底。
CATCHUP_DAYS = 60
CATCHUP_ABOVE_MA250 = 0.08


def _avg(xs):
    return sum(xs) / len(xs) if xs else None


def _pct(x, digits=1, sign=False):
    if x is None:
        return "--"
    pattern = ("%+." if sign else "%.") + str(digits) + "f%%"
    return pattern % (x * 100.0)


def _px(x):
    return "--" if x is None else "%.0f" % x


def _display_px(row, px):
    """中证800 的 v3 内部点位是 1000 点基准，展示时换成真实点位。"""
    if px is None:
        return None
    if row.get("key") == "zz800" and row.get("star_px") and row.get("px_display"):
        return px * row["px_display"] / row["star_px"]
    return px


def _append_live_row(series, row):
    """把今天尚未进入估值缓存的行情追加成一行，指标按同一口径重算。"""
    qd = row.get("quote_date")
    if not qd or row.get("star") is None or row.get("pall") is None:
        return series
    try:
        d = date.fromisoformat(qd)
    except (TypeError, ValueError):
        return series
    if not series or d <= series[-1]["date"]:
        return series
    px = row.get("star_px")
    if px is None:
        return series
    prices = [x["px"] for x in series]
    prices2 = prices + [px]
    live = dict(series[-1])
    live.update({"date": d, "px": px, "pall": row["pall"],
                 "star": row["star"], "dd3": row.get("star_dd")})
    for w in (20, 60, 120, 250):
        live["ma%d" % w] = _avg(prices2[-w:]) if len(prices2) >= w else None
    for w in (60, 120, 250):
        a = live["ma%d" % w]
        j = len(series) - 20
        b = (_avg(prices[max(0, j - w + 1):j + 1])
             if j >= 0 and j + 1 >= w else None)
        live["ma%d_slope20" % w] = (a / b - 1.0) if (a and b) else None
    live["dd1y"] = (px / max(prices2[-250:]) - 1.0
                    if len(prices2) >= 60 else None)
    if all(live.get("ma%d" % w) for w in (20, 60, 120, 250)):
        score = (int(px > live["ma20"]) + int(live["ma20"] > live["ma60"])
                 + int(live["ma60"] > live["ma120"])
                 + int(live["ma120"] > live["ma250"]))
        live["trend_score"] = score
        live["trend"] = "强" if score >= 3 else ("弱" if score <= 1 else "中")
    else:
        live["trend_score"] = None
        live["trend"] = "?"
    live["val_star"] = (sb.valuation_star(row["pall"])
                        if row.get("pall") is not None else None)
    live["price_star"] = (sb.price_star(row["star_dd"])
                          if row.get("star_dd") is not None else None)
    return series + [live]


def _action_text(code):
    return {
        "T1": "4.8★ 建仓（首笔）",
        "T2": "加第二笔",
        "T3": "加第三笔",
        "S1": "卖出 1/3（≥60%）",
        "S2": "卖出剩余 2/3（≥63%）",
        "HOLD": "强趋势：剩余 2/3 暂不卖，转移动止盈",
        "TR": "移动止盈触发：清仓",
        "保险": "保险清仓",
    }.get(code, code or "")


def _current_action(row):
    pall = row.get("pall") or 0.0
    star = row.get("star") or 0.0
    if pall >= 0.63:
        return "卖出剩余 2/3（≥63%）"
    if pall >= 0.60:
        return "卖出 1/3（≥60%）"
    if star >= 4.8:
        return "可买 1/3（≥4.8★）"
    return "不买"


def _distance(row):
    if row.get("star") is not None and row["star"] >= 4.8:
        return "已到 4.8 星买点"
    bits = []
    pall = row.get("pall")
    if pall is not None:
        if pall <= 0.20:
            bits.append("估值分位 %s，已满足 ≤20%%" % _pct(pall))
        else:
            bits.append("估值分位 %s，需 ≤20%%" % _pct(pall))
    t1 = row.get("price_t1")
    px = row.get("px_display")
    gap = row.get("price_gap")
    if t1 is not None and px is not None:
        if gap is not None and gap <= 0:
            bits.append("价格 %s，已低于 4.8★ 门槛 %s" % (_px(px), _px(t1)))
        else:
            bits.append("价格 %s，需 ≤%s（还需跌 %s）" % (
                _px(px), _px(t1), _pct(gap)))
    return "；".join(bits)


def attach(row, key, weight_hs300):
    """把 v3 当前状态写入 row，供网页/推送直接使用。"""
    for name in ("v3_action", "v3_short", "v3_detail", "v3_stage",
                 "v3_t1_px", "v3_t1_date", "v3_t1_dd1y", "v3_t2_px",
                 "v3_t3_px", "v3_cycle_high", "v3_max_gain",
                 "v3_trail_line", "v3_strong", "v3_position",
                 "v3_today_action", "v3_today_code",
                 "v3_current_action", "v3_first_frac",
                 "v3_insurance_ready"):
        row[name] = None
    row["v3_action"] = ""
    row["v3_short"] = ""
    row["v3_detail"] = ""
    row["v3_current_action"] = _current_action(row)
    if key == "market":
        return

    try:
        series = rv.build_series(key)
    except Exception:  # noqa: BLE001
        return
    series = [x for x in series if x.get("pall") is not None and x.get("px")]
    if not series:
        return
    series = _append_live_row(series, row)
    try:
        res = rv.simulate(series, rule="bull-trail", trail=TRAIL, bull=BULL,
                          entry_mode="depth_trend", insurance_mode="trend",
                          insurance_gain=INSURANCE_GAIN,
                          insurance_dd=INSURANCE_DD,
                          catchup_days=CATCHUP_DAYS,
                          catchup_above_ma250=CATCHUP_ABOVE_MA250)
    except Exception:  # noqa: BLE001
        return

    last = series[-1]
    px, pall = last["px"], last["pall"]
    ma250 = last.get("ma250")
    ma250_slope = last.get("ma250_slope20")
    strong = bool(ma250 and px / ma250 - 1.0 >= BULL)
    row["v3_strong"] = strong

    trades = res.get("trades") or []
    last_trade = trades[-1] if trades else None
    today_trade = (last_trade if last_trade
                   and last_trade.get("date") == last["date"] else None)
    if today_trade:
        row["v3_today_code"] = today_trade.get("action")
        row["v3_today_action"] = _action_text(today_trade.get("action"))

    open_cycle = (res.get("shares") or 0) > 1e-9
    if not open_cycle:
        if today_trade:
            row["v3_action"] = _action_text(today_trade.get("action"))
            row["v3_short"] = row["v3_action"]
            row["v3_detail"] = "今日触发；已空仓，等下一个 4.8★。"
        elif row.get("star") is not None and row["star"] >= 4.8:
            d1 = last.get("dd1y")
            frac = 1.0 / 3.0
            if d1 is not None:
                if d1 <= -0.60:
                    frac = DEEP_FRAC
                elif d1 <= -0.40:
                    frac = MID_FRAC
            row["v3_first_frac"] = frac
            row["v3_action"] = ("4.8★ 建仓：首笔 %.0f%%（1年回撤 %s）"
                                % (frac * 100, _pct(d1)))
            row["v3_short"] = "4.8★建仓%.0f%%" % (frac * 100)
            row["v3_detail"] = (
                "修正星级 %.2f；1年回撤 %s；按深度首笔投 %.0f%%，"
                "剩余现金分给 T2/T3。" % (row["star"], _pct(d1), frac * 100))
        else:
            row["v3_action"] = "空仓等待 4.8★"
            row["v3_short"] = "空仓等待4.8★"
            row["v3_detail"] = _distance(row)
        return

    cyc = (res.get("cycles") or [])[-1]
    ctrades = cyc.get("trades") or []
    buys = [t for t in ctrades if t.get("action") in ("T1", "T2", "T3")]
    if not buys:
        return
    t1 = buys[0]
    t1_row = next((x for x in series if x["date"] == t1["date"]), None)
    t1_px = t1["px"]
    t1_dd1y = t1_row.get("dd1y") if t1_row else None
    stage = len(buys)
    s1_done = any(t.get("action") == "S1" for t in ctrades)
    trail_on = any(t.get("action") == "HOLD" for t in ctrades)
    total_amt = sum(t.get("amt") or 0.0 for t in buys)
    total_sh = sum(t.get("sh") or 0.0 for t in buys)
    avg_cost = (total_amt / total_sh) if total_sh else None
    cyc_rows = [x for x in series if x["date"] >= t1["date"]]
    cycle_high = max((x["px"] for x in cyc_rows), default=px)
    max_gain = (max((x["px"] / avg_cost - 1.0 for x in cyc_rows),
                    default=0.0) if avg_cost else 0.0)
    hold = next((t for t in ctrades if t.get("action") == "HOLD"), None)
    trail_high = (max((x["px"] for x in series
                       if hold and x["date"] >= hold["date"]), default=None)
                  if hold else None)
    trail_line = (trail_high * (1.0 - TRAIL)
                  if trail_high is not None else None)
    first_frac = 1.0 / 3.0
    if t1_dd1y is not None:
        if t1_dd1y <= -0.60:
            first_frac = DEEP_FRAC
        elif t1_dd1y <= -0.40:
            first_frac = MID_FRAC
    insurance_ready = bool(
        max_gain >= INSURANCE_GAIN and cycle_high > 0
        and px / cycle_high - 1.0 <= -INSURANCE_DD
        and ma250_slope is not None and ma250_slope > 0)

    row.update({
        "v3_stage": stage,
        "v3_t1_px": t1_px,
        "v3_t1_date": t1["date"].isoformat(),
        "v3_t1_dd1y": t1_dd1y,
        "v3_t2_px": t1_px * 0.90,
        "v3_t3_px": t1_px * 0.80,
        "v3_cycle_high": cycle_high,
        "v3_max_gain": max_gain,
        "v3_trail_line": trail_line,
        "v3_position": (total_amt / cyc["start_cash"]
                        if cyc.get("start_cash") else None),
        "v3_first_frac": first_frac,
        "v3_insurance_ready": insurance_ready,
    })

    if today_trade:
        code = today_trade.get("action")
        if code == "T1":
            row["v3_action"] = ("4.8★ 建仓：首笔 %.0f%%（1年回撤 %s）"
                                % (first_frac * 100, _pct(t1_dd1y)))
            row["v3_short"] = "4.8★建仓%.0f%%" % (first_frac * 100)
        else:
            row["v3_action"] = _action_text(code)
            row["v3_short"] = row["v3_action"]
    elif trail_on:
        if trail_line is not None and px <= trail_line:
            row["v3_action"] = "移动止盈触发：清仓"
            row["v3_short"] = "移动止盈触发"
        else:
            row["v3_action"] = ("强趋势：剩余 2/3 持有，移动止盈线 %s"
                                % _px(_display_px(row, trail_line)))
            row["v3_short"] = ("强趋势，持有；止盈线 %s"
                               % _px(_display_px(row, trail_line)))
    elif s1_done:
        if pall >= 0.63 and not strong:
            row["v3_action"] = "卖出剩余 2/3（≥63%）"
            row["v3_short"] = "卖剩余2/3"
        else:
            row["v3_action"] = "已卖 1/3；等 ≥63%（强趋势则转移动止盈）"
            row["v3_short"] = "已卖1/3，等63%"
    elif stage < 3:
        nxt = stage + 1
        if nxt == 2:
            row["v3_action"] = ("持有；等第二笔：≤%s，或趋势确认"
                                "（120 日线转上 / T1 后 60 天站上年线+8%%）"
                                % _px(_display_px(row, row["v3_t2_px"])))
            row["v3_short"] = "持有，等第二笔"
        else:
            row["v3_action"] = ("持有；等第三笔：≤%s，或 250 日线转上"
                                % _px(_display_px(row, row["v3_t3_px"])))
            row["v3_short"] = "持有，等第三笔"
    else:
        row["v3_action"] = "三笔已完成；等分位 ≥60% 卖 1/3"
        row["v3_short"] = "满仓，等60%"

    bits = [
        "T1 %s @%s（1年回撤 %s）" % (
            row["v3_t1_date"], _px(_display_px(row, t1_px)),
            _pct(t1_dd1y)),
        "首笔 %.0f%%；已投 %d/3 笔（%.0f%%）" % (
            first_frac * 100, stage, (row.get("v3_position") or 0.0) * 100),
        "T2 ≤%s / T3 ≤%s" % (
            _px(_display_px(row, row["v3_t2_px"])),
            _px(_display_px(row, row["v3_t3_px"]))),
        "本轮高点 %s，浮盈最高 %s" % (
            _px(_display_px(row, cycle_high)), _pct(max_gain)),
    ]
    if s1_done:
        bits.append("已卖 1/3")
    if trail_line is not None:
        bits.append("移动止盈线 %s（高点回撤12%%）"
                    % _px(_display_px(row, trail_line)))
    if ma250:
        bits.append("年线 %s（现价 %s）" % (
            _px(_display_px(row, ma250)),
            _pct(px / ma250 - 1.0, 1, True)))
    if insurance_ready and strong:
        bits.append("保险条件已满足，但强趋势中暂不触发")
    row["v3_detail"] = "；".join(bits) + "。"
