# -*- coding: utf-8 -*-
"""数据抓取与解析：天天基金、蛋卷估值、晨星评级、微信文章。

只用 Python 标准库，不依赖 requests/pandas。
"""
import gzip
import hashlib
import html as htmlmod
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, date

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
BEIJING_OFFSET = timedelta(hours=8)


class FetchError(Exception):
    pass


# ---------------------------------------------------------------------------
# HTTP / 缓存
# ---------------------------------------------------------------------------

def _safe_key(key):
    key = re.sub(r"[^0-9A-Za-z_.-]+", "_", key or "")
    if len(key) > 120:
        key = hashlib.md5(key.encode("utf-8")).hexdigest()
    return key


def _cache_path(cache_dir, url, cache_key):
    if not cache_dir:
        return None
    os.makedirs(cache_dir, exist_ok=True)
    name = cache_key or hashlib.md5(url.encode("utf-8")).hexdigest()
    return os.path.join(cache_dir, _safe_key(name) + ".cache")


def _decode(raw):
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="ignore")


def http_get_text(url, headers=None, timeout=20, retries=2, cache_dir=None,
                  cache_key=None, max_age_hours=6, offline=False, proxy=None):
    """带磁盘缓存和重试的 GET，返回 str。失败时尽量回退到旧缓存。"""
    path = _cache_path(cache_dir, url, cache_key)
    if path and os.path.exists(path):
        age = time.time() - os.path.getmtime(path)
        if offline or age < max_age_hours * 3600:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()

    if offline:
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        raise FetchError("离线模式且没有缓存：%s" % url)

    hdrs = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
        "Accept-Encoding": "gzip",
    }
    if headers:
        hdrs.update(headers)

    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            handlers = [urllib.request.ProxyHandler({})]  # 默认直连，避免系统代理干扰
            if proxy:
                handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})]
            opener = urllib.request.build_opener(*handlers)
            with opener.open(req, timeout=timeout) as resp:
                raw = resp.read()
                enc = (resp.headers.get("Content-Encoding") or "").lower()
            if enc == "gzip" or raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            text = _decode(raw)
            if path:
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(text)
                os.replace(tmp, path)
            return text
        except Exception as exc:  # noqa: BLE001 - 网络异常种类多，统一重试
            last_err = exc
            if attempt < retries:
                time.sleep(0.8 * (attempt + 1))

    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    raise FetchError("抓取失败：%s（%s）" % (url, last_err))


# ---------------------------------------------------------------------------
# 天天基金
# ---------------------------------------------------------------------------

