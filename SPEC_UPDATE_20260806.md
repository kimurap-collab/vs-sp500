# 管理画面強化 指示書（Fable起草 2026-08-06・SPEC.md追補）

## 仕様の出典（大将の発言・承認「いけ」取得済み）
- 「保有銘柄にリンクを貼ってほしい。あと１株あたりの購入単価も掲載してほしい。現在値も取得できるならしてほしい。１日一回の取得でいいよ」
- 「銘柄を購入した理由も端的に書いてほしい。そして株価いくらを目指してるのかなども」
- リンク先＝Yahoo系、テーゼ内容＝Fable一任（theses.json 作成済み・編集禁止）

## 対象ファイル
- 修正: `report.py`（data.json生成の拡張）、`index.html`（保有一覧の表示拡張）、`daily_run.py`（--report-onlyフラグ追加）
- 参照のみ: `theses.json`（Fable管理。**編集禁止**）
- それ以外のファイルは変更禁止（charter.md / SPEC.md / portfolio.py / advisor.py / market.py / config.py）
  - 例外: 平均取得単価の計算関数はどこに置くのが自然かで判断してよい（portfolio.pyに追加する場合はその旨を差分欄に記載）

## 機能仕様

### 1. data.json の holdings 拡張
各保有銘柄のオブジェクトに以下を追加:
```json
{
  "ticker": "VOO", "name": "S&P500 ETF", "shares": 31.33,
  "value_jpy": 3500000, "weight_pct": 35.0,
  "price": 707.60,          // 現在値（銘柄の建て通貨。毎朝取得の終値）
  "avg_cost": 707.60,       // 平均取得単価（建て通貨）
  "currency": "USD",        // "USD" | "JPY"
  "link": "https://finance.yahoo.com/quote/VOO",
  "reason": "S&P500コア。上昇トレンド順行の主力",
  "target": "$780（+10%）"
}
```
- **平均取得単価**: trades.csv から計算。BUYは加重平均で更新（単価は約定price。手数料は含めない）、SELLは株数を減らすだけで平均単価は変えない（総平均法）
- **リンク**: USD銘柄 → `https://finance.yahoo.com/quote/<ticker>`、`1306.T` → `https://finance.yahoo.co.jp/quote/1306.T`
- **reason / target**: theses.json から取得。無い銘柄は空文字

### 2. index.html の保有一覧
- 各銘柄の1行目: 銘柄名（`link`へのリンク。`target="_blank" rel="noopener"`）・株数・**取得単価・現在値**・評価額・ウェイト
- 取得単価・現在値は通貨記号付き（USD=`$707.60`、JPY=`¥423.7`。小数はUSD2桁・JPY1桁）
- 2行目（同セル内の小さめグレー文字などモバイルで崩れない形）: 「理由: <reason>　目標: <target>」
- 保有一覧の下に注記を小さく表示: 「※目標は12ヶ月の目安。売買は投資憲章のルール（配分・トレンド）で判断」
- 現在値の列ヘッダは「現在値(前日終値)」とする

### 3. daily_run.py に `--report-only` フラグ追加
- 動作: 台帳読み込み→市場データ取得→NAV計算→data.json再生成→git commit & push のみ
- **行わないこと**: Sonnet判断・約定・history.csv追記・Telegram送信・portfolio.json保存
- 用途: 表示変更を即座に管理画面へ反映するため

## 検証手順（すべて実施し結果を報告に含める）
1. 平均取得単価の計算が trades.csv と一致することを確認（例: VOOのavg_costは707.5999…になるはず。全6銘柄分を出力）
2. `python3 daily_run.py --dry-run` がエラーなく通る（既存フローの非破壊確認）
3. `python3 daily_run.py --report-only` を実行 → data.json に新フィールドが入っていることをローカルで確認 → push される
4. GitHub PagesのActionsワークフロー完了を待ち（`gh run watch`）、`curl https://kimurap-collab.github.io/vs-sp500/data.json` で新フィールド（price/avg_cost/link/reason/target）を確認
5. Telegramが**送信されていない**ことを確認（--report-onlyの動作確認）

## 禁止事項
- theses.json / charter.md / SPEC.md の編集
- Telegram送信（今回の作業では0通）
- vs-sp500外の変更

## 報告フォーマット
- 変更ファイルと変更概要・検証手順1〜5の実際の出力・仕様との差分欄（差分ゼロなら「差分なし」）
