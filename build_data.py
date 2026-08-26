#!/usr/bin/env python3
"""
build_data.py — 섹터 + 마켓브레스 데이터를 하루 한 번 계산해 data.json 으로 저장한다.

  python build_data.py                 # 전체 실행
  python build_data.py --limit 800     # 유니버스를 800개로 줄여 빠르게 테스트
  python build_data.py --quotes-only   # ETF 시세만 (브레스 건너뜀)
  python build_data.py --no-sector     # 섹터별 브레스 생략

출력: data.json  (대시보드가 이 파일 하나만 읽는다)

v2 변경점
  · ETF 종가 시계열을 그대로 내보낸다 → 대시보드가 순위 변화·사분면 꼬리를 직접 계산
  · 상대강도를 비율식으로 통일하고 여섯 기간 모두 계산
  · 섹터별 4% 돌파 카운트 추가 (상관 기반 근사 분류)
  · yfinance 가 막히면 Stooq 로 보충

브레스 정의는 Stockbee Market Monitor 를 따른다.
T2108 은 원래 Worden 지표라 그대로 가져올 수 없어 같은 정의(40일선 위 비율)로 직접 계산한다.
값이 원본과 소수점까지 같지는 않다. 방향과 극단 수준이 맞으면 충분하다.
"""
from __future__ import annotations
import argparse, io, json, os, sys, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests

# ─────────────────────────── 설정 ───────────────────────────
BENCH = "SPY"
MACRO = ["SPY", "QQQ", "DIA", "IWM", "^VIX", "^TNX", "DX-Y.NYB", "CL=F"]
MACRO_ALIAS = {"^VIX": "VIX", "^TNX": "US10Y", "DX-Y.NYB": "DXY", "CL=F": "WTI"}

SECTORS = ["XLK","XLC","XLY","XLP","XLE","XLF","XLV","XLI","XLB","XLRE","XLU"]
THEMES  = ["SMH","IGV","SKYY","GRID","URA","XOP","ITA","ARKX","BOTZ","CIBR",
           "QTUM","XBI","PAVE","IYT","GDX","FFTY",
           "WGMI","IBIT","ETHA"]          # 크립토는 이 3개만
ETFS    = sorted(set(MACRO + SECTORS + THEMES))

# Stockbee TC2000 v12.4 스캔 필터 (스캔별로 다르다 — 전역 필터 아님)
VOL_4PCT    = 100_000    # 4% 스캔: 당일 거래량 ≥ 10만주 AND V > V1
DOLLAR_VOL  = 250_000    # 25%/50%/13% 스캔: 20일 평균 종가×거래량 ≥ $250K
MIN_C20     = 5.0        # 월간 스캔: 20일 전 종가 ≥ $5
TICKER_CAP  = 500        # 섹터별 상세 종목 수 상한 (사실상 전체, 폭주 방지용 천장)
BREADTH_DAYS = 60        # 매 실행 시 다시 계산할 브레스 일수
HISTORY_KEEP = 250       # data.json 에 남길 최대 일수
SERIES_KEEP = 280        # 내보낼 ETF 종가 일수
LOOKBACK_DAYS = 420      # 내려받을 일봉 기간(달력일)
SECTOR_MIN_CORR = 0.35   # 이 값 미만이면 섹터 미분류
SECTOR_CORR_WIN = 120    # 상관 계산에 쓸 거래일

UA = {"User-Agent": "Mozilla/5.0 (compatible; sector-dashboard/1.0)"}
SESSION = None   # main 에서 확정 거래일로 채운다


# ─────────────────────── 거래일 판정 ───────────────────────
def last_session_date():
    """미국 주식장 기준 '마지막으로 마감이 확정된 거래일'.
    뉴욕 시각 16:15 이전이면 전 거래일을 돌려준다(서머타임 자동 반영).
    휴장일은 판별하지 않지만, 이 날짜 '이하'로만 자르므로 문제되지 않는다."""
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/New_York"))
    except Exception:                       # tzdata 없는 환경 대비: EST 고정
        now = datetime.now(timezone.utc) - timedelta(hours=5)
    d = now.date()
    if now.hour * 60 + now.minute < 16 * 60 + 15:
        d -= timedelta(days=1)
    while d.weekday() >= 5:                 # 토·일 → 직전 금요일
        d -= timedelta(days=1)
    return d


# ─────────────────────── Stockbee 원본 시트 ───────────────────────
STOCKBEE_SHEET = ("https://docs.google.com/spreadsheet/pub"
                  "?key=0Am_cU8NLIU20dEhiQnVHN3Nnc3B1S3J6eGhKZFo0N3c")

def _sb_col(h: str):
    """시트 헤더 → 내부 필드명 (헤더 문구가 바뀌어도 견디게 느슨하게 매칭)."""
    n = "".join(ch for ch in h.lower() if ch.isalnum())
    if "t2108" in n: return "t2108"
    if "universe" in n: return "universe"
    if "5day" in n: return "r5"
    if "10day" in n: return "r10"
    up, dn = "up" in n, "down" in n
    if "4" in n and "quarter" not in n and "month" not in n and "34" not in n:
        if up: return "up4"
        if dn: return "dn4"
    if "25" in n and "quarter" in n: return "q25u" if up else ("q25d" if dn else None)
    if "25" in n and "month" in n:   return "m25u" if up else ("m25d" if dn else None)
    if "50" in n and "month" in n:   return "m50u" if up else ("m50d" if dn else None)
    if "13" in n and "34" in n:      return "u13" if up else ("d13" if dn else None)
    return None


