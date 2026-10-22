# -*- coding: utf-8 -*-
"""报告渲染：Markdown 摘要、自包含 HTML 面板、推送文案。"""
import html as htmlmod
from datetime import date


def esc(value):
    return htmlmod.escape("" if value is None else str(value))


def fmt_pct(value, digits=1, sign=False):
    if value is None:
        return "--"
    try:
        number = float(value) * 100
    except (TypeError, ValueError):
        return "--"
    pattern = ("%+." if sign else "%.") + str(digits) + "f%%"
    return pattern % number


def fmt_num(value, digits=4):
    if value is None:
        return "--"
    try:
        return ("%." + str(digits) + "f") % float(value)
    except (TypeError, ValueError):
        return "--"


def stars_html(count):
    if count is None:
        return '<span class="muted">未评级</span>'
    try:
        n = int(count)
    except (TypeError, ValueError):
        return '<span class="muted">未评级</span>'
    n = max(0, min(5, n))
    return ('<span class="stars" title="晨星评级 %d 星">%s%s</span>'
            '<span class="muted"> %d/5</span>' % (n, "★" * n, "☆" * (5 - n), n))


def stars_text(count):
    if count is None:
        return "--"
    try:
        n = int(count)
    except (TypeError, ValueError):
        return "--"
    n = max(0, min(5, n))
    return "★" * n + "☆" * (5 - n)


def zone_of(result, low_pct=0.20):
    if result.get("error"):
        return "error", "数据异常"
    state = result.get("state") or {}
    m = result.get("metrics") or {}
    zone = state.get("zone") or "normal"
    if zone == "extreme":
        return "extreme", "深度止盈区"
    if zone == "high":
        return "high", "止盈区"
    if zone == "dormant":
        return "dormant", "止盈后观望"
    val = m.get("val_pct")
    if val is not None and val <= low_pct:
        return "low", "低估区"
    navp = m.get("nav_pct_3y")
    if val is None and navp is not None and navp <= 0.15:
        return "low", "低位"
    return "normal", "正常"


def action_of(result, low_pct=0.20):
    signals = result.get("signals") or []
    if signals:
        return signals[0].get("action") or "有信号"
    state = result.get("state") or {}
    zone = state.get("zone") or "normal"
    if zone in ("high", "extreme"):
        return "持有，等待回撤止盈"
    if zone == "dormant":
        return "已止盈，等待下一轮低估"
    m = result.get("metrics") or {}
    val = m.get("val_pct")
    morningstar = m.get("morningstar")
    if val is not None and val <= low_pct:
        if morningstar is not None and morningstar <= 2:
            return "低估，但评级偏低：控制仓位"
        return "可分批买入"
    navp = m.get("nav_pct_3y")
    dd = m.get("dd_250")
    if val is None and navp is not None and navp <= 0.15 and dd is not None and dd <= -0.20:
        return "低位，可小额定投" if m.get("kind") == "active" else "低位，可分批买入"
    return "持有观察"


def _sparkline(values, width=180, height=42):
    values = [float(v) for v in (values or []) if v is not None]
    if len(values) < 2:
        return '<div class="spark-empty">走势数据不足</div>'
    # 最多取 160 个点，避免 SVG 太长
    if len(values) > 160:
        step = len(values) / 160.0
        values = [values[int(i * step)] for i in range(160)]
    low, high = min(values), max(values)
    span = high - low or 1.0
    pts = []
    for i, value in enumerate(values):
        x = i * width / float(len(values) - 1)
        y = height - 3 - (value - low) / span * (height - 6)
        pts.append("%.1f,%.1f" % (x, y))
    color = "#d05252" if values[-1] >= values[0] else "#2e9e6b"
    area = "0,%d %s %d,%d" % (height, " ".join(pts), width, height)
    return (
        '<svg class="spark" viewBox="0 0 %d %d" preserveAspectRatio="none" aria-hidden="true">'
        '<polygon points="%s" fill="%s" opacity="0.08"></polygon>'
        '<polyline points="%s" fill="none" stroke="%s" stroke-width="1.8" '
        'stroke-linejoin="round" stroke-linecap="round"></polyline></svg>'
    ) % (width, height, area, color, " ".join(pts), color)


