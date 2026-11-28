/* 크립토 데스크 공용: 숫자 형식, 캔버스 차트, 거래소 데이터, 지지/저항 엔진 */
const C = {
  bg:'#111827', grid:'#1a2230', axis:'#5b6475', text:'#e5e7eb', mute:'#8b95a7',
  blue:'#3b82f6', red:'#f0506e', green:'#22c55e', teal:'#2dd4bf', orange:'#f59e0b', yellow:'#facc15', white:'#e5e7eb'
};
const LIQ_URL = 'https://raw.githubusercontent.com/stopfirst/market-dash/liq-data/liq.json';
const FAPI = 'https://fapi.binance.com';
const SAPI = 'https://api.binance.com';

/* ---------- 형식 ---------- */
const nf = (v, d = 0) => (v == null || isNaN(v)) ? '–' : Number(v).toLocaleString('en-US', {minimumFractionDigits: d, maximumFractionDigits: d});
const sgn = (v, d = 0) => (v > 0 ? '+' : v < 0 ? '−' : '') + nf(Math.abs(v), d);
function pxd(p){ return p >= 1000 ? 1 : p >= 100 ? 2 : p >= 1 ? 3 : 5; }
function fpx(p){ return nf(p, p >= 10000 ? 0 : pxd(p)); }
/* 달러 → 한국식 (억/만) */
function usdKo(v, d){
  if (v == null || isNaN(v)) return '–';
  const a = Math.abs(v), s = v < 0 ? '−' : '';
  if (a >= 1e8) return s + nf(a / 1e8, d ?? (a >= 1e10 ? 0 : 1)) + '억';
  if (a >= 1e4) return s + nf(a / 1e4, d ?? 0) + '만';
  return s + nf(a, 0);
}
function usdM(v){ const a = Math.abs(v); return (v<0?'−':'') + '$' + (a >= 1e9 ? nf(a/1e9,2)+'B' : a >= 1e6 ? nf(a/1e6,1)+'M' : nf(a/1e3,0)+'K'); }
const DOW = ['일','월','화','수','목','금','토'];
function kst(ts){ return new Date(ts + 9 * 3600e3); } // UTC 필드로 KST 읽기
function fKst(ts, mode = 'full'){
  const d = kst(ts), p = n => String(n).padStart(2,'0');
  const md = `${p(d.getUTCMonth()+1)}/${p(d.getUTCDate())}`;
  const hm = `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
  if (mode === 'md') return md;
  if (mode === 'hm') return hm;
  if (mode === 'mdw') return `${md}(${DOW[d.getUTCDay()]})`;
  if (mode === 'date') return `${d.getUTCFullYear()}-${p(d.getUTCMonth()+1)}-${p(d.getUTCDate())} (${DOW[d.getUTCDay()]})`;
  return `${md}(${DOW[d.getUTCDay()]}) ${hm}`;
}
async function getJSON(url, opt){
  const r = await fetch(url, opt);
  if (!r.ok) throw new Error(r.status + ' ' + url.split('?')[0]);
  return r.json();
}

/* ---------- 캔버스 ---------- */
function setupCanvas(cv, h){
  const dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth || cv.parentElement.clientWidth;
  cv.style.height = h + 'px';
  cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
  const g = cv.getContext('2d'); g.setTransform(dpr,0,0,dpr,0,0);
  g.font = '11px -apple-system,BlinkMacSystemFont,"Noto Sans KR",sans-serif';
  return {g, w, h};
}
function niceTicks(lo, hi, n = 4){
  if (!(hi > lo)) { hi = lo + 1; }
  const raw = (hi - lo) / n, mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const st = [1,2,2.5,5,10].map(m => m*mag).find(s => s >= raw) || 10*mag;
  const out = []; for (let v = Math.ceil(lo/st)*st; v <= hi + 1e-9; v += st) out.push(+v.toFixed(10));
  return out;
}
function pillAt(g, x, y, text, bg, fg = '#0b0f17', align = 'left'){
  g.save(); g.font = 'bold 11px -apple-system,sans-serif';
  const w = g.measureText(text).width + 10, h = 17;
  let x0 = align === 'right' ? x - w : align === 'center' ? x - w/2 : x;
  g.fillStyle = bg; roundRect(g, x0, y - h/2, w, h, 8); g.fill();
  g.fillStyle = fg; g.textBaseline = 'middle'; g.fillText(text, x0 + 5, y + .5);
  g.restore(); return w;
}
function roundRect(g, x, y, w, h, r){ g.beginPath(); g.moveTo(x+r,y); g.arcTo(x+w,y,x+w,y+h,r); g.arcTo(x+w,y+h,x,y+h,r); g.arcTo(x,y+h,x,y,r); g.arcTo(x,y,x+w,y,r); g.closePath(); }

/* 시계열 차트: line / area / bar(양수·음수 색) */
function tsChart(cv, o){
  const {g, w, h} = setupCanvas(cv, o.height || 180);
  const pad = {l: 6, r: o.padR ?? 52, t: o.padT ?? 10, b: 20};
  const X = o.x, n = X.length; if (!n) return;
  let lo = Infinity, hi = -Infinity;
  for (const s of o.series) for (const v of s.y) if (v != null && isFinite(v)) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
  if (o.zero) { lo = Math.min(lo, 0); hi = Math.max(hi, 0); }
  if (o.yMin != null) lo = o.yMin;
  const span = hi - lo || Math.abs(hi) || 1; hi += span * .08; if (!o.zero && o.yMin == null) lo -= span * .08;
  const pw = w - pad.l - pad.r, ph = h - pad.t - pad.b;
  const xs = i => pad.l + (n === 1 ? pw/2 : i * pw / (n - 1));
  const ys = v => pad.t + (hi - v) / (hi - lo) * ph;
  g.strokeStyle = C.grid; g.lineWidth = 1; g.fillStyle = C.axis; g.textBaseline = 'middle';
  for (const t of niceTicks(lo, hi, o.ticks || 4)) { const y = ys(t); g.beginPath(); g.moveTo(pad.l, y); g.lineTo(w - pad.r, y); g.stroke(); g.fillText((o.yfmt || nf)(t), w - pad.r + 6, y); }
  if (o.zero) { g.strokeStyle = '#334155'; g.beginPath(); g.moveTo(pad.l, ys(0)); g.lineTo(w - pad.r, ys(0)); g.stroke(); }
  if (o.shade) { const [a, b] = o.shade; g.fillStyle = 'rgba(148,163,184,.08)'; g.fillRect(xs(a), pad.t, xs(b) - xs(a), ph); }
  // x 라벨
  g.fillStyle = C.axis; g.textBaseline = 'top';
  const labs = o.xlabels || []; let lastX = -99;
  for (const [i, t] of labs) { const x = xs(i); const tw = g.measureText(t).width; if (x - tw/2 < lastX + 6 || x + tw/2 > w - pad.r + 30) continue; g.fillText(t, Math.max(pad.l, x - tw/2), h - pad.b + 5); lastX = x + tw/2; }
  for (const s of o.series) {
    if (s.type === 'bar') {
      const bw = Math.max(1, pw / n * .8);
      s.y.forEach((v, i) => { if (v == null) return; g.fillStyle = v >= 0 ? (s.color || C.teal) : (s.neg || C.orange); const y0 = ys(0), y1 = ys(v); g.fillRect(xs(i) - bw/2, Math.min(y0, y1), bw, Math.max(1, Math.abs(y1 - y0))); });
    } else {
      const pts = []; s.y.forEach((v, i) => { if (v != null && isFinite(v)) pts.push([xs(i), ys(v), v]); });
      if (!pts.length) continue;
      if (s.type === 'area') {
        if (s.neg) { // 0 기준 양/음 색 분리
          for (const [col, test] of [[s.color, v => v >= 0], [s.neg, v => v < 0]]) {
            g.save(); g.beginPath(); const y0 = ys(0);
            g.rect(pad.l, test(1) ? pad.t : y0, pw, test(1) ? y0 - pad.t : pad.t + ph - y0); g.clip();
            g.beginPath(); g.moveTo(pts[0][0], y0); pts.forEach(p => g.lineTo(p[0], p[1])); g.lineTo(pts[pts.length-1][0], y0); g.closePath();
            g.fillStyle = col + '55'; g.fill(); g.strokeStyle = col; g.lineWidth = 1.4; g.beginPath(); pts.forEach((p, k) => k ? g.lineTo(p[0], p[1]) : g.moveTo(p[0], p[1])); g.stroke(); g.restore();
          }
          continue;
        }
        const base = o.zero ? ys(0) : pad.t + ph;
        g.beginPath(); g.moveTo(pts[0][0], base); pts.forEach(p => g.lineTo(p[0], p[1])); g.lineTo(pts[pts.length-1][0], base); g.closePath();
        g.fillStyle = s.fill || 'rgba(148,163,184,.16)'; g.fill();
      }
      g.strokeStyle = s.color || C.white; g.lineWidth = s.width || 1.6; g.beginPath();
      pts.forEach((p, k) => k ? g.lineTo(p[0], p[1]) : g.moveTo(p[0], p[1])); g.stroke();
      if (s.pill !== false) { const p = pts[pts.length-1]; g.fillStyle = s.color || C.white; g.beginPath(); g.arc(p[0], p[1], 3, 0, 7); g.fill(); }
    }
  }
  for (const L of (o.labels || [])) { // {i, v, text, color, align}
    const x = xs(L.i), y = ys(L.v); g.fillStyle = L.color || C.mute; g.textBaseline = 'bottom';
    g.font = '11px -apple-system,sans-serif'; const tw = g.measureText(L.text).width;
    let tx = x - tw/2; tx = Math.max(pad.l + 2, Math.min(tx, w - pad.r - tw - 2));
    g.fillText(L.text, tx, y - 5); if (L.dot) { g.beginPath(); g.arc(x, y, 3, 0, 7); g.fill(); }
  }
  if (o.endPill) { const v = o.endPill.v; pillAt(g, w - pad.r + 2, ys(v), o.endPill.text, o.endPill.bg || C.white); }
  return {xs, ys, pad, w, h};
}

/* ---------- 거래소 데이터 ---------- */
async function bnKlines(sym, tf, limit = 500, spot = false){
  const url = spot ? `${SAPI}/api/v3/klines?symbol=${sym}&interval=${tf}&limit=${Math.min(limit,1000)}`
                   : `${FAPI}/fapi/v1/klines?symbol=${sym}&interval=${tf}&limit=${Math.min(limit,1500)}`;
  const d = await getJSON(url);
  return d.map(k => ({t: k[0], o: +k[1], h: +k[2], l: +k[3], c: +k[4], v: +k[5], tb: +k[9], tc: k[6]}));
}
async function bnOIHist(sym, period, limit = 500){
  const d = await getJSON(`${FAPI}/futures/data/openInterestHist?symbol=${sym}&period=${period}&limit=${limit}`);
  return d.map(x => ({t: +x.timestamp, oi: +x.sumOpenInterest, usd: +x.sumOpenInterestValue}));
}
async function bybitOIHist(sym, n = 500){ // 5분, 코인 단위
  let out = [], cursor = '';
  for (let k = 0; k < 3 && out.length < n; k++) {
    const d = await getJSON(`https://api.bybit.com/v5/market/open-interest?category=linear&symbol=${sym}&intervalTime=5min&limit=200${cursor ? '&cursor=' + encodeURIComponent(cursor) : ''}`);
    out = out.concat((d.result?.list || []).map(x => ({t: +x.timestamp, oi: +x.openInterest})));
    cursor = d.result?.nextPageCursor; if (!cursor) break;
  }
  return out.sort((a, b) => a.t - b.t);
}
async function okxOIHist(coin){ // 5분, 코인 단위 (CORS 막히면 실패)
  const d = await getJSON(`https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-history?instId=${coin}-USDT-SWAP&period=5m&limit=100`);
  return (d.data || []).map(x => ({t: +x[0], oi: +x[2]})).sort((a, b) => a.t - b.t);
}
async function bitgetOI(sym){ const d = await getJSON(`https://api.bitget.com/api/v2/mix/market/open-interest?symbol=${sym}&productType=usdt-futures`); return +d.data.openInterestList[0].size; }
async function hlCtx(coin){
  const d = await getJSON('https://api.hyperliquid.xyz/info', {method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify({type:'metaAndAssetCtxs'})});
  const i = d[0].universe.findIndex(u => u.name === coin); return {oi: +d[1][i].openInterest, mark: +d[1][i].markPx};
}
async function loadLiq(){ return getJSON(LIQ_URL + '?t=' + Math.floor(Date.now() / 120000)); }

