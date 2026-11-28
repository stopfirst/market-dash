#!/usr/bin/env python3
"""하이퍼리퀴드 청산 지도 데이터 (GitHub Actions 30분마다)

하이퍼리퀴드는 모든 지갑의 포지션과 청산가가 공개돼 있다. 다만 '전체 지갑 목록' API가 없어서
  1) 리더보드(계좌 10만 달러 이상) 주소
  2) 실시간 체결(웹소켓 trades)에 찍힌 지갑 주소
  3) 리더보드 계좌의 서브계정(리더보드에 안 나옴), 볼트(HLP 하위 볼트 포함)
를 모아 두고(actions/cache 로 실행 간 유지), 매 실행마다 포지션을 다시 읽어 청산가를 가격대별로 합친다.
커버리지 = 읽은 포지션 크기 합 / 거래소 전체 OI. 실행이 쌓일수록 주소가 늘어 커버리지가 오른다.

출력: liq.json  (coins: BTC, ETH, SOL, HYPE)
"""
import argparse, asyncio, json, os, random, time
import aiohttp

API = "https://api.hyperliquid.xyz/info"
WS = "wss://api.hyperliquid.xyz/ws"
LB = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
VAULTS = "https://stats-data.hyperliquid.xyz/Mainnet/vaults"
WS_COINS = 40       # 웹소켓 체결 구독 코인 수(거래대금 상위) — 다른 코인 거래자도 BTC를 들고 있을 수 있음
COINS = ["BTC", "ETH", "SOL", "HYPE"]
RANGE = 0.25      # 현재가 ±25%
STEP = 0.005      # 0.5% 구간
BIG_USD = 2_000_000  # 대형 포지션 목록 기준


def now():
    return int(time.time())


async def post(s, body, tries=5):
    for k in range(tries):
        try:
            async with s.post(API, json=body, timeout=aiohttp.ClientTimeout(total=20)) as r:
                if r.status == 429:
                    await asyncio.sleep(2 + 3 * k)
                    continue
                if r.status != 200:
                    await asyncio.sleep(1)
                    continue
                return await r.json()
        except Exception:
            await asyncio.sleep(1 + k)
    return None


async def discover_ws(seconds, coins=COINS, users=None):
    """웹소켓 trades 로 활동 중인 지갑 주소 수집"""
    users = set() if users is None else users
    if seconds <= 0:
        return users
    try:
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(WS, heartbeat=20) as ws:
                for c in coins:
                    await ws.send_json({"method": "subscribe", "subscription": {"type": "trades", "coin": c}})
                end = time.time() + seconds
                while time.time() < end:
                    try:
                        msg = await ws.receive(timeout=max(1, end - time.time()))
                    except asyncio.TimeoutError:
                        break
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
                        continue
                    d = json.loads(msg.data)
                    if d.get("channel") == "trades":
                        for t in d.get("data", []):
                            for u in t.get("users", []):
                                users.add(u.lower())
    except Exception as e:
        print("ws 오류:", e)
    return users


async def leaderboard(s, min_value):
    """[(주소, 계좌가치)]"""
    try:
        async with s.get(LB, timeout=aiohttp.ClientTimeout(total=120)) as r:
            d = await r.json(content_type=None)
        return [(x["ethAddress"].lower(), float(x.get("accountValue") or 0)) for x in d.get("leaderboardRows", [])
                if float(x.get("accountValue") or 0) >= min_value]
    except Exception as e:
        print("리더보드 오류:", e)
        return []


async def vaults(s, min_tvl):
    """TVL 기준 볼트 주소 + 부모 볼트의 하위 볼트 주소"""
    out = set()
    try:
        async with s.get(VAULTS, timeout=aiohttp.ClientTimeout(total=120)) as r:
            d = await r.json(content_type=None)
        for v in d:
            sm = v.get("summary", {})
            if sm.get("isClosed"):
                continue
            rel = sm.get("relationship") or {}
            if rel.get("type") == "parent":
                out.update(x.lower() for x in rel.get("data", {}).get("childAddresses", []))
            if float(sm.get("tvl") or 0) >= min_tvl:
                out.add(sm["vaultAddress"].lower())
    except Exception as e:
        print("볼트 오류:", e)
    return out


async def scan_subs(s, masters, budget_s, conc):
    """마스터 계좌의 서브계정 → {서브주소: clearinghouseState} (응답에 상태가 같이 옴)"""
    sem = asyncio.Semaphore(conc)
    out, done = {}, []
    t_end = time.time() + budget_s

    async def one(m):
        if time.time() > t_end:
            return
        async with sem:
            if time.time() > t_end:
                return
            r = await post(s, {"type": "subAccounts", "user": m})
            if r is None:
                return
            done.append(m)
            for x in r or []:
                u = (x.get("subAccountUser") or "").lower()
                if u:
                    out[u] = x.get("clearinghouseState") or {}

    await asyncio.gather(*[one(m) for m in masters])
    return out, done