def _bar(value, label, low_pct=0.20, high_pct=0.80):
    if value is None:
        return ('<div class="metric-row"><span class="metric-label">%s</span>'
                '<span class="muted">无数据</span></div>' % esc(label))
    try:
        pct = max(0.0, min(1.0, float(value)))
        low = max(0.0, min(1.0, float(low_pct)))
        high = max(low, min(1.0, float(high_pct)))
    except (TypeError, ValueError):
        return ""
    grad = ("background:linear-gradient(90deg,#2e9e6b 0 %.0f%%,#d8a63d %.0f%% %.0f%%,"
            "#c94a4a %.0f%% 100%%)" % (low * 100, low * 100, high * 100, high * 100))
    return (
        '<div class="metric-row"><span class="metric-label">%s</span>'
        '<div class="bar" style="%s"><i class="marker" style="left:%.1f%%"></i></div>'
        '<b class="metric-value">%.0f%%</b></div>'
    ) % (esc(label), grad, pct * 100, pct * 100)


def _sort_key(result, low_pct=0.20):
    if result.get("error"):
        return (9, 0)
    if result.get("signals"):
        return (0, 0)
    zone, _ = zone_of(result, low_pct)
    order = {"extreme": 1, "high": 2, "low": 3, "dormant": 4, "normal": 5}
    return (order.get(zone, 6), 0)


