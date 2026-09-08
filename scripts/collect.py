#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
마켓 데스크 데이터 수집기 — 매크로 레짐 판단용.

GitHub Actions 안에서 실행됩니다. 브라우저가 아니라 서버에서 돌기 때문에
CORS 제약이 없고, 시세 수집에는 API 키가 필요 없습니다.

  config.json          → 무엇을 수집할지
  config/calendar.json → 경제 일정(직접 관리)
  data/latest.json     → 결과 (페이지가 이 파일 하나만 읽습니다)
  data/dart_corp.json  → DART 기업코드 캐시 (자동 생성)

핵심은 '비율(ratio)'입니다. 두 자산의 상대강도가 시장이 무엇에 베팅하는지를
가격보다 먼저 보여줍니다. 그 비율들의 최근 움직임을 지난 1년 분포에서
백분위로 환산해 성장축·물가축 점수를 냅니다 — 예측이 아니라 현재 반영치의 요약.

수집에 실패한 항목은 추정하지 않습니다. 직전 값을 그대로 두고 stale 표시만
남깁니다 — 화면에 틀린 숫자가 뜨는 것보다 낫습니다.
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

# 기간 = 거래일 수
PERIODS = [("d1", 1), ("w1", 5), ("m1", 21), ("m3", 63), ("m6", 126)]

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


def downsample(pairs, target=130):
    """[(date, value)] 를 target 개 언저리로 솎아냅니다. 마지막 점은 항상 남깁니다."""
    n = len(pairs)
    if n <= target:
        return pairs
    step = n / float(target)
    out = [pairs[int(i * step)] for i in range(target)]
    if out[-1] != pairs[-1]:
        out[-1] = pairs[-1]
    return out


def pct_change(hist, days):
    """hist = [(date, close)] 오름차순. days 거래일 전 대비 % 변화."""
    if not hist or len(hist) <= days:
        return None
    a, b = hist[-1 - days][1], hist[-1][1]
    if not a:
        return None
    return (b - a) / a * 100.0


def ytd_change(hist):
    if not hist:
        return None
    year = hist[-1][0][:4]
    base = None
    for d, v in hist:
        if d[:4] == year:
            base = v
            break
    if not base:
        return None
    return (hist[-1][1] - base) / base * 100.0


def returns_block(hist):
    out = {k: rnd(pct_change(hist, n), 3) for k, n in PERIODS}
    out["ytd"] = rnd(ytd_change(hist), 3)
    return out


def percentile_rank(values, x):
    """values 분포에서 x 가 몇 백분위인지 (0~100)."""
    vals = [v for v in values if v is not None]
    if len(vals) < 20 or x is None:
        return None
    below = sum(1 for v in vals if v < x)
    equal = sum(1 for v in vals if v == x)
    return (below + equal / 2.0) / len(vals) * 100.0


# ==========================================================================
# 시세 — 야후 파이낸스 (키 불필요)
# ==========================================================================

YAHOO = ("https://query1.finance.yahoo.com/v8/finance/chart/"
         "{sym}?range=1y&interval=1d")

STOOQ_FALLBACK = {
    "^GSPC": "^spx", "^IXIC": "^ndq", "^DJI": "^dji", "^RUT": "^rut",
    "^N225": "^nkx", "^HSI": "^hsi", "^VIX": "^vix", "^STOXX50E": "^sx5e",
    "GC=F": "gc.f", "SI=F": "si.f", "HG=F": "hg.f",
    "CL=F": "cl.f", "BZ=F": "cb.f", "NG=F": "ng.f",
    "KRW=X": "usdkrw", "JPY=X": "usdjpy",
    "BTC-USD": "btcusd", "ETH-USD": "ethusd",
}

_cache = {}      # symbol -> {"hist": [(dateISO, close)], "currency": str} 또는 None

BATCH = 20


