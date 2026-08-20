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
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests

# ─────────────────────────── 설정 ───────────────────────────
BENCH = "SPY"
MACRO = ["SPY", "QQQ", "DIA", "IWM", "^VIX", "^TNX", "DX-Y.NYB"]
MACRO_ALIAS = {"^VIX": "VIX", "^TNX": "US10Y", "DX-Y.NYB": "DXY"}

SECTORS = ["XLK","XLC","XLY","XLP","XLE","XLF","XLV","XLI","XLB","XLRE","XLU"]
THEMES  = ["SMH","IGV","SKYY","GRID","URA","XOP","ITA","ARKX","BOTZ","CIBR",
           "QTUM","XBI","PAVE","IYT","GDX","FFTY",
           "WGMI","IBIT","ETHA"]          # 크립토는 이 3개만
ETFS    = sorted(set(MACRO + SECTORS + THEMES))

# Stockbee TC2000 v12.4 스캔 필터 (스캔별로 다르다 — 전역 필터 아님)
VOL_4PCT    = 100_000    # 4% 스캔: 당일 거래량 ≥ 10만주 AND V > V1
DOLLAR_VOL  = 250_000    # 25%/50%/13% 스캔: 20일 평균 종가×거래량 ≥ $250K
MIN_C20     = 5.0        # 월간 스캔: 20일 전 종가 ≥ $5
BREADTH_DAYS = 60        # 매 실행 시 다시 계산할 브레스 일수
HISTORY_KEEP = 250       # data.json 에 남길 최대 일수
SERIES_KEEP = 280        # 내보낼 ETF 종가 일수
LOOKBACK_DAYS = 420      # 내려받을 일봉 기간(달력일)
SECTOR_MIN_CORR = 0.35   # 이 값 미만이면 섹터 미분류
SECTOR_CORR_WIN = 120    # 상관 계산에 쓸 거래일

UA = {"User-Agent": "Mozilla/5.0 (compatible; sector-dashboard/1.0)"}


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


def fetch_stockbee() -> dict:
    """Stockbee Market Monitor 공개 구글시트에서 최근 값을 읽는다. 실패하면 {}."""
    try:
        r = requests.get(STOCKBEE_SHEET + "&output=csv", headers=UA, timeout=40)
        if r.ok and "," in r.text[:2000] and "<html" not in r.text[:200].lower():
            df = pd.read_csv(io.StringIO(r.text))
            got = parse_stockbee(df)
            if got:
                return got
    except Exception:
        pass
    try:  # csv 게시가 막혀 있으면 HTML 표로
        r = requests.get(STOCKBEE_SHEET + "&output=html", headers=UA, timeout=40)
        for df in pd.read_html(io.StringIO(r.text)):
            if df.shape[0] > 5 and df.shape[1] > 8:
                if not any("date" in str(c).lower() for c in df.columns):
                    df.columns = df.iloc[0]
                    df = df.iloc[1:]
                got = parse_stockbee(df)
                if got:
                    return got
    except Exception:
        pass
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
            df = pd.read_csv(io.StringIO(txt), sep="|")
            df = df[~df.iloc[:, 0].astype(str).str.startswith("File Creation")]
            sym_col = "Symbol" if "Symbol" in df.columns else "ACT Symbol"
            if "Test Issue" in df.columns:
                df = df[df["Test Issue"] != "Y"]
            if etf_col in df.columns:
                df = df[df[etf_col] != "Y"]
            for s in df[sym_col].astype(str):
                s = s.strip()
                # 워런트/유닛/우선주/권리 제외
                if not s or len(s) > 5 or any(ch in s for ch in ".$+-^"):
                    continue
                if len(s) == 5 and s[-1] in "WRUPQZ":
                    continue
                out.add(s)
        except Exception as e:
            print(f"  ! 유니버스 {url} 실패: {e}", file=sys.stderr)
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

    # 빠졌거나 빈 티커는 한 번 더 시도
    missing = [t for t in tickers if t not in C.columns or C[t].dropna().empty]
    if missing and len(missing) < len(tickers):
        try:
            time.sleep(3)
            C2, V2 = _retry_batch(missing, period_days)
            for t in C2.columns:
                if t not in C.columns or C[t].dropna().empty:
                    C[t] = C2[t]; V[t] = V2.get(t)
            print(f"  재시도로 {len([t for t in missing if t in C.columns and not C[t].dropna().empty])}/{len(missing)}개 보충")
        except Exception:
            pass
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


# ─────────────────────── 메인 ───────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="유니버스 상한 (테스트용)")
    ap.add_argument("--quotes-only", action="store_true")
    ap.add_argument("--no-sector", action="store_true")
    ap.add_argument("--no-stockbee", action="store_true", help="원본 시트를 쓰지 않고 자체 계산만")
    ap.add_argument("--out", default="data.json")
    args = ap.parse_args()

    print("1) ETF 시세")
    C_etf, V_etf = download(ETFS, LOOKBACK_DAYS)
    C_etf, V_etf, _ = stooq_fill(C_etf, V_etf, ETFS)
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
        print(f"  받은 종목 {C.shape[1]}개 / 거래일 {C.shape[0]}일")

        if not args.no_sector:
            print("4) 섹터 분류")
            secmap = build_sector_map(C, C_etf)

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

    out = {
        "asof": asof,
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