def dashboard_html(results, meta):
    meta = meta or {}
    th = meta.get("thresholds") or {}
    low_pct = float(th.get("buy_low_pct", 0.20))
    high_pct = float(th.get("sell_high_pct", 0.80))
    results = sorted(results or [], key=lambda r: _sort_key(r, low_pct))
    signal_count = meta.get("signal_count", 0)
    market_star = meta.get("market_star")
    star_text = "--" if market_star is None else "%.1f / 5" % market_star

    cards = []
    for result in results:
        if result.get("error"):
            fund = result.get("fund") or {}
            cards.append(
                '<section class="card zone-error">'
                '<div class="card-head"><h2>%s <span class="code">%s</span></h2>'
                '<span class="badge">数据异常</span></div>'
                '<p class="error-text">%s</p>'
                '<p class="hint">检查基金代码是否正确，或稍后重试。单个基金失败不影响其他基金。</p>'
                '</section>' % (esc(fund.get("alias") or fund.get("code")),
                                esc(fund.get("code")), esc(result.get("error")))
            )
            continue

        m = result.get("metrics") or {}
        state = result.get("state") or {}
        signals = result.get("signals") or []
        zone, zone_label = zone_of(result, low_pct)

        sig_html = ""
        if signals:
            items = []
            for sig in signals:
                items.append('<li><b>%s</b><span>%s</span></li>' % (
                    esc(sig.get("action")), esc(sig.get("reason"))))
            sig_html = '<div class="signal-box"><div class="signal-title">触发提醒</div><ul>%s</ul></div>' % (
                "".join(items))
        else:
            sig_html = '<div class="signal-box quiet"><div class="signal-title">当前动作</div><p>%s</p></div>' % esc(action_of(result, low_pct))

        val_value = m.get("val_pct")
        if val_value is None:
            val_value = m.get("nav_pct_3y")
            val_label = "净值 3 年分位"
        else:
            val_label = "估值分位"

        peak_dd = None
        if state.get("peak_nav") and m.get("level_nav"):
            peak_dd = m["level_nav"] / state["peak_nav"] - 1
        elif state.get("peak_index") and m.get("level_index"):
            peak_dd = m["level_index"] / state["peak_index"] - 1

        cost_html = ""
        if m.get("cost") is not None and m.get("shares") is not None and m.get("nav"):
            try:
                profit = (float(m["nav"]) - float(m["cost"])) * float(m["shares"])
                cost_html = ('<div class="metric-row"><span class="metric-label">持仓盈亏</span>'
                             '<b class="metric-value %s">%+.2f 元</b></div>' % (
                                 "up" if profit >= 0 else "down", profit))
            except (TypeError, ValueError):
                cost_html = ""

        cards.append("""
<section class="card zone-%(zone)s">
  <div class="card-head">
    <h2>%(alias)s <span class="code">%(code)s</span></h2>
    <span class="badge">%(zone_label)s</span>
  </div>
  <div class="nav-row">
    <div class="nav-value">%(nav)s <small>%(nav_date)s</small></div>
    <div class="chg %(chg_class)s">%(chg)s</div>
  </div>
  <div class="sub">%(name)s · %(ftype)s%(index_part)s</div>
  %(spark)s
  <div class="metrics">
    %(bar_val)s
    <div class="metric-row"><span class="metric-label">距 250 日高点</span>
      <b class="metric-value %(dd_class)s">%(dd250)s</b></div>
    <div class="metric-row"><span class="metric-label">距止盈区峰值</span>
      <b class="metric-value %(peak_class)s">%(peak_dd)s</b></div>
    <div class="metric-row"><span class="metric-label">晨星评级</span>
      <span class="metric-value">%(stars)s</span></div>
    <div class="metric-row"><span class="metric-label">止盈回撤阈值</span>
      <b class="metric-value">%(trailing)s</b></div>
    %(cost)s
  </div>
  %(signals)s
  <div class="card-foot">
    <span>状态：%(zone_label)s</span>
    <span>数据：%(source)s</span>
  </div>
</section>
""" % {
            "zone": zone,
            "zone_label": zone_label,
            "alias": esc(m.get("alias")),
            "code": esc(m.get("code")),
            "name": esc(m.get("name")),
            "ftype": esc(m.get("ftype")),
            "index_part": (" · 跟踪 %s" % esc(m.get("index_name"))) if m.get("index_name") else "",
            "nav": fmt_num(m.get("nav")),
            "nav_date": esc(m.get("nav_date")),
            "chg": fmt_pct((m.get("day_change") or 0) / 100.0, 2, sign=True)
                   if m.get("day_change") is not None else "--",
            "chg_class": "up" if (m.get("day_change") or 0) >= 0 else "down",
            "spark": _sparkline(m.get("spark")),
            "bar_val": _bar(val_value, val_label, low_pct, high_pct),
            "dd250": fmt_pct(m.get("dd_250"), 1, sign=True),
            "dd_class": "down" if (m.get("dd_250") or 0) < 0 else "up",
            "peak_dd": fmt_pct(peak_dd, 1, sign=True),
            "peak_class": "down" if (peak_dd or 0) < 0 else "up",
            "stars": stars_html(m.get("morningstar")),
            "trailing": fmt_pct(m.get("trailing_drawdown"), 0),
            "cost": cost_html,
            "signals": sig_html,
            "source": esc(m.get("val_source") or "天天基金"),
        })

    market_note = "5 星最便宜（螺丝钉口径）" if market_star is not None else "未解析到公众号星级"
    return """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>基金监控面板 · %(date)s</title>
<style>
  :root {
    --bg:#f4f6f8; --card:#ffffff; --text:#1d2530; --muted:#6b7785;
    --line:#e3e8ee; --green:#2e9e6b; --amber:#c58b2a; --red:#c94a4a;
    --blue:#3a6ea5; --shadow:0 1px 2px rgba(16,24,40,.04),0 8px 24px rgba(16,24,40,.06);
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
    font-family:"Microsoft YaHei","PingFang SC","Segoe UI",system-ui,sans-serif;
    -webkit-font-smoothing:antialiased; }
  .wrap { max-width:1320px; margin:0 auto; padding:24px 20px 48px; }
  header { display:flex; flex-wrap:wrap; align-items:flex-end; justify-content:space-between;
    gap:14px; margin-bottom:20px; }
  h1 { font-size:22px; margin:0 0 6px; letter-spacing:.5px; }
  .meta { color:var(--muted); font-size:13px; line-height:1.7; }
  .market { display:inline-flex; align-items:baseline; gap:6px; background:#fff;
    border:1px solid var(--line); border-radius:999px; padding:8px 14px; box-shadow:var(--shadow); }
  .market b { font-size:20px; color:var(--blue); }
  .market small { color:var(--muted); font-size:12px; }
  .watch-card { margin-bottom:18px; border-top-color:var(--blue); overflow-x:auto; }
  .watch-title { font-size:14px; font-weight:700; color:#475467; margin-bottom:8px; }
  .watch-title span { font-size:11px; font-weight:400; color:var(--muted); margin-left:8px; }
  .watch-table { width:100%%; border-collapse:collapse; font-size:13px; }
  .watch-table th, .watch-table td { padding:7px 6px; border-bottom:1px solid var(--line);
    text-align:right; white-space:nowrap; }
  .watch-table th:first-child, .watch-table td:first-child { text-align:left; }
  .watch-table th { color:var(--muted); font-weight:600; font-size:11px; }
  .watch-star { font-size:16px; font-weight:700; color:var(--blue); }
  .watch-zone { text-align:left; white-space:normal; }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(330px,1fr)); gap:16px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:14px;
    padding:18px; box-shadow:var(--shadow); border-top:4px solid var(--line); }
  .card.zone-low { border-top-color:var(--green); }
  .card.zone-normal { border-top-color:#9aa7b6; }
  .card.zone-high { border-top-color:var(--amber); }
  .card.zone-extreme { border-top-color:var(--red); }
  .card.zone-dormant { border-top-color:#667085; }
  .card.zone-error { border-top-color:#98a2b3; }
  .card-head { display:flex; justify-content:space-between; align-items:flex-start; gap:10px; }
  .card-head h2 { font-size:16px; margin:0; font-weight:700; line-height:1.4; }
  .code { font-size:12px; color:var(--muted); font-weight:400; margin-left:6px; }
  .badge { font-size:12px; padding:3px 9px; border-radius:999px; background:#eef2f6;
    color:#475467; white-space:nowrap; }
  .zone-low .badge { background:#e7f6ee; color:#1c7a50; }
  .zone-high .badge { background:#fdf3e2; color:#9a6717; }
  .zone-extreme .badge { background:#fdeaea; color:#b13737; }
  .zone-dormant .badge { background:#eef0f3; color:#4b5563; }
  .nav-row { display:flex; align-items:baseline; justify-content:space-between; margin-top:12px; }
  .nav-value { font-size:26px; font-weight:700; font-variant-numeric:tabular-nums; }
  .nav-value small { font-size:12px; color:var(--muted); font-weight:400; margin-left:6px; }
  .chg { font-size:15px; font-weight:700; font-variant-numeric:tabular-nums; }
  .up { color:#c94a4a; }
  .down { color:#2e9e6b; }
  .sub { font-size:12px; color:var(--muted); margin-top:4px; line-height:1.5; }
  .spark { width:100%%; height:44px; margin:10px 0 4px; display:block; }
  .spark-empty { color:var(--muted); font-size:12px; padding:14px 0; }
  .metrics { border-top:1px dashed var(--line); margin-top:8px; padding-top:10px; }
  .metric-row { display:flex; align-items:center; gap:10px; padding:5px 0; font-size:13px; }
  .metric-label { color:var(--muted); width:112px; flex:none; }
  .metric-value { margin-left:auto; font-variant-numeric:tabular-nums; font-weight:700; }
  .bar { flex:1; height:8px; border-radius:4px; position:relative;
    background:linear-gradient(90deg,#2e9e6b 0 20%%,#d8a63d 20%% 80%%,#c94a4a 80%% 100%%); }
  .marker { position:absolute; top:-3px; width:4px; height:14px; border-radius:2px;
    background:#1d2530; box-shadow:0 0 0 2px #fff; transform:translateX(-2px); }
  .signal-box { margin-top:12px; border:1px solid var(--line); border-radius:10px;
    padding:10px 12px; background:#fafbfc; }
  .signal-box.quiet { background:#fbfcfd; }
  .signal-box ul { margin:6px 0 0; padding-left:18px; }
  .signal-box li { margin:4px 0; font-size:13px; line-height:1.55; }
  .signal-box li span { display:block; color:var(--muted); font-size:12px; margin-top:2px; }
  .signal-title { font-size:12px; font-weight:700; color:#475467; letter-spacing:.5px; }
  .signal-box p { margin:6px 0 0; font-size:13px; }
  .card-foot { display:flex; justify-content:space-between; gap:10px; color:var(--muted);
    font-size:11px; margin-top:12px; }
  .muted { color:var(--muted); }
  .stars { color:#c58b2a; letter-spacing:1px; }
  .error-text { color:#b13737; font-size:13px; line-height:1.6; }
  .hint { color:var(--muted); font-size:12px; line-height:1.6; }
  footer { margin-top:26px; color:var(--muted); font-size:12px; line-height:1.8; }
  @media (max-width:520px) { .wrap { padding:16px 12px 36px; } .metric-label { width:96px; } }
  @media print { body { background:#fff; } .card { box-shadow:none; } }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>基金监控面板</h1>
      <div class="meta">%(date)s · 生成于 %(generated)s<br>
        基金 %(fund_count)d 只 · 新信号 %(signal_count)d 条 · 数据源 %(source)s</div>
    </div>
    <div class="market"><b>%(market_star)s</b><small>市场星级<br>%(market_note)s</small></div>
  </header>
  <section class="card watch-card">%(index_watch)s</section>
  <main class="grid">
  %(cards)s
  </main>
  <footer>
    规则口径：估值分位越低越便宜；进入止盈区提醒减仓约 1/3，深度止盈区再减 1/3，
    从止盈区峰值回撤达到阈值提醒卖出剩余（回撤止盈，不预测顶部）。<br>
    “市场星级”来自公众号文章，5 星最便宜、1 星最贵；“晨星评级”是基金质量评级，5 星最优，
    两者方向不同，不互为买卖信号。<br>
    本工具只做规则化提醒，不自动交易，不构成投资建议。数据来自天天基金、蛋卷、晨星，
    可能存在延迟或错误。
  </footer>
</div>
</body>
</html>
""" % {
        "date": esc(meta.get("date")),
        "generated": esc(meta.get("generated_at")),
        "fund_count": meta.get("fund_count", 0),
        "signal_count": signal_count,
        "source": esc(meta.get("data_source") or "天天基金"),
        "market_star": esc(star_text),
        "market_note": esc(market_note),
        "index_watch": index_watch_html(meta),
        "cards": "\n".join(cards),
    }


