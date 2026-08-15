import yfinance as yf, pandas as pd, numpy as np

# 本番(market.py)と同じ定義: 単純移動平均RSI14 / 52週高値=直近252営業日のmax / Adj Close(配当込み)
df = yf.download("VOO", start="2013-12-01", end="2026-08-17", auto_adjust=True, progress=False)
close = df["Close"].squeeze()

delta = close.diff()
gain = delta.clip(lower=0); loss = -delta.clip(upper=0)
rsi = 100 - (100 / (1 + gain.rolling(14).mean() / loss.rolling(14).mean()))
high52 = close.rolling(252).max()
dd = (close / high52 - 1.0) * 100.0   # 52週高値からの下落率(負)

idx = close.index
start = pd.Timestamp("2015-01-01")
mask_win = idx >= start
last = idx[-1]
fwd_cut = last - pd.DateOffset(months=12)   # これ以降の起点は12ヶ月フォワードが取れない

def fwd12(d):
    t = d + pd.DateOffset(months=12)
    if t > last: return None
    j = idx.searchsorted(t)
    if j >= len(idx): return None
    return (close.iloc[j] / close.loc[d] - 1.0) * 100.0

# ベースライン: 窓内の全営業日の12ヶ月フォワード平均
base_vals = [fwd12(d) for d in idx[mask_win & (idx <= fwd_cut)]]
base_vals = [v for v in base_vals if v is not None]
BASE = float(np.mean(base_vals))
print(f"評価窓: {idx[mask_win][0].date()} 〜 {last.date()}  営業日 {mask_win.sum()}")
print(f"12ヶ月フォワードが取れる起点の上限: {fwd_cut.date()}  (n={len(base_vals)})")
print(f"ベースライン(全期間の任意日の12ヶ月フォワード平均) = {BASE:+.2f}%")
YEARS = (fwd_cut - idx[mask_win][0]).days / 365.25
print(f"エピソード/年 の分母(年) = {YEARS:.2f}\n")

def episodes(dd_th, rsi_th):
    cond = (dd <= -dd_th) & mask_win
    if rsi_th is not None: cond = cond & (rsi < rsi_th)
    days = idx[cond.fillna(False)]
    eps, lastd = [], None
    for d in days:
        if lastd is None or (d - lastd).days > 30:
            eps.append(d); lastd = d
        else:
            lastd = d          # 30日以内の連続成立は同一エピソードに畳む(起点は最初の日)
    kept = [d for d in eps if d <= fwd_cut]
    dropped = len(eps) - len(kept)
    ex = [fwd12(d) - BASE for d in kept if fwd12(d) is not None]
    return kept, dropped, ex

DDS = [5, 8, 10, 12, 15, 20]
RSIS = [25, 30, 35, 40, None]
lab = lambda r: "なし" if r is None else str(r)

print("## グリッド（セル = エピソード数/年 ／ 平均超過pt ／ n）")
print("| 下落率＼RSI | " + " | ".join(f"<{lab(r)}" for r in RSIS) + " |")
print("|---|" + "---|"*len(RSIS))
rows = {}
for d in DDS:
    cells = []
    for r in RSIS:
        kept, dropped, ex = episodes(d, r)
        rows[(d,r)] = (kept, dropped, ex)
        cells.append("—" if not ex else f"{len(kept)/YEARS:.2f}／{np.mean(ex):+.1f}／n={len(ex)}")
    print(f"| -{d}% | " + " | ".join(cells) + " |")

print("\n## 主要組み合わせの詳細")
print("| 条件 | n | 除外 | 平均超過 | 標準偏差 | 最小 | 最大 | 便益/年(現金5%) | 便益/年(現金2%) |")
print("|---|---|---|---|---|---|---|---|---|")
for d, r in [(12,30),(12,None),(10,30),(8,30),(8,35),(5,30),(5,35),(15,30),(20,30),(0,30)]:
    if d == 0:
        cond = mask_win & (rsi < r); days = idx[cond.fillna(False)]
        eps, lastd = [], None
        for x in days:
            if lastd is None or (x-lastd).days > 30: eps.append(x); lastd = x
            else: lastd = x
        kept = [x for x in eps if x <= fwd_cut]; dropped = len(eps)-len(kept)
        ex = [fwd12(x)-BASE for x in kept if fwd12(x) is not None]
        name = f"RSI<{r} のみ（下落率条件なし）"
    else:
        kept, dropped, ex = rows[(d,r)]; name = f"-{d}% かつ RSI<{lab(r)}"
    if not ex: print(f"| {name} | 0 | {dropped} | — | — | — | — | — | — |"); continue
    epy = len(kept)/YEARS; m = np.mean(ex)
    sd = np.std(ex, ddof=1) if len(ex) > 1 else float('nan')
    print(f"| {name} | {len(ex)} | {dropped} | {m:+.2f} | {sd:.2f} | {min(ex):+.2f} | {max(ex):+.2f} "
          f"| {epy*0.025*m:+.3f}pt | {epy*0.010*m:+.3f}pt |")

print("\n## 現行条件(-12%/RSI30)のエピソード起点日")
kept, dropped, ex = rows[(12,30)]
for d in kept:
    f = fwd12(d)
    print(f"  {d.date()}  DD={float(dd.loc[d]):.1f}%  RSI={float(rsi.loc[d]):.1f}  12mフォワード={f:+.2f}%  超過={f-BASE:+.2f}pt")
print(f"  （12ヶ月フォワードが取れず除外: {dropped}件）")
