#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""指数星级日报：中证800 / 中证500 / 全市场。

星级只看指数自己的历史，不是螺丝钉的全市场星级：
  修正星级 = min(估值星级, 价格星级)
  估值星级：全历史 PE/PB 综合分位 10/20/40/60/80/100%
            -> 5.0/4.8/4.0/3.0/2.0/1.0
  价格星级：相对过去 3 年最高点的回撤 <=5/10/20/30%
            -> 3.8/4.2/4.5/5.0
  4.8 星 = 估值分位 <=20% 且价格距 3 年高点 <= -26%
  全市场没有统一价格指数，所以全市场只算估值星级，作参考。

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

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
ALERT_STATE_PATH = os.path.join(BASE, "data", "alert_state.json")
PAGE_URL = (os.environ.get("INDEX_PAGE_URL")
            or "https://guanghuipeng.github.io/fund-monitor/")

DEFAULT_KEYS = [("zz800", "中证800"), ("zz500", "中证500"), ("market", "全市场")]


QUOTE_SECIDS = {"zz800": "1.000906", "zz500": "1.000905"}
QUOTE_TENCENT = {"zz800": "sh000906", "zz500": "sh000905"}
CN_TZ = timezone(timedelta(hours=8))


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
        with urllib.request.urlopen(req, timeout=10) as resp:
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
        with urllib.request.urlopen(req, timeout=10) as resp:
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


def fetch_quote(key):
    """实时/收盘行情：腾讯优先，失败再试东方财富；都失败返回 None。"""
    return _quote_tencent(key) or _quote_eastmoney(key)


def _valuation_percentile(pe, pb, key, weight_hs300):
    rows = cb._load_raw_rows(key, weight_hs300)
    pes = sorted(float(r["pe_ttm"]) for r in rows)
    pbs = sorted(float(r["pb"]) for r in rows)
    return (0.6 * bisect.bisect_right(pes, pe) / float(len(pes))
            + 0.4 * bisect.bisect_right(pbs, pb) / float(len(pbs)))


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
    if key != "market" and row.get("high_display"):
        row["price_t1"] = row["high_display"] * 0.74  # price_star 4.8 的位置
        row["price_gap"] = row["px_display"] / row["price_t1"] - 1.0
        row["val_gap"] = row["pall"] - 0.20
    else:
        row["price_t1"] = None
        row["price_gap"] = None
        row["val_gap"] = None
    _attach_trend(row, key, quote, weight_hs300)
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


def distance_text(row):
    if not row["tradeable"]:
        return "全市场只作参考，不可直接买"
    suffix = ("（%s）" % row["estimate_label"]) if row.get("estimate") else ""
    if row["star"] >= 4.8:
        return "已到 4.8 星买点" + suffix
    parts = []
    parts.append("估值分位需 ≤20%%（现在 %.1f%%）" % (row["pall"] * 100))
    if row["price_t1"] is not None:
        parts.append("价格需 ≤%.0f（现在 %.0f，%+.1f%%）" % (
            row["price_t1"], row["px_display"], row["price_gap"] * 100))
    return "；".join(parts) + suffix


def markdown(rows, title_date=None):
    d = title_date or datetime.now(CN_TZ).date().isoformat()
    lines = ["## 指数星级日报｜%s" % d, ""]
    verdict_bits = []
    for r in rows:
        if not r["tradeable"]:
            continue
        if r["pall"] >= 0.63:
            action = "**卖出剩余 2/3**（≥63%%）"
        elif r["pall"] >= 0.60:
            action = "**卖出 1/3**（≥60%%）"
        elif r["star"] >= 4.8:
            action = "可买 1/3（≥4.8★）"
        else:
            action = "不买"
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
                 "不是全市场星级。4.8 星才是第一档买点；全市场那一行只作参考。")
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
    lines.append("### 规则")
    lines.append("- 买入：4.8 星买 1/3；价格再跌 10% 买 1/3；再跌 20% 买最后 1/3。")
    lines.append("- 卖出：全历史分位 ≥60% 卖 1/3；≥63% 卖剩余 2/3。")
    lines.append("- 保险：浮盈最高 ≥45% 后，从本轮高点回撤 10% 清仓（S1 前）。")
    lines.append("")
    lines.append("数据：乐咕 PE/PB + 腾讯/东方财富行情；中证800 的 PE/PB 为 75% 沪深300 + "
                 "25% 中证500 合成，价格优先用中证800 实际点位。仅为估值记录，不构成投资建议。")
    return "\n".join(lines)