def _table_rows(results, low_pct=0.20):
    lines = []
    for result in results:
        if result.get("error"):
            fund = result.get("fund") or {}
            lines.append("| %s | %s | -- | -- | -- | -- | -- | 数据异常 | %s |" % (
                _md(fund.get("alias") or fund.get("code")), _md(fund.get("code")),
                _md(result.get("error"))))
            continue
        m = result.get("metrics") or {}
        state = result.get("state") or {}
        zone, zone_label = zone_of(result, low_pct)
        val = m.get("val_pct")
        val_text = fmt_pct(val, 0) if val is not None else fmt_pct(m.get("nav_pct_3y"), 0)
        val_label = "" if val is not None else "（净值分位）"
        peak_dd = None
        if state.get("peak_nav") and m.get("level_nav"):
            peak_dd = m["level_nav"] / state["peak_nav"] - 1
        lines.append("| %s | %s | %s | %s | %s%s | %s | %s | %s | %s |" % (
            _md(m.get("alias")), _md(m.get("code")), fmt_num(m.get("nav")),
            fmt_pct((m.get("day_change") or 0) / 100.0, 2, sign=True)
            if m.get("day_change") is not None else "--",
            val_text, val_label,
            fmt_pct(m.get("dd_250"), 1, sign=True),
            stars_text(m.get("morningstar")),
            zone_label, _md(action_of(result))))
    return "\n".join(lines)


