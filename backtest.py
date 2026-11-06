#!/usr/bin/env python3
"""
backtest.py — 마켓브레스 신호가 실제로 다음날·며칠 뒤 지수 움직임을 얼마나 맞혔는지 검증한다.

  python backtest.py                    # 전체 재계산 (약 6년, GitHub Actions 에서 20~40분)
  python backtest.py --years 3          # 기간 줄이기
  python backtest.py --limit 800        # 유니버스를 줄여 빠르게 시험
  python backtest.py --predict          # 무거운 계산 없이 저장된 모델 + data.json 최신 행으로 오늘 판단만

출력
  backtest/breadth_long.csv   자체 계산 브레스 장기 히스토리(재사용·검증용)
  backtest/report.md          사람이 읽는 결과표 (한국어)
  backtest/summary.json       브리핑이 읽는 압축 요약 (신호별 적중률·평균, 모델 성능)
  backtest/model.json         로지스틱 모델 계수 (--predict 가 사용)
  backtest/today.json         최신 거래일 기준 활성 신호 + 모델 확률 (--predict 가 매일 갱신)

방법
  · 브레스(t일 종가 기준) → 목표값
      gap1  : t+1 시가 / t 종가 − 1          (다음날 장초 갭)
      oc1   : t+1 종가 / t+1 시가 − 1        (다음날 장중 흐름)
      cc1   : t+1 종가 / t 종가 − 1
      hi1/lo1: t+1 고가·저가 / t 종가 − 1
      r5/r10: t+5·t+10 종가 / t 종가 − 1
      mae5  : t+1~t+5 최저가 / t 종가 − 1    (그 사이 최대 역행)
  · 신호마다 '해당일 전체'와 '에피소드 첫날(5거래일 이상 쉬었다가 다시 켜진 날)' 두 가지로 센다.
    연속으로 켜진 날을 다 세면 표본이 부풀려지므로, 신뢰도 판단은 에피소드 기준을 본다.
  · 기준선(아무 날이나 샀을 때) 대비 초과 평균과 적중률 차이를 같이 낸다.
  · 로지스틱 모델은 확장 창 walk-forward(21거래일마다 재학습)로 표본 밖 성능만 보고한다.

한계
  · 유니버스는 '현재 상장 종목'이라 과거에 상폐된 종목이 빠진다(생존 편향).
    → 카운트형 지표는 날짜별 유니버스 크기로 현재 규모에 맞춰 환산한다.
  · 자체 계산값은 Stockbee 원본과 조금 다르다. 방향과 극단 수준을 보는 용도다.
"""
from __future__ import annotations
import argparse, json, math, os, sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests

import build_data as bd

OUT_DIR = "backtest"
INDEXES = ["SPY", "QQQ", "IWM"]
TARGETS = ["gap1", "oc1", "cc1", "hi1", "lo1", "r5", "r10", "mae5"]
KEY_TARGETS = ["gap1", "cc1", "r5", "r10"]          # 요약에 싣는 목표값
WARMUP = 260                                         # 252일 신고저·200일선 준비 기간
EPISODE_GAP = 5
COUNT_FIELDS = ["up4", "dn4", "q25u", "q25d", "m25u", "m25d", "m50u", "m50d",
                "u13", "d13", "b20u", "b20d", "nh", "nl"]
MODEL_FEATURES = ["t2108", "a50", "a200", "r5", "r10", "up4", "dn4",
                  "q25u", "q25d", "m25u", "m25d", "b20u", "b20d", "nh", "nl", "mco",
                  "t2108_d5", "spy_r5"]