def _esc(s):
    import html
    return html.escape("" if s is None else str(s))


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


def html(rows, title_date=None):
    d = title_date or datetime.now(CN_TZ).date().isoformat()
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

    verdicts = []
    for r in rows:
        if not r["tradeable"]:
            continue
        if r["pall"] >= 0.63:
            cls, text = "sell", "卖出剩余 2/3（≥63%）"
            sub = "估值分位 %.1f%%，进入第二档止盈区" % (r["pall"] * 100)
        elif r["pall"] >= 0.60:
            cls, text = "sell", "卖出 1/3（≥60%）"
            sub = "估值分位 %.1f%%，进入第一档止盈区" % (r["pall"] * 100)
        elif r["star"] >= 4.8:
            cls, text = "buy", "可以买第一档（1/3）"
            sub = "已同时满足估值和价格条件"
        elif r["star"] >= 4.5:
            cls, text = "watch", "接近买点，继续等"
            sub = "还差一点，别提前满仓"
        else:
            cls, text = "wait", "不买，继续等 4.8 星"
            sub = "估值分位 %.1f%%（需 ≤20%%）· 价格 %.0f（需 ≤%.0f）" % (
                r["pall"] * 100, r["px_display"], r["price_t1"])
        if r.get("estimate"):
            sub += " · " + r["estimate_label"]
        verdicts.append(
            '<div class="verdict %s"><div class="v-name">%s</div>'
            '<div class="v-star">%.2f<span>★</span></div>'
            '<div class="v-text">%s</div><div class="v-sub">%s</div></div>' % (
                cls, _esc(r["name"]), r["star"], text, _esc(sub)))

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
<p class="meta">估值数据截至 %(data_date)s%(quote_text)s%(estimate_text)s · 生成于 %(generated)s · 中证800 / 中证500 / 全市场</p>
<section class="verdicts">%(verdicts)s</section>
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
<section class="card">
<h2>距离 4.8 星买点</h2>
<ul>%(distance)s</ul>
</section>
<section class="card rule">
<h2>怎么读</h2>
<p>修正星级 = min(估值星级, 价格星级)，只看该指数自己的历史，<b>不是全市场星级</b>。
4.8 星才是第一档买点：估值分位 ≤20%%，且价格距 3 年高点 ≤-26%%。</p>
<p>买入：4.8 星买 1/3；价格再跌 10%% 买 1/3；再跌 20%% 买最后 1/3。<br>
卖出：全历史分位 ≥60%% 卖 1/3；≥63%% 卖剩余 2/3。<br>
保险：浮盈最高 ≥45%% 后，从本轮高点回撤 10%% 清仓（S1 前）。</p>
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
        "trs": "".join(trs),
        "trends": "".join(trend_rows),
        "distance": "".join(distance),
    }


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}  # GitHub Actions 里没有 config.json，推送配置走环境变量


def _alert_signals(rows):
    """当天触发的买/卖档位。信号消失后会自动从状态里清掉，下次重新触发再提醒。"""
    out = {}
    for r in rows:
        key = r.get("key")
        if key == "market" or r.get("star") is None:
            continue
        star = r["star"]
        pall = r.get("pall") or 0.0
        px = r.get("px_display")
        t1 = r.get("price_t1")
        if star >= 4.8:
            out[key + ":buy48"] = "4.8星建仓"
        if t1 and px:
            if px <= t1 * 0.80:
                out[key + ":buy80"] = "价格≤T1×0.80（第三笔）"
            elif px <= t1 * 0.90:
                out[key + ":buy90"] = "价格≤T1×0.90（第二笔）"
        if pall >= 0.63:
            out[key + ":sell63"] = "分位≥63%（卖剩余）"
        elif pall >= 0.60:
            out[key + ":sell60"] = "分位≥60%（卖1/3）"
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
        pall = r.get("pall") or 0.0
        if pall >= 0.63:
            action = "卖出剩余 2/3"
        elif pall >= 0.60:
            action = "卖出 1/3"
        elif (r.get("star") or 0) >= 4.8:
            action = "可买 1/3"
        else:
            action = "不买，等待"
        items.append((r, action))
    return items