def _md(text):
    return ("" if text is None else str(text)).replace("|", "\\|").replace("\n", " ")


def _signal_lines(signals):
    if not signals:
        return "无新的买卖信号。"
    out = []
    for sig in signals:
        out.append("- **%s %s**：%s\n  - %s" % (
            sig.get("alias") or "", sig.get("code") or "",
            sig.get("action") or "", sig.get("reason") or ""))
    return "\n".join(out)


def index_watch_markdown(meta):
    """日报里的指数星级表（中证800/中证500/全市场并排）。"""
    rows = (meta or {}).get("index_watch") or []
    if not rows:
        return "暂无指数星级数据。"
    lines = ["| 指数 | 修正星级 | 估值星级 | 价格星级 | 全历史分位 | 3年回撤 | 状态 |",
             "|---|---:|---:|---:|---:|---:|---|"]
    for r in rows:
        price_star = "--" if r.get("price_star") is None else "%.2f" % r["price_star"]
        dd = "--" if r.get("star_dd") is None else fmt_pct(r["star_dd"], 1, sign=True)
        lines.append("| %s | **%.2f** | %.2f | %s | %s | %s | %s |" % (
            _md(r.get("name")), r.get("star", 0), r.get("val_star", 0),
            price_star, fmt_pct(r.get("pall"), 1), dd, _md(r.get("zone"))))
    lines.append("")
    lines.append("> 修正星级 = min(估值星级, 价格星级)，只看该指数自己的历史；"
                 "全市场仅估值，作参考。4.8 星才是第一档买点。")
    lines.append(">")
    lines.append("> 买入：4.8 星买 1/3，价格再跌 10% / 20% 各买 1/3。"
                 "卖出：全历史分位 ≥60% 卖 1/3，≥63% 卖剩余 2/3。"
                 "保险：浮盈最高 ≥45% 后，从本轮高点回撤 10% 清仓（S1 前）。")
    return "\n".join(lines)


