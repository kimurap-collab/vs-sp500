# ドル建て移行＋moomoo約定基盤 実装指示書（Fable起草 2026-08-14・SPEC.md追補第4弾）

憲章v1.4に合わせ、(A)評価通貨を円→ドル、(B)1306.T→EWJ、(C)売買と実費をmoomooの仮想口座に移す。

## 仕様の出典（大将の発言・承認「いけ」2026-08-14取得済み）
- 「レートも円じゃなくてドルでもいいよ」（評価通貨の変更）
- 「日本etfもmoomooで買えないならはずしていい」＋「同様のetfが売ってないか探してほしいな」（1306.T→EWJへの置換）
- 「これはベースをmoomooにして、その結果をこちらの管理画面に移してきたらよいのでは？それだと簡単になりそう。
  moomooでの取引には信憑性もあるし」（約定と実費をmoomooに移す）
- EWJの選定理由・ベンチマークをコード計算で残す判断・縮退設計は、Fableが提案し大将が「いけ」で承認
- **憲章v1.4が確定仕様である。charter.md と食い違ったら charter.md が正**

## 前提情報（実測済み・そのまま使ってよい）
- OpenD: `127.0.0.1:11111` で稼働中。Python SDKは `moomoo`（`pip install moomoo-api` 導入済み）
- 仮想口座: `acc_id=5338087` / `TrdEnv.SIMULATE` / `TrdMarket.US` / `SecurityFirm.FUTUSG`
- 仮想口座の現保有（2026-08-13に発注・約定済み）: VOO 31 / QQQ 21 / GLD 23 / IEF 33 / XLV 18
- 台帳(portfolio.json)の現保有: VOO 31.33 / QQQ 22.08 / GLD 24.39 / IEF 33.94 / XLV 19.29 / 1306.T 2360.16（端株）
- 仮想口座の現金は約$947,203（口座規模$1M）。**うちの実験規模は約$63,343なので、口座のNAVをそのまま使ってはならない**
- 気配取得の書式: `US.VOO` 等。`get_market_snapshot` / `request_history_kline` が使える
- **moomooはドル円を提供しない**（`FX.USDJPY` は "Unsupported quote market."）。為替はyfinanceのまま

## 対象ファイル
- 新規: `broker.py`（moomoo接続の薄いラッパ）、`tools/migrate_to_usd.py`（一度きりの移行スクリプト）
- 修正: `config.py` / `portfolio.py` / `market.py` / `daily_run.py` / `report.py` / `index.html` / `advisor.py`
- 台帳: `ledger/portfolio.json` / `ledger/history.csv`（移行スクリプトが書き換える）
- **編集禁止**: `charter.md` / `LOOP_PROMPT.md` / `SPEC*.md` / `theses.json` / `candidates/` / `logs/`

---

## 1. broker.py（新規・moomooの薄いラッパ）

外部依存をここに閉じ込める。他のモジュールはmoomoo SDKを直接importしないこと。

```python
def is_available() -> bool          # 127.0.0.1:11111 に3秒で繋がるか（socketで事前確認。SDKは無限リトライするため必須）
def get_positions() -> dict[str, float] | None   # {'VOO': 31.0, ...}。失敗時None
def get_cash() -> float | None                   # 口座の現金(USD)。失敗時None
def place_market_order(ticker: str, qty: int, side: str) -> dict | None
                                    # side='BUY'|'SELL'。約定まで待って {'filled_qty','avg_price'} を返す
def get_snapshot(tickers: list[str]) -> dict[str, float] | None   # 現在値（照合用）
```
- 接続は都度open/closeし、必ずcloseする（接続数が枯渇するため）
- 全関数に**必ずタイムアウト**を付ける（`signal.alarm` 等。無応答で固まる実績あり）
- 例外は握りつぶさずログに出し、戻り値Noneで表現する

## 2. config.py

- `INITIAL_CAPITAL_JPY` → **`INITIAL_CAPITAL_USD = 63343.06`**（10,000,000 ÷ 157.87）
- `BASE_CURRENCY = "USD"`
- WHITELISTから `1306.T` を削除し、**`EWJ`（currency=USD, name="MSCI日本株 ETF", type="etf"）を追加**
- **削除**: `US_ETF_FEE_RATE` / `US_ETF_FEE_CAP_USD` / `JP_ETF_FEE_RATE` / `FX_SPREAD_JPY_PER_USD`
  （コストはmoomooの実費に置き換わるため。参照箇所も全て消すこと）
- `FX_TICKER` は残してよい（管理画面の参考表示用。評価計算には使わない）
- 追加: `MOOMOO_HOST="127.0.0.1"` / `MOOMOO_PORT=11111` / `MOOMOO_ACC_ID=5338087`

## 3. portfolio.py

- `cash_jpy`/`cash_usd` の2本立てをやめ、**`cash_usd` 1本**にする
- `compute_nav_jpy` → **`compute_nav_usd(state, market)`**。為替を掛けない。全銘柄USD建て
- `compute_bench_nav_jpy` → **`compute_bench_nav_usd(state, voo_close)`** = `bench_units * voo_close`
  （**bench_unitsはVOOの株数なので通貨に依存しない。値は一切変更しないこと**）
- `compute_ticker_weight` / `compute_cash_ratio` から `usdjpy_mid` 引数を削除
- `execute_trades` の改造:
  - 手数料計算・為替両替のロジックを**全て削除**
  - 約定は `broker.place_market_order` に委譲し、**実約定株数・実約定価格**を使う
  - **現金は「moomooの現金増減」をそのまま台帳に適用する**（発注前後で `broker.get_cash()` を取り、差分を `cash_usd` に反映）
  - 株数は整数のみ（`int`）。注文金額から株数を求める際は切り捨て
  - ガードレール（ホワイトリスト・現金非負・集中規制・個別株ルール・1日10件）は**発注前チェックとして維持**
