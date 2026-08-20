#!/usr/bin/env python3
"""
build_data.py — 섹터 + 마켓브레스 데이터를 하루 한 번 계산해 data.json 으로 저장한다.

  python build_data.py                 # 전체 실행
  python build_data.py --limit 800     # 유니버스를 800개로 줄여 빠르게 테스트
  python build_data.py --quotes-only   # ETF 시세만 (브레스 건너뜀)

출력: data.json  (대시보드가 이 파일 하나만 읽는다)

브레스 정의는 Stockbee Market Monitor 를 따른다.
T2108 은 원래 Worden 지표라 그대로 가져올 수 없어 같은 정의(40일선 위 비율)로 직접 계산한다.
값이 원본과 소수점까지 같지는 않다. 방향과 극단 수준이 맞으면 충분하다.
"""
from __future__ import annotations
import argparse, io, json, os, sys, time
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

MIN_PRICE   = 3.0        # 저가주 제외
MIN_VOL     = 100_000    # 최소 거래량(주) — Stockbee 필터
BREADTH_DAYS = 60        # 매 실행 시 다시 계산할 브레스 일수
HISTORY_KEEP = 250       # data.json 에 남길 최대 일수
LOOKBACK_DAYS = 400      # 내려받을 일봉 기간(달력일)

UA = {"User-Agent": "Mozilla/5.0 (compatible; sector-dashboard/1.0)"}


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
                df = yf.download(
                    chunk, period=f"{period_days}d", interval="1d",
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
    return C, V


def stooq_close(ticker: str) -> pd.Series | None:
    """yfinance 가 막혔을 때의 예비 경로 (서버에서 부르므로 CORS 무관)."""
    sym = {"^VIX": "^vix", "^TNX": "10usy.b", "DX-Y.NYB": "dx.f"}.get(ticker, ticker.lower() + ".us")
    try:
        r = requests.get(f"https://stooq.com/q/d/l/?s={sym}&i=d", headers=UA, timeout=30)
        df = pd.read_csv(io.StringIO(r.text))
        if "Close" not in df:
            return None
        s = pd.Series(df["Close"].values, index=pd.to_datetime(df["Date"]))
        return s.dropna()
    except Exception:
        return None


# ─────────────────────── 계산 ───────────────────────
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


def quotes_block(C: pd.DataFrame) -> dict:
    out = {}
    bench = C[BENCH].dropna() if BENCH in C else None
    for t in C.columns:
        s = C[t].dropna()
        if len(s) < 10:
            continue
        row = {
            "px": round(float(s.iloc[-1]), 4),
            "d1": pct_back(s, 1), "w1": pct_back(s, 5), "m1": pct_back(s, 21),
            "m3": pct_back(s, 63), "m6": pct_back(s, 126), "ytd": ytd_pct(s),
        }
        for n, key in ((5, "rs_w1"), (21, "rs_m1"), (63, "rs_m3")):
            if bench is not None and len(bench) > n:
                a, b = pct_back(s, n), pct_back(bench, n)
                row[key] = None if (a is None or b is None) else round(a - b, 2)
        for win, key in ((50, "a50"), (200, "a200")):
            row[key] = bool(s.iloc[-1] > s.tail(win).mean()) if len(s) >= win else None
        out[MACRO_ALIAS.get(t, t)] = row
    return out


def breadth_block(C: pd.DataFrame, V: pd.DataFrame, days: int) -> list[dict]:
    """Stockbee Market Monitor 필드를 일자별로 계산."""
    C = C.sort_index()
    V = V.reindex(index=C.index, columns=C.columns)

    liquid = V.rolling(20, min_periods=5).mean() >= MIN_VOL
    valid = (C >= MIN_PRICE) & liquid & C.notna()

    r1 = C.pct_change(1) * 100
    vol_up = V > V.shift(1)                       # 4% 스캔은 거래량 증가 조건 포함
    up4 = ((r1 >= 4) & vol_up & valid).sum(axis=1)
    dn4 = ((r1 <= -4) & vol_up & valid).sum(axis=1)

    def moved(n, pct, up=True):
        ch = C.pct_change(n) * 100
        cond = (ch >= pct) if up else (ch <= -pct)
        return (cond & valid).sum(axis=1)

    q25u, q25d = moved(65, 25, True), moved(65, 25, False)
    m25u, m25d = moved(20, 25, True), moved(20, 25, False)
    m50u, m50d = moved(20, 50, True), moved(20, 50, False)
    u13, d13 = moved(34, 13, True), moved(34, 13, False)

    def above(win):
        ma = C.rolling(win, min_periods=win).mean()
        ok = (C > ma) & valid
        n = (valid & ma.notna()).sum(axis=1)
        return (ok.sum(axis=1) / n.replace(0, np.nan) * 100).round(1)

    t2108, a50, a200 = above(40), above(50), above(200)

    hi252 = C.rolling(252, min_periods=60).max()
    lo252 = C.rolling(252, min_periods=60).min()
    nh = ((C >= hi252) & valid).sum(axis=1)
    nl = ((C <= lo252) & valid).sum(axis=1)

    r5 = (up4.rolling(5).sum() / dn4.rolling(5).sum().replace(0, np.nan)).round(2)
    r10 = (up4.rolling(10).sum() / dn4.rolling(10).sum().replace(0, np.nan)).round(2)
    uni = valid.sum(axis=1)

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
        })
    return rows


# ─────────────────────── 메인 ───────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="유니버스 상한 (테스트용)")
    ap.add_argument("--quotes-only", action="store_true")
    ap.add_argument("--out", default="data.json")
    args = ap.parse_args()

    print("1) ETF 시세")
    C_etf, _ = download(ETFS, LOOKBACK_DAYS)
    missing = [t for t in ETFS if t not in C_etf.columns or C_etf[t].dropna().empty]
    for t in missing:                       # 예비 경로
        s = stooq_close(t)
        if s is not None and len(s):
            C_etf[t] = s
            print(f"  · {t} → Stooq 로 보충")
    quotes = quotes_block(C_etf)
    print(f"  {len(quotes)}개 종목")

    breadth, universe, bsrc = [], None, None
    if not args.quotes_only:
        print("2) 유니버스")
        syms = load_universe(args.limit)
        print("3) 전 종목 일봉 (몇 분 걸린다)")
        C, V = download(syms, LOOKBACK_DAYS)
        print(f"  받은 종목 {C.shape[1]}개 / 거래일 {C.shape[0]}일")
        print("4) 브레스 계산")
        breadth = breadth_block(C, V, BREADTH_DAYS)
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
    for r in breadth:
        merged[r["d"]] = r
    history = [merged[k] for k in sorted(merged)][-HISTORY_KEEP:]

    asof = None
    if BENCH in C_etf and not C_etf[BENCH].dropna().empty:
        asof = C_etf[BENCH].dropna().index[-1].strftime("%Y-%m-%d")

    out = {
        "asof": asof,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "universe": universe or (history[-1]["universe"] if history else None),
        "breadth_source": bsrc or prev.get("breadth_source"),
        "quotes": quotes or prev.get("quotes", {}),
        "breadth": {"history": history},
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print(f"완료 → {args.out}  ({os.path.getsize(args.out)/1024:.0f} KB, 브레스 {len(history)}일)")


if __name__ == "__main__":
    main()