# ─────────────────────── 신호 정의 ───────────────────────
# (키, 설명, 판별식). 판별식은 브레스 DataFrame 을 받아 bool Series 를 돌려준다.
# 브리핑에서 오늘 값으로 같은 판별을 할 수 있게 rule 문자열도 함께 싣는다.
SIGNALS = [
    ("dn4_700",   "4%↓ ≥ 700 (대규모 투매)",           "dn4>=700"),
    ("dn4_400",   "4%↓ ≥ 400",                         "dn4>=400"),
    ("up4_300",   "4%↑ ≥ 300",                         "up4>=300"),
    ("up4_500",   "4%↑ ≥ 500 (강한 매수 쏠림)",        "up4>=500"),
    ("r5_lt05",   "5일비 < 0.5",                       "r5<0.5"),
    ("r5_gt2",    "5일비 > 2",                         "r5>2"),
    ("r10_lt05",  "10일비 < 0.5",                      "r10<0.5"),
    ("r10_gt2",   "10일비 > 2",                        "r10>2"),
    ("t2108_lt20","T2108 < 20",                        "t2108<20"),
    ("t2108_lt30","T2108 < 30",                        "t2108<30"),
    ("t2108_gt70","T2108 > 70",                        "t2108>70"),
    ("t2108_gt80","T2108 > 80",                        "t2108>80"),
    ("t2108_up5", "T2108 5일간 +10p 이상 회복",         "t2108_d5>=10"),
    ("t2108_dn5", "T2108 5일간 −10p 이상 하락",         "t2108_d5<=-10"),
    ("b20d_100",  "Bear20 ≥ 100",                      "b20d>=100"),
    ("b20d_200",  "Bear20 ≥ 200",                      "b20d>=200"),
    ("b20u_le25", "Bull20 ≤ 25",                       "b20u<=25"),
    ("b20u_100",  "Bull20 ≥ 100",                      "b20u>=100"),
    ("q25u_lt300","Q25↑ < 300 (분기 상승 종목 고갈)",   "q25u<300"),
    ("m25d_300",  "M25↓ ≥ 300",                        "m25d>=300"),
    ("nl_700",    "52주 신저가 ≥ 700",                 "nl>=700"),
    ("nh_gt_nl",  "신고가 > 신저가",                   "nh>nl"),
    ("mco_lt-50", "맥클렐란 오실레이터 < −50",          "mco<-50"),
    ("mco_gt50",  "맥클렐란 오실레이터 > 50",           "mco>50"),
    ("mco_up0",   "오실레이터 0선 상향 돌파",           "mco>0 & mco_prev<=0"),
    ("mcs_up",    "서메이션 5일 연속 상승",             "mcs_up5"),
    ("fng_lt25",  "F&G < 25 (극단적 공포)",            "fng<25"),
    ("fng_lt15",  "F&G < 15",                          "fng<15"),
    ("fng_gt75",  "F&G > 75 (극단적 탐욕)",            "fng>75"),
    ("div_up",    "지수↑(SPY +0.3%) 인데 T2108 −3p 이상", "spy_r1>=0.3 & t2108_d1<=-3"),
    ("div_dn",    "지수↓(SPY −0.3%) 인데 T2108 +3p 이상", "spy_r1<=-0.3 & t2108_d1>=3"),
    # 조합: 브리핑에서 자주 쓰는 판단
    ("capit",     "투매 바닥: dn4≥700 & T2108<30",       "dn4>=700 & t2108<30"),
    ("washout",   "과매도: 5일비<0.5 & T2108<30",        "r5<0.5 & t2108<30"),
    ("thrust",    "반등 확인: 4%↑≥300 & T2108 5일 +5p",  "up4>=300 & t2108_d5>=5"),
    ("fear_wash", "공포+과매도: F&G<25 & T2108<30",      "fng<25 & t2108<30"),
    ("weak_bnc",  "약한 반등: 4%↑<300 & T2108<40 & 서메이션↓", "up4<300 & t2108<40 & mcs_d5<0"),
]


def _ops(df: pd.DataFrame) -> dict:
    return {c: df[c] for c in df.columns}


def eval_rule(df: pd.DataFrame, rule: str) -> pd.Series:
    """'a>=1 & b<2' 형태의 간단한 규칙만 해석한다(eval 미사용)."""
    out = pd.Series(True, index=df.index)
    for part in rule.split("&"):
        p = part.strip()
        if p in df.columns:                       # bool 컬럼
            out &= df[p].fillna(False).astype(bool)
            continue
        for op in (">=", "<=", ">", "<"):
            if op in p:
                a, b = [x.strip() for x in p.split(op, 1)]
                lhs = df[a]
                rhs = df[b] if b in df.columns else float(b)
                m = {">=": lhs >= rhs, "<=": lhs <= rhs, ">": lhs > rhs, "<": lhs < rhs}[op]
                out &= m.fillna(False)
                break
        else:
            raise ValueError(f"규칙 해석 실패: {p}")
    return out


def eval_rule_row(row: dict, rule: str):
    """하루치 dict 에 같은 규칙 적용 (값이 없으면 None)."""
    for part in rule.split("&"):
        p = part.strip()
        if p in row:
            if row[p] is None:
                return None
            if not row[p]:
                return False
            continue
        for op in (">=", "<=", ">", "<"):
            if op in p:
                a, b = [x.strip() for x in p.split(op, 1)]
                lhs = row.get(a)
                rhs = row.get(b) if b in row else float(b)
                if lhs is None or rhs is None:
                    return None
                ok = {">=": lhs >= rhs, "<=": lhs <= rhs, ">": lhs > rhs, "<": lhs < rhs}[op]
                if not ok:
                    return False
                break
        else:
            return None
    return True


# ─────────────────────── 데이터 수집 ───────────────────────
def fetch_index_ohlc(start: str) -> dict:
    import yfinance as yf
    df = yf.download(INDEXES, start=start, interval="1d", auto_adjust=True,
                     progress=False, threads=True, group_by="ticker")
    out = {}
    for t in INDEXES:
        try:
            sub = df[t][["Open", "High", "Low", "Close"]].dropna()
            sub.index = pd.to_datetime(sub.index).tz_localize(None).normalize()
            out[t] = sub
        except Exception as e:
            print(f"  ! {t} OHLC 실패: {e}")
    return out


