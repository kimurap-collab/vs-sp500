# 個別株解禁の実装指示書（Fable起草 2026-08-13・SPEC.md追補第3弾）

憲章v1.2で個別株が解禁された。憲章「個別株を持つ場合の追加ルール」をコード側で強制する。

## 仕様の出典（大将の発言）
- 「q2は2」＝米国株・日本株・ETF全般・現金比率も自由（2026-08-05。当初から個別株は許可されていた）
- 「別に勝手に『小型株買います』いうてくれていいで。そんな制限俺はした覚えがない。君が自律的にいろいろ考えて自由にやってもらいたいんだけど」（2026-08-13）
- 追加ルールの中身（損切り-20%・リバランス買い増し対象外・1銘柄5%/合計20%・テーゼ必須）はFableが設計（大将から自律的判断を委任されている領域）

## 対象ファイル
- 修正: `portfolio.py`（ガードレール強制・損切り判定）、`advisor.py`（番兵への説明）、`daily_run.py`（損切り執行の呼び出し）
- 修正済み・参照のみ: `config.py`（`is_stock()`・`MAX_STOCK_WEIGHT`・`MAX_STOCK_TOTAL_WEIGHT`・`STOCK_STOP_LOSS` を追加済み）
- 編集禁止: `charter.md`、`LOOP_PROMPT.md`、`SPEC*.md`、`theses.json`、`candidates/`

## 実装内容

### 1. portfolio.py: 個別株ガードレール（execute_trades内）
既存の集中規制チェックの箇所に追加する。`config.is_stock(ticker)` が True の銘柄について:
- 約定後のその銘柄のウェイトが `MAX_STOCK_WEIGHT`（5%）を超えたら拒否（理由文字列に「個別株1銘柄上限」）
- 約定後の**個別株合計**ウェイトが `MAX_STOCK_TOTAL_WEIGHT`（20%）を超えたら拒否（理由に「個別株合計上限」）
- **BUY** かつ `rule` が `rebalance` の場合は拒否（理由に「個別株はリバランスで買い増さない」）。
  `rule` が `initial_build` / `dip_buy` / `defense_switch` / `defense_return` のBUYは許可、SELLは全ルールで許可

### 2. portfolio.py: 損切り判定関数（新規）
```python
def check_stop_losses(state, market, usdjpy_mid) -> list[dict]:
    """個別株のうち平均取得単価比が STOCK_STOP_LOSS を下回るものの全売却注文を返す。"""
```
- `compute_avg_costs()` の平均取得単価と現在値（建て通貨で比較）を突き合わせる
- 該当銘柄について `{"action": "SELL", "ticker": ..., "amount_jpy": <全額>, "rule": "stop_loss"}` を返す
- ETFは対象外

### 3. daily_run.py: 損切りの執行
- **Sonnetの判断より前**に `check_stop_losses` を実行し、返った売却注文を `execute_trades` に通す（ルール由来の強制執行であり、番兵の裁量ではない）
- 損切りが発生したらTelegram報告に `🔻 損切り: <銘柄> (-XX.X%)` の行を追加
- `execute_trades` 側で `rule == "stop_loss"` のSELLは非ターゲット取引の1日上限（NAVの10%）の対象外とする

### 4. advisor.py: 番兵プロンプトの追記
現在のポートフォリオ情報に各銘柄の種別（ETF/個別株）を含め、システムプロンプトに以下を追加:
- 「個別株は憲章の追加ルールが適用される。リバランスでの買い増しは提案するな（下振れしても放置でよい）。上振れ分の利確売りは提案してよい」
- 「損切りはコード側が自動執行するのでお前が提案する必要はない」

## 検証手順（すべて実施し結果を報告）
1. `config.is_stock` の動作（既存ETFは全て False）
2. テスト用に一時的に個別株を1件WHITELISTへ追加した状態で（テスト後は必ず元に戻す）:
   a. 5%超のBUYが拒否される
   b. `rule="rebalance"` のBUYが拒否され、`rule="dip_buy"` のBUYは通る
   c. 個別株合計20%超が拒否される
   d. 取得単価比-25%の保有に対し `check_stop_losses` が全売却注文を返す（-15%では返さない）
3. `python3 daily_run.py --dry-run` が正常（既存フロー非破壊。現在は個別株ゼロなので挙動不変であること）
4. 既存のETFのみのポートフォリオで、上記変更による挙動変化がないこと（ガードレールの誤発火なし）

## 禁止事項
- charter.md / LOOP_PROMPT.md / theses.json / SPEC*.md / candidates/ の編集
- WHITELISTへの個別株の恒久追加（銘柄選定はFableがテーゼと共に行う。テストで足したら必ず戻す）
- Telegram送信（0通）
- vs-sp500外への変更

## 報告フォーマット
- 変更ファイルと変更概要／検証1〜4の実出力／仕様との差分欄（差分ゼロなら「差分なし」）