def parse_stockbee(df: pd.DataFrame) -> dict:
    """헤더 매칭 → {날짜: {필드: 값}}. 못 알아본 컬럼은 버린다."""
    cols = {}
    for c in df.columns:
        k = _sb_col(str(c))
        if k and k not in cols.values():
            cols[c] = k
    df = df.loc[:, [c for c in df.columns
                    if str(c).strip() and str(c).lower() not in ("nan", "none")
                    and not str(c).strip().isdigit()]]
    date_col = None
    for c in df.columns:
        if "date" in str(c).lower():
            date_col = c
            break
    if date_col is None or "up4" not in cols.values():
        return {}
    out = {}
    for _, row in df.iterrows():
        d = pd.to_datetime(str(row[date_col]), errors="coerce")
        if pd.isna(d):
            continue
        rec = {}
        for c, k in cols.items():
            v = str(row[c]).replace(",", "").replace("%", "").strip()
            try:
                fv = float(v)
            except ValueError:
                continue
            rec[k] = round(fv, 2) if k in ("r5", "r10", "t2108") else int(fv)
        if rec:
            out[d.strftime("%Y-%m-%d")] = rec
    return out


def _find_header(df: pd.DataFrame) -> pd.DataFrame:
    """구글 pubhtml 표는 행번호 열·병합 제목행이 섞여 온다. 'Date'가 든 행을 찾아 헤더로 세운다."""
    for i in range(min(8, len(df))):
        vals = [str(x).strip().lower() for x in df.iloc[i].tolist()]
        if any(v == "date" or v.startswith("date") for v in vals):
            out = df.iloc[i + 1:].copy()
            out.columns = [str(x) for x in df.iloc[i]]
            return out
    return df


def fetch_stockbee() -> dict:
    """Stockbee Market Monitor 공개 구글시트. 여러 주소를 차례로 시도하고,
    실패 원인을 로그로 남긴다. 전부 실패하면 {} (자체 계산으로 폴백)."""
    key = "0Am_cU8NLIU20dEhiQnVHN3Nnc3B1S3J6eGhKZFo0N3c"
    tries = [
        ("legacy-csv",  f"https://docs.google.com/spreadsheet/pub?key={key}&output=csv"),
        ("gviz-csv",    f"https://docs.google.com/spreadsheet/gviz/tq?key={key}&tqx=out:csv"),
        ("old-csv",     f"https://spreadsheets.google.com/pub?key={key}&output=csv"),
        ("old-gviz",    f"https://spreadsheets.google.com/tq?key={key}&tqx=out:csv"),
        ("widget-html", f"https://docs.google.com/spreadsheet/pub?key={key}&output=html&widget=true"),
        ("pub-html",    f"https://docs.google.com/spreadsheet/pub?key={key}"),
    ]
    for name, url in tries:
        try:
            bua = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")}
            r = requests.get(url, headers=bua, timeout=40, allow_redirects=True)
            if not r.ok:
                print(f"  · {name}: HTTP {r.status_code}")
                continue
            head = r.text[:400].lower()
            if "<html" not in head and "," in r.text[:2000]:
                raw = pd.read_csv(io.StringIO(r.text), header=None, dtype=str,
                                  keep_default_na=False, on_bad_lines="skip")
                got = parse_stockbee(_find_header(raw))
                if got:
                    print(f"  · {name}: OK (csv, {len(got)}일)")
                    return got
                snip = " ".join(r.text[:300].split())
                print(f"  · {name}: csv 받았으나 필드 매칭 실패 | 앞부분: {snip[:160]}")
                continue
            # HTML → 표 추출
            try:
                tables = pd.read_html(io.StringIO(r.text))
            except ImportError as e:
                print(f"  · {name}: HTML 파서 없음({e}) — daily.yml 에 lxml 설치 필요")
                continue
            except ValueError:
                print(f"  · {name}: HTML 에 표 없음")
                continue
            for df in sorted(tables, key=lambda d: -(d.shape[0] * d.shape[1])):
                got = parse_stockbee(_find_header(df))
                if got:
                    print(f"  · {name}: OK (html, {df.shape[0]}행)")
                    return got
            snip = " ".join(r.text[:200].split())
            print(f"  · {name}: 표 {len(tables)}개 중 매칭 없음 | 응답 앞부분: {snip[:140]}")
        except Exception as e:
            print(f"  · {name}: {type(e).__name__} {e}")
    return {}