def index_watch_html(meta):
    rows = (meta or {}).get("index_watch") or []
    if not rows:
        return ""
    trs = []
    for r in rows:
        price_star = "--" if r.get("price_star") is None else "%.2f" % r["price_star"]
        dd = "--" if r.get("star_dd") is None else fmt_pct(r["star_dd"], 1, sign=True)
        trs.append(
            '<tr><td>%s</td><td class="watch-star">%.2f</td><td>%.2f</td>'
            '<td>%s</td><td>%s</td><td>%s</td><td class="watch-zone">%s</td></tr>' % (
                esc(r.get("name")), r.get("star", 0), r.get("val_star", 0),
                price_star, fmt_pct(r.get("pall"), 1), dd, esc(r.get("zone"))))
    return (
        '<div class="watch-title">指数星级 <span>指数自己的口径，不是全市场星级</span></div>'
        '<table class="watch-table"><thead><tr><th>指数</th><th>修正星级</th>'
        '<th>估值星级</th><th>价格星级</th><th>全历史分位</th><th>3年回撤</th>'
        '<th>状态</th></tr></thead><tbody>%s</tbody></table>' % "".join(trs)
    ) + ('<div class="hint" style="margin-top:10px">买入：4.8 星买 1/3，'
         '价格再跌 10% / 20% 各买 1/3；卖出：全历史分位 ≥60% 卖 1/3，'
         '≥63% 卖剩余 2/3；保险：浮盈最高 ≥45% 后回撤 10% 清仓（S1 前）。</div>')


def daily_markdown(results, meta):
    meta = meta or {}
    th = meta.get("thresholds") or {}
    low_pct = float(th.get("buy_low_pct", 0.20))
    deep_pct = float(th.get("buy_deep_pct", 0.10))
    sell_high = float(th.get("sell_high_pct", 0.80))
    sell_extreme = float(th.get("sell_extreme_pct", 0.90))
    market_star = meta.get("market_star")
    market_line = ("市场星级 %.1f / 5（越高越便宜）" % market_star
                   if market_star is not None else "市场星级：未从公众号解析到")
    signals = []
    for result in results or []:
        signals.extend(result.get("signals") or [])
    return """# 基金监控日报 %(date)s

> %(market)s ｜ 新信号 %(count)d 条 ｜ 数据源：%(source)s

## 指数星级

%(index_watch)s

## 信号

%(signals)s

## 基金状态

| 基金 | 代码 | 净值 | 日涨跌 | 估值分位 | 距250日高点 | 晨星 | 状态 | 动作 |
|---|---|---|---|---|---|---|---|---|
%(rows)s

## 规则说明

- 买入：指数估值分位 ≤%(buy_low)s 提示分批买入，≤%(buy_deep)s 提示加大定投；主动基金用净值位置代替估值。
- 卖出：进入止盈区（≥%(sell_high)s）减仓约 1/3，深度止盈区（≥%(sell_extreme)s）再减 1/3；从止盈区峰值回撤达到阈值（宽基 8%% / 行业 10%% / 主动 12%%）卖出剩余。
- 市场星级（公众号，5 星最便宜）和晨星评级（5 星最优质）方向不同，不互为买卖信号。
- 只提醒，不自动交易；不构成投资建议。

详细面板：`reports/dashboard.html`
""" % {
        "date": meta.get("date") or date.today().isoformat(),
        "market": market_line,
        "count": len(signals),
        "source": meta.get("data_source") or "天天基金",
        "index_watch": index_watch_markdown(meta),
        "signals": _signal_lines(signals),
        "rows": _table_rows(results or [], low_pct),
        "buy_low": "%.0f%%" % (low_pct * 100),
        "buy_deep": "%.0f%%" % (deep_pct * 100),
        "sell_high": "%.0f%%" % (sell_high * 100),
        "sell_extreme": "%.0f%%" % (sell_extreme * 100),
    }