async def scan(s, addrs, budget_s, conc):
    sem = asyncio.Semaphore(conc)
    out = {}
    t_end = time.time() + budget_s

    async def one(a):
        if time.time() > t_end:
            return
        async with sem:
            if time.time() > t_end:
                return
            st = await post(s, {"type": "clearinghouseState", "user": a})
            if st is not None:
                out[a] = st

    await asyncio.gather(*[one(a) for a in addrs])
    return out


def build(states, ctx, prev_big, t):
    res = {}
    for c in COINS:
        px = ctx[c]["mark"]
        oi = ctx[c]["oi"]
        n = int(RANGE / STEP)
        lo = px * (1 - RANGE)
        edges = [round(px * (1 - RANGE + i * STEP), 6) for i in range(2 * n + 1)]
        longb = [0.0] * (2 * n)
        shortb = [0.0] * (2 * n)
        cov_l = cov_s = 0.0          # 읽은 포지션 크기(코인)
        liq_l = liq_s = 0.0          # 청산가 있는 포지션 금액(달러)
        out_l = out_s = 0.0          # ±25% 밖
        cnt = 0
        big = []
        for a, st in states.items():
            for ap in st.get("assetPositions", []):
                p = ap.get("position", {})
                if p.get("coin") != c:
                    continue
                sz = float(p.get("szi") or 0)
                if sz == 0:
                    continue
                cnt += 1
                usd = abs(sz) * px
                lp = p.get("liquidationPx")
                lp = float(lp) if lp not in (None, "") else None
                if sz > 0:
                    cov_l += sz
                else:
                    cov_s += -sz
                if lp and lp > 0:
                    i = int((lp - lo) / (px * STEP))
                    if sz > 0:
                        liq_l += usd
                        if 0 <= i < 2 * n and lp < px:
                            longb[i] += usd
                        else:
                            out_l += usd
                    else:
                        liq_s += usd
                        if 0 <= i < 2 * n and lp > px:
                            shortb[i] += usd
                        else:
                            out_s += usd
                if usd >= BIG_USD:
                    lev = p.get("leverage") or {}
                    key = f"{a}:{c}:{'L' if sz > 0 else 'S'}"
                    pb = prev_big.get(key)
                    first = pb["first"] if pb else t
                    big.append({
                        "addr": a, "side": "롱" if sz > 0 else "숏", "size": round(abs(sz), 4),
                        "usd": round(usd), "entry": float(p.get("entryPx") or 0),
                        "liq": lp, "lev": lev.get("value"), "mode": lev.get("type"),
                        "upnl": round(float(p.get("unrealizedPnl") or 0)),
                        "first": first, "prev_size": pb["size"] if pb else None,
                    })
        big.sort(key=lambda x: -x["usd"])
        res[c] = {
            "price": px, "oi": oi, "oi_usd": round(oi * px), "edges": edges,
            "long": [round(x) for x in longb], "short": [round(x) for x in shortb],
            "cov_long": round(cov_l / oi, 4) if oi else None,
            "cov_short": round(cov_s / oi, 4) if oi else None,
            "liq_long_usd": round(liq_l), "liq_short_usd": round(liq_s),
            "out_long_usd": round(out_l), "out_short_usd": round(out_s),
            "positions": cnt, "big": big[:60], "_bigall": big,
        }
    return res


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="liq.json")
    ap.add_argument("--state", default="state/hl_state.json")
    ap.add_argument("--ws", type=int, default=240, help="웹소켓 주소 수집 초")
    ap.add_argument("--budget", type=int, default=900, help="포지션 조회 시간 예산 초")
    ap.add_argument("--conc", type=int, default=8)
    ap.add_argument("--min-value", type=float, default=10_000)
    ap.add_argument("--sub-budget", type=int, default=240, help="서브계정 탐색 시간 예산 초")
    ap.add_argument("--vault-tvl", type=float, default=10_000)
    a = ap.parse_args()

    t0 = now()
    st = {"addrs": {}, "lb_at": 0, "big": {}}
    if os.path.exists(a.state):
        try:
            st = json.load(open(a.state))
        except Exception:
            pass
    addrs = st.setdefault("addrs", {})   # addr -> [마지막 확인 ts, 포지션 있었나(0/1)]

    async with aiohttp.ClientSession() as s:
        # 리더보드는 하루 1번 갱신
        masters = st.setdefault("masters", {})   # 리더보드 주소 -> [계좌가치, 서브계정 확인 ts]
        if t0 - st.get("lb_at", 0) > 86400 or not addrs or a.min_value < st.get("lb_min", 1e18):
            for x, av in await leaderboard(s, a.min_value):
                addrs.setdefault(x, [0, 1])
                masters[x] = [av, masters.get(x, [0, 0])[1]]
            for x in await vaults(s, a.vault_tvl):
                addrs.setdefault(x, [0, 1])
            st["lb_at"] = t0
            st["lb_min"] = a.min_value
        meta = await post(s, {"type": "metaAndAssetCtxs"})
        uni = [x["name"] for x in meta[0]["universe"]]
        # 웹소켓은 조회하는 동안 내내 백그라운드로 (거래대금 상위 코인)
        vol = sorted(((float(meta[1][i].get("dayNtlVlm") or 0), u) for i, u in enumerate(uni)), reverse=True)
        ws_coins = list(dict.fromkeys(COINS + [u for _, u in vol[:WS_COINS]]))
        ws_users = set()
        ws_task = asyncio.create_task(discover_ws(a.ws + a.sub_budget + a.budget, ws_coins, ws_users))

        # 서브계정: 계좌가치 큰 순, 3일 안에 확인한 마스터는 건너뜀
        todo = [m for m, v in sorted(masters.items(), key=lambda kv: -kv[1][0]) if t0 - v[1] > 3 * 86400]
        sub_states, done = await scan_subs(s, todo, a.sub_budget, a.conc)
        for m in done:
            masters[m][1] = t0
        for u in sub_states:
            addrs.setdefault(u, [0, 1])
        print(f"서브계정 확인 마스터 {len(done)}/{len(todo)} · 서브계정 {len(sub_states)}")

        # 웹소켓 몇 분 모은 뒤 조회 시작 (조회 중 새로 잡힌 주소는 다음 실행에)
        await asyncio.sleep(max(0, a.ws - a.sub_budget))
        new = 0
        for u in list(ws_users):
            if u not in addrs:
                addrs[u] = [0, 1]
                new += 1
        print(f"주소 {len(addrs)} (웹소켓 신규 {new} / 수집 {len(ws_users)} · 코인 {len(ws_coins)})")

        ctx = {}
        for c in COINS:
            i = uni.index(c)
            d = meta[1][i]
            ctx[c] = {"mark": float(d["markPx"]), "oi": float(d["openInterest"])}

        # 우선순위: 포지션 있던 주소(오래 안 본 순) → 한 번도 안 본 주소 → 나머지(오래 안 본 순)
        items = list(addrs.items())
        random.shuffle(items)
        items.sort(key=lambda kv: (0 if kv[1][1] else 1, kv[1][0]))
        order = [k for k, _ in items if k not in sub_states]
        states = await scan(s, order, a.budget, a.conc)
        states.update(sub_states)
        ws_task.cancel()
        try:
            await ws_task
        except BaseException:
            pass
        late = 0
        for u in list(ws_users):
            if u not in addrs:
                addrs[u] = [0, 1]
                late += 1
        print(f"조회 중 웹소켓 신규 {late}")

    for k, v in states.items():
        has = any(float(p["position"].get("szi") or 0) != 0 for p in v.get("assetPositions", []))
        addrs[k] = [t0, 1 if has else 0]
    # 30일 넘게 포지션 없는 주소 정리
    for k in [k for k, v in addrs.items() if v[1] == 0 and v[0] and t0 - v[0] > 30 * 86400]:
        addrs.pop(k)

    # 이번에 못 읽은 '포지션 있던' 주소는 직전 상태를 재사용 (6시간 이내)
    cache = st.get("pos_cache", {})
    for k, v in states.items():
        if v.get("assetPositions"):
            cache[k] = {"t": t0, "s": {"assetPositions": [
                {"position": p["position"]} for p in v["assetPositions"]
                if p["position"].get("coin") in COINS]}}
        else:
            cache.pop(k, None)
    merged = {}
    stale = 0
    for k, v in list(cache.items()):
        if k in states:
            merged[k] = states[k]
        elif t0 - v["t"] < 6 * 3600:
            merged[k] = v["s"]
            stale += 1
        else:
            cache.pop(k)
    st["pos_cache"] = cache

    res = build(merged, ctx, st.get("big", {}), t0)
    st["big"] = {f"{b['addr']}:{c}:{'L' if b['side']=='롱' else 'S'}": {"first": b["first"], "size": b["size"]}
                 for c in COINS for b in res[c].pop("_bigall")}

    out = {"updated": now(), "started": t0, "source": "Hyperliquid", "scanned": len(states), "reused": stale,
           "addresses": len(addrs), "range": RANGE, "step": STEP, "coins": res}
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    json.dump(out, open(a.out, "w"), ensure_ascii=False, separators=(",", ":"))
    os.makedirs(os.path.dirname(a.state) or ".", exist_ok=True)
    json.dump(st, open(a.state, "w"), separators=(",", ":"))
    b = res["BTC"]
    print(f"조회 {len(states)} 재사용 {stale} · BTC 커버리지 롱 {b['cov_long']} 숏 {b['cov_short']} · 포지션 {b['positions']} · {now()-t0}s")


if __name__ == "__main__":
    asyncio.run(main())