def fetch_fear_greed() -> dict:
    """CNN Fear & Greed Index. 실패하면 {} — 화면에서 그 칸만 빠진다."""
    bua = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
           "Accept": "application/json, text/plain, */*"}
    urls = [
        "https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
        "https://production.dataviz.cnn.io/index/fearandgreed/current",
    ]
    for u in urls:
        try:
            r = requests.get(u, headers=bua, timeout=30)
            if not r.ok:
                print(f"  · F&G {u.rsplit('/', 1)[-1]}: HTTP {r.status_code}")
                continue
            j = r.json()
            cur = j.get("fear_and_greed") or j
            score = cur.get("score")
            if score is None:
                continue
            out = {
                "score": round(float(score), 1),
                "rating": cur.get("rating"),
                "prev": (round(float(cur["previous_close"]), 1)
                         if cur.get("previous_close") is not None else None),
                "w1": (round(float(cur["previous_1_week"]), 1)
                       if cur.get("previous_1_week") is not None else None),
                "m1": (round(float(cur["previous_1_month"]), 1)
                       if cur.get("previous_1_month") is not None else None),
            }
            hist = (j.get("fear_and_greed_historical") or {}).get("data") or []
            if hist:
                out["history"] = [
                    {"d": datetime.fromtimestamp(h["x"] / 1000, timezone.utc).strftime("%Y-%m-%d"),
                     "v": round(float(h["y"]), 1)}
                    for h in hist[-60:] if h.get("x") and h.get("y") is not None
                ]
            print(f"  · F&G: {out['score']} ({out.get('rating')})")
            return out
        except Exception as e:
            print(f"  · F&G {u.rsplit('/', 1)[-1]}: {type(e).__name__} {e}")
    return {}


# ─────────────────────── 섹터 분류 ───────────────────────
# 나스닥 스크리너가 상장 전 종목의 섹터를 준다. 한 번의 요청으로 끝난다.
NASDAQ_SECTOR = {
    "health care": "XLV", "healthcare": "XLV",
    "technology": "XLK", "computer and technology": "XLK",
    "finance": "XLF", "financials": "XLF", "financial": "XLF",
    "energy": "XLE", "oils/energy": "XLE",
    "consumer discretionary": "XLY", "consumer services": "XLY",
    "retail/wholesale": "XLY", "auto/tires/trucks": "XLY",
    "consumer staples": "XLP", "consumer non-durables": "XLP",
    "consumer durables": "XLY",
    "industrials": "XLI", "industrial products": "XLI",
    "capital goods": "XLI", "transportation": "XLI", "aerospace": "XLI",
    "basic materials": "XLB", "basic industries": "XLB",
    "real estate": "XLRE",
    "utilities": "XLU", "public utilities": "XLU",
    "telecommunications": "XLC", "communication services": "XLC",
    "media": "XLC",
}


def fetch_listed_sectors() -> dict:
    """나스닥 스크리너에서 {티커: SPDR섹터} 를 받는다. 실패하면 {}."""
    bua = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
           "Accept": "application/json, text/plain, */*"}
    url = ("https://api.nasdaq.com/api/screener/stocks"
           "?tableonly=true&limit=25000&download=true")
    try:
        r = requests.get(url, headers=bua, timeout=60)
        if not r.ok:
            print(f"  ! 섹터 목록: HTTP {r.status_code}")
            return {}
        rows = ((r.json().get("data") or {}).get("rows")) or []
        out, unknown = {}, set()
        for row in rows:
            sym = str(row.get("symbol", "")).strip().upper()
            sec = str(row.get("sector", "")).strip().lower()
            if not sym or not sec:
                continue
            spdr = NASDAQ_SECTOR.get(sec)
            if spdr:
                out[sym] = spdr
            else:
                unknown.add(sec)
        if unknown:
            print(f"  · 매핑 못한 섹터명: {sorted(unknown)[:6]}")
        print(f"  거래소 섹터 {len(out)}종목")
        return out
    except Exception as e:
        print(f"  ! 섹터 목록: {type(e).__name__} {e}")
        return {}


# ─────────────────────── 유니버스 ───────────────────────
def load_universe(limit: int | None) -> list[str]:
    """나스닥/기타 상장 보통주 목록. ETF·테스트·워런트·우선주는 뺀다."""
    out: set[str] = set()
    for url, etf_col in (
        ("https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt", "ETF"),
        ("https://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt", "ETF"),
    ):
        try:
            txt = requests.get(url, headers=UA, timeout=40).text
            # dtype=str, keep_default_na=False 가 없으면 티커 "NA"·"NAN" 이 실수로 바뀐다
            df = pd.read_csv(io.StringIO(txt), sep="|", dtype=str,
                             keep_default_na=False, na_values=[])
            df = df[~df.iloc[:, 0].astype(str).str.startswith("File Creation")]
            sym_col = "Symbol" if "Symbol" in df.columns else "ACT Symbol"
            if "Test Issue" in df.columns:
                df = df[df["Test Issue"] != "Y"]
            if etf_col in df.columns:
                df = df[df[etf_col] != "Y"]
            for s in df[sym_col].astype(str):
                s = s.strip().upper()
                # 워런트/유닛/우선주/권리 제외
                if not s or len(s) > 5 or any(ch in s for ch in ".$+-^"):
                    continue
                if len(s) == 5 and s[-1] in "WRUPQZ":
                    continue
                out.add(s)
            print(f"  · {url.rsplit('/', 1)[-1]}: {len(df)}행")
        except Exception as e:
            print(f"  ! 유니버스 {url.rsplit('/', 1)[-1]} 실패: {type(e).__name__} {e}")
    if not out:
        print("  ! 유니버스를 하나도 받지 못했습니다 — 브레스를 건너뜁니다.")
    elif len(out) < 5000:
        print(f"  ! 유니버스가 {len(out)}개뿐입니다. 보통 6,000개 이상이라 한쪽 목록이 실패했을 수 있습니다.")
    syms = sorted(out)
    if limit:
        syms = syms[:limit]
    print(f"  유니버스 {len(syms)}개")
    return syms


