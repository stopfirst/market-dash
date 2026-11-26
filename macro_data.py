#!/usr/bin/env python3
"""
매크로 자금흐름 블록 — data.json 의 "macro" 키만 갱신한다.

차트보다 먼저 '돈의 방향'을 본다:
  달러 · 단기금리(2년물) · 장기금리 · 원유 · 물가(CPI) · 유동성(연준 순유동성)
  · 신용(하이일드 스프레드) · 시장 전체 · 나스닥 · 글로벌 · 섹터 공격/방어

  python macro_data.py --out data.json

브레스 계산(build_data.py)과 분리돼 있어서, 여기서 실패해도 기존 화면은 그대로다.
시세: yfinance  /  거시지표: FRED 공개 CSV(키 불필요)
"""
from __future__ import annotations
import argparse, io, json, os, sys, time
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests

UA = {"User-Agent": "Mozilla/5.0 (compatible; market-dash-macro/1.0)"}

# 시세(일봉 종가). 키 = 화면에 쓰는 이름
TICKERS = {
    "SPY": "SPY", "QQQ": "QQQ", "IWM": "IWM", "DIA": "DIA",
    "VIX": "^VIX", "DXY": "DX-Y.NYB", "WTI": "CL=F", "GOLD": "GC=F",
    "SHY": "SHY", "TLT": "TLT", "HYG": "HYG", "LQD": "LQD",
    "ACWI": "ACWI", "EFA": "EFA", "EEM": "EEM",
    "KOSPI": "^KS11", "USDKRW": "KRW=X", "USDJPY": "JPY=X", "BTC": "BTC-USD",
    "SMH": "SMH", "TQQQ": "TQQQ",
    "XLK": "XLK", "XLC": "XLC", "XLY": "XLY", "XLP": "XLP", "XLE": "XLE",
    "XLF": "XLF", "XLV": "XLV", "XLI": "XLI", "XLB": "XLB", "XLRE": "XLRE", "XLU": "XLU",
}
OFFENSE = ["XLK", "XLY", "XLC", "XLF", "XLI"]      # 경기민감·성장
DEFENSE = ["XLP", "XLU", "XLV", "XLRE"]            # 방어

# FRED 시리즈
FRED = {
    "US2Y": "DGS2",            # 2년물 금리(%) — 연준 기대
    "US10Y": "DGS10",          # 10년물 금리(%)
    "CURVE": "T10Y2Y",         # 10Y-2Y (%p)
    "FFR": "DFF",              # 실효 연방기금금리
    "CPI": "CPIAUCSL",         # CPI (월)
    "CORE": "CPILFESL",        # 근원 CPI (월)
    "WALCL": "WALCL",          # 연준 총자산 (백만$, 주)
    "TGA": "WTREGEN",          # 재무부 일반계정 (백만$, 주)
    "RRP": "RRPONTSYD",        # 역레포 (십억$, 일)
    "HYOAS": "BAMLH0A0HYM2",   # 하이일드 스프레드 (%)
    "BROAD": "DTWEXBGS",       # 무역가중 달러(광의, 26개국) — BBDXY 무료 대용
    "EMUSD": "DTWEXEMEGS",     # 신흥국 대비 무역가중 달러
}
PERIODS = {"d1": 1, "w1": 5, "m1": 21, "m3": 63}
KEEP = 260


def fred(series_id: str, start="2022-01-01") -> pd.Series:
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}&cosd={start}"
    for a in range(3):
        try:
            r = requests.get(url, timeout=30)   # FRED 는 임의 UA 를 막는다(응답 지연) — 기본 UA 사용
            r.raise_for_status()
            df = pd.read_csv(io.StringIO(r.text))
            df.columns = ["d", "v"]
            df["d"] = pd.to_datetime(df["d"])
            df["v"] = pd.to_numeric(df["v"], errors="coerce")
            return df.dropna().set_index("d")["v"]
        except Exception as e:
            if a == 2:
                print(f"  ! FRED {series_id}: {e}", file=sys.stderr)
            time.sleep(2 + a * 2)
    return pd.Series(dtype=float)