# --------------------------------------------------------------------------
# 1순위: yfinance 배치 조회
#
# 종목마다 따로 부르면 요청이 수십 번이 되고, 야후는 데이터센터 IP(=GitHub
# Actions)에 그 빈도를 허용하지 않습니다 — HTTP 429 로 막힙니다.
# yfinance 는 여러 종목을 한 번에 받아오고 쿠키·crumb 처리도 대신 해줍니다.
# --------------------------------------------------------------------------

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
            df = yf.download(chunk, period="1y", interval="1d",
                             group_by="ticker", auto_adjust=False,
                             threads=False, progress=False, timeout=60)
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
                hist = [(idx.strftime("%Y-%m-%d"), float(v))
                        for idx, v in col.items()
                        if v == v and v is not None]     # NaN 제외
                if len(hist) < 2:
                    continue
                _cache[sym] = {"hist": hist,
                               "currency": "KRW" if sym.endswith((".KS", ".KQ")) else ""}
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


# --------------------------------------------------------------------------
# 시장 구분
#
# 시장마다 휴장일이 다릅니다. 미국이 노동절로 쉬는 날 한국은 정상 거래하므로
# 두 지수의 "기준일"이 달라지는데, 이건 수집 실패가 아니라 정상입니다.
# 항목마다 어느 시장 소속인지 표시해 두고, 화면에서는 "그 시장의 최근
# 거래일"과 비교해 늦은 것만 지연으로 표시합니다.
# --------------------------------------------------------------------------

MARKET_LABEL = {"KR": "한국 증시", "JP": "일본 증시", "HK": "홍콩 증시",
                "EU": "유럽 증시", "US": "미국 증시",
                "FX": "외환", "FUT": "선물·지수", "CRYPTO": "암호화폐"}

MARKET_ORDER = ["KR", "US", "JP", "HK", "EU", "FX", "FUT", "CRYPTO"]

# 접미사만으로는 갈리지 않는 것들. 달러인덱스는 ICE 지수라 티커가 .NYB 로
# 끝나지만 뉴욕 증시 시간표를 따르지 않습니다. 미국 증시가 쉬는 날에도
# 값이 갱신되므로 여기를 안 잡아주면 "미국 증시 최근 거래일"이 하루
# 앞당겨져, 정상인 미국 지수들이 전부 지연으로 잘못 표시됩니다.
#
# 달러인덱스는 현물 환율(24시간)보다 일봉 마감이 반나절 늦게 붙어서 FX 로
# 묶으면 늘 하루 뒤처져 보입니다. 실제로는 ICE 선물 시간표와 같으므로
# 선물 쪽에 넣습니다.
MARKET_OVERRIDE = {"DX-Y.NYB": "FUT"}


def market_of(symbol):
    s = (symbol or "").upper()
    if s in MARKET_OVERRIDE:
        return MARKET_OVERRIDE[s]
    if s in NAVER_INDEX or s.endswith((".KS", ".KQ")):
        return "KR"
    if s == "^N225" or s.endswith(".T"):
        return "JP"
    if s == "^HSI" or s.endswith(".HK"):
        return "HK"
    if s in ("^STOXX50E", "^FTSE", "^GDAXI", "^FCHI") or s.endswith((".L", ".DE", ".PA")):
        return "EU"
    if s.endswith("-USD"):
        return "CRYPTO"
    if s.endswith("=X"):
        return "FX"
    if s.endswith("=F"):
        return "FUT"
    return "US"


def hist_naver(symbol):
    if symbol in NAVER_INDEX:
        code = NAVER_INDEX[symbol]
    elif symbol.endswith((".KS", ".KQ")):
        code = symbol.split(".")[0]
    else:
        raise ValueError("네이버 대상 아님")

    end = datetime.now(KST)
    bgn = end - timedelta(days=400)
    txt = http_get(NAVER % (code, bgn.strftime("%Y%m%d"), end.strftime("%Y%m%d")),
                   headers={"Referer": "https://finance.naver.com/"},
                   timeout=40).decode("utf-8", "replace")
    rows = NAVER_ROW.findall(txt)
    if len(rows) < 2:
        raise ValueError("행 없음")
    hist = [("%s-%s-%s" % (r[0][:4], r[0][4:6], r[0][6:8]), float(r[4])) for r in rows]
    return {"hist": hist, "currency": "KRW"}