# ─────────────────────── 가격 다운로드 ───────────────────────
def download(tickers: list[str], period_days: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """종가·거래량 DataFrame(index=날짜, columns=티커) 반환."""
    import yfinance as yf
    closes, vols = [], []
    B = 200
    for i in range(0, len(tickers), B):
        chunk = tickers[i:i + B]
        for attempt in range(3):
            try:
                start = (datetime.now(timezone.utc) - pd.Timedelta(days=period_days)).strftime("%Y-%m-%d")
                df = yf.download(
                    chunk, start=start, interval="1d",
                    auto_adjust=False, progress=False, threads=True, group_by="column",
                )
                if df is None or df.empty:
                    raise RuntimeError("빈 응답")
                c = df["Close"] if "Close" in df else pd.DataFrame()
                v = df["Volume"] if "Volume" in df else pd.DataFrame()
                if isinstance(c, pd.Series):
                    c = c.to_frame(chunk[0]); v = v.to_frame(chunk[0])
                closes.append(c); vols.append(v)
                break
            except Exception as e:
                if attempt == 2:
                    print(f"  ! {chunk[0]}… 배치 실패: {e}", file=sys.stderr)
                else:
                    time.sleep(2 + attempt * 3)
        print(f"  {min(i+B, len(tickers))}/{len(tickers)}", end="\r", flush=True)
    print()
    C = pd.concat(closes, axis=1) if closes else pd.DataFrame()
    V = pd.concat(vols, axis=1) if vols else pd.DataFrame()
    C = C.loc[:, ~C.columns.duplicated()].sort_index()
    V = V.loc[:, ~V.columns.duplicated()].sort_index()

    # 빠졌거나 빈 티커는 소량 배치로 두 차례 더 시도한다
    for attempt in range(2):
        missing = [t for t in tickers if t not in C.columns or C[t].dropna().empty]
        if not missing or len(missing) == len(tickers):
            break
        got = 0
        for i in range(0, len(missing), 50):
            chunk = missing[i:i + 50]
            try:
                time.sleep(1.5)
                C2, V2 = _retry_batch(chunk, period_days)
                for t in C2.columns:
                    if t not in C.columns or C[t].dropna().empty:
                        C[t] = C2[t]
                        if t in V2.columns:
                            V[t] = V2[t]
                        got += 1
            except Exception:
                continue
        print(f"  재시도{attempt + 1}: {got}/{len(missing)}개 보충")
        if got == 0:
            break
    return C, V


def _retry_batch(tickers, period_days):
    import yfinance as yf
    start = (datetime.now(timezone.utc) - pd.Timedelta(days=period_days)).strftime("%Y-%m-%d")
    df = yf.download(tickers, start=start, interval="1d",
                     auto_adjust=False, progress=False, threads=True, group_by="column")
    c = df["Close"] if "Close" in df else pd.DataFrame()
    v = df["Volume"] if "Volume" in df else pd.DataFrame()
    if isinstance(c, pd.Series):
        c = c.to_frame(tickers[0]); v = v.to_frame(tickers[0])
    return c.sort_index(), v.sort_index()


def stooq_one(ticker: str):
    """Stooq 일봉 하나. 서버에서 부르므로 CORS 무관."""
    sym = {"^VIX": "^vix", "^TNX": "10usy.b", "DX-Y.NYB": "dx.f"}.get(
        ticker, ticker.lower().replace(".", "-") + ".us")
    try:
        r = requests.get(f"https://stooq.com/q/d/l/?s={sym}&i=d", headers=UA, timeout=25)
        df = pd.read_csv(io.StringIO(r.text))
        if "Close" not in df or len(df) < 30:
            return None, None
        idx = pd.to_datetime(df["Date"])
        c = pd.Series(df["Close"].values, index=idx, name=ticker).dropna()
        v = pd.Series(df["Volume"].values, index=idx, name=ticker) if "Volume" in df else None
        return c, v
    except Exception:
        return None, None


def stooq_fill(C: pd.DataFrame, V: pd.DataFrame, wanted: list[str], workers=12):
    """yfinance 가 놓친 티커를 Stooq 로 메운다."""
    missing = [t for t in wanted if t not in C.columns or C[t].dropna().empty]
    if not missing:
        return C, V, 0
    print(f"  Stooq 로 {len(missing)}개 보충 시도")
    got = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for t, (c, v) in zip(missing, ex.map(stooq_one, missing)):
            if c is None:
                continue
            C[t] = c
            if v is not None:
                V[t] = v
            got += 1
    print(f"  {got}개 보충됨")
    return C.sort_index(), V.sort_index(), got


# ─────────────────────── 계산 ───────────────────────
PERIOD_N = {"d1": 1, "w1": 5, "m1": 21, "m3": 63, "m6": 126}


def pct_back(s: pd.Series, n: int):
    s = s.dropna()
    if len(s) < n + 1:
        return None
    a, b = s.iloc[-1], s.iloc[-1 - n]
    return None if not b else round(float(a / b - 1) * 100, 2)


def ytd_pct(s: pd.Series):
    s = s.dropna()
    if s.empty:
        return None
    y = s.index[-1].year
    prev = s[s.index.year < y]
    base = prev.iloc[-1] if len(prev) else s.iloc[0]
    return None if not base else round(float(s.iloc[-1] / base - 1) * 100, 2)


def rel_pct(a, b):
    """상대강도 = 비율식.  (1+a)/(1+b) - 1.   단순 차이(a-b)와는 다르다."""
    if a is None or b is None:
        return None
    denom = 1 + b / 100.0
    if denom == 0:
        return None
    return round(((1 + a / 100.0) / denom - 1) * 100, 2)


def quotes_block(C: pd.DataFrame) -> dict:
    out = {}
    bench = C[BENCH].dropna() if BENCH in C else None
    bref = {}
    if bench is not None:
        for p, n in PERIOD_N.items():
            bref[p] = pct_back(bench, n)
        bref["ytd"] = ytd_pct(bench)

    for t in C.columns:
        s = C[t].dropna()
        if len(s) < 10:
            continue
        row = {"px": round(float(s.iloc[-1]), 4)}
        for p, n in PERIOD_N.items():
            row[p] = pct_back(s, n)
        row["ytd"] = ytd_pct(s)
        for p in list(PERIOD_N) + ["ytd"]:            # 여섯 기간 전부
            row["rs_" + p] = rel_pct(row[p], bref.get(p))
        for win, key in ((50, "a50"), (200, "a200")):
            row[key] = bool(s.iloc[-1] > s.tail(win).mean()) if len(s) >= win else None
        key = MACRO_ALIAS.get(t, t)
        if key == "US10Y" and row.get("px") is not None:
            # 벤더에 따라 ^TNX 가 % (4.7) 로 오기도, 지수식 ×10 (47.0) 으로 오기도 한다
            med = float(s.tail(60).median())
            if med > 20:
                row["px"] = round(row["px"] / 10.0, 3)
        out[key] = row
    return out


def series_block(C: pd.DataFrame):
    """공통 날짜축 + 티커별 종가 배열. 대시보드가 이걸로 순위변화·꼬리를 계산한다."""
    C = C.tail(SERIES_KEEP)
    dates = [d.strftime("%Y-%m-%d") for d in C.index]
    ser = {}
    for t in C.columns:
        col = C[t]
        if col.dropna().empty:
            continue
        key = MACRO_ALIAS.get(t, t)
        div = 1.0
        if key == "US10Y":
            med = col.dropna().tail(60).median()
            if med is not None and med > 20:
                div = 10.0                      # ^TNX 지수식(×10)일 때만 나눈다
        ser[key] = [None if pd.isna(x) else round(float(x) / div, 4) for x in col]
    return dates, ser


def breadth_block(C: pd.DataFrame, V: pd.DataFrame, days: int, sector_map=None):
    """Stockbee Market Monitor — TC2000 v12.4 공개 스캔 공식을 그대로 구현.
    https://stockbee.blogspot.com/2014/08/how-i-get-market-monitor-numbers.html
    (rows, sectors_today) 반환."""
    C = C.sort_index()
    V = V.reindex(index=C.index, columns=C.columns)

    # 안 거래된 날의 결측을 직전 종가로 메운다(최대 10일). 이걸 안 하면
    # 유동성 낮은 종목이 롤링 창의 결측 하루 때문에 장기 스캔에서 전부 빠진다.
    C = C.ffill(limit=10)
    Vavg = V.fillna(0)          # 평균 거래량 계산용 — 안 거래된 날은 0으로

    r1 = C.pct_change(1) * 100
    # 4%: 100*(C-C1)/C1 >= 4 AND V >= 100000 AND V > V1   (가격·달러볼륨 필터 없음)
    vol_ok = (V >= VOL_4PCT) & (V > V.shift(1))
    up4_mask = (r1 >= 4) & vol_ok
    dn4_mask = (r1 <= -4) & vol_ok
    up4, dn4 = up4_mask.sum(axis=1), dn4_mask.sum(axis=1)

    # 25%/50%/13% 스캔 공통 필터: AVGC20 * AVGV20 >= 250000
    dollar = (C.rolling(20, min_periods=16).mean()
              * Vavg.rolling(20, min_periods=16).mean()) >= DOLLAR_VOL

    # 분기: 65일 최저/최고 "종가" 대비 (65일 전 종가가 아니다!)
    minc65 = C.rolling(65, min_periods=52).min()
    maxc65 = C.rolling(65, min_periods=52).max()
    q25u = ((100 * ((C + .01) - (minc65 + .01)) / (minc65 + .01) >= 25) & dollar).sum(axis=1)
    q25d = ((100 * ((C + .01) - (maxc65 + .01)) / (maxc65 + .01) <= -25) & dollar).sum(axis=1)

    # 월간: C20 >= 5 AND 달러볼륨 AND 20일 전 종가 대비 (이건 point-to-point 맞음)
    c20 = C.shift(20).replace(0, np.nan)
    mch = 100 * (C - c20) / c20
    mfil = (c20 >= MIN_C20) & dollar
    m25u = ((mch >= 25) & mfil).sum(axis=1)
    m25d = ((mch <= -25) & mfil).sum(axis=1)
    m50u = ((mch >= 50) & mfil).sum(axis=1)
    m50d = ((mch <= -50) & mfil).sum(axis=1)

    # 13%/34일: 34일 최저/최고 종가 대비
    minc34 = C.rolling(34, min_periods=28).min()
    maxc34 = C.rolling(34, min_periods=28).max()
    u13 = ((100 * ((C + .01) - (minc34 + .01)) / (minc34 + .01) >= 13) & dollar).sum(axis=1)
    d13 = ((100 * ((C + .01) - (maxc34 + .01)) / (maxc34 + .01) <= -13) & dollar).sum(axis=1)

    # 추세 폭: T2108 계열은 Worden 지표라 정의(이동평균선 위 비율)만 같게 계산.
    # Worden 은 필터 없이 전 종목 기준 — 여기서도 필터 없이 계산한다.
    def above(win):
        ma = C.rolling(win, min_periods=max(10, int(win * 0.8))).mean()
        ok = (C > ma)
        n = ma.notna().sum(axis=1)
        return (ok.sum(axis=1) / n.replace(0, np.nan) * 100).round(1)

    t2108, a50, a200 = above(40), above(50), above(200)

    hi252 = C.rolling(252, min_periods=60).max()
    lo252 = C.rolling(252, min_periods=60).min()
    nh = (C >= hi252).sum(axis=1)
    nl = (C <= lo252).sum(axis=1)

    # 등락 종목 수 → 맥클렐란 오실레이터/서메이션 (비율 보정식)
    adv = (r1 > 0).sum(axis=1)
    dec = (r1 < 0).sum(axis=1)
    rana = (adv - dec) / (adv + dec).replace(0, np.nan) * 1000
    mco = (rana.ewm(span=19, adjust=False).mean()
           - rana.ewm(span=39, adjust=False).mean()).round(1)
    mcs = mco.fillna(0).cumsum().round(0)

    r5 = (up4.rolling(5).sum() / dn4.rolling(5).sum().replace(0, np.nan)).round(2)
    r10 = (up4.rolling(10).sum() / dn4.rolling(10).sum().replace(0, np.nan)).round(2)
    uni = C.notna().sum(axis=1)

    # 섹터별 요약 (최신 거래일 기준, 4% 스캔과 같은 조건)
    sectors = []
    if sector_map and len(C.index):
        m25u_mask = (mch >= 25) & mfil
        groups = {}
        for tk, sec in sector_map.items():
            groups.setdefault(sec, []).append(tk)
        d_last = C.index[-1]
        r1_last, mch_last = r1.loc[d_last], mch.loc[d_last]

        def picks(mask_row, cols, val_row, cap=TICKER_CAP):
            hits = []
            for t in cols:
                try:
                    if bool(mask_row[t]):
                        v = val_row.get(t)
                        if v is not None and np.isfinite(v):
                            hits.append((t, round(float(v), 1)))
                except (KeyError, TypeError, ValueError):
                    continue
            hits.sort(key=lambda x: -abs(x[1]))
            return [{"t": t, "c": v} for t, v in hits[:cap]]

        for sec, cols in groups.items():
            cols = [c for c in cols if c in C.columns]
            if not cols:
                continue
            u_ser = up4_mask[cols].sum(axis=1)
            sectors.append({
                "s": sec,
                "up4": int(u_ser.loc[d_last]),
                "dn4": int(dn4_mask[cols].sum(axis=1).loc[d_last]),
                "u5": int(u_ser.tail(5).sum()),
                "m25u": int(m25u_mask[cols].sum(axis=1).loc[d_last]),
                "n": len(cols),
                # 탭했을 때 보여줄 실제 종목 (상위 몇 개만)
                "up": picks(up4_mask.loc[d_last], cols, r1_last),
                "dn": picks(dn4_mask.loc[d_last], cols, r1_last),
                "mom": picks(m25u_mask.loc[d_last], cols, mch_last),
            })
        sectors.sort(key=lambda r: -r["up4"])

    def val(s, d):
        v = s.get(d)
        if v is None or (isinstance(v, float) and not np.isfinite(v)):
            return None
        return round(float(v), 2) if isinstance(v, float) else int(v)

    rows = []
    for d in C.index[-days:]:
        rows.append({
            "d": d.strftime("%Y-%m-%d"),
            "up4": val(up4, d), "dn4": val(dn4, d), "r5": val(r5, d), "r10": val(r10, d),
            "q25u": val(q25u, d), "q25d": val(q25d, d),
            "m25u": val(m25u, d), "m25d": val(m25d, d),
            "m50u": val(m50u, d), "m50d": val(m50d, d),
            "u13": val(u13, d), "d13": val(d13, d),
            "t2108": val(t2108, d), "a50": val(a50, d), "a200": val(a200, d),
            "nh": val(nh, d), "nl": val(nl, d), "universe": val(uni, d),
            "adv": val(adv, d), "dec": val(dec, d), "mco": val(mco, d), "mcs": val(mcs, d),
        })
    return rows, sectors


def build_sector_map(C_uni: pd.DataFrame, C_etf: pd.DataFrame) -> dict:
    """
    종목을 11개 SPDR 섹터 중 하나로 붙인다.
    무료로 GICS 를 통째로 받을 방법이 마땅치 않아, 최근 120거래일 일간수익률의
    상관이 가장 높은 섹터 ETF 로 분류한다. 근사값이며 상관이 낮으면 미분류로 둔다.
    """
    secs = [s for s in SECTORS if s in C_etf.columns]
    if not secs or C_uni.empty:
        return {}
    ru = C_uni.pct_change().tail(SECTOR_CORR_WIN)
    re_ = C_etf[secs].pct_change().reindex(ru.index).tail(SECTOR_CORR_WIN)

    U = ru.to_numpy(dtype="float64")
    E = re_.to_numpy(dtype="float64")
    ok = ~np.isnan(E).any(axis=1)
    U, E = U[ok], E[ok]
    if len(U) < 40:
        return {}
    with np.errstate(invalid="ignore"):
        Um = np.nanmean(U, axis=0); Us = np.nanstd(U, axis=0)
        Uz = np.nan_to_num((U - Um) / np.where(Us == 0, np.nan, Us))
        Ez = (E - E.mean(axis=0)) / np.where(E.std(axis=0) == 0, np.nan, E.std(axis=0))
    corr = np.nan_to_num((Uz.T @ np.nan_to_num(Ez)) / len(U), nan=-1.0)
    best, bestv = corr.argmax(axis=1), corr.max(axis=1)

    out = {}
    for i, tk in enumerate(ru.columns):
        if bestv[i] >= SECTOR_MIN_CORR:
            out[tk] = secs[best[i]]
    print(f"  섹터 분류 {len(out)}/{len(ru.columns)}개 (상관 {SECTOR_MIN_CORR} 이상)")
    return out


def diagnose(tk: str):
    """한 종목이 스캔에 왜 걸리는지/안 걸리는지 관문별로 보여준다."""
    print(f"=== {tk} 진단 (기준 거래일 {SESSION}) ===")
    C, V = download([tk], LOOKBACK_DAYS)
    if tk not in C.columns or C[tk].dropna().empty:
        print("  ✗ 시세를 못 받았습니다. 티커가 맞는지, 상장폐지·심볼변경은 아닌지 확인하세요.")
        return
    C = C.loc[C.index <= pd.Timestamp(SESSION)]
    V = V.reindex(index=C.index)
    c, v = C[tk].dropna(), V[tk]
    d = c.index[-1]
    px, prev = float(c.iloc[-1]), float(c.iloc[-2])
    ch = (px / prev - 1) * 100
    vol = float(v.get(d, float("nan")))
    vol1 = float(v.get(c.index[-2], float("nan")))
    print(f"  최종 거래일 {d.date()} · 종가 {px:.2f} · 전일대비 {ch:+.2f}%")
    print(f"  거래량 {vol:,.0f} (전일 {vol1:,.0f})")

    ok = lambda b: "OK " if b else "✗  "
    print(f"  {ok(abs(ch) >= 4)}① 4% 이상 변동          : {ch:+.2f}%")
    print(f"  {ok(vol >= VOL_4PCT)}② 당일 거래량 10만주 이상 : {vol:,.0f}")
    print(f"  {ok(vol > vol1)}③ 전일보다 거래량 증가    : {'예' if vol > vol1 else '아니오'}")
    dv = float(c.tail(20).mean() * v.tail(20).mean())
    print(f"  {ok(dv >= DOLLAR_VOL)}④ 20일 평균 달러볼륨 25만$ : ${dv:,.0f}  (분기·월간 스캔용)")

    sec = fetch_listed_sectors().get(tk)
    print(f"  {ok(bool(sec))}⑤ 섹터 분류              : {sec or '미분류 — 어느 섹터 목록에도 안 나옵니다'}")
    if str(d.date()) != str(SESSION):
        print(f"  ! 이 종목의 최종 거래일({d.date()})이 기준일({SESSION})과 다릅니다.")
    print("  ①②③ 을 모두 통과해야 '오늘 4% 돌파'에 들어갑니다.")


# ─────────────────────── 메인 ───────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="유니버스 상한 (테스트용)")
    ap.add_argument("--quotes-only", action="store_true")
    ap.add_argument("--no-sector", action="store_true")
    ap.add_argument("--no-stockbee", action="store_true", help="원본 시트를 쓰지 않고 자체 계산만")
    ap.add_argument("--force", action="store_true", help="최신이어도 다시 계산")
    ap.add_argument("--why", metavar="TICKER",
                    help="특정 종목이 왜 안 잡히는지 진단하고 종료")
    ap.add_argument("--out", default="data.json")
    args = ap.parse_args()

    global SESSION
    SESSION = last_session_date()
    if args.why:
        diagnose(args.why.strip().upper())
        return
    sess_str = SESSION.strftime("%Y-%m-%d")

    prev_peek = {}
    if os.path.exists(args.out):
        try:
            prev_peek = json.load(open(args.out, encoding="utf-8"))
        except Exception:
            prev_peek = {}
    if not args.force and prev_peek.get("asof") == sess_str:
        hist = (prev_peek.get("breadth") or {}).get("history") or []
        done = hist and hist[-1].get("d") == sess_str and (hist[-1].get("sb") or args.no_stockbee)
        if done:
            print(f"이미 {sess_str} 데이터가 최신입니다 — 건너뜁니다.")
            return

    print(f"기준 거래일: {sess_str}")
    print("1) ETF 시세")
    C_etf, V_etf = download(ETFS, LOOKBACK_DAYS)
    C_etf, V_etf, _ = stooq_fill(C_etf, V_etf, ETFS)
    # DXY·^TNX 같은 24시간/장외 종목은 '오늘 진행 중' 시세가 마지막 봉으로 끼어든다.
    # 미국 주식장의 마지막 완결일(SPY 기준) 이후 행은 잘라서, 모든 값을 종가 대 종가로 통일한다.
    cutoff = pd.Timestamp(SESSION)
    if BENCH in C_etf.columns and not C_etf[BENCH].dropna().empty:
        cutoff = min(cutoff, C_etf[BENCH].dropna().index[-1])
    C_etf = C_etf.loc[C_etf.index <= cutoff]
    V_etf = V_etf.loc[V_etf.index <= cutoff]
    print(f"  확정 거래일 {cutoff.date()} 까지만 사용 (장중 미완성 봉 제외)")
    quotes = quotes_block(C_etf)
    dates, series = series_block(C_etf)
    print(f"  {len(quotes)}개 종목 / 시계열 {len(dates)}일")

    breadth, sectors, universe, bsrc, secmap = [], [], None, None, {}
    if not args.quotes_only:
        print("2) 유니버스")
        syms = load_universe(args.limit)
        print("3) 전 종목 일봉 (몇 분 걸린다)")
        C, V = download(syms, LOOKBACK_DAYS)
        if C.empty or C.shape[1] < len(syms) * 0.3:
            print("  ! yfinance 수집이 부실하다 — Stooq 로 보충", file=sys.stderr)
            C, V, _ = stooq_fill(C, V, syms)
        good = int((C.notna().sum(axis=0) > 20).sum())
        print(f"  받은 종목 {good}/{len(syms)}개 (유효 시세 기준) / 거래일 {C.shape[0]}일")
        if good < len(syms) * 0.7:
            print(f"  ! 수집률 {good / max(len(syms), 1):.0%} — 낮습니다. 브레스 카운트가 실제보다 작게 나옵니다.")

        if not args.no_sector:
            print("4) 섹터 분류")
            secmap = fetch_listed_sectors()
            secmap = {k: v for k, v in secmap.items() if k in C.columns}
            listed_n = len(secmap)
            missing = [t for t in C.columns if t not in secmap]
            if missing:                 # 거래소 목록에 없는 종목만 상관법으로 보충
                corr = build_sector_map(C[missing], C_etf)
                secmap.update(corr)
                print(f"  거래소 {listed_n}종목 + 상관 보충 {len(corr)}종목 "
                      f"= {len(secmap)}종목 (미분류 {C.shape[1] - len(secmap)})")

        C = C.loc[C.index <= cutoff]
        V = V.loc[V.index <= cutoff]
        print("5) 브레스 계산")
        breadth, sectors = breadth_block(C, V, BREADTH_DAYS, secmap or None)
        if breadth:
            universe = breadth[-1]["universe"]
            bsrc = "자체 계산 (yfinance)"

    # 이전 결과와 합쳐 히스토리를 이어붙인다
    prev = {}
    if os.path.exists(args.out):
        try:
            prev = json.load(open(args.out, encoding="utf-8"))
        except Exception:
            prev = {}
    old = (prev.get("breadth") or {}).get("history") or []
    merged = {r["d"]: r for r in old}
    # 자체 계산 병합: 이미 원본 시트(sb=1)로 채워진 행은 자체-전용 필드만 갱신한다.
    # 이렇게 해야 시트가 하루 죽어도 이미 받아둔 원본 숫자가 자체값으로 퇴행하지 않는다.
    SELF_KEYS = ("nh", "nl", "a50", "a200", "adv", "dec", "mco", "mcs")
    for r in breadth:
        cur = merged.get(r["d"])
        if cur and cur.get("sb"):
            for k in SELF_KEYS:
                if r.get(k) is not None:
                    cur[k] = r[k]
        else:
            merged[r["d"]] = r

    # Stockbee 원본 시트가 있으면 겹치는 필드를 원본 값으로 덮는다.
    # NH/NL·%MA·맥클렐란·섹터별 카운트는 시트에 없으므로 자체 계산이 그대로 남는다.
    if not args.no_stockbee:
        print("6) Stockbee 원본 시트")
        sb = fetch_stockbee()
        if sb:
            for d, vals in sb.items():
                base = merged.get(d, {"d": d})
                base.update(vals)
                base["sb"] = 1                    # 출처 표시: 원본 시트
                merged[d] = base
            bsrc = "Stockbee 원본 시트 · NH/NL·%MA·맥클렐란·섹터는 자체 계산"
            print(f"  {len(sb)}일 병합 (최신 {max(sb)})")
        else:
            print("  ! 시트 접근 실패 — 자체 계산 유지")
    history = [merged[k] for k in sorted(merged)][-HISTORY_KEEP:]

    asof = dates[-1] if dates else prev.get("asof")

    print("7) CNN Fear & Greed")
    fng = fetch_fear_greed() or prev.get("fng")

    out = {
        "asof": asof,
        "fng": fng,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "universe": (history[-1].get("universe") if history else None) or universe,
        "breadth_source": bsrc or prev.get("breadth_source"),
        "sector_map": "상관 기반 근사" if secmap else prev.get("sector_map"),
        "sectors": sectors or prev.get("sectors"),
        "bench": BENCH,
        "dates": dates or prev.get("dates"),
        "series": series or prev.get("series"),
        "quotes": quotes or prev.get("quotes", {}),
        "breadth": {"history": history},
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print(f"완료 → {args.out}  ({os.path.getsize(args.out)/1024:.0f} KB, "
          f"브레스 {len(history)}일, 시계열 {len(series)}종목)")


if __name__ == "__main__":
    main()