def weekly_markdown(results, meta):
    meta = meta or {}
    th = meta.get("thresholds") or {}
    low_pct = float(th.get("buy_low_pct", 0.20))
    market_star = meta.get("market_star")
    market_line = ("市场星级 %.1f / 5（越高越便宜）" % market_star
                   if market_star is not None else "市场星级：未解析")
    week_lines = []
    watch = []
    for result in results or []:
        if result.get("error"):
            continue
        m = result.get("metrics") or {}
        zone, zone_label = zone_of(result, low_pct)
        week_lines.append("| %s | %s | %s | %s | %s | %s | %s |" % (
            _md(m.get("alias")), _md(m.get("code")),
            fmt_num(m.get("nav")), fmt_pct(m.get("ret_5"), 2, sign=True),
            fmt_pct(m.get("val_pct"), 0) if m.get("val_pct") is not None
            else fmt_pct(m.get("nav_pct_3y"), 0),
            zone_label, _md(action_of(result, low_pct))))
        if zone in ("high", "extreme"):
            watch.append("%s 处于%s，关注回撤止盈" % (m.get("alias"), zone_label))
        elif zone == "low":
            watch.append("%s 处于低估/低位，可按计划分批" % m.get("alias"))
    watch_text = "\n".join("- " + w for w in watch) if watch else "- 本周没有需要特别操作的基金。"
    return """# 基金周报 %(date)s

> %(market)s ｜ 本周涨跌按近 5 个交易日计算

## 本周关注

%(watch)s

## 持仓状态

| 基金 | 代码 | 最新净值 | 本周涨跌 | 估值/净值分位 | 状态 | 动作 |
|---|---|---|---|---|---|---|
%(rows)s

## 说明

- 周报只汇总状态；真正的买卖提醒会在触发当天即时推送。
- 估值分位是后视镜，低估可能更低、高估可能更高，按纪律分批执行。
- 不构成投资建议。
""" % {
        "date": meta.get("date") or date.today().isoformat(),
        "market": market_line,
        "watch": watch_text,
        "rows": "\n".join(week_lines),
    }


def notification_title(signals):
    if not signals:
        return "基金监控"
    if len(signals) == 1:
        sig = signals[0]
        tier = (" " + sig.get("tier")) if sig.get("tier") else ""
        return "基金提醒｜%s%s" % (sig.get("alias") or sig.get("code"), tier)
    return "基金提醒｜%d 条信号" % len(signals)


def notification_markdown(signals, results, meta):
    meta = meta or {}
    lines = []
    if meta.get("market_star") is not None:
        lines.append("市场星级 **%.1f / 5**（越高越便宜）" % meta["market_star"])
    lines.append("")
    for sig in signals:
        lines.append("### %s %s" % (sig.get("alias") or "", sig.get("code") or ""))
        lines.append("**%s**" % (sig.get("action") or ""))
        lines.append("")
        lines.append(sig.get("reason") or "")
        lines.append("")
        lines.append("净值 %s（%s） ｜ 估值/净值分位 %s ｜ 晨星 %s" % (
            fmt_num(sig.get("nav")), sig.get("nav_date") or "--",
            fmt_pct(sig.get("val_pct"), 0) if sig.get("val_pct") is not None
            else fmt_pct(sig.get("nav_pct_3y"), 0),
            "--" if sig.get("morningstar") is None else "%d 星" % sig["morningstar"]))
        lines.append("")
    lines.append("---")
    lines.append("只提醒，不自动交易。详细面板：reports/dashboard.html")
    return "\n".join(lines)