/* 브라우저가 열려 있는 동안 OI 스냅샷 기록 (Bitget·Hyperliquid 처럼 이력 API가 없는 곳) */
function snapStore(key){
  const K = 'oisnap:' + key;
  let a = []; try { a = JSON.parse(localStorage.getItem(K) || '[]'); } catch (e) {}
  return {
    all: () => a,
    add(t, oi){ a.push({t, oi}); const cut = Date.now() - 3 * 86400e3; a = a.filter(x => x.t > cut).slice(-5000); try { localStorage.setItem(K, JSON.stringify(a)); } catch (e) {} },
  };
}

/* ---------- 변동성 단계 ---------- */
function volState(bars){ // 마지막 봉 = 진행 중
  if (bars.length < 40) return null;
  const done = bars.slice(0, -1), cur = bars[bars.length - 1];
  const rng = b => (b.h - b.l) / b.o * 100;
  const avg = a => a.reduce((s, b) => s + rng(b), 0) / a.length;
  const longAvg = avg(done.slice(-90)), shortAvg = avg(done.slice(-6));
  const ratio = shortAvg / longAvg;
  const lv = ratio < .6 ? 1 : ratio < .85 ? 2 : ratio < 1.15 ? 3 : ratio < 1.5 ? 4 : 5;
  return {lv, ratio, avg: longAvg, now: rng(cur), until: cur.tc + 1, rising: shortAvg > avg(done.slice(-12, -6))};
}