- 新規 `reconcile_positions(state) -> tuple[bool, str]`:
  台帳の保有と `broker.get_positions()` を比較。全銘柄一致でTrue。不一致なら理由文字列を返す
- `compute_avg_costs` は trades.csv ベースのまま（通貨がUSD一本になるだけ）

## 4. market.py
- 価格取得はyfinanceのまま（`EWJ` を含む米国ETFのみ。`1306.T` はもう来ない）
- 為替取得関数は残してよいが、**評価計算からは呼ばれなくなる**
- 既存のリトライ機構はそのまま

## 5. daily_run.py

新しい毎朝の流れ:
1. 台帳読込 → 価格取得（yfinance）
2. **異常停止判定**（既存: データ鮮度・±12%・NAV整合性・±30%配信破損）※通貨がUSDになるだけ
3. **moomooの可用性を確認**（`broker.is_available()`）
   - **繋がる場合**: `reconcile_positions` を実行。**不一致なら売買せず警告**（🔻 保有不一致）
   - **繋がらない場合**: 売買を行わず、**評価・記録・報告は通常どおり続ける**。報告に `⚠️ moomoo未接続（売買停止・評価のみ）` を付す
4. 配当処理（既存。JPY側の分岐は削除）
5. Sonnet判断（moomooに繋がっており、かつ照合OKの時のみ実行）
6. 約定（`execute_trades` 経由でmoomooに実発注）
7. 評価・記録（USD建て）→ data.json → git push → Telegram
8. 自律ループ起動（既存のまま）

## 6. report.py / index.html
- 金額表示を全て **`$` 表記**に（円表記の箇所を全て置換）。小数2桁
- `nav_jpy`/`bench_jpy`/`diff_jpy` → `nav_usd`/`bench_usd`/`diff_usd`（data.jsonのキー名も変更）
- 保有一覧の `link` は EWJ も Yahoo Finance 形式（`https://finance.yahoo.com/quote/EWJ`）
- Telegram文面も `$` 表記に。月次勝敗表も同様
- **管理画面に「moomoo連動」であることを1行注記**（例: 「約定・手数料はmoomoo仮想口座の実績」）

## 7. advisor.py
- ポートフォリオ情報の通貨をUSDに。`amount_jpy` → **`amount_usd`**（スキーマとプロンプトの両方）
- 株数は整数単位である旨をシステムプロンプトに明記（端株は買えない）

## 8. tools/migrate_to_usd.py（一度きりの移行）

`--dry-run` を必ず実装し、非dry-runでのみ書き込む。手順:

1. 現在価格（yfinance: VOO/QQQ/GLD/IEF/XLV/1306.T/EWJ）と現在のドル円を取得
2. **現行NAVを円で計算 → 現在のドル円で割って `NAV_usd` を得る**（移行時点の資産価値を保存）
3. **history.csv をドル建てに書き換え**:
   - 各行の日付の USDJPY 終値（yfinanceの `USDJPY=X` 履歴）で `nav_jpy`/`bench_jpy` を割る
   - 列名を `nav_usd,bench_usd,diff_usd,diff_pct,cash_ratio` に。**`diff_pct` は再計算せず既存値をそのまま使う**
     （通貨変換で不変なため。ここで再計算するとズレが出る）
4. **EWJをmoomooで買う**: `NAV_usd × 10%` ÷ EWJ現在値 → 整数株。`broker.place_market_order` で発注し約定を待つ
5. **moomooの実保有を読み、台帳の holdings をそれと完全一致させる**（VOO/QQQ/GLD/IEF/XLV/EWJ。1306.Tは消える）
6. `cash_usd = NAV_usd − (5で得た保有を現在値で評価した合計)`
7. trades.csv に移行の記録を追記（`rule="usd_migration"`。1306.T売却とEWJ購入の2行、および株数調整の注記）
8. portfolio.json を新形式で保存（`cash_usd` 単一・`start_date` と `bench_units` は**変更しない**）

## 検証手順（すべて実施し、実際の出力を報告に含めること）
1. `python3 tools/migrate_to_usd.py --dry-run` → エラーゼロ。移行前後のNAVが（EWJ手数料を除き）一致することを表示
2. 移行を実行 → portfolio.json / history.csv / trades.csv の実際の中身を貼る
3. **移行前後で `diff_pct` の履歴が1行も変化していないこと**を差分で示す（これが最重要の検証。勝敗記録の不変性）
4. `python3 daily_run.py --dry-run --no-loop` が正常終了
5. `broker.reconcile_positions` が「一致」を返すこと
6. OpenDを止めた状態で `daily_run.py --dry-run --no-loop` を実行し、**売買停止・評価継続**になることを確認
   （確認後、OpenDは元通り起動しておくこと。停止手順: `pkill -f "MacOS/OpenD"`、
   起動手順は大将の手動ログインが要るため**停止しないで済む方法があるならそちらを選び、その旨を報告すること**）
7. data.json と管理画面がドル表記になっていること（生成物の該当箇所を貼る）

## 禁止事項
- charter.md / LOOP_PROMPT.md / SPEC*.md / theses.json / candidates/ の編集
- **実口座（TrdEnv.REAL）への発注は絶対禁止。SIMULATEのみ**
- 移行スクリプトを検証なしに複数回実行すること（冪等でないため。必ず--dry-runで確認してから1回だけ）
- Telegram送信（0通）
- vs-sp500外への変更
- `bench_units` と `start_date` の変更（勝敗の基準線であり不変）

## 報告フォーマット
- 変更・新規ファイルと変更概要／検証1〜7の実出力／**移行前後のdiff_pct不変の証明**／仕様との差分欄
