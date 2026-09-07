#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
마켓 데스크 데이터 수집기.

GitHub Actions 안에서 실행됩니다. 브라우저가 아니라 서버에서 돌기 때문에
CORS 제약이 없고, 시세 수집에는 API 키가 필요 없습니다.

  config.json          → 무엇을 수집할지
  config/calendar.json → 경제 일정(직접 관리)
  data/latest.json     → 결과 (페이지가 이 파일 하나만 읽습니다)
  data/dart_corp.json  → DART 기업코드 캐시 (자동 생성)

수집에 실패한 항목은 추정하지 않습니다. 직전 실행의 값을 그대로 두고
stale 표시만 남깁니다 — 화면에 틀린 숫자가 뜨는 것보다 낫습니다.
"""

import io
import json
import os
import random
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "config.json")
CALENDAR = os.path.join(ROOT, "config", "calendar.json")
OUT = os.path.join(ROOT, "data", "latest.json")
DART_MAP = os.path.join(ROOT, "data", "dart_corp.json")

KST = timezone(timedelta(hours=9))
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

warnings = []


# ==========================================================================
# 공통
# ==========================================================================

def http_get(url, tries=3, timeout=25, headers=None):
    last = None
    ctx = ssl.create_default_context()
    base = {"User-Agent": UA, "Accept": "*/*",
            "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8"}
    if headers:
        base.update(headers)
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=base)
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                return r.read()
        except Exception as e:                          # noqa: BLE001
            last = e
            if i < tries - 1:
                time.sleep(1.2 * (i + 1) + random.random())
    raise last


def warn(msg):
    warnings.append(msg)
    print("  ! " + msg, file=sys.stderr)


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as e:
        print("JSON 문법 오류: %s — %s" % (path, e), file=sys.stderr)
        if default is None:
            raise
        return default


def strip_comments(d):
    if isinstance(d, dict):
        return {k: strip_comments(v) for k, v in d.items() if not k.startswith("_")}
    if isinstance(d, list):
        return [strip_comments(x) for x in d]
    return d


def rnd(x, n=4):
    return None if x is None else round(float(x), n)


# ==========================================================================
# 시세 — 야후 파이낸스 (키 불필요)
# ==========================================================================

YAHOO = ("https://query1.finance.yahoo.com/v8/finance/chart/"
         "{sym}?range=1mo&interval=1d")

STOOQ_FALLBACK = {
    "^GSPC": "^spx", "^IXIC": "^ndq", "^DJI": "^dji", "^RUT": "^rut",
    "^N225": "^nkx", "^HSI": "^hsi", "^VIX": "^vix", "^STOXX50E": "^sx5e",
    "GC=F": "gc.f", "SI=F": "si.f", "HG=F": "hg.f",
    "CL=F": "cl.f", "BZ=F": "cb.f", "NG=F": "ng.f",
    "KRW=X": "usdkrw", "JPY=X": "usdjpy",
    "BTC-USD": "btcusd", "ETH-USD": "ethusd",
}

_cache = {}


# --------------------------------------------------------------------------
# 1순위: yfinance 배치 조회
#
# 종목마다 따로 부르면 요청이 수십 번이 되고, 야후는 데이터센터 IP(=GitHub
# Actions)에 그 정도 빈도를 허용하지 않습니다 — HTTP 429 로 막힙니다.
# yfinance 는 여러 종목을 한 번에 받아오고 쿠키·crumb 처리도 대신 해줍니다.
# 이걸 먼저 돌려 캐시를 채우고, 실패한 것만 개별 경로로 넘깁니다.
# --------------------------------------------------------------------------

BATCH = 20


def batch_prefetch(symbols):
    syms = [s for s in dict.fromkeys(symbols) if s]
    if not syms:
        return
    try:
        import yfinance as yf
    except ImportError:
        warn("yfinance 가 없어 배치 조회를 건너뜁니다 (개별 조회로 진행).")
        return

    got = 0
    for i in range(0, len(syms), BATCH):
        chunk = syms[i:i + BATCH]
        try:
            df = yf.download(chunk, period="1mo", interval="1d",
                             group_by="ticker", auto_adjust=False,
                             threads=False, progress=False, timeout=40)
        except Exception as e:                          # noqa: BLE001
            warn("배치 조회 실패 (%d개) — %s" % (len(chunk), e))
            time.sleep(3)
            continue
        if df is None or len(df) == 0:
            time.sleep(2)
            continue

        for sym in chunk:
            try:
                if len(chunk) == 1:
                    col = df["Close"]
                else:
                    if sym not in df.columns.get_level_values(0):
                        continue
                    col = df[sym]["Close"]
                ser = [(idx, float(v)) for idx, v in col.items()
                       if v == v and v is not None]     # NaN 제외
                if not ser:
                    continue
                price = ser[-1][1]
                prev = ser[-2][1] if len(ser) >= 2 else None
                _cache[sym] = {
                    "price": price,
                    "change": None if prev is None else price - prev,
                    "pct": None if not prev else (price - prev) / prev * 100.0,
                    "asof": ser[-1][0].strftime("%m.%d"),
                    "currency": "KRW" if sym.endswith((".KS", ".KQ")) else "",
                    "spark": [rnd(v, 4) for _, v in ser[-22:]],
                }
                got += 1
            except Exception:                           # noqa: BLE001
                continue
        time.sleep(2)
    print("  배치 조회로 %d/%d 확보" % (got, len(syms)))


# --------------------------------------------------------------------------
# 한국 종목 예비 경로 — 네이버금융 (키 불필요)
# --------------------------------------------------------------------------

NAVER = ("https://api.finance.naver.com/siseJson.naver"
         "?symbol=%s&requestType=1&startTime=%s&endTime=%s&timeframe=day")
NAVER_ROW = re.compile(
    r"\[\s*'(\d{8})'\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)")
NAVER_INDEX = {"^KS11": "KOSPI", "^KQ11": "KOSDAQ"}


def quote_naver(symbol):
    if symbol in NAVER_INDEX:
        code = NAVER_INDEX[symbol]
    elif symbol.endswith((".KS", ".KQ")):
        code = symbol.split(".")[0]
    else:
        raise ValueError("네이버 대상 아님")

    end = datetime.now(KST)
    bgn = end - timedelta(days=45)
    txt = http_get(NAVER % (code, bgn.strftime("%Y%m%d"), end.strftime("%Y%m%d")),
                   headers={"Referer": "https://finance.naver.com/"}
                   ).decode("utf-8", "replace")
    rows = NAVER_ROW.findall(txt)
    if not rows:
        raise ValueError("행 없음")
    closes = [(r[0], float(r[4])) for r in rows]
    price = closes[-1][1]
    prev = closes[-2][1] if len(closes) >= 2 else None
    d = closes[-1][0]
    return {"price": price,
            "change": None if prev is None else price - prev,
            "pct": None if not prev else (price - prev) / prev * 100.0,
            "asof": "%s.%s" % (d[4:6], d[6:8]),
            "currency": "KRW",
            "spark": [rnd(v, 4) for _, v in closes[-22:]]}


def quote_yahoo(symbol):
    raw = http_get(YAHOO.format(sym=urllib.parse.quote(symbol)))
    doc = json.loads(raw.decode("utf-8"))
    res = (doc.get("chart") or {}).get("result") or []
    if not res:
        raise ValueError("빈 응답")
    r0 = res[0]
    meta = r0.get("meta") or {}

    closes = (((r0.get("indicators") or {}).get("quote") or [{}])[0]).get("close") or []
    closes = [c for c in closes if c is not None]

    price = meta.get("regularMarketPrice")
    prev = meta.get("previousClose")
    if prev is None:
        prev = meta.get("chartPreviousClose")
    if price is None and closes:
        price = closes[-1]
    if prev is None and len(closes) >= 2:
        prev = closes[-2]
    if price is None:
        raise ValueError("가격 없음")

    ts = meta.get("regularMarketTime")
    asof = datetime.fromtimestamp(ts, KST).strftime("%m.%d") if ts \
        else datetime.now(KST).strftime("%m.%d")

    spark = [rnd(c, 4) for c in closes[-22:]]

    return {"price": price,
            "change": None if prev is None else price - prev,
            "pct": None if not prev else (price - prev) / prev * 100.0,
            "asof": asof, "currency": meta.get("currency") or "",
            "spark": spark}


def quote_stooq(stooq_sym):
    url = "https://stooq.com/q/l/?s=%s&f=sd2t2ohlcv&h&e=csv" % stooq_sym
    lines = http_get(url).decode("utf-8", "replace").strip().splitlines()
    if len(lines) < 2:
        raise ValueError("빈 CSV")
    rec = dict(zip([h.strip().lower() for h in lines[0].split(",")],
                   lines[1].split(",")))
    close, open_ = rec.get("close"), rec.get("open")
    if not close or close.upper() == "N/D":
        raise ValueError("값 없음")
    price = float(close)
    prev = float(open_) if open_ and open_.upper() != "N/D" else None
    return {"price": price,
            "change": None if prev is None else price - prev,
            "pct": None if not prev else (price - prev) / prev * 100.0,
            "asof": (rec.get("date") or "")[5:].replace("-", ".") or
                    datetime.now(KST).strftime("%m.%d"),
            "currency": "", "spark": []}


def quote(symbol):
    """배치 캐시 → 네이버(한국) → 야후 → stooq 순. 전부 실패하면 None."""
    if symbol in _cache:
        return _cache[symbol]

    errs = []
    out = None
    kr = symbol.endswith((".KS", ".KQ")) or symbol in NAVER_INDEX

    if kr:
        try:
            out = quote_naver(symbol)
        except Exception as e:                          # noqa: BLE001
            errs.append("네이버:" + str(e)[:60])

    if out is None:
        try:
            out = quote_yahoo(symbol)
        except Exception as e:                          # noqa: BLE001
            errs.append("야후:" + str(e)[:60])

    if out is None:
        alt = STOOQ_FALLBACK.get(symbol)
        if alt:
            try:
                out = quote_stooq(alt)
            except Exception as e:                      # noqa: BLE001
                errs.append("stooq:" + str(e)[:60])

    if out is None:
        warn("%s 수집 실패 (%s)" % (symbol, " / ".join(errs)))

    _cache[symbol] = out
    time.sleep(0.5 if out is None else 0.8)
    return out


# ==========================================================================
# 공포탐욕지수 — CNN (키 불필요, 비공식 경로)
# ==========================================================================

FNG = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"


def collect_fng():
    try:
        doc = json.loads(http_get(FNG, headers={
            "Accept": "application/json",
            "Referer": "https://edition.cnn.com/markets/fear-and-greed",
        }).decode("utf-8"))
        f = doc.get("fear_and_greed") or {}
        score = f.get("score")
        if score is None:
            raise ValueError("score 없음")
        ts = f.get("timestamp")
        asof = ""
        if ts:
            try:
                asof = datetime.fromtimestamp(float(ts) / 1000, KST).strftime("%m.%d")
            except Exception:                           # noqa: BLE001
                asof = ""
        return {"value": rnd(score, 1), "label": f.get("rating") or "",
                "asof": asof,
                "prevClose": rnd(f.get("previous_close"), 1),
                "prevWeek": rnd(f.get("previous_1_week"), 1),
                "prevMonth": rnd(f.get("previous_1_month"), 1)}
    except Exception as e:                              # noqa: BLE001
        warn("공포탐욕지수 수집 실패 (%s)" % e)
        return None


# ==========================================================================
# DART 전자공시 — 선택 (환경변수 DART_API_KEY 가 있을 때만)
# ==========================================================================

DART_CORP_ZIP = "https://opendart.fss.or.kr/api/corpCode.xml?crtfc_key=%s"
DART_LIST = ("https://opendart.fss.or.kr/api/list.json"
             "?crtfc_key=%s&corp_code=%s&bgn_de=%s&end_de=%s"
             "&page_count=10&sort=date&sort_mth=desc")
DART_VIEW = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=%s"


def dart_corp_map(key, stock_codes):
    """종목코드 → DART 기업코드. 한 번 받아 캐시하고 재사용합니다."""
    cached = load_json(DART_MAP, {}) or {}
    missing = [c for c in stock_codes if c not in cached]
    if not missing:
        return cached

    print("  DART 기업코드 내려받는 중 (%d개 신규)…" % len(missing))
    try:
        raw = http_get(DART_CORP_ZIP % key, timeout=60)
        # 오류 시 zip 이 아니라 JSON 이 옵니다.
        if raw[:2] != b"PK":
            msg = raw[:200].decode("utf-8", "replace")
            raise ValueError("zip 이 아님 — %s" % msg)
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            name = next(n for n in z.namelist() if n.lower().endswith(".xml"))
            xml = z.read(name)
        root = ET.fromstring(xml)
        found = {}
        for el in root.iter("list"):
            sc = (el.findtext("stock_code") or "").strip()
            if sc and sc in stock_codes:
                found[sc] = (el.findtext("corp_code") or "").strip()
        cached.update(found)
        with open(DART_MAP, "w", encoding="utf-8") as f:
            json.dump(cached, f, ensure_ascii=False, indent=1, sort_keys=True)
        still = [c for c in stock_codes if c not in cached]
        if still:
            warn("DART 기업코드를 못 찾은 종목: %s" % ", ".join(still))
        return cached
    except Exception as e:                              # noqa: BLE001
        warn("DART 기업코드 목록 실패 (%s)" % e)
        return cached


def collect_dart(watch_cfg, days):
    key = os.environ.get("DART_API_KEY", "").strip()
    if not key:
        print("  DART 키 없음 — 공시 수집 건너뜀 (선택 기능)")
        return []

    kr = [w for w in watch_cfg if w["symbol"].endswith((".KS", ".KQ"))]
    codes = {w["symbol"].split(".")[0]: w["name"] for w in kr}
    if not codes:
        return []

    cmap = dart_corp_map(key, set(codes))
    end = datetime.now(KST)
    bgn = end - timedelta(days=max(1, int(days)))
    out = []
    for stock_code, name in codes.items():
        corp = cmap.get(stock_code)
        if not corp:
            continue
        try:
            doc = json.loads(http_get(
                DART_LIST % (key, corp,
                             bgn.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
            ).decode("utf-8"))
        except Exception as e:                          # noqa: BLE001
            warn("DART 공시 조회 실패 (%s): %s" % (name, e))
            continue

        status = str(doc.get("status", ""))
        if status == "013":          # 조회 결과 없음 — 정상입니다.
            continue
        if status != "000":
            warn("DART 응답 코드 %s (%s) — %s" % (status, name, doc.get("message", "")))
            continue

        for it in (doc.get("list") or [])[:5]:
            d = it.get("rcept_dt") or ""
            out.append({
                "name": name,
                "code": stock_code,
                "date": "%s-%s-%s" % (d[0:4], d[4:6], d[6:8]) if len(d) == 8 else d,
                "title": (it.get("report_nm") or "").strip(),
                "filer": (it.get("flr_nm") or "").strip(),
                "url": DART_VIEW % (it.get("rcept_no") or ""),
            })
        time.sleep(0.3)

    out.sort(key=lambda x: x["date"], reverse=True)
    return out[:40]


# ==========================================================================
# 뉴스 — 구글 뉴스 RSS (키 불필요)
# ==========================================================================

GNEWS = "https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"
TAG = re.compile(r"<[^>]+>")
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def collect_news(queries, per_query):
    items, seen = [], set()
    for spec in queries:
        q, kicker = spec.get("q", ""), spec.get("kicker", "")
        try:
            root = ET.fromstring(http_get(
                GNEWS.format(q=urllib.parse.quote(q + " when:2d"))))
        except Exception as e:                          # noqa: BLE001
            warn("뉴스 '%s' 수집 실패 (%s)" % (q, e))
            continue

        got = 0
        for it in root.iterfind(".//item"):
            if got >= per_query:
                break
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            if not title or title in seen:
                continue
            seen.add(title)

            source = ""
            src_el = it.find("source")
            if src_el is not None and src_el.text:
                source = src_el.text.strip()
                if title.endswith(" - " + source):
                    title = title[: -(len(source) + 3)].strip()
            elif " - " in title:
                title, source = [s.strip() for s in title.rsplit(" - ", 1)]

            body = re.sub(r"\s+", " ", TAG.sub(" ", it.findtext("description") or "")).strip()
            if source and body.endswith(source):
                body = body[: -len(source)].strip()

            date = ""
            m = re.search(r"(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})", it.findtext("pubDate") or "")
            if m and m.group(2) in MONTHS:
                date = "%02d.%02d" % (MONTHS.index(m.group(2)) + 1, int(m.group(1)))

            items.append({"kicker": kicker, "title": title, "body": body[:180],
                          "source": (source + (" · " + date if date else "")).strip(" ·"),
                          "url": link})
            got += 1
        time.sleep(0.5)
    return items


# ==========================================================================
# 조립
# ==========================================================================

def carry(prev_list, name):
    for x in prev_list or []:
        if x.get("name") == name:
            y = dict(x)
            y["stale"] = True
            return y
    return None


def simple_block(specs, prev_list, extra=None):
    """이름·심볼만 있는 목록(섹터·크립토 등)을 공통으로 처리합니다."""
    out = []
    for s in specs:
        q = quote(s["symbol"])
        if q is None:
            old = carry(prev_list, s["name"])
            if old:
                out.append(old)
            continue
        row = {"name": s["name"], "symbol": s["symbol"],
               "dp": s.get("dp", 2),
               "value": rnd(q["price"]), "change": rnd(q["change"]),
               "pct": rnd(q["pct"], 3), "asof": q["asof"], "spark": q["spark"]}
        if extra:
            row.update({k: s.get(k) for k in extra})
        out.append(row)
    return out


def main():
    cfg = strip_comments(load_json(CONFIG))
    cal = strip_comments(load_json(CALENDAR, {"items": []}))
    prev = load_json(OUT, {}) or {}

    out = {"site": cfg.get("site", {}),
           "updatedAt": datetime.now(KST).isoformat(timespec="seconds"),
           "homeIndices": cfg.get("home_indices", []),
           "calendar": cal.get("items", [])}

    # 개별 호출은 요청 수가 많아 야후에 막힙니다(HTTP 429). 먼저 한 번에
    # 묶어서 받아 캐시를 채우고, 못 받은 것만 아래에서 개별로 다시 시도합니다.
    print("배치 조회…")
    allsyms = []
    for key in ("indices", "macro", "rates", "sectors", "crypto", "watchlist"):
        allsyms += [s["symbol"] for s in cfg.get(key, []) if s.get("symbol")]
    if cfg.get("sentiment", {}).get("symbol"):
        allsyms.append(cfg["sentiment"]["symbol"])
    batch_prefetch(allsyms)

    print("지수…")
    out["indices"] = simple_block(cfg.get("indices", []), prev.get("indices"),
                                  extra=["group"])

    print("환율·원자재…")
    out["macro"] = simple_block(cfg.get("macro", []), prev.get("macro"),
                                extra=["unit", "prefix", "suffix"])

    print("금리…")
    out["rates"] = simple_block(cfg.get("rates", []), prev.get("rates"),
                                extra=["years"])

    print("스프레드 계산…")
    by_sym = {r["symbol"]: r for r in out["rates"] if r.get("value") is not None}
    out["spreads"] = []
    for sp in cfg.get("spreads", []):
        a, b = by_sym.get(sp["long"]), by_sym.get(sp["short"])
        if not a or not b:
            continue
        out["spreads"].append({"name": sp["name"], "note": sp.get("note", ""),
                               "value": rnd(a["value"] - b["value"], 3),
                               "asof": a.get("asof", "")})

    print("섹터…")
    out["sectors"] = simple_block(cfg.get("sectors", []), prev.get("sectors"))

    print("크립토…")
    out["crypto"] = simple_block(cfg.get("crypto", []), prev.get("crypto"))

    print("시장심리…")
    s = cfg.get("sentiment", {})
    q = quote(s.get("symbol", "^VIX"))
    sent = {"avg10y": s.get("avg10y", 19.72), "note": s.get("note", "")}
    if q:
        sent.update({"vix": rnd(q["price"]), "asof": q["asof"], "spark": q["spark"]})
    else:
        old = prev.get("sentiment") or {}
        sent.update({"vix": old.get("vix"), "asof": old.get("asof"),
                     "spark": old.get("spark", []), "stale": True})
    fng = collect_fng()
    sent["fng"] = fng if fng else (prev.get("sentiment") or {}).get("fng")
    out["sentiment"] = sent

    print("관심종목…")
    out["watchlist"] = []
    for spec in cfg.get("watchlist", []):
        q = quote(spec["symbol"])
        if q is None:
            old = carry(prev.get("watchlist"), spec["name"])
            if old:
                out["watchlist"].append(old)
            continue
        krw = spec["symbol"].endswith((".KS", ".KQ"))
        out["watchlist"].append({
            "name": spec["name"], "symbol": spec["symbol"],
            "code": spec["symbol"].split(".")[0],
            "market": spec.get("market", ""), "tag": spec.get("tag", ""),
            "currency": q["currency"] or ("KRW" if krw else "USD"),
            "value": rnd(q["price"]), "change": rnd(q["change"]),
            "pct": rnd(q["pct"], 3), "asof": q["asof"], "spark": q["spark"]})

    print("뉴스…")
    news = collect_news(cfg.get("news_queries", []),
                        int(cfg.get("news_per_query", 2)))
    out["news"] = news or (prev.get("news") or [])
    if not news:
        warn("뉴스를 하나도 못 가져와 직전 값을 유지합니다.")

    print("전자공시(DART)…")
    dcfg = cfg.get("dart", {})
    if dcfg.get("enabled"):
        f = collect_dart(cfg.get("watchlist", []), dcfg.get("days", 14))
        out["filings"] = f if f else (prev.get("filings") or [])
        out["dartOn"] = bool(os.environ.get("DART_API_KEY", "").strip())
    else:
        out["filings"], out["dartOn"] = [], False

    out["warnings"] = warnings

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print("\n완료 — 지수 %d · 매크로 %d · 금리 %d · 섹터 %d · 관심종목 %d "
          "· 뉴스 %d · 공시 %d · 경고 %d"
          % (len(out["indices"]), len(out["macro"]), len(out["rates"]),
             len(out["sectors"]), len(out["watchlist"]), len(out["news"]),
             len(out["filings"]), len(warnings)))

    if not out["indices"] and not out["watchlist"]:
        print("수집된 시세가 하나도 없습니다.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