def fetch_fng_history(start: str) -> pd.Series:
    bua = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
           "Accept": "application/json, text/plain, */*"}
    try:
        r = requests.get(f"https://production.dataviz.cnn.io/index/fearandgreed/graphdata/{start}",
                         headers=bua, timeout=40)
        hist = (r.json().get("fear_and_greed_historical") or {}).get("data") or []
        byd = {}
        for h in hist:
            if h.get("x") and h.get("y") is not None:
                d = datetime.fromtimestamp(h["x"] / 1000, timezone.utc).strftime("%Y-%m-%d")
                byd[d] = float(h["y"])
        s = pd.Series(byd, dtype=float)
        s.index = pd.to_datetime(s.index)
        print(f"  F&G 히스토리 {len(s)}일 ({s.index.min().date() if len(s) else '-'}~)")
        return s.sort_index()
    except Exception as e:
        print(f"  ! F&G 히스토리 실패: {type(e).__name__} {e}")
        return pd.Series(dtype=float)


def build_long_breadth(years: float, limit: int | None) -> pd.DataFrame:
    days = int(365 * years) + 420
    syms = bd.load_universe(limit)
    print(f"  전 종목 일봉 {days}일치 (오래 걸린다)")
    C, V = bd.download(syms, days)
    if C.empty:
        raise SystemExit("시세를 받지 못했습니다")
    C.index = pd.to_datetime(C.index).tz_localize(None).normalize()
    V.index = C.index
    # 메모리 절약
    C = C.astype("float32"); V = V.astype("float32")
    sess = pd.Timestamp(bd.last_session_date())
    C = C.loc[C.index <= sess]; V = V.loc[V.index <= sess]
    good = int((C.notna().sum(axis=0) > 20).sum())
    print(f"  받은 종목 {good}/{len(syms)} / 거래일 {C.shape[0]}")
    rows, _ = bd.breadth_block(C, V, C.shape[0], None)
    df = pd.DataFrame(rows)
    df["d"] = pd.to_datetime(df["d"])
    df = df.set_index("d").iloc[WARMUP:]
    return df


# ─────────────────────── 파생 피처 ───────────────────────
def add_features(df: pd.DataFrame, spy_close: pd.Series | None, fng: pd.Series | None,
                 scale_to: float | None = None) -> pd.DataFrame:
    df = df.copy().sort_index()
    # 카운트형 지표를 현재 유니버스 규모로 환산 (생존 편향 보정)
    if "universe" in df and df["universe"].notna().any():
        ref = scale_to or float(df["universe"].dropna().iloc[-1])
        k = ref / df["universe"].replace(0, np.nan)
        for c in COUNT_FIELDS:
            if c in df:
                df[c] = df[c].astype(float) * k
    df["t2108_d1"] = df["t2108"].diff(1)
    df["t2108_d5"] = df["t2108"].diff(5)
    df["mco_prev"] = df["mco"].shift(1)
    df["mcs_d5"] = df["mcs"].diff(5)
    up = (df["mcs"].diff() > 0).astype(float)
    df["mcs_up5"] = up.rolling(5).sum() >= 5
    if spy_close is not None:
        s = spy_close.reindex(df.index)
        df["spy_r1"] = s.pct_change(1) * 100
        df["spy_r5"] = s.pct_change(5) * 100
    else:
        df["spy_r1"] = np.nan; df["spy_r5"] = np.nan
    df["fng"] = fng.reindex(df.index) if fng is not None and len(fng) else np.nan
    return df


def targets_for(ohlc: pd.DataFrame, idx: pd.DatetimeIndex) -> pd.DataFrame:
    o = ohlc.sort_index()                       # 지수 거래일 기준으로 이동시킨 뒤 브레스 날짜에 맞춘다
    C, O, H, L = o["Close"], o["Open"], o["High"], o["Low"]
    t = pd.DataFrame(index=o.index)
    t["gap1"] = (O.shift(-1) / C - 1) * 100
    t["oc1"] = (C.shift(-1) / O.shift(-1) - 1) * 100
    t["cc1"] = (C.shift(-1) / C - 1) * 100
    t["hi1"] = (H.shift(-1) / C - 1) * 100
    t["lo1"] = (L.shift(-1) / C - 1) * 100
    t["r5"] = (C.shift(-5) / C - 1) * 100
    t["r10"] = (C.shift(-10) / C - 1) * 100
    lows = pd.concat([L.shift(-k) for k in range(1, 6)], axis=1).min(axis=1, skipna=False)
    t["mae5"] = (lows / C - 1) * 100
    return t.reindex(idx)


def episodes(mask: pd.Series) -> pd.Series:
    """연속 신호의 첫날만 True (직전 EPISODE_GAP 거래일 동안 꺼져 있었던 날)."""
    m = mask.fillna(False).astype(bool)
    prev = m.astype(float).shift(1).rolling(EPISODE_GAP, min_periods=1).max().fillna(0) > 0
    return m & ~prev


def stat(x: pd.Series) -> dict:
    x = x.dropna()
    n = len(x)
    if n == 0:
        return {"n": 0}
    sd = float(x.std(ddof=1)) if n > 1 else float("nan")
    return {"n": n, "mean": round(float(x.mean()), 3), "med": round(float(x.median()), 3),
            "hit": round(float((x > 0).mean()) * 100, 1),
            "sd": None if math.isnan(sd) else round(sd, 3)}