def hist_yahoo(symbol):
    raw = http_get(YAHOO.format(sym=urllib.parse.quote(symbol)), timeout=40)
    doc = json.loads(raw.decode("utf-8"))
    res = (doc.get("chart") or {}).get("result") or []
    if not res:
        raise ValueError("빈 응답")
    r0 = res[0]
    meta = r0.get("meta") or {}
    ts = r0.get("timestamp") or []
    closes = (((r0.get("indicators") or {}).get("quote") or [{}])[0]).get("close") or []
    hist = []
    for t, c in zip(ts, closes):
        if c is None:
            continue
        hist.append((datetime.fromtimestamp(t, KST).strftime("%Y-%m-%d"), float(c)))
    if len(hist) < 2:
        raise ValueError("가격 없음")
    return {"hist": hist, "currency": meta.get("currency") or ""}


def hist_stooq(stooq_sym):
    url = "https://stooq.com/q/d/l/?s=%s&i=d" % stooq_sym
    lines = http_get(url, timeout=40).decode("utf-8", "replace").strip().splitlines()
    if len(lines) < 3:
        raise ValueError("빈 CSV")
    head = [h.strip().lower() for h in lines[0].split(",")]
    di, ci = head.index("date"), head.index("close")
    hist = []
    for ln in lines[1:]:
        p = ln.split(",")
        try:
            hist.append((p[di], float(p[ci])))
        except (ValueError, IndexError):
            continue
    if len(hist) < 2:
        raise ValueError("값 없음")
    return {"hist": hist[-260:], "currency": ""}


def series(symbol):
    """배치 캐시 → 네이버(한국) → 야후 → stooq 순. 전부 실패하면 None."""
    if symbol in _cache:
        return _cache[symbol]

    errs, out = [], None
    kr = symbol.endswith((".KS", ".KQ")) or symbol in NAVER_INDEX

    if kr:
        try:
            out = hist_naver(symbol)
        except Exception as e:                          # noqa: BLE001
            errs.append("네이버:" + str(e)[:50])
    if out is None:
        try:
            out = hist_yahoo(symbol)
        except Exception as e:                          # noqa: BLE001
            errs.append("야후:" + str(e)[:50])
    if out is None:
        alt = STOOQ_FALLBACK.get(symbol)
        if alt:
            try:
                out = hist_stooq(alt)
            except Exception as e:                      # noqa: BLE001
                errs.append("stooq:" + str(e)[:50])
    if out is None:
        warn("%s 수집 실패 (%s)" % (symbol, " / ".join(errs)))

    _cache[symbol] = out
    time.sleep(0.5 if out is None else 0.8)
    return out


def quote(symbol):
    """마지막 값 + 전일대비 + 스파크라인."""
    s = series(symbol)
    if not s:
        return None
    h = s["hist"]
    price, prev = h[-1][1], h[-2][1]
    return {"price": price,
            "change": price - prev,
            "pct": None if not prev else (price - prev) / prev * 100.0,
            "asof": h[-1][0][5:].replace("-", "."),
            "date": h[-1][0],
            "currency": s["currency"],
            "spark": [rnd(v, 4) for _, v in downsample(h[-22:], 22)],
            "hist": h}


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
        ts, asof = f.get("timestamp"), ""
        if ts:
            try:
                asof = datetime.fromtimestamp(float(ts) / 1000, KST).strftime("%m.%d")
            except Exception:                           # noqa: BLE001
                asof = ""
        return {"value": rnd(score, 1), "label": f.get("rating") or "", "asof": asof,
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
    cached = load_json(DART_MAP, {}) or {}
    missing = [c for c in stock_codes if c not in cached]
    if not missing:
        return cached
    print("  DART 기업코드 내려받는 중 (%d개 신규)…" % len(missing))
    try:
        raw = http_get(DART_CORP_ZIP % key, timeout=60)
        if raw[:2] != b"PK":
            raise ValueError("zip 이 아님 — %s" % raw[:200].decode("utf-8", "replace"))
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
                DART_LIST % (key, corp, bgn.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
            ).decode("utf-8"))
        except Exception as e:                          # noqa: BLE001
            warn("DART 공시 조회 실패 (%s): %s" % (name, e))
            continue
        status = str(doc.get("status", ""))
        if status == "013":
            continue
        if status != "000":
            warn("DART 응답 코드 %s (%s) — %s" % (status, name, doc.get("message", "")))
            continue
        for it in (doc.get("list") or [])[:5]:
            d = it.get("rcept_dt") or ""
            out.append({
                "name": name, "code": stock_code,
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
ENT = {"&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">",
       "&quot;": '"', "&#39;": "'", "&apos;": "'"}
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def unescape(s):
    for k, v in ENT.items():
        s = s.replace(k, v)
    s = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), s)
    return re.sub(r"\s+", " ", s).strip()


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
            title = unescape(it.findtext("title") or "")
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

            body = unescape(TAG.sub(" ", it.findtext("description") or ""))
            if source and body.endswith(source):
                body = body[: -len(source)].strip()

            date = ""
            m = re.search(r"(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})",
                          it.findtext("pubDate") or "")
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