def prices() -> pd.DataFrame:
    import yfinance as yf
    start = (pd.Timestamp.now("UTC") - pd.Timedelta(days=420)).strftime("%Y-%m-%d")
    syms = list(TICKERS.values())
    for a in range(3):
        try:
            df = yf.download(syms, start=start, interval="1d", auto_adjust=False,
                             progress=False, threads=True, group_by="column")
            C = df["Close"]
            break
        except Exception as e:
            if a == 2:
                raise
            time.sleep(3)
    inv = {v: k for k, v in TICKERS.items()}
    C = C.rename(columns=inv).sort_index()
    C.index = pd.to_datetime(C.index).tz_localize(None)
    # 미국 주식장 마지막 완결일(SPY) 기준으로 맞춘다 — 24시간 종목의 장중 봉 제거
    last = C["SPY"].dropna().index[-1]
    C = C.loc[C.index <= last]
    C = C[C["SPY"].notna()]          # 미국 거래일 축으로 통일
    return C.ffill(limit=3)


def chg(s: pd.Series, n: int):
    s = s.dropna()
    if len(s) <= n or not s.iloc[-1 - n]:
        return None
    return round(float(s.iloc[-1] / s.iloc[-1 - n] - 1) * 100, 2)


def stat(s: pd.Series) -> dict:
    s = s.dropna()
    row = {"px": round(float(s.iloc[-1]), 4)}
    for k, n in PERIODS.items():
        row[k] = chg(s, n)
    row["a50"] = bool(s.iloc[-1] > s.tail(50).mean()) if len(s) >= 50 else None
    row["a200"] = bool(s.iloc[-1] > s.tail(200).mean()) if len(s) >= 200 else None
    return row


def hi52(s: pd.Series) -> dict:
    """52주(약 1년) 고점 대비 위치. near = 고점 1% 이내, new5 = 최근 5개 관측 안에 고점 갱신."""
    s = s.dropna()
    s = s[s.index > s.index[-1] - pd.Timedelta(days=365)]
    if len(s) < 20:
        return {}
    hi = float(s.max()); lo = float(s.min()); last = float(s.iloc[-1])
    return {"hi": round(hi, 3), "lo": round(lo, 3), "off": round((last / hi - 1) * 100, 2),
            "near": bool(last >= hi * 0.99), "new5": bool(s.tail(5).max() >= hi),
            "hi_d": s.idxmax().strftime("%Y-%m-%d")}


def rate_stat(s: pd.Series) -> dict:
    """금리·스프레드: 변화를 bp 로."""
    s = s.dropna()
    out = {"px": round(float(s.iloc[-1]), 3), "d": s.index[-1].strftime("%Y-%m-%d")}
    for k, days in (("w1", 7), ("m1", 30), ("m3", 91)):
        past = s[s.index <= s.index[-1] - pd.Timedelta(days=days)]
        out[k] = round(float(s.iloc[-1] - past.iloc[-1]) * 100) if len(past) else None
    return out


def ser(s: pd.Series, idx=None, nd=4):
    if idx is not None:
        s = s.reindex(idx)
    return [None if pd.isna(x) else round(float(x), nd) for x in s]


def sig(key, name, score, value, why):
    return {"k": key, "n": name, "s": int(score), "v": value, "why": why}


