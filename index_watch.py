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
import json
import os
import sys
from datetime import date, datetime

import cycle_backtest as cb

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")

DEFAULT_KEYS = [("zz800", "中证800"), ("zz500", "中证500"), ("market", "全市场")]


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
    if key != "market" and s["star_high"]:
        row["price_t1"] = s["star_high"] * 0.74  # price_star 4.8 的位置
        row["price_gap"] = s["star_px"] / row["price_t1"] - 1.0
        row["val_gap"] = s["pall"] - 0.20
    else:
        row["price_t1"] = None
        row["price_gap"] = None
        row["val_gap"] = None
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
    if row["star"] >= 4.8:
        return "已到 4.8 星买点"
    parts = []
    parts.append("估值分位需 ≤20%%（现在 %.1f%%）" % (row["pall"] * 100))
    if row["price_t1"] is not None:
        parts.append("价格需 ≤%.0f（现在 %.0f，%+.1f%%）" % (
            row["price_t1"], row["star_px"], row["price_gap"] * 100))
    return "；".join(parts)


def markdown(rows, title_date=None):
    d = title_date or date.today().isoformat()
    lines = ["## 指数星级日报｜%s" % d, ""]
    verdict_bits = []
    for r in rows:
        if not r["tradeable"]:
            continue
        verdict_bits.append("%s **%.2f★** %s" % (
            r["name"], r["star"], "可买 1/3" if r["star"] >= 4.8 else "不买"))
    if verdict_bits:
        lines.append("**结论**：%s。" % "；".join(verdict_bits))
        lines.append("")
    lines.append("| 指数 | 修正星级 | 估值星级 | 价格星级 | 全历史分位 | 3年回撤 | PE | PB | 状态 |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---|")
    for r in rows:
        lines.append("| %s | **%.2f** | %.2f | %s | %.1f%% | %s | %.2f | %.2f | %s |" % (
            r["name"], r["star"], r["val_star"],
            "--" if r["price_star"] is None else "%.2f" % r["price_star"],
            r["pall"] * 100,
            "--" if r["star_dd"] is None else "%.1f%%" % (r["star_dd"] * 100),
            r["pe"], r["pb"], r["zone"]))
    lines.append("")
    lines.append("**怎么读**：修正星级 = min(估值星级, 价格星级)，只看该指数自己的历史，"
                 "不是全市场星级。4.8 星才是第一档买点；全市场那一行只作参考。")
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
    lines.append("数据：乐咕 PE/PB + 指数点位（中证800 为 75% 沪深300 + 25% 中证500 合成）。"
                 "仅为估值记录，不构成投资建议。")
    return "\n".join(lines)


def _esc(s):
    import html
    return html.escape("" if s is None else str(s))


def html(rows, title_date=None):
    d = title_date or date.today().isoformat()
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    data_dates = sorted({r["data_date"] for r in rows})
    data_date_text = data_dates[-1] if data_dates else d

    trs = []
    for r in rows:
        cls = "market" if not r["tradeable"] else ""
        price_star = "--" if r["price_star"] is None else "%.2f" % r["price_star"]
        dd = "--" if r["star_dd"] is None else "%.1f%%" % (r["star_dd"] * 100)
        trs.append(
            '<tr class="%s"><td class="name">%s</td>'
            '<td class="star">%.2f</td><td>%.2f</td><td>%s</td>'
            '<td>%.1f%%</td><td>%s</td><td>%.2f</td><td>%.2f</td>'
            '<td class="zone">%s</td></tr>' % (
                cls, _esc(r["name"]), r["star"], r["val_star"], price_star,
                r["pall"] * 100, dd, r["pe"], r["pb"], _esc(r["zone"])))

    distance = []
    for r in rows:
        distance.append('<li><b>%s</b>：%.2f 星。%s。</li>' % (
            _esc(r["name"]), r["star"], _esc(distance_text(r))))

    verdicts = []
    for r in rows:
        if not r["tradeable"]:
            continue
        if r["star"] >= 4.8:
            cls, text = "buy", "可以买第一档（1/3）"
            sub = "已同时满足估值和价格条件"
        elif r["star"] >= 4.5:
            cls, text = "watch", "接近买点，继续等"
            sub = "还差一点，别提前满仓"
        else:
            cls, text = "wait", "不买，继续等 4.8 星"
            sub = "估值分位 %.1f%%（需 ≤20%%）· 价格 %.0f（需 ≤%.0f）" % (
                r["pall"] * 100, r["star_px"], r["price_t1"])
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
.v-name{font-size:13px;color:var(--muted)}
.v-star{font-size:32px;font-weight:700;line-height:1.15;color:var(--text)}
.v-star span{font-size:16px;color:var(--accent);margin-left:2px}
.v-text{font-size:15px;font-weight:700;margin-top:2px}
.verdict.buy .v-text{color:var(--green)}
.verdict.watch .v-text{color:var(--amber)}
.verdict.wait .v-text{color:#475467}
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
h1{font-size:21px}
}
</style>
</head>
<body>
<main>
<h1>指数星级日报</h1>
<p class="meta">数据截至 %(data_date)s · 生成于 %(generated)s · 中证800 / 中证500 / 全市场</p>
<section class="verdicts">%(verdicts)s</section>
<section class="card">
<table>
<thead><tr><th>指数</th><th>修正星级</th><th>估值星级</th><th>价格星级</th>
<th>全历史分位</th><th>3年回撤</th><th>PE</th><th>PB</th><th>状态</th></tr></thead>
<tbody>%(trs)s</tbody>
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
<p class="footer">数据：乐咕 PE/PB + 指数点位；中证800 为 75%% 沪深300 + 25%% 中证500 合成。
仅为估值记录，不构成投资建议。</p>
</main>
</body>
</html>
""" % {
        "d": _esc(d),
        "data_date": _esc(data_date_text),
        "generated": _esc(generated),
        "verdicts": "".join(verdicts),
        "trs": "".join(trs),
        "distance": "".join(distance),
    }


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def push(rows):
    import datasource as ds
    try:
        config = load_config()
    except Exception as exc:  # noqa: BLE001
        print("读取 config.json 失败：%s" % exc, file=sys.stderr)
        return 1
    push_cfg = config.get("push") or {}
    if not push_cfg.get("enabled"):
        print("推送未启用：把 config.json 里 push.enabled 改成 true，并填 serverchan_key "
              "或 pushplus_token。")
        return 1
    main_row = next((r for r in rows if r["key"] == "zz800"), None)
    title = "指数星级｜%s" % (("中证800 %.2f★" % main_row["star"])
                             if main_row else date.today().isoformat())
    ok, msg = ds.send_push(config, title, markdown(rows))
    if ok:
        print("推送成功：%s" % title)
        return 0
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
                    help="通过 config.json 的 Server酱/PushPlus 推送")
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
        return push(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