def _clean_num(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("%", "").replace(",", "")
    if text in ("", "--", "---", "—"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _table_cell(text, label):
    """从基金档案页里取 `<th>label</th><td>值</td>`。"""
    pattern = re.escape(label) + r"</th><td[^>]*>(.*?)</td>"
    m = re.search(pattern, text, re.S)
    if not m:
        return None
    value = re.sub(r"<[^>]+>", " ", m.group(1))
    return htmlmod.unescape(value).strip() or None


def fetch_fund_basic(code, cache_dir=None, offline=False):
    """不依赖被反爬的移动接口：用基金档案页 + 净值历史拼出基础信息。"""
    jbgk_url = "https://fundf10.eastmoney.com/jbgk_%s.html" % code
    page = http_get_text(jbgk_url, cache_dir=cache_dir, cache_key="jbgk_%s" % code,
                         max_age_hours=24 * 7, offline=offline)

    ftype = _table_cell(page, "基金类型") or ""
    tracking = _table_cell(page, "跟踪标的")
    if tracking and ("无跟踪标的" in tracking or tracking in ("--", "-")):
        tracking = None
    estab = _table_cell(page, "成立日期/规模") or ""

    nav_data = fetch_nav_history(code, cache_dir=cache_dir, offline=offline)
    rows = nav_data.get("rows") or []
    if not rows:
        raise FetchError("基金 %s 没有净值数据" % code)
    last = rows[-1]
    name = nav_data.get("name") or code

    # 基金档案页的基金简称有时更准确
    short_name = _table_cell(page, "基金简称")
    if short_name and len(short_name) <= 40:
        name = short_name

    return {
        "code": code,
        "name": name,
        "ftype": ftype,
        "index_code": None,
        "index_name": tracking,
        "nav": last.get("nav"),
        "acc_nav": last.get("acc"),
        "nav_date": last.get("date") or "",
        "risk": "",
        "estab": estab,
        "source": "天天基金",
    }


def fetch_nav_history(code, cache_dir=None, offline=False):
    """返回按日期升序的净值列表，字段 date/nav/acc/change。"""
    url = "https://fund.eastmoney.com/pingzhongdata/%s.js" % code
    text = http_get_text(url, cache_dir=cache_dir, cache_key="nav_%s" % code,
                         max_age_hours=6, offline=offline)
    if "Data_netWorthTrend" not in text:
        path = _cache_path(cache_dir, url, "nav_%s" % code)
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
        raise FetchError("基金 %s 净值数据为空" % code)

    name_m = re.search(r'var fS_name = "(.*?)"', text)
    name = name_m.group(1) if name_m else code

    net_m = re.search(r"var Data_netWorthTrend = (\[.*?\]);", text, re.S)
    acc_m = re.search(r"var Data_ACWorthTrend = (\[.*?\]);", text, re.S)
    net = json.loads(net_m.group(1)) if net_m else []
    acc = json.loads(acc_m.group(1)) if acc_m else []

    acc_map = {}
    for item in acc:
        try:
            acc_map[int(item[0])] = float(item[1])
        except (TypeError, ValueError, IndexError):
            continue

    rows = []
    for item in net:
        try:
            ts = int(item["x"])
            nav = float(item["y"])
        except (KeyError, TypeError, ValueError):
            continue
        dt = datetime.utcfromtimestamp(ts / 1000.0) + BEIJING_OFFSET
        rows.append({
            "date": dt.date().isoformat(),
            "nav": nav,
            "acc": acc_map.get(ts),
            "change": _clean_num(item.get("equityReturn")),
        })
    rows.sort(key=lambda r: r["date"])
    return {"code": code, "name": name, "rows": rows}


def fetch_ratings(code, cache_dir=None, offline=False):
    url = "https://api.fund.eastmoney.com/F10/JJPJ/?fundcode=%s&pageIndex=1&pageSize=50" % code
    text = http_get_text(
        url,
        headers={"Referer": "https://fundf10.eastmoney.com/jjpj_%s.html" % code},
        cache_dir=cache_dir, cache_key="rating_%s" % code,
        max_age_hours=24, offline=offline,
    )
    text = text.strip()
    if text.startswith("jQuery(") and text.endswith(")"):
        text = text[len("jQuery("):-1]
    obj = json.loads(text)
    rows = obj.get("Data") or []
    if not rows:
        return {}

    def first_valid(field):
        for row in rows:
            value = row.get(field)
            if value not in (None, "", "--", "-"):
                return value
        return None

    def as_star(field):
        value = first_valid(field)
        try:
            number = int(float(value))
        except (TypeError, ValueError):
            return None
        return number if 1 <= number <= 5 else None

    return {
        "date": rows[0].get("RDATE"),
        "morningstar": as_star("CXPJ3"),
        "zhaoshang": as_star("ZSPJ"),
        "shanghai3": as_star("SZPJ3"),
        "shanghai5": as_star("ZSPJ5"),
        "jianan": as_star("JAPJ"),
    }


def fetch_tracking_index(code, cache_dir=None, offline=False):
    url = "https://fundf10.eastmoney.com/jbgk_%s.html" % code
    text = http_get_text(url, cache_dir=cache_dir, cache_key="jbgk_%s" % code,
                         max_age_hours=24 * 7, offline=offline)
    m = re.search(r"跟踪标的</th><td[^>]*>(.*?)</td>", text, re.S)
    if not m:
        return None
    value = re.sub(r"<[^>]+>", "", m.group(1))
    value = htmlmod.unescape(value).strip()
    return value or None


def search_funds(query, cache_dir=None, offline=False):
    url = ("https://fundsuggest.eastmoney.com/FundSearch/api/FundSearchAPI.ashx"
           "?m=1&key=%s" % urllib.parse.quote(query))
    text = http_get_text(url, cache_dir=cache_dir, cache_key="search_%s" % query,
                         max_age_hours=24 * 30, offline=offline)
    obj = json.loads(text)
    out = []
    for item in obj.get("Datas") or []:
        out.append({"code": item.get("CODE"), "name": item.get("NAME"),
                    "category": item.get("CATEGORY")})
    return out


# ---------------------------------------------------------------------------
# 蛋卷指数估值
# ---------------------------------------------------------------------------

def fetch_index_valuations(cache_dir=None, offline=False):
    url = "https://danjuanfunds.com/djapi/index_eva/dj"
    text = http_get_text(url, cache_dir=cache_dir, cache_key="danjuan_eva",
                         max_age_hours=6, offline=offline)
    obj = json.loads(text)
    items = ((obj.get("data") or {}).get("items")) or []
    if not items and not offline:
        path = _cache_path(cache_dir, url, "danjuan_eva")
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
        raise FetchError("蛋卷估值接口返回空数据")
    out = {}
    for item in items:
        code = item.get("index_code")
        if not code:
            continue
        item_date = None
        ts = item.get("ts")
        if ts:
            try:
                item_date = (datetime.utcfromtimestamp(float(ts) / 1000.0)
                             + BEIJING_OFFSET).date().isoformat()
            except (TypeError, ValueError, OSError):
                item_date = None
        out[code] = {
            "code": code,
            "name": item.get("name") or code,
            "pe": _clean_num(item.get("pe")),
            "pb": _clean_num(item.get("pb")),
            "pe_pct": _clean_num(item.get("pe_percentile")),
            "pb_pct": _clean_num(item.get("pb_percentile")),
            "roe": _clean_num(item.get("roe")),
            "yield": _clean_num(item.get("yeild")),
            "eva_type": (item.get("eva_type") or "").lower(),
            "ttype": item.get("ttype"),
            "date": item_date,
        }
    return {"items": out, "fetched": datetime.now().isoformat(timespec="seconds")}


def _norm_name(text):
    if not text:
        return ""
    text = str(text).lower()
    for word in ("指数", "全收益", "净价", "加权", "中证", "国证", "申万",
                 "（", "）", "(", ")", " ", "　", "基金", "etf", "lof", "联接"):
        text = text.replace(word, "")
    return text


def map_index_code(index_code, index_name, valuations):
    """把基金基础信息里的 INDEXCODE/INDEXNAME 映射到蛋卷指数代码。"""
    items = (valuations or {}).get("items") or {}
    raw = str(index_code or "").strip()
    if raw:
        candidates = []
        upper = raw.upper()
        if upper.startswith("SH") or upper.startswith("SZ") or upper.startswith("CSI") or upper.startswith("HK"):
            candidates.append(upper)
        if raw.isdigit():
            if raw.startswith("399"):
                candidates += ["SZ" + raw, "SH" + raw]
            elif raw.startswith("000"):
                candidates += ["SH" + raw, "SZ" + raw]
            else:
                candidates += [raw, "SZ" + raw, "SH" + raw]
        else:
            candidates += [raw, "CSI" + raw, "SH" + raw, "SZ" + raw]
        for cand in candidates:
            if cand in items:
                return cand

    name = _norm_name(index_name)
    if name:
        # 先精确，再互相包含
        for code, item in items.items():
            if _norm_name(item.get("name")) == name:
                return code
        best = None
        for code, item in items.items():
            other = _norm_name(item.get("name"))
            if other and (other in name or name in other):
                score = min(len(other), len(name))
                if best is None or score > best[0]:
                    best = (score, code)
        if best:
            return best[1]
    return None


def fetch_index_quote(index_code, cache_dir=None, offline=False):
    """SH/SZ 指数实时点位；无法映射时返回 None。"""
    code = str(index_code or "").upper()
    if code.startswith("SH") and code[2:].isdigit():
        secid = "1." + code[2:]
    elif code.startswith("SZ") and code[2:].isdigit():
        secid = "0." + code[2:]
    else:
        return None
    url = ("https://push2.eastmoney.com/api/qt/stock/get?secid=%s"
           "&fields=f43,f44,f45,f46,f57,f58,f60,f169,f170" % secid)
    text = http_get_text(url, cache_dir=cache_dir, cache_key="quote_%s" % code,
                         max_age_hours=0.05, offline=offline)
    obj = json.loads(text)
    d = obj.get("data") or {}
    price = _clean_num(d.get("f43"))
    if price is None:
        return None
    return {"code": code, "name": d.get("f58") or code,
            "level": price / 100.0, "change_pct": _clean_num(d.get("f170"))}


# ---------------------------------------------------------------------------
# 微信公众号文章
# ---------------------------------------------------------------------------

def _strip_tags(chunk):
    chunk = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", chunk, flags=re.S | re.I)
    chunk = re.sub(r"<br\s*/?>", "\n", chunk, flags=re.I)
    chunk = re.sub(r"</(p|div|li|tr|h\d)>", "\n", chunk, flags=re.I)
    chunk = re.sub(r"<[^>]+>", " ", chunk)
    chunk = htmlmod.unescape(chunk)
    chunk = chunk.replace("\xa0", " ")
    chunk = re.sub(r"[ \t]+", " ", chunk)
    chunk = re.sub(r"\n\s*\n+", "\n", chunk)
    return chunk.strip()


def _parse_tables(chunk):
    tables = []
    for table_html in re.findall(r"<table[^>]*>.*?</table>", chunk, re.S | re.I):
        rows = []
        for tr in re.findall(r"<tr[^>]*>.*?</tr>", table_html, re.S | re.I):
            cells = []
            for cell in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S | re.I):
                cells.append(_strip_tags(cell).replace("\n", " ").strip())
            if any(cells):
                rows.append(cells)
        if rows:
            tables.append(rows)
    return tables


def fetch_article(url, cache_dir=None, offline=False):
    headers = {"Referer": "https://mp.weixin.qq.com/"}
    text = http_get_text(url, headers=headers, cache_dir=cache_dir,
                         cache_key="article_%s" % hashlib.md5(url.encode("utf-8")).hexdigest(),
                         max_age_hours=24 * 30, offline=offline)

    title = ""
    for pattern in (
        r'<meta[^>]+property="og:title"[^>]+content="([^"]*)"',
        r'<meta[^>]+content="([^"]*)"[^>]+property="og:title"',
        r'<h1[^>]+id="activity-name"[^>]*>(.*?)</h1>',
    ):
        m = re.search(pattern, text, re.S | re.I)
        if m:
            title = _strip_tags(m.group(1))
            if title:
                break

    publish = ""
    m = re.search(r'var\s+ct\s*=\s*"(\d+)"', text)
    if m:
        publish = (datetime.utcfromtimestamp(int(m.group(1))) + BEIJING_OFFSET).date().isoformat()
    else:
        m = re.search(r'id="publish_time"[^>]*>(.*?)</em>', text, re.S)
        if m:
            publish = _strip_tags(m.group(1))

    content_html = ""
    m = re.search(r'id="js_content"', text)
    if m:
        start = text.find(">", m.end())
        if start < 0:
            start = m.end()
        tail = text[start + 1:]
        cut = re.search(r"<script|</body>", tail, re.I)
        content_html = tail[:cut.start()] if cut else tail

    if not content_html:
        raise ValueError("未找到公众号正文（链接可能已失效、被删除，或触发微信验证）")

    body_text = _strip_tags(content_html)
    tables = _parse_tables(content_html)
    image_count = len(re.findall(r"<img\b", content_html, re.I))

    # 微信文章顶部通常有市场星级，先只看正文前 1200 字，降低误匹配
    star = parse_market_star(body_text[:1200])

    return {
        "url": url,
        "title": title or "(无标题)",
        "publish": publish,
        "text": body_text,
        "tables": tables,
        "images": image_count,
        "market_star": star,
        "text_length": len(body_text),
    }


def parse_market_star(text):
    """螺丝钉/ETF拯救世界口径：5 星最便宜，1 星最贵。

    文章开头常有一组“1星是…/2星是…/5星则是…”的定义，结尾才写
    “目前市场整体处于4星级”。必须优先匹配带“市场/目前/星级为”的
    当前值，不能见到第一个“N星”就返回。
    """
    if not text:
        return None
    t = re.sub(r"\s+", "", text)

    prioritized = [
        r"市场(?:整体)?(?:投资机会)?(?:目前|当前|今日|今天)?(?:处于|在|为|是)\s*(\d(?:\.\d)?)\s*星",
        r"(?:目前|当前|今日|今天)(?:的)?(?:市场(?:整体)?)?(?:投资机会)?(?:处于|在|为|是)\s*(\d(?:\.\d)?)\s*星",
        r"市场星级\s*(?:为|是|：|:)?\s*(\d(?:\.\d)?)",
        r"星级\s*(?:为|是|：|:)\s*(\d(?:\.\d)?)",
    ]
    for pattern in prioritized:
        m = re.search(pattern, t)
        if m:
            try:
                value = float(m.group(1))
            except ValueError:
                continue
            if 0.5 <= value <= 6.0:
                return value

    # 兜底：排除定义句式（“1星是…”“5星则…”），只有一个候选值时才采信。
    values = []
    for m in re.finditer(r"(\d(?:\.\d)?)\s*星(?:级)?", t):
        tail = t[m.end():m.end() + 2]
        if tail.startswith(("是", "则", "代表", "表示", "为")):
            continue
        try:
            value = float(m.group(1))
        except ValueError:
            continue
        if 0.5 <= value <= 6.0:
            values.append(value)
    if len(set(values)) == 1:
        return values[0]
    return None


def extract_index_overrides(tables, valuations):
    """从文章表格里尽力提取指数估值；识别不了就返回空，绝不编造。"""
    items = (valuations or {}).get("items") or {}
    result = {}
    for rows in tables:
        if len(rows) < 2:
            continue
        header_idx = None
        header = None
        for i, row in enumerate(rows[:3]):
            joined = " ".join(row)
            if ("指数" in joined or "名称" in joined) and (
                    "百分位" in joined or "PE" in joined.upper() or "市盈率" in joined
                    or "估值" in joined):
                header_idx = i
                header = row
                break
        if header is None:
            continue

        def find_col(*words):
            for j, cell in enumerate(header):
                upper = cell.upper()
                if any(w.upper() in upper for w in words):
                    return j
            return None

        col_name = find_col("指数", "名称")
        col_pe = find_col("PE", "市盈率")
        col_pb = find_col("PB", "市净率")
        col_pct = find_col("百分位", "分位")
        col_level = find_col("估值", "状态")
        if col_name is None:
            continue

        for row in rows[header_idx + 1:]:
            if col_name >= len(row):
                continue
            name = row[col_name].strip()
            if not name:
                continue
            matched = map_index_code(None, name, valuations)
            if not matched:
                continue
            entry = {"name": name, "date": None, "source": "公众号"}
            if col_pe is not None and col_pe < len(row):
                entry["pe"] = _clean_num(row[col_pe])
            if col_pb is not None and col_pb < len(row):
                entry["pb"] = _clean_num(row[col_pb])
            if col_pct is not None and col_pct < len(row):
                value = _clean_num(row[col_pct])
                if value is not None:
                    entry["pct"] = value / 100.0 if value > 1 else value
            if col_level is not None and col_level < len(row):
                level_text = row[col_level]
                if "低估" in level_text:
                    entry["level"] = "low"
                elif "高估" in level_text:
                    entry["level"] = "high"
                elif "正常" in level_text:
                    entry["level"] = "mid"
            if len(entry) > 3:
                result[matched] = entry
    return result


# ---------------------------------------------------------------------------
# 推送
# ---------------------------------------------------------------------------

def send_serverchan(sendkey, title, markdown, timeout=15):
    url = "https://sctapi.ftqq.com/%s.send" % sendkey
    data = urllib.parse.urlencode({"title": title[:32], "desp": markdown}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": UA,
        "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
    })
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        text = _decode(resp.read())
    try:
        obj = json.loads(text)
    except ValueError:
        return False, text[:300]
    ok = obj.get("code") == 0
    return ok, text[:300]


def send_pushplus(token, title, markdown, timeout=15):
    url = "https://www.pushplus.plus/send"
    payload = json.dumps({
        "token": token, "title": title, "content": markdown, "template": "markdown",
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={
        "User-Agent": UA, "Content-Type": "application/json; charset=utf-8",
    })
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        text = _decode(resp.read())
    try:
        obj = json.loads(text)
    except ValueError:
        return False, text[:300]
    ok = obj.get("code") == 200
    return ok, text[:300]


def send_push(config, title, markdown):
    push = config.get("push") or {}
    provider = (push.get("provider") or "serverchan").lower()
    if provider == "pushplus":
        token = push.get("pushplus_token") or ""
        if not token:
            return False, "未配置 pushplus_token"
        return send_pushplus(token, title, markdown)
    key = push.get("serverchan_key") or ""
    if not key:
        return False, "未配置 serverchan_key"
    return send_serverchan(key, title, markdown)


def today_str():
    return date.today().isoformat()