def build() -> dict:
    print("1) 시세")
    C = prices()
    print(f"  {C.shape[1]}종목 · 마지막 {C.index[-1].date()}")
    print("2) FRED")
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(5) as ex:
        F = dict(zip(FRED, ex.map(fred, FRED.values())))
    for k, s in F.items():
        print(f"  {k}: {s.index[-1].date() if len(s) else '실패'}")

    q = {k: stat(C[k]) for k in C.columns if C[k].dropna().size > 30}
    # 상대강도(SPY 대비, 1M) — 섹터·지수
    for k, r in q.items():
        b = q["SPY"]
        for p in ("w1", "m1", "m3"):
            if r.get(p) is not None and b.get(p) is not None:
                r["rs_" + p] = round(((1 + r[p] / 100) / (1 + b[p] / 100) - 1) * 100, 2)

    rates = {k: rate_stat(F[k]) for k in ("US2Y", "US10Y", "CURVE", "FFR", "HYOAS") if len(F[k])}
    for k in ("DXY", "USDKRW", "USDJPY", "TQQQ", "SMH"):
        if k in q:
            q[k]["h52"] = hi52(C[k])
    fx = {}
    for k in ("BROAD", "EMUSD"):
        if len(F[k]):
            r = rate_stat(F[k]); r.pop("w1", None)
            s_ = F[k].dropna()
            for kk, days in (("w1", 7), ("m1", 30), ("m3", 91)):
                past = s_[s_.index <= s_.index[-1] - pd.Timedelta(days=days)]
                r[kk] = round(float(s_.iloc[-1] / past.iloc[-1] - 1) * 100, 2) if len(past) else None
            r["h52"] = hi52(s_)
            fx[k] = r

    # 물가: YoY, 3개월 연율
    infl = {}
    for k in ("CPI", "CORE"):
        s = F[k]
        if len(s) > 15:
            yoy = (s / s.shift(12) - 1) * 100
            a3 = ((s / s.shift(3)) ** 4 - 1) * 100
            infl[k] = {
                "m": s.index[-1].strftime("%Y-%m"),
                "yoy": round(float(yoy.iloc[-1]), 2),
                "yoy_prev": round(float(yoy.iloc[-2]), 2),
                "a3": round(float(a3.iloc[-1]), 2),
                "hist": [{"m": d.strftime("%Y-%m"), "yoy": round(float(v), 2)}
                         for d, v in yoy.dropna().tail(36).items()],
            }

    # 연준 순유동성 = 총자산 − TGA − 역레포 (십억$)
    liq = None
    if len(F["WALCL"]) and len(F["TGA"]) and len(F["RRP"]):
        w = F["WALCL"] / 1000.0                                   # 백만$ → 십억$
        t = F["TGA"].reindex(w.index, method="ffill") / 1000.0    # 백만$ → 십억$
        r = F["RRP"].reindex(w.index, method="ffill")
        nl = (w - t - r).dropna()
        liq = {
            "d": nl.index[-1].strftime("%Y-%m-%d"),
            "v": round(float(nl.iloc[-1])),
            "w4": round(float(nl.iloc[-1] - nl.iloc[-5])) if len(nl) > 5 else None,
            "w13": round(float(nl.iloc[-1] - nl.iloc[-14])) if len(nl) > 14 else None,
            "hist": [{"d": d.strftime("%Y-%m-%d"), "v": round(float(v))} for d, v in nl.tail(78).items()],
        }

    # ── 신호 ──────────────────────────────────────────
    S = []
    g = lambda k, p: (q.get(k) or {}).get(p)

    v = g("DXY", "m1")
    if v is not None:
        sc = 1 if v < -1 else -1 if v > 1 else 0
        S.append(sig("DXY", "달러", sc, f"1M {v:+.1f}%",
                     "달러 약세 → 위험자산·신흥국으로 돈이 나가기 쉬움" if sc > 0 else
                     "달러 강세 → 달러로 돈이 빨려 들어감, 위험자산 역풍" if sc < 0 else
                     "달러 횡보 — 방향 중립"))
    b = fx.get("BROAD"); em = fx.get("EMUSD"); dh = (q.get("DXY") or {}).get("h52") or {}
    if b and b.get("h52"):
        hot = sum(bool(x) for x in (dh.get("near"), b["h52"].get("near"), (em or {}).get("h52", {}).get("near")))
        sc = -1 if hot >= 2 else 1 if (b.get("m1") or 0) < -1 else 0
        S.append(sig("BROAD", "무역가중 달러", sc, f"광의 {b['px']:.1f} · 52주고점 대비 {b['h52']['off']:+.1f}%",
                     f"DXY·광의·신흥국 달러 중 {hot}개가 52주 고점권 — 글로벌 강달러, 수출입·신흥국 압박" if sc < 0 else
                     "무역가중 달러 약세 — 글로벌 유동성 우호" if sc > 0 else f"고점권 {hot}/3 — 강달러 압력 제한적"))
    kr, jp = q.get("USDKRW"), q.get("USDJPY")
    if kr and jp:
        a, j = kr.get("m1") or 0, jp.get("m1") or 0
        sc = -1 if a > 1 and j > 1 else 1 if a < -1 and j < -1 else 0
        S.append(sig("ASIAFX", "아시아 통화", sc, f"원 {kr['px']:,.0f} · 엔 {jp['px']:.1f}",
                     "원·엔 동반 약세 → 외국인 수급 이탈 압력" if sc < 0 else
                     "원·엔 동반 강세 → 아시아로 자금 유입" if sc > 0 else "원·엔 엇갈림 / 진정"))
    r2 = rates.get("US2Y")
    if r2 and r2.get("m1") is not None:
        v = r2["m1"]
        sc = 1 if v < -15 else -1 if v > 15 else 0
        S.append(sig("US2Y", "단기채(2년)", sc, f"{r2['px']:.2f}% · 1M {v:+d}bp",
                     "2년물 하락 → 시장이 금리인하(완화)를 반영" if sc > 0 else
                     "2년물 상승 → 긴축 기대, 밸류에이션 압박" if sc < 0 else
                     "2년물 횡보 — 연준 기대 변화 작음"))
    v = g("WTI", "m1")
    if v is not None:
        sc = -1 if v > 10 else 0
        S.append(sig("WTI", "원유", sc, f"1M {v:+.1f}%",
                     "유가 급등 → 물가 재점화·금리 부담" if sc < 0 else
                     ("유가 급락 → 물가엔 좋지만 수요 둔화 신호일 수 있음" if v < -10 else "유가 안정 — 물가 압력 제한적")))
    c = infl.get("CORE")
    if c:
        sc = 1 if c["a3"] < c["yoy"] - 0.3 else -1 if c["a3"] > c["yoy"] + 0.3 else 0
        S.append(sig("CPI", "물가(근원 CPI)", sc, f"YoY {c['yoy']:.1f}% · 3M연율 {c['a3']:.1f}%",
                     "최근 3개월 물가가 연간보다 낮음 → 둔화 추세, 완화 여지" if sc > 0 else
                     "최근 3개월 물가가 연간보다 높음 → 재가속, 긴축 리스크" if sc < 0 else
                     "물가 추세 평탄"))
    if liq and liq.get("w4") is not None:
        v = liq["w4"]
        sc = 1 if v > 100 else -1 if v < -100 else 0
        S.append(sig("LIQ", "연준 순유동성", sc, f"${liq['v']:,}B · 4주 {v:+,}B",
                     "시중 유동성 증가 → 자산가격 지지" if sc > 0 else
                     "시중 유동성 감소 → 자산가격 역풍" if sc < 0 else "유동성 큰 변화 없음"))
    h = rates.get("HYOAS")
    if h and h.get("m1") is not None:
        v = h["m1"]
        sc = 1 if v < -20 else -1 if v > 30 else 0
        S.append(sig("HY", "신용(하이일드)", sc, f"{h['px']:.2f}% · 1M {v:+d}bp",
                     "스프레드 축소 → 위험 선호, 돈이 위험자산으로" if sc > 0 else
                     "스프레드 확대 → 신용 경계, 돈이 빠져나가는 중" if sc < 0 else "신용시장 안정"))
    sp = q.get("SPY")
    if sp:
        sc = 1 if sp["a50"] and (sp["m1"] or 0) > 0 else -1 if (not sp["a50"]) and (sp["m1"] or 0) < 0 else 0
        S.append(sig("SPY", "시장 전체(S&P)", sc, f"1M {sp['m1']:+.1f}% · {'50일선 위' if sp['a50'] else '50일선 아래'}",
                     "지수 상승 추세 유지" if sc > 0 else "지수 하락 추세" if sc < 0 else "지수 방향성 약함"))
    v = g("QQQ", "rs_m1")
    if v is not None:
        sc = 1 if v > 1 else -1 if v < -1 else 0
        S.append(sig("QQQ", "나스닥 상대강도", sc, f"vs SPY 1M {v:+.1f}%",
                     "성장·기술주로 돈이 쏠림" if sc > 0 else "성장주에서 돈이 빠짐" if sc < 0 else "성장·가치 균형"))
    a, e = g("ACWI", "m1"), g("EEM", "m1")
    if a is not None and e is not None:
        sc = 1 if a > 0 and e > 0 else -1 if a < -2 and e < -2 else 0
        S.append(sig("GLOBAL", "글로벌", sc, f"ACWI {a:+.1f}% · EM {e:+.1f}%",
                     "글로벌 동반 상승 — 위험선호 확산" if sc > 0 else "글로벌 동반 약세 — 위험회피" if sc < 0 else "지역별 엇갈림"))
    off = [g(x, "m1") for x in OFFENSE if g(x, "m1") is not None]
    dfn = [g(x, "m1") for x in DEFENSE if g(x, "m1") is not None]
    if off and dfn:
        v = float(np.mean(off) - np.mean(dfn))
        sc = 1 if v > 2 else -1 if v < -2 else 0
        S.append(sig("ROT", "섹터 공격/방어", sc, f"공격−방어 1M {v:+.1f}%p",
                     "경기민감·성장 섹터로 자금 이동 (위험선호)" if sc > 0 else
                     "필수소비·유틸·헬스케어로 자금 이동 (방어)" if sc < 0 else "공격/방어 균형"))
    vx = q.get("VIX")
    if vx:
        v = vx["px"]
        sc = 1 if v < 16 else -1 if v > 25 else 0
        S.append(sig("VIX", "변동성(VIX)", sc, f"{v:.1f}",
                     "공포 낮음" if sc > 0 else "공포 높음 — 현금화·헤지 수요" if sc < 0 else "보통"))

    total = sum(x["s"] for x in S)
    secm1 = [g(x, "m1") for x in OFFENSE + DEFENSE + ["XLE", "XLB"] if g(x, "m1") is not None]
    disp = float(np.std(secm1)) if secm1 else 0.0
    spm1 = (sp or {}).get("m1") or 0
    if total >= 4 and spm1 > 0:
        regime, rdesc = "상승 유입", "달러·금리·신용·섹터가 같은 방향 — 돈이 위험자산으로 들어오는 중"
    elif total <= -4:
        regime, rdesc = "이탈", "위험자산에서 돈이 빠져 달러·채권·방어로 이동"
    elif disp >= 3.5 and abs(spm1) < 3:
        regime, rdesc = "순환", "지수는 제자리, 섹터 사이에서 돈이 옮겨 다니는 중"
    else:
        regime, rdesc = "혼조", "신호가 엇갈림 — 한쪽에 크게 베팅하기 어려운 구간"

    secs = OFFENSE + DEFENSE + ["XLE", "XLB"]
    rank = sorted([{"s": s, "w1": g(s, "rs_w1"), "m1": g(s, "rs_m1"), "m3": g(s, "rs_m3")}
                   for s in secs if g(s, "rs_m1") is not None], key=lambda x: -x["m1"])

    # ── 판단 근거 체크리스트 ──────────────────────────
    checks = []
    def chk(name, val, verdict, alert):
        checks.append({"n": name, "v": val, "t": verdict, "a": alert})
    if dh:
        chk("달러인덱스(DXY) 연고점", f"{q['DXY']['px']:.2f} · 고점 {dh['hi']:.2f} ({dh['hi_d']})",
            "52주 고점 갱신" if dh["new5"] else f"고점 대비 {dh['off']:+.1f}%", dh["near"])
    for k, nm in (("BROAD", "무역가중 달러 DTWEXBGS (BBDXY 대용)"), ("EMUSD", "신흥국 대비 달러 DTWEXEMEGS")):
        r = fx.get(k)
        if r and r.get("h52"):
            chk(nm, f"{r['px']:.2f} · 1M {r['m1']:+.1f}% ({r['d']})",
                "52주 고점권" if r["h52"]["near"] else f"고점 대비 {r['h52']['off']:+.1f}%", r["h52"]["near"])
    if kr:
        ser_ = C["USDKRW"].dropna().tail(5)
        hold = bool((ser_ >= 1340).all())
        chk("원/달러 1,340 안착", f"{kr['px']:,.1f}원 · 1W {kr['w1']:+.1f}%",
            "5거래일 연속 1,340 이상 — 안착" if hold else ("1,340 위 — 안착 확인 중" if kr["px"] >= 1340 else "1,340 아래"), hold)
    if jp:
        calm = (jp.get("w1") or 0) <= 0
        chk("엔화 진정 여부", f"{jp['px']:.2f}엔 · 1W {jp['w1']:+.1f}% · 1M {jp['m1']:+.1f}%",
            "엔 강세 전환 — 진정" if calm else "엔 약세 지속 — 아시아 통화 압박", not calm)
    top = rank[:3]; bot = rank[-2:]
    chk("자금 유입 섹터 (1M RS 상위)", " · ".join(f"{x['s']} {x['m1']:+.1f}%" for x in top),
        "방어·내수 쪽으로 이동" if all(x["s"] in DEFENSE + ["XLI", "XLF", "XLY"] for x in top) else "공격 섹터 포함", False)
    chk("자금 이탈 섹터 (1M RS 하위)", " · ".join(f"{x['s']} {x['m1']:+.1f}%" for x in bot), "", False)
    sm = q.get("SMH")
    if sm:
        alert = (sm.get("rs_m1") or 0) < -3
        chk("반도체 SMH", f"${sm['px']:.1f} · 1M {sm['m1']:+.1f}% · vs SPY {sm.get('rs_m1', 0):+.1f}%",
            ("50일선 위" if sm["a50"] else "50일선 아래") + (" · 상대약세" if alert else ""), alert)

    idx = C.index[-KEEP:]
    series = {k: ser(C[k], idx) for k in C.columns}
    for k in ("US2Y", "US10Y", "HYOAS"):
        if len(F[k]):
            series[k] = ser(F[k].reindex(F[k].index.union(idx)).ffill(), idx, 3)

    return {
        "asof": C.index[-1].strftime("%Y-%m-%d"),
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "regime": regime, "regime_desc": rdesc, "score": total, "max": len(S),
        "dispersion": round(disp, 2),
        "signals": S,
        "checks": checks, "fx": fx,
        "q": q, "rates": rates, "infl": infl, "liq": liq,
        "sector_rank": rank,
        "dates": [d.strftime("%Y-%m-%d") for d in idx],
        "series": series,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data.json")
    a = ap.parse_args()
    try:
        m = build()
    except Exception as e:
        print(f"! 매크로 블록 실패 — 기존 값 유지: {e}", file=sys.stderr)
        return
    d = {}
    if os.path.exists(a.out):
        d = json.load(open(a.out, encoding="utf-8"))
    d["macro"] = m
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, separators=(",", ":"))
    print(f"완료 → {a.out}  국면 {m['regime']} ({m['score']:+d}/{m['max']})")


if __name__ == "__main__":
    main()