def _fresh_desc(fresh):
    names = {"zz800": "中证800", "zz500": "中证500"}
    return "；".join("%s %s" % (names.get(k.split(":")[0], k), v)
                    for k, v in sorted(fresh.items()))


def push_brief(rows, fresh=None):
    """推送只保留关键信息，详细走势让用户点链接看网页。"""
    today = datetime.now(CN_TZ).strftime("%Y-%m-%d")
    lines = ["**指数星级｜%s**" % today]
    if fresh:
        lines.append("新增信号：%s" % _fresh_desc(fresh))
    lines.append("")
    for r, action in _brief_items(rows):
        pall = r.get("pall") or 0.0
        vtxt = "估值分位 %s（%s）" % (
            _fmt_pct(pall), "卖点≥60%" if pall >= 0.60 else "买点≤20%")
        price = "点位 %s" % _fmt_px(r.get("px_display"))
        if r.get("price_t1"):
            price += "（T1 %s）" % _fmt_px(r["price_t1"])
        est = "｜%s" % r["estimate_label"] if r.get("estimate_label") else ""
        lines.append("- **%s %.2f★**｜%s｜今日 %s｜%s｜%s%s" % (
            r["name"], r["star"], action, _fmt_pct(r.get("chg_pct"), 2, sign=True),
            vtxt, price, est))
    lines += ["", "完整走势图：[打开网页](%s)" % PAGE_URL]
    return "\n".join(lines)


def push_brief_html(rows, fresh=None):
    import html as htmlmod
    today = datetime.now(CN_TZ).strftime("%Y-%m-%d")
    parts = ["<h3>指数星级｜%s</h3>" % today]
    if fresh:
        parts.append("<p><b>新增信号</b>：%s</p>" % htmlmod.escape(_fresh_desc(fresh)))
    parts.append("<ul>")
    for r, action in _brief_items(rows):
        pall = r.get("pall") or 0.0
        vtxt = "估值分位 %s（%s）" % (
            _fmt_pct(pall), "卖点≥60%" if pall >= 0.60 else "买点≤20%")
        price = "点位 %s" % _fmt_px(r.get("px_display"))
        if r.get("price_t1"):
            price += "（T1 %s）" % _fmt_px(r["price_t1"])
        est = "｜%s" % r["estimate_label"] if r.get("estimate_label") else ""
        parts.append("<li><b>%s %.2f★</b>｜%s｜今日 %s｜%s｜%s%s</li>" % (
            htmlmod.escape(r["name"]), r["star"], action,
            _fmt_pct(r.get("chg_pct"), 2, sign=True), vtxt, price, est))
    parts.append("</ul>")
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
    ap = argparse.ArgumentParser(description="中证800/中证500/全市场 指数星级日报")
    ap.add_argument("--index", default="all",
                    choices=["all", "zz800", "zz500", "market"])
    ap.add_argument("--weight-hs300", type=float, default=0.75,
                    help="合成中证800时沪深300的市值权重，默认 0.75")
    ap.add_argument("--refresh", action="store_true",
                    help="先联网刷新指数估值历史（失败则继续用缓存）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
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
        parent = os.path.dirname(os.path.abspath(args.html))
        os.makedirs(parent, exist_ok=True)
        with open(args.html, "w", encoding="utf-8") as fh:
            fh.write(html(rows))
        print("HTML 已写入：%s" % args.html)

    if args.push:
        return push(rows, signal_only=args.signal_only, daily=args.daily)
    return 0


if __name__ == "__main__":
    sys.exit(main())