# ─────────────────────── 로지스틱 모델 (numpy) ───────────────────────
def _fit_logit(X, y, l2=1.0, iters=400, lr=0.1):
    n, k = X.shape
    w = np.zeros(k); b = math.log((y.mean() + 1e-6) / (1 - y.mean() + 1e-6))
    for _ in range(iters):
        z = X @ w + b
        p = 1 / (1 + np.exp(-np.clip(z, -30, 30)))
        g = p - y
        w -= lr * (X.T @ g / n + l2 * w / n)
        b -= lr * g.mean()
    return w, b


def walk_forward(df: pd.DataFrame, y: pd.Series, feats: list[str], start=500, step=21):
    data = pd.concat([df[feats], y.rename("y")], axis=1).dropna()
    if len(data) < start + 50:
        return None, None
    X_all, y_all = data[feats].values.astype(float), data["y"].values.astype(float)
    preds = np.full(len(data), np.nan)
    for i in range(start, len(data), step):
        # 목표가 미래 수익률이라 학습셋은 i-10 까지만 (정보 누출 방지)
        tr = slice(0, max(0, i - 10))
        mu, sd = X_all[tr].mean(0), X_all[tr].std(0) + 1e-9
        w, b = _fit_logit((X_all[tr] - mu) / sd, y_all[tr])
        te = slice(i, min(i + step, len(data)))
        z = ((X_all[te] - mu) / sd) @ w + b
        preds[te] = 1 / (1 + np.exp(-z))
    ok = ~np.isnan(preds)
    p, yy = preds[ok], y_all[ok]
    base = float(yy.mean())
    res = {
        "n_oos": int(ok.sum()),
        "base_rate": round(base * 100, 1),
        "acc": round(float(((p > 0.5) == (yy > 0.5)).mean()) * 100, 1),
        "acc_always_up": round(max(base, 1 - base) * 100, 1),
        "brier": round(float(((p - yy) ** 2).mean()), 4),
        "brier_base": round(float(((base - yy) ** 2).mean()), 4),
    }
    # 확률 구간별 실제 적중
    bins = [0, .35, .45, .55, .65, 1.01]
    cal = []
    for lo, hi in zip(bins[:-1], bins[1:]):
        m = (p >= lo) & (p < hi)
        if m.sum() >= 10:
            cal.append({"p": f"{int(lo*100)}–{min(int(hi*100),100)}%", "n": int(m.sum()),
                        "actual_up": round(float(yy[m].mean()) * 100, 1)})
    res["calibration"] = cal
    # 최종 모델 (전 구간 학습) 저장용
    mu, sd = X_all.mean(0), X_all.std(0) + 1e-9
    w, b = _fit_logit((X_all - mu) / sd, y_all)
    model = {"features": feats, "mu": mu.round(6).tolist(), "sd": sd.round(6).tolist(),
             "w": w.round(6).tolist(), "b": round(float(b), 6)}
    res["weights"] = sorted([{"f": f, "w": round(float(x), 3)} for f, x in zip(feats, w)],
                            key=lambda r: -abs(r["w"]))
    return res, model


def predict_prob(model: dict, row: dict):
    xs = []
    for f, mu, sd in zip(model["features"], model["mu"], model["sd"]):
        v = row.get(f)
        if v is None or (isinstance(v, float) and math.isnan(v)):
            v = mu                              # 없으면 평균으로 (중립)
        xs.append((float(v) - mu) / sd)
    z = float(np.dot(xs, model["w"]) + model["b"])
    return 1 / (1 + math.exp(-z))


# ─────────────────────── 리포트 ───────────────────────
def fmt(v, d=2):
    return "–" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v:+.{d}f}"



# ─────────────────────── 시계별 종합 신호 ───────────────────────
HORIZONS = [("open", "다음날 장초", "gap1"), ("close", "다음날 종가", "cc1"), ("week", "1주(5거래일)", "r5")]
SEL_MIN_EP = 15      # 신호 채택 최소 에피소드 수
SEL_EDGE = 6.0       # 기준선 대비 상승 비율 차이(%p)


def _select(df, tgt: pd.Series, idx_mask: pd.Series):
    """idx_mask 구간에서 에피소드 기준으로 tgt 상승 비율이 기준선보다 ±SEL_EDGE 이상인 신호를 고른다."""
    base_hit = float((tgt[idx_mask].dropna() > 0).mean() * 100)
    up, dn = [], []
    for key, label, rule in SIGNALS:
        if key.startswith("fng"):            # F&G 히스토리는 기간이 짧을 수 있어 종합에서 뺀다
            continue
        m = eval_rule(df, rule) & idx_mask
        ep = episodes(m)
        x = tgt[ep].dropna()
        if len(x) < SEL_MIN_EP:
            continue
        hit = float((x > 0).mean() * 100)
        if hit - base_hit >= SEL_EDGE:
            up.append(key)
        elif hit - base_hit <= -SEL_EDGE:
            dn.append(key)
    return up, dn, base_hit