def block(specs, prev_list, extra=None, with_returns=False):
    out = []
    for s in specs:
        q = quote(s["symbol"])
        if q is None:
            old = carry(prev_list, s["name"])
            if old:
                out.append(old)
            continue
        row = {"name": s["name"], "symbol": s["symbol"], "dp": s.get("dp", 2),
               "value": rnd(q["price"]), "change": rnd(q["change"]),
               "pct": rnd(q["pct"], 3), "asof": q["asof"], "spark": q["spark"],
               "mkt": market_of(s["symbol"]), "_d": q["date"]}
        if with_returns:
            row["ret"] = returns_block(q["hist"])
        if extra:
            row.update({k: s.get(k) for k in extra})
        out.append(row)
    return out


def build_ratios(cfg, prev):
    """비율선. 두 종목의 날짜를 맞춰 나눈 시계열입니다."""
    out = []
    for spec in cfg.get("ratios", []):
        a, b = series(spec["num"]), series(spec["den"])
        if not a or not b:
            old = carry(prev.get("ratios"), spec["name"])
            if old:
                out.append(old)
            continue
        bmap = dict(b["hist"])
        pairs = [(d, v / bmap[d]) for d, v in a["hist"] if bmap.get(d)]
        if len(pairs) < 30:
            continue
        ser = downsample(pairs, 130)
        out.append({
            "name": spec["name"], "num": spec["num"], "den": spec["den"],
            "axis": spec.get("axis", "-"),
            "up": spec.get("up", ""), "down": spec.get("down", ""),
            "note": spec.get("note", ""),
            "asof": pairs[-1][0][5:].replace("-", "."),
            "ret": returns_block(pairs),
            "dates": [d[5:] for d, _ in ser],
            "vals": [rnd(v, 6) for _, v in ser],
            "_full": pairs,          # 레짐 계산용 (JSON 저장 전에 제거)
        })
    return out


def regime_score(ratios, names, lookback):
    """
    각 비율의 '최근 lookback일 변화율'을 그 비율이 지난 1년간 보여온 변동 크기로
    나눠 -100~+100 으로 환산하고 평균냅니다.

    평균을 빼지 않는(=0 기준) 이유: 추세가 꾸준한 비율도 제대로 +로 잡히게 하기
    위해서입니다. 평균 대비로 재면 1년 내내 오른 비율이 '평범함(0점)'으로
    나와버려, 정작 중요한 지속적 우위를 놓칩니다.
    변동 크기로 나누므로 자산마다 변동성이 달라도 같은 잣대가 됩니다.
    백분위(pct)는 참고용으로 같이 담습니다.
    """
    parts, used = [], []
    for r in ratios:
        if r["name"] not in names or "_full" not in r:
            continue
        full = r["_full"]
        if len(full) < lookback + 30:
            continue
        chgs = []
        for i in range(lookback, len(full)):
            a, b = full[i - lookback][1], full[i][1]
            if a:
                chgs.append((b - a) / a * 100.0)
        if len(chgs) < 30:
            continue
        cur = chgs[-1]
        rms = (sum(x * x for x in chgs) / len(chgs)) ** 0.5
        if rms <= 0:
            continue
        score = max(-100.0, min(100.0, cur / rms * 60.0))
        parts.append(score)
        used.append({"name": r["name"], "chg": rnd(cur, 2),
                     "pct": rnd(percentile_rank(chgs, cur), 1),
                     "score": rnd(score, 1)})
    if not parts:
        return None, used
    return rnd(sum(parts) / len(parts), 1), used


