#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
마켓 데스크 데이터 수집기.

GitHub Actions 안에서 실행됩니다. 브라우저가 아니라 서버에서 돌기 때문에
CORS 제약이 없고, API 키도 필요하지 않습니다.

  config.json          → 무엇을 수집할지
  config/calendar.json → 경제 일정(직접 관리)
  data/latest.json     → 결과 (페이지가 이 파일 하나만 읽습니다)

수집에 실패한 항목은 추정하지 않습니다. 직전 실행의 값을 그대로 두고
stale 표시만 남깁니다 — 화면에 틀린 숫자가 뜨는 것보다 낫습니다.
"""

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
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "config.json")
CALENDAR = os.path.join(ROOT, "config", "calendar.json")
OUT = os.path.join(ROOT, "data", "latest.json")

KST = timezone(timedelta(hours=9))
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

warnings = []


# --------------------------------------------------------------------------
# 공통
# --------------------------------------------------------------------------

def http_get(url, tries=3, timeout=20):
    """GET 후 bytes 반환. 실패하면 마지막 예외를 던집니다."""
    last = None
    ctx = ssl.create_default_context()
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "*/*",
                "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
            })
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                return r.read()
        except Exception as e:                      # noqa: BLE001
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
    """키가 _ 로 시작하는 설명용 항목을 제거합니다."""
    if isinstance(d, dict):
        return {k: strip_comments(v) for k, v in d.items() if not k.startswith("_")}
    if isinstance(d, list):
        return [strip_comments(x) for x in d]
    return d


# --------------------------------------------------------------------------
# 시세 — 야후 파이낸스 차트 API (키 불필요)
# --------------------------------------------------------------------------

YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=5d&interval=1d"

# 야후가 막히거나 값을 안 줄 때 쓰는 예비 소스 (stooq, CSV)
STOOQ_FALLBACK = {
    "^GSPC": "^spx", "^IXIC": "^ndq", "^DJI": "^dji",
    "^N225": "^nkx", "^VIX": "^vix",
    "GC=F": "gc.f", "SI=F": "si.f", "CL=F": "cl.f", "BZ=F": "cb.f",
    "KRW=X": "usdkrw",
}


def quote_yahoo(symbol):
    raw = http_get(YAHOO.format(sym=urllib.parse.quote(symbol)))
    doc = json.loads(raw.decode("utf-8"))
    res = (doc.get("chart") or {}).get("result") or []
    if not res:
        raise ValueError("빈 응답")
    meta = res[0].get("meta") or {}

    price = meta.get("regularMarketPrice")
    prev = meta.get("previousClose")
    if prev is None:
        prev = meta.get("chartPreviousClose")

    # meta 에 없으면 일봉 종가 배열에서 마지막 두 개를 씁니다.
    if price is None or prev is None:
        closes = (((res[0].get("indicators") or {}).get("quote") or [{}])[0]).get("close") or []
        closes = [c for c in closes if c is not None]
        if len(closes) >= 2:
            price = closes[-1] if price is None else price
            prev = closes[-2] if prev is None else prev

    if price is None:
        raise ValueError("가격 없음")

    ts = meta.get("regularMarketTime")
    asof = datetime.fromtimestamp(ts, KST).strftime("%m.%d") if ts else \
        datetime.now(KST).strftime("%m.%d")

    change = None if prev is None else price - prev
    pct = None if not prev else (price - prev) / prev * 100.0
    return {"price": price, "change": change, "pct": pct, "asof": asof,
            "currency": meta.get("currency") or ""}


def quote_stooq(stooq_sym):
    url = "https://stooq.com/q/l/?s=%s&f=sd2t2ohlcv&h&e=csv" % stooq_sym
    text = http_get(url).decode("utf-8", "replace").strip().splitlines()
    if len(text) < 2:
        raise ValueError("빈 CSV")
    head = [h.strip().lower() for h in text[0].split(",")]
    row = text[1].split(",")
    rec = dict(zip(head, row))
    close, open_ = rec.get("close"), rec.get("open")
    if not close or close.upper() == "N/D":
        raise ValueError("값 없음")
    price = float(close)
    prev = float(open_) if open_ and open_.upper() != "N/D" else None
    change = None if prev is None else price - prev
    pct = None if not prev else (price - prev) / prev * 100.0
    asof = (rec.get("date") or "")[5:].replace("-", ".")
    return {"price": price, "change": change, "pct": pct,
            "asof": asof or datetime.now(KST).strftime("%m.%d"), "currency": ""}


def quote(symbol):
    """야후 → stooq 순으로 시도. 둘 다 실패하면 None."""
    try:
        return quote_yahoo(symbol)
    except Exception as e:                          # noqa: BLE001
        alt = STOOQ_FALLBACK.get(symbol)
        if alt:
            try:
                q = quote_stooq(alt)
                warn("%s: 야후 실패(%s) → stooq 로 대체" % (symbol, type(e).__name__))
                return q
            except Exception as e2:                 # noqa: BLE001
                warn("%s: 야후·stooq 모두 실패 (%s / %s)" % (symbol, e, e2))
                return None
        warn("%s: 시세 수집 실패 (%s)" % (symbol, e))
        return None


# --------------------------------------------------------------------------
# 뉴스 — 구글 뉴스 RSS (키 불필요)
# --------------------------------------------------------------------------

GNEWS = "https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"
TAG = re.compile(r"<[^>]+>")


def collect_news(queries, per_query):
    items = []
    seen = set()
    for spec in queries:
        q = spec.get("q", "")
        kicker = spec.get("kicker", "")
        try:
            raw = http_get(GNEWS.format(q=urllib.parse.quote(q + " when:2d")))
            root = ET.fromstring(raw)
        except Exception as e:                      # noqa: BLE001
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

            # 구글 뉴스 제목은 "기사제목 - 언론사" 형태입니다. 언론사는 떼어냅니다.
            source = ""
            src_el = it.find("source")
            if src_el is not None and src_el.text:
                source = src_el.text.strip()
                if source and title.endswith(" - " + source):
                    title = title[: -(len(source) + 3)].strip()
            elif " - " in title:
                title, source = title.rsplit(" - ", 1)
                title, source = title.strip(), source.strip()

            body = TAG.sub(" ", it.findtext("description") or "")
            body = re.sub(r"\s+", " ", body).strip()
            if source and body.endswith(source):
                body = body[: -len(source)].strip()
            body = body[:180]

            date = ""
            pub = it.findtext("pubDate")
            if pub:
                m = re.search(r"(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})", pub)
                if m:
                    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
                    try:
                        date = "%02d.%02d" % (months.index(m.group(2)) + 1, int(m.group(1)))
                    except ValueError:
                        date = ""

            items.append({"kicker": kicker, "title": title, "body": body,
                          "source": (source + (" · " + date if date else "")).strip(" ·"),
                          "url": link})
            got += 1
        time.sleep(0.5)
    return items


# --------------------------------------------------------------------------
# 조립
# --------------------------------------------------------------------------

def carry_over(prev_list, name, field="name"):
    for x in prev_list or []:
        if x.get(field) == name:
            return x
    return None


def main():
    cfg = strip_comments(load_json(CONFIG))
    cal = strip_comments(load_json(CALENDAR, {"items": []}))
    prev = load_json(OUT, {}) or {}

    out = {
        "site": cfg.get("site", {}),
        "updatedAt": datetime.now(KST).isoformat(timespec="seconds"),
        "indices": [],
        "macro": [],
        "sentiment": {},
        "watchlist": [],
        "news": [],
        "calendar": cal.get("items", []),
        "warnings": [],
    }

    print("지수 수집…")
    for spec in cfg.get("indices", []):
        q = quote(spec["symbol"])
        old = carry_over(prev.get("indices"), spec["name"])
        if q is None and old:
            old = dict(old); old["stale"] = True
            out["indices"].append(old); continue
        if q is None:
            continue
        out["indices"].append({
            "name": spec["name"], "symbol": spec["symbol"], "dp": spec.get("dp", 2),
            "value": q["price"], "change": q["change"], "pct": q["pct"], "asof": q["asof"],
        })
        time.sleep(0.4)

    print("환율·원자재·금리 수집…")
    for spec in cfg.get("macro", []):
        q = quote(spec["symbol"])
        old = carry_over(prev.get("macro"), spec["name"])
        if q is None and old:
            old = dict(old); old["stale"] = True
            out["macro"].append(old); continue
        if q is None:
            continue
        out["macro"].append({
            "name": spec["name"], "symbol": spec["symbol"], "unit": spec.get("unit", ""),
            "dp": spec.get("dp", 2), "prefix": spec.get("prefix", ""),
            "suffix": spec.get("suffix", ""),
            "value": q["price"], "pct": q["pct"], "asof": q["asof"],
        })
        time.sleep(0.4)

    print("시장심리 수집…")
    s = cfg.get("sentiment", {})
    q = quote(s.get("symbol", "^VIX"))
    if q:
        out["sentiment"] = {"vix": q["price"], "asof": q["asof"] + " 기준",
                            "avg10y": s.get("avg10y", 19.72), "note": s.get("note", "")}
    else:
        out["sentiment"] = dict(prev.get("sentiment") or {}, note=s.get("note", ""), stale=True)

    print("관심종목 수집…")
    for spec in cfg.get("watchlist", []):
        q = quote(spec["symbol"])
        old = carry_over(prev.get("watchlist"), spec["name"])
        if q is None and old:
            old = dict(old); old["stale"] = True
            out["watchlist"].append(old); continue
        if q is None:
            continue
        cur = q["currency"] or ("KRW" if spec["symbol"].endswith((".KS", ".KQ")) else "USD")
        out["watchlist"].append({
            "name": spec["name"], "symbol": spec["symbol"],
            "code": spec["symbol"].split(".")[0], "market": spec.get("market", ""),
            "currency": cur, "price": q["price"], "change": q["change"],
            "pct": q["pct"], "asof": q["asof"],
        })
        time.sleep(0.4)

    print("뉴스 수집…")
    news = collect_news(cfg.get("news_queries", []), int(cfg.get("news_per_query", 2)))
    out["news"] = news if news else (prev.get("news") or [])
    if not news:
        warn("뉴스를 하나도 못 가져와 직전 값을 유지합니다.")

    out["warnings"] = warnings

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print("\n완료 — 지수 %d · 매크로 %d · 관심종목 %d · 뉴스 %d · 경고 %d"
          % (len(out["indices"]), len(out["macro"]), len(out["watchlist"]),
             len(out["news"]), len(warnings)))

    # 전부 실패하면 0바이트짜리 화면을 커밋하는 대신 실패로 끝냅니다.
    if not out["indices"] and not out["watchlist"]:
        print("수집된 시세가 하나도 없습니다.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