def _score(df, up, dn):
    rules = {k: r for k, _, r in SIGNALS}
    su = sum(eval_rule(df, rules[k]).astype(int) for k in up) if up else pd.Series(0, index=df.index)
    sd = sum(eval_rule(df, rules[k]).astype(int) for k in dn) if dn else pd.Series(0, index=df.index)
    return (su - sd).clip(-3, 3), su, sd


def level_of(hit, base_hit, n, oos_hit=None, oos_base=None):
    """과거 적중 기준 확신도. 보통·높음 모두 표본 밖(후반부, n>=10)에서도 같은 방향이어야 한다.
    표본 밖 결과가 없거나(표본 부족) 반대 방향이면 '낮음'."""
    if hit is None or n is None or n < 20:
        return "낮음"
    edge = hit - base_hit
    if oos_hit is None or oos_base is None:
        return "낮음"
    oedge = oos_hit - oos_base
    if oedge * edge <= 0:
        return "낮음"
    if abs(edge) >= 15 and abs(oedge) >= 5:
        return "높음"
    if abs(edge) >= 8 and abs(oedge) >= 3:
        return "보통"
    return "낮음"


def composite(df, T):
    """시계별로 신호를 골라 점수(상승 신호 수 − 하락 신호 수)를 만들고, 점수별 실제 결과를 낸다.
    전 구간 선택(표본 안)과, 전반부에서 고르고 후반부에서 평가(표본 밖) 두 가지를 함께 보고한다."""
    if "SPY" not in T:
        return {}
    out = {}
    n = len(df)
    first = pd.Series(np.arange(n) < n // 2, index=df.index)
    allm = pd.Series(True, index=df.index)
    for hk, hlabel, tk in HORIZONS:
        tgt = T["SPY"][tk]
        up, dn, base_hit = _select(df, tgt, allm)
        score, su, sd = _score(df, up, dn)
        # 표본 밖: 전반부로 고르고 후반부로 평가
        up_o, dn_o, _ = _select(df, tgt, first)
        score_o, _, _ = _score(df, up_o, dn_o)
        second = ~first
        oos_base = float((tgt[second].dropna() > 0).mean() * 100)
        bks = []
        for sc in range(-3, 4):
            m = score == sc
            x = tgt[m].dropna()
            xo = tgt[(score_o == sc) & second].dropna()
            hit = round(float((x > 0).mean() * 100), 1) if len(x) else None
            oh = round(float((xo > 0).mean() * 100), 1) if len(xo) >= 10 else None
            bks.append({"score": sc, "n": int(len(x)),
                        "hit": hit, "mean": round(float(x.mean()), 3) if len(x) else None,
                        "oos_n": int(len(xo)), "oos_hit": oh,
                        "level": level_of(hit, base_hit, len(x), oh, oos_base) if sc != 0 else "낮음"})
        out[hk] = {"label": hlabel, "target": tk, "up": up, "dn": dn,
                   "base_hit": round(base_hit, 1), "oos_base_hit": round(oos_base, 1),
                   "buckets": bks,
                   "last_strong": [d.strftime("%Y-%m-%d") for d in score[score.abs() >= 2].index[-5:]]}
        print(f"  {hlabel}: 상승신호 {len(up)} / 하락신호 {len(dn)} · 기준 {base_hit:.1f}%")
    return out


def split_stats(df, T):
    """개별 신호를 전반부·후반부로 나눠 SPY 상승 비율을 각각 잰다(에피소드 첫날 기준)."""
    if "SPY" not in T:
        return {}
    n = len(df)
    first = pd.Series(np.arange(n) < n // 2, index=df.index)
    halves = {"h1": first, "h2": ~first}
    res = {"cut": df.index[n // 2].strftime("%Y-%m-%d"), "base": {}, "signals": {}}
    for _, _, tk in HORIZONS:
        tgt = T["SPY"][tk]
        res["base"][tk] = {h: round(float((tgt[m].dropna() > 0).mean() * 100), 1) for h, m in halves.items()}
    for key, label, rule in SIGNALS:
        sig = eval_rule(df, rule)
        ep = episodes(sig)
        row = {"label": label}
        for _, _, tk in HORIZONS:
            tgt = T["SPY"][tk]
            row[tk] = {}
            for h, m in halves.items():
                x = tgt[ep & m].dropna()
                row[tk][h] = [int(len(x)), round(float((x > 0).mean() * 100), 1) if len(x) else None,
                              round(float(x.mean()), 3) if len(x) else None]
        res["signals"][key] = row
    return res


def run_backtest(args):
    os.makedirs(OUT_DIR, exist_ok=True)
    start_dt = (pd.Timestamp.utcnow().tz_localize(None) - pd.Timedelta(days=int(365 * args.years) + 420))
    start = start_dt.strftime("%Y-%m-%d")

    print("1) 장기 브레스 (자체 계산)")
    long_csv = os.path.join(OUT_DIR, "breadth_long.csv")
    if args.reuse and os.path.exists(long_csv):
        br = pd.read_csv(long_csv, parse_dates=["d"]).set_index("d")
        print(f"  저장본 재사용 {len(br)}일")
    else:
        br = build_long_breadth(args.years, args.limit)
        br.to_csv(long_csv, float_format="%.4g")
    print("2) 지수 OHLC")
    ohlc = fetch_index_ohlc(start)
    print("3) F&G 히스토리")
    fng = fetch_fng_history((br.index.min()).strftime("%Y-%m-%d"))

    df = add_features(br, ohlc.get("SPY", pd.DataFrame()).get("Close"), fng)
    T = {t: targets_for(ohlc[t], df.index) for t in INDEXES if t in ohlc}

    base = {t: {k: stat(T[t][k]) for k in TARGETS} for t in T}
    rows = []
    for key, label, rule in SIGNALS:
        m = eval_rule(df, rule)
        ep = episodes(m)
        rec = {"key": key, "label": label, "rule": rule,
               "days": int(m.sum()), "episodes": int(ep.sum()),
               "last": (m[m].index.max().strftime("%Y-%m-%d") if m.any() else None),
               "all": {}, "ep": {}}
        for t in T:
            rec["all"][t] = {k: stat(T[t][k][m]) for k in TARGETS}
            rec["ep"][t] = {k: stat(T[t][k][ep]) for k in TARGETS}
        rows.append(rec)

    # 구간별 분석
    buckets = {}
    for col, edges in (("t2108", [0, 20, 30, 40, 50, 60, 70, 80, 101]),
                       ("r5", [0, .5, .8, 1.2, 2, 99]),
                       ("dn4", [0, 200, 400, 700, 99999]),
                       ("up4", [0, 150, 300, 500, 99999]),
                       ("fng", [0, 15, 25, 45, 55, 75, 101])):
        if col not in df or df[col].notna().sum() < 50:
            continue
        cat = pd.cut(df[col], edges, right=False)
        tb = []
        for iv, grp in df.groupby(cat, observed=True):
            ix = grp.index
            tb.append({"bin": f"{iv.left:g}–{iv.right:g}", "n": len(ix),
                       **{f"{t}_{k}": stat(T[t][k].reindex(ix)).get("mean")
                          for t in ("SPY", "QQQ") if t in T for k in ("gap1", "r5")},
                       "SPY_r5_hit": stat(T["SPY"]["r5"].reindex(ix)).get("hit") if "SPY" in T else None})
        buckets[col] = tb

    print("4) 로지스틱 walk-forward")
    model_res = {}
    final_model = None
    feats = [f for f in MODEL_FEATURES if f in df and df[f].notna().mean() > 0.9]
    for tgt in ("r5", "gap1"):
        if "SPY" not in T:
            break
        y = (T["SPY"][tgt] > 0).astype(float).where(T["SPY"][tgt].notna())
        res, mdl = walk_forward(df, y, feats)
        if res:
            model_res[tgt] = res
            if tgt == "r5":
                final_model = mdl
            print(f"  SPY {tgt}: OOS 정확도 {res['acc']}% (항상 상승 가정 {res['acc_always_up']}%), "
                  f"Brier {res['brier']} vs {res['brier_base']}")

    print("5) 시계별 종합 신호")
    comp = composite(df, T)
    split = split_stats(df, T)

    period = f"{df.index.min().date()} ~ {df.index.max().date()}"
    # ── summary.json (브리핑용, 작게) ──
    def short(s):
        return None if not s or not s.get("n") else [s["n"], s.get("mean"), s.get("hit")]
    summary = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "period": period, "days": int(len(df)),
        "fmt": "각 값 [n, 평균%, 상승비율%] · ep=에피소드 첫날 기준",
        "base": {t: {k: short(base[t][k]) for k in ("gap1", "r5")} for t in ("SPY", "QQQ") if t in base},
        "signals": [{
            "key": r["key"], "label": r["label"], "rule": r["rule"], "ep_n": r["episodes"],
            **{f"{t}_{k}": short(r["ep"][t][k]) for t in ("SPY", "QQQ") if t in r["ep"]
               for k in ("gap1", "r5", "r10")},
        } for r in rows],
        "composite": comp,
        "split": split,
        "model": {k: {x: v[x] for x in ("n_oos", "acc", "acc_always_up", "brier", "brier_base")}
                  for k, v in model_res.items()},
        "notes": "자체 계산 브레스(현재 상장 종목 기준, 카운트는 현재 유니버스 규모로 환산). "
                 "ep_n<10 인 신호는 참고만.",
    }
    json.dump(summary, open(os.path.join(OUT_DIR, "summary.json"), "w", encoding="utf-8"),
              ensure_ascii=False, separators=(",", ":"))
    if final_model:
        final_model["generated"] = summary["generated"]
        final_model["period"] = period
        json.dump(final_model, open(os.path.join(OUT_DIR, "model.json"), "w"), separators=(",", ":"))
    json.dump({"signals": rows, "base": base, "buckets": buckets, "model": model_res, "composite": comp, "split": split,
               "period": period},
              open(os.path.join(OUT_DIR, "full.json"), "w", encoding="utf-8"),
              ensure_ascii=False, separators=(",", ":"))

    # ── report.md ──
    L = [f"# 마켓브레스 백테스트", "",
         f"- 기간: {period} ({len(df)}거래일), 생성 {summary['generated']}",
         "- 값: 평균 수익률(%) / 상승 비율(%). **에피소드 첫날 기준**(연속 신호 중복 제거).",
         "- gap1 = 다음날 시가 갭, cc1 = 다음날 종가, r5·r10 = 5·10거래일 뒤 종가.", "",
         "## 기준선 (아무 날이나)", "",
         "| 지수 | gap1 | cc1 | r5 | r10 |", "|---|---|---|---|---|"]
    for t in base:
        L.append(f"| {t} | " + " | ".join(
            f"{fmt(base[t][k]['mean'])} / {base[t][k]['hit']}" for k in KEY_TARGETS) + " |")
    for t in [x for x in ("SPY", "QQQ", "IWM") if x in T]:
        L += ["", f"## 신호별 결과 — {t}", "",
              "| 신호 | 에피소드 | gap1 | cc1 | r5 | r10 | r5 초과 | 5일 최대역행 | 최근 |",
              "|---|---|---|---|---|---|---|---|---|"]
        for r in rows:
            e = r["ep"][t]
            if not e["r5"].get("n"):
                L.append(f"| {r['label']} | 0 | | | | | | | |"); continue
            cells = [f"{fmt(e[k].get('mean'))} / {e[k].get('hit')}" for k in KEY_TARGETS]
            ex = e["r5"]["mean"] - base[t]["r5"]["mean"]
            L.append(f"| {r['label']} | {r['episodes']} | " + " | ".join(cells) +
                     f" | {fmt(ex)} | {fmt(e['mae5'].get('mean'))} | {r['last'] or ''} |")
    for col, tb in buckets.items():
        L += ["", f"## 구간별 — {col}", "",
              "| 구간 | 일수 | SPY gap1 | SPY r5 | SPY r5 상승비율 | QQQ gap1 | QQQ r5 |",
              "|---|---|---|---|---|---|---|"]
        for b in tb:
            L.append(f"| {b['bin']} | {b['n']} | {fmt(b.get('SPY_gap1'))} | {fmt(b.get('SPY_r5'))} | "
                     f"{b.get('SPY_r5_hit')} | {fmt(b.get('QQQ_gap1'))} | {fmt(b.get('QQQ_r5'))} |")
    if split:
        L += ["", f"## 개별 신호 전반부/후반부 분할 (SPY, 기준일 {split['cut']} 전/후, 상승 비율 % (n))", "",
              "| 신호 | " + " | ".join(f"{tk} 전반 | {tk} 후반" for _, _, tk in HORIZONS) + " |",
              "|---|" + "---|" * (2 * len(HORIZONS)),
              "| 기준선 | " + " | ".join(f"{split['base'][tk]['h1']} | {split['base'][tk]['h2']}" for _, _, tk in HORIZONS) + " |"]
        for k, r in split["signals"].items():
            cells = []
            for _, _, tk in HORIZONS:
                for h in ("h1", "h2"):
                    nn, hit, _ = r[tk][h]
                    cells.append(f"{hit if hit is not None else '–'} ({nn})")
            L.append(f"| {r['label']} | " + " | ".join(cells) + " |")
    if comp:
        L += ["", "## 시계별 종합 점수 (SPY, 점수 = 상승 신호 수 − 하락 신호 수, ±3 에서 자름)", "",
              "표본 밖 = 전반부 데이터로 신호를 고르고 후반부에서 잰 상승 비율.", ""]
        for hk, c in comp.items():
            L += [f"### {c['label']} ({c['target']}) — 기준 상승 비율 {c['base_hit']}% (후반부 {c['oos_base_hit']}%)",
                  f"- 상승 쪽 신호: {', '.join(c['up']) or '없음'}",
                  f"- 하락 쪽 신호: {', '.join(c['dn']) or '없음'}", "",
                  "| 점수 | 일수 | 상승 비율 | 평균 % | 표본 밖 일수 | 표본 밖 상승 비율 | 확신도 |",
                  "|---|---|---|---|---|---|---|"]
            for b in c["buckets"]:
                L.append(f"| {b['score']:+d} | {b['n']} | {b['hit'] if b['hit'] is not None else '–'} | "
                         f"{fmt(b['mean'])} | {b['oos_n']} | {b['oos_hit'] if b['oos_hit'] is not None else '–'} | {b['level']} |")
            L.append("")
    if model_res:
        L += ["", "## 로지스틱 모델 (SPY 상승 여부, walk-forward 표본 밖)", ""]
        for k, v in model_res.items():
            L += [f"### {k}", f"- 표본 밖 {v['n_oos']}일, 정확도 **{v['acc']}%** "
                  f"(항상 상승이라고 찍으면 {v['acc_always_up']}%)",
                  f"- Brier {v['brier']} (기준 {v['brier_base']}, 낮을수록 좋음)",
                  "- 확률 구간별 실제 상승 비율: " +
                  ", ".join(f"{c['p']} → {c['actual_up']}% (n={c['n']})" for c in v["calibration"]),
                  "- 영향 큰 피처: " + ", ".join(f"{w['f']} {w['w']:+}" for w in v["weights"][:6]), ""]
    open(os.path.join(OUT_DIR, "report.md"), "w", encoding="utf-8").write("\n".join(L) + "\n")
    print(f"완료 → {OUT_DIR}/report.md, summary.json")
    run_predict(args)


# ─────────────────────── 매일 판단 (가벼움) ───────────────────────
def run_predict(args):
    sp = os.path.join(OUT_DIR, "summary.json")
    if not os.path.exists(args.data) or not os.path.exists(sp):
        print("  predict: data.json 또는 summary.json 없음 — 건너뜀"); return
    data = json.load(open(args.data, encoding="utf-8"))
    hist = (data.get("breadth") or {}).get("history") or []
    if len(hist) < 7:
        return
    df = pd.DataFrame(hist)
    df["d"] = pd.to_datetime(df["d"]); df = df.set_index("d").sort_index()
    for c in df.columns:
        if c not in ("sb",):
            df[c] = pd.to_numeric(df[c], errors="coerce")
    spy = None
    try:
        dates = pd.to_datetime(data.get("dates") or [])
        ser = (data.get("series") or {}).get("SPY")
        if ser and len(ser) == len(dates):
            spy = pd.Series(ser, index=dates, dtype=float)
    except Exception:
        spy = None
    fh = (data.get("fng") or {}).get("history") or []
    fng = pd.Series({pd.Timestamp(h["d"]): h["v"] for h in fh}, dtype=float) if fh else None
    # data.json 은 이미 Stockbee 규모(약 6,500)라 환산하지 않는다
    df = add_features(df.drop(columns=["universe"], errors="ignore"), spy, fng)
    last = df.iloc[-1]
    row = {k: (None if pd.isna(v) else (bool(v) if isinstance(v, (bool, np.bool_)) else float(v)))
           for k, v in last.items()}
    summary = json.load(open(sp, encoding="utf-8"))
    active = []
    for s in summary["signals"]:
        if eval_rule_row(row, s["rule"]):
            active.append({k: s[k] for k in s if k != "rule"})
    out = {"d": df.index[-1].strftime("%Y-%m-%d"), "backtest_period": summary.get("period"),
           "base": summary.get("base"), "active": active}
    comp = summary.get("composite") or {}
    if comp:
        rules = {k: r for k, _, r in SIGNALS}
        out["horizons"] = {}
        for hk, c in comp.items():
            on_up = [k for k in c["up"] if eval_rule_row(row, rules[k])]
            on_dn = [k for k in c["dn"] if eval_rule_row(row, rules[k])]
            sc = max(-3, min(3, len(on_up) - len(on_dn)))
            b = next((x for x in c["buckets"] if x["score"] == sc), {})
            out["horizons"][hk] = {
                "label": c["label"], "score": sc, "up_on": on_up, "dn_on": on_dn,
                "lean": "상승" if sc > 0 else ("하락" if sc < 0 else "중립"),
                # level 은 저장된 값 대신 현재 규칙으로 다시 계산(규칙 변경이 월간 재실행 전에도 반영되도록)
                "level": (level_of(b.get("hit"), c["base_hit"], b.get("n"), b.get("oos_hit"),
                                   c.get("oos_base_hit")) if sc != 0 and b else "낮음"),
                "hit": b.get("hit"), "n": b.get("n"),
                "oos_hit": b.get("oos_hit"), "oos_n": b.get("oos_n"),
                "base_hit": c["base_hit"], "oos_base_hit": c.get("oos_base_hit")}
    mp = os.path.join(OUT_DIR, "model.json")
    if os.path.exists(mp):
        model = json.load(open(mp))
        out["p_spy_up_5d"] = round(predict_prob(model, row) * 100, 1)
        out["model_oos"] = summary.get("model", {}).get("r5")
    json.dump(out, open(os.path.join(OUT_DIR, "today.json"), "w", encoding="utf-8"),
              ensure_ascii=False, separators=(",", ":"))
    print(f"  today.json: {out['d']} 활성 신호 {len(active)}개"
          + (f", 5일 상승 확률 {out.get('p_spy_up_5d')}%" if "p_spy_up_5d" in out else ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float, default=6)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--reuse", action="store_true", help="저장된 breadth_long.csv 재사용")
    ap.add_argument("--predict", action="store_true", help="오늘 판단만 (가벼움)")
    ap.add_argument("--data", default="data.json")
    args = ap.parse_args()
    if args.predict:
        run_predict(args)
    else:
        run_backtest(args)


if __name__ == "__main__":
    main()