QUADRANTS = {
    ("up", "down"): {
        "label": "회복 · 골디락스",
        "desc": "성장은 살아나는데 물가 압력은 눌려 있는 국면. 역사적으로 위험자산 전반, 특히 기술·성장주와 소형주가 유리했던 구간입니다.",
        "good": ["기술·성장주", "소형주", "경기소비재", "하이일드"],
        "bad": ["필수소비재", "유틸리티", "금", "현금"],
    },
    ("up", "up"): {
        "label": "확장 · 과열",
        "desc": "성장과 물가가 함께 오르는 국면. 실물·가격 전가력이 있는 쪽이 유리하고, 장기채는 금리 상승에 불리합니다.",
        "good": ["에너지", "소재·산업재", "원자재", "가치주"],
        "bad": ["장기국채", "고밸류 성장주", "리츠"],
    },
    ("down", "up"): {
        "label": "스태그플레이션",
        "desc": "성장은 꺾이는데 물가는 안 잡히는 국면. 가장 다루기 까다로운 구간으로, 주식·채권이 동시에 부진할 수 있습니다.",
        "good": ["금", "에너지", "현금·단기채", "물가연동채"],
        "bad": ["경기소비재", "소형주", "장기국채", "하이일드"],
    },
    ("down", "down"): {
        "label": "둔화 · 침체",
        "desc": "성장과 물가가 함께 내려가는 국면. 금리 인하 기대가 붙으면서 듀레이션이 긴 안전자산이 유리해집니다.",
        "good": ["장기국채", "유틸리티", "필수소비재", "헬스케어"],
        "bad": ["소형주", "에너지", "경기민감주", "하이일드"],
    },
}


def build_regime(cfg, ratios):
    rc = cfg.get("regime", {})
    lb = int(rc.get("lookback_days", 63))
    g, g_used = regime_score(ratios, set(rc.get("growth", [])), lb)
    i, i_used = regime_score(ratios, set(rc.get("inflation", [])), lb)
    if g is None or i is None:
        return None
    q = QUADRANTS[("up" if g >= 0 else "down", "up" if i >= 0 else "down")]
    return {"growth": g, "inflation": i, "lookbackDays": lb,
            "growthParts": g_used, "inflationParts": i_used,
            "quadrant": q["label"], "desc": q["desc"],
            "good": q["good"], "bad": q["bad"],
            "quadrants": [{"key": k[0] + "|" + k[1], "label": v["label"]}
                          for k, v in QUADRANTS.items()]}


ROW_BLOCKS = ("indices", "macro", "rates", "sectors", "assets",
              "crypto", "watchlist")