/* ---------- 지지/저항 엔진 (1D·4H 스윙 → 1H 정밀화) ---------- */
function pivots(bars, k = 3){
  const hs = [], ls = [];
  for (let i = k; i < bars.length - k; i++) {
    let H = true, L = true;
    for (let j = i - k; j <= i + k; j++) { if (j === i) continue; if (bars[j].h >= bars[i].h) H = false; if (bars[j].l <= bars[i].l) L = false; }
    if (H) hs.push(i); if (L) ls.push(i);
  }
  return {hs, ls};
}
function srZones(d1, h4, h1, price){
  const raw = [];
  const add = (bars, tf, w) => {
    const {hs, ls} = pivots(bars.slice(0, -1), tf === '1D' ? 2 : 3);
    for (const i of hs) { const b = bars[i]; raw.push({lo: Math.max(b.o, b.c), hi: b.h, tf, w, t: b.t, kind: 'H'}); }
    for (const i of ls) { const b = bars[i]; raw.push({lo: b.l, hi: Math.min(b.o, b.c), tf, w, t: b.t, kind: 'L'}); }
  };
  add(d1, '1D', 2); add(h4, '4H', 1);
  // 너무 두꺼운 존은 꼬리 쪽 0.6% 로 자름
  for (const z of raw) { const mx = z.hi * .006; if (z.hi - z.lo > mx) { if (z.kind === 'H') z.lo = z.hi - mx; else z.hi = z.lo + mx; } if (z.hi - z.lo < z.hi * .0008) { z.lo -= z.hi * .0004; z.hi += z.hi * .0004; } }
  raw.sort((a, b) => a.lo - b.lo);
  const merged = [];
  for (const z of raw) {
    const m = merged.find(m => z.lo <= m.hi * 1.002 && z.hi >= m.lo * .998 && (Math.max(m.hi, z.hi) - Math.min(m.lo, z.lo)) <= m.hi * .01);
    if (m) { m.lo = Math.min(m.lo, z.lo); m.hi = Math.max(m.hi, z.hi); m.score += z.w; m.tfs.add(z.tf); m.t = Math.max(m.t, z.t); m.n++; }
    else merged.push({lo: z.lo, hi: z.hi, score: z.w, tfs: new Set([z.tf]), t: z.t, n: 1});
  }
  // 1H 정밀화: 존 안의 1H 스윙 꼬리 범위로 좁힘
  const p1 = pivots(h1.slice(0, -1), 3);
  for (const m of merged) {
    const inside = [...p1.hs.map(i => [h1[i].h, Math.max(h1[i].o, h1[i].c)]), ...p1.ls.map(i => [Math.min(h1[i].o, h1[i].c), h1[i].l])]
      .filter(([a, b]) => Math.max(a, b) >= m.lo && Math.min(a, b) <= m.hi);
    if (inside.length) { const lo = Math.max(m.lo, Math.min(...inside.map(x => Math.min(...x)))), hi = Math.min(m.hi, Math.max(...inside.map(x => Math.max(...x)))); if (hi > lo) { m.lo = lo; m.hi = hi; } m.tfs.add('1H'); }
  }
  // 이미 크게 관통된 존(최근 1H 종가가 존을 여러 번 가로지름)은 점수 감점
  const recent = h1.slice(-72);
  for (const m of merged) { let cross = 0; for (let i = 1; i < recent.length; i++) { const a = recent[i-1].c, b = recent[i].c; if ((a - m.lo) * (b - m.lo) < 0 || (a - m.hi) * (b - m.hi) < 0) cross++; } m.score -= cross * .5; m.cross = cross; }
  const above = merged.filter(m => m.lo > price).sort((a, b) => a.lo - b.lo);
  const below = merged.filter(m => m.hi < price).sort((a, b) => b.hi - a.hi);
  const pick = arr => { const out = [];
    for (const m of arr.slice(0, 8)) { if (m.score < 1) continue; if (out.some(o => m.lo <= o.hi * 1.002 && m.hi >= o.lo * .998)) { const o = out.find(o => m.lo <= o.hi * 1.002 && m.hi >= o.lo * .998); o.lo = Math.min(o.lo, m.lo); o.hi = Math.max(o.hi, m.hi); o.score += m.score; m.tfs.forEach(t => o.tfs.add(t)); continue; } out.push({...m, tfs: new Set(m.tfs)}); if (out.length >= 2) break; }
    return out; };
  const R = pick(above), S = pick(below);
  const label = (m, i, p) => { const tfs = ['1D','4H','1H'].filter(t => m.tfs.has(t)); return {...m, name: p + (i + 1), tfs: tfs.join(' / '), kind: m.score >= 3 ? '원점' : '반응'}; };
  const zones = [...R.map((m, i) => label(m, i, 'R')), ...S.map((m, i) => label(m, i, 'S'))];
  const best = zones.reduce((b, z) => (!b || z.score > b.score) ? z : b, null); if (best) best.star = true;
  return zones;
}
function structure(h4){
  const {hs, ls} = pivots(h4.slice(0, -1), 3);
  const H = hs.slice(-2).map(i => h4[i].h), L = ls.slice(-2).map(i => h4[i].l);
  if (H.length < 2 || L.length < 2) return '판단 보류';
  if (H[1] > H[0] && L[1] > L[0]) return '상승 구조';
  if (H[1] < H[0] && L[1] < L[0]) return '하락 구조';
  return '횡보 구조';
}
/* FVG: 3봉 갭, 아직 채워지지 않은 것 */
function fvgs(bars, tf, maxN = 3){
  const out = [];
  for (let i = 2; i < bars.length - 1; i++) {
    const a = bars[i-2], c = bars[i];
    if (c.l > a.h) out.push({lo: a.h, hi: c.l, dir: 'up', i, t: bars[i-1].t, tf});
    if (c.h < a.l) out.push({lo: c.h, hi: a.l, dir: 'dn', i, t: bars[i-1].t, tf});
  }
  return out.filter(z => { for (let j = z.i + 1; j < bars.length; j++) { if (z.dir === 'up' ? bars[j].l <= z.lo : bars[j].h >= z.hi) return false; } return true; })
            .slice(-maxN);
}

/* 공용 상단바 */
function topbar(active){
  const items = [['../index.html','메뉴'],['index.html','허브'],['liq.html','청산 지도'],['desk.html','분석 데스크'],['stable.html','스테이블코인'],['sr.html','지지/저항 봇']];
  document.getElementById('top').innerHTML = `<div class="brand"><i></i>TRADING DESK</div><div class="nav">${items.map(([h,t]) => `<a href="${h}" class="${h===active?'on':''}">${t}</a>`).join('')}</div>`;
}