def build_markets(out):
    """시장별 최근 거래일을 모으고, 각 행에 지연 여부를 표시합니다.

    같은 시장의 여러 종목이 같은 날짜를 가리키면 그게 그 시장의 최근
    거래일입니다. 오늘 날짜보다 며칠 이르더라도 휴장이면 정상입니다.
    반대로 같은 시장 안에서 혼자만 날짜가 뒤처진 항목은 진짜 지연입니다.
    """
    rows = []
    for key in ROW_BLOCKS:
        for r in out.get(key) or []:
            if not r.get("mkt") and r.get("symbol"):
                r["mkt"] = market_of(r["symbol"])
            rows.append(r)

    latest = {}
    for r in rows:
        d, m = r.get("_d"), r.get("mkt")
        if d and m and d > latest.get(m, ""):
            latest[m] = d

    today = datetime.now(KST).date()
    markets = []
    for m in MARKET_ORDER:
        if m not in latest:
            continue
        d = latest[m]
        try:
            lag = (today - datetime.strptime(d, "%Y-%m-%d").date()).days
        except Exception:
            lag = 0
        markets.append({"code": m, "label": MARKET_LABEL.get(m, m),
                        "date": d, "asof": d[5:].replace("-", "."),
                        "lagDays": lag})

    # 개별 항목이 자기 시장보다 뒤처졌을 때만 지연으로 봅니다.
    for r in rows:
        d, m = r.pop("_d", None), r.get("mkt")
        if d and m and m in latest and d < latest[m]:
            r["behind"] = True

    return markets


def main():
    cfg = strip_comments(load_json(CONFIG))
    cal = strip_comments(load_json(CALENDAR, {"items": []}))
    prev = load_json(OUT, {}) or {}

    out = {"site": cfg.get("site", {}),
           "updatedAt": datetime.now(KST).isoformat(timespec="seconds"),
           "homeIndices": cfg.get("home_indices", []),
           "calendar": cal.get("items", []),
           "periods": [p[0] for p in PERIODS] + ["ytd"]}

    print("배치 조회…")
    allsyms = []
    for key in ("indices", "macro", "rates", "sectors", "crypto",
                "watchlist", "cross_assets"):
        allsyms += [s["symbol"] for s in cfg.get(key, []) if s.get("symbol")]
    for r in cfg.get("ratios", []):
        allsyms += [r["num"], r["den"]]
    if cfg.get("sentiment", {}).get("symbol"):
        allsyms.append(cfg["sentiment"]["symbol"])
    batch_prefetch(allsyms)

    print("지수…")
    out["indices"] = block(cfg.get("indices", []), prev.get("indices"),
                           extra=["group"], with_returns=True)
    print("환율·원자재…")
    out["macro"] = block(cfg.get("macro", []), prev.get("macro"),
                         extra=["unit", "prefix", "suffix"], with_returns=True)
    print("금리…")
    out["rates"] = block(cfg.get("rates", []), prev.get("rates"), extra=["years"])

    print("스프레드…")
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
    out["sectors"] = block(cfg.get("sectors", []), prev.get("sectors"),
                           extra=["cyc"], with_returns=True)
    print("크로스에셋…")
    out["assets"] = block(cfg.get("cross_assets", []), prev.get("assets"),
                          extra=["group"], with_returns=True)
    print("크립토…")
    out["crypto"] = block(cfg.get("crypto", []), prev.get("crypto"))

    print("비율선…")
    ratios = build_ratios(cfg, prev)
    print("레짐 판정…")
    out["regime"] = build_regime(cfg, ratios) or (prev.get("regime"))
    for r in ratios:
        r.pop("_full", None)
    out["ratios"] = ratios or (prev.get("ratios") or [])

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
            "pct": rnd(q["pct"], 3), "asof": q["asof"], "spark": q["spark"],
            "ret": returns_block(q["hist"])})

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

    out["markets"] = build_markets(out)
    out["warnings"] = warnings

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))

    reg = out.get("regime") or {}
    print("\n완료 — 지수 %d · 섹터 %d · 크로스에셋 %d · 비율 %d · 관심종목 %d "
          "· 뉴스 %d · 공시 %d · 경고 %d"
          % (len(out["indices"]), len(out["sectors"]), len(out["assets"]),
             len(out["ratios"]), len(out["watchlist"]), len(out["news"]),
             len(out["filings"]), len(warnings)))
    if reg:
        print("레짐: %s (성장 %+.0f / 물가 %+.0f)"
              % (reg.get("quadrant", "?"), reg.get("growth", 0), reg.get("inflation", 0)))

    if not out["indices"] and not out["watchlist"]:
        print("수집된 시세가 하나도 없습니다.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
