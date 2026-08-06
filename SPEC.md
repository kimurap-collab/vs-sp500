# vs-sp500 実装指示書 v1.0（Fable起草 2026-08-06）

仮想1000万円を運用し、S&P500（VOO円換算・配当込み）に円建てで勝てるかを検証するシステム。
毎朝7時にlaunchdで自動実行し、Sonnetが投資憲章（charter.md）に基づき売買判断、
台帳更新→GitHub Pagesの管理画面更新→Telegram報告を行う。

## 仕様の出典（大将の発言）
- 仮想1000万円・両替自由 →「予算1000万円（日本円）で。もちろん外貨への両替は自由だ」
- 毎朝の自動ジョブ →「q1は1」（毎朝の自動ジョブを1本だけ復活させる案を採択）
- 投資対象は米日株・ETF・金・債券・現金自由 →「q2は2」
- 月次勝敗判定 →「q3は一ヶ月ごとに勝敗」
- 判断方式（憲章＋Sonnet番兵・コスト設計）はFableに一任 →「君が勝てると思う方法を選択せよ」
- 管理画面＋S&P比較表示＋Telegram報告＋リンク →「管理画面を作ってもらって、そこに売買の記録と利益を書いてくれたら。spとの比較（いくら勝ってる・負けてる）を表記。telegramで報告もして。そこに書かれたリンクにアクセスして管理画面を見にいくから」
- GitHub Pages公開 →「q6) 1でいいよ」
- 実装一式の承認 →「ike」「いけ」（2026-08-06、提案文【対象】記載範囲）

## ディレクトリ構成（すべて新規作成）
```
~/Documents/my-first-project/ai-hedge-fund/vs-sp500/
├── charter.md          # 投資憲章（既存・Fable管理。編集禁止）
├── SPEC.md             # 本ファイル（編集禁止）
├── config.py           # 定数（パス・手数料率・ティッカー等）と.env読み込み
├── market.py           # yfinanceでの市場データ取得
├── portfolio.py        # 台帳I/O・評価額計算・ガードレール検証・約定処理
├── advisor.py          # Sonnet呼び出し（売買判断）
├── report.py           # data.json生成・Telegram送信
├── daily_run.py        # エントリポイント（毎朝実行）
├── index.html          # 管理画面（静的・data.jsonを読む）
├── ledger/
│   ├── portfolio.json  # 現在の状態（現金・保有・ベンチマーク・モード・開始日）
│   ├── trades.csv      # 取引履歴
│   └── history.csv     # 日次評価額の推移
├── data.json           # 管理画面用データ（daily_runが再生成）
├── logs/               # 実行ログ（gitignore）
├── .env                # APIキー類（gitignore・下記参照）
└── .gitignore          # .env, logs/, __pycache__
```

## .env（実装時に作成。値は以下からコピーする）
- `CLAUDE_API_KEY` ← `~/Documents/my-first-project/finance-team/.env` の同名キーの値
- `TELEGRAM_BOT_TOKEN` ← `~/Documents/my-first-project/robo-taisho/.env` の `ROBO_TAISHO_BOT_TOKEN` の値
- `TELEGRAM_CHAT_ID` ← 同ファイルの `TAISHO_TELEGRAM_ID` の値
- 読み込みは `config.py` で（python-dotenvが無ければ自前で数行のパーサでよい）

## 全体フロー（daily_run.py）
1. 台帳と charter.md を読み込む
2. market.py で市場データ取得（保有銘柄＋ホワイトリスト全銘柄＋VOO＋USDJPY=X）
   - 各銘柄: 直近終値・終値日付・200日移動平均(VOO)・RSI14(VOO)・52週高値(VOO)・当日配当
3. **初回判定**: `portfolio.json` の `start_date` が null かつ charter.md のターゲット配分が記入済みなら:
   - `start_date` を今日に設定
   - ベンチマーク初期化: `bench_units = 10_000_000 / (VOO終値 × USDJPY仲値)`（手数料なし）
4. **配当処理**: 保有銘柄の当日配当を現金に加算（USD銘柄→USD現金、JPY銘柄→JPY現金）。
   ベンチマーク側はVOO配当を `bench_units += units × 配当 / VOO終値` で再投資
5. **売買判断**:
   - スキップ条件: ターゲット未記入 / start_dateがnull / VOOの終値日付が前回処理日から進んでいない（休場）
   - スキップでなければ advisor.py でSonnetに判断させる
6. **約定処理**: Sonnetの提案を portfolio.py がガードレール検証。違反注文は拒否しログに記録。
   通過した注文を直近終値で約定（手数料・為替スプレッドは charter.md のコストモデル通り）。
   USD建て購入時にUSD現金が足りなければ不足分を自動両替（スプレッド適用）
7. **評価・記録**: 円建て評価額とベンチマーク評価額を計算し history.csv に追記（同日重複は上書き）
8. **data.json 再生成 → git commit & push**（コミットメッセージ: `update YYYY-MM-DD`）
9. **Telegram送信**（下記フォーマット）
10. 例外時: logs/ に記録し、可能なら「⚠️ vs-sp500 実行エラー: <一行要約>」をTelegram送信

## 評価額の計算式
- 自分側 NAV(JPY) = JPY現金 + USD現金×USDJPY仲値 + Σ(USD銘柄株数×終値×USDJPY仲値) + Σ(JPY銘柄株数×終値)
- ベンチマーク NAV(JPY) = bench_units × VOO終値 × USDJPY仲値
- 差額 = 自分NAV − ベンチマークNAV（プラス=勝ち）

## advisor.py（Sonnet呼び出し）
- Python SDK `anthropic` を使用。`Anthropic(api_key=CLAUDE_API_KEY)`
- モデルID: **`claude-sonnet-5`**（正確にこの文字列）
- `output_config={"format": {"type": "json_schema", "schema": ...}}` で構造化出力を強制。
  `temperature`等のサンプリングパラメータは**渡さない**（Sonnet 5では400エラーになる）
- `output_config` に `"effort": "low"` も併せて指定、`max_tokens=2000`
- 入力: charter.md全文＋現在のポートフォリオ＋市場スナップショット（各銘柄の終値・ウェイト、VOOの200日線/RSI/52週高値、現在モード、200日線との上下連続日数）
- 出力スキーマ:
```json
{
  "type": "object",
  "properties": {
    "trades": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "action": {"type": "string", "enum": ["BUY", "SELL"]},
          "ticker": {"type": "string"},
          "amount_jpy": {"type": "integer"},
          "rule": {"type": "string", "enum": ["rebalance", "defense_switch", "defense_return", "dip_buy", "initial_build"]}
        },
        "required": ["action", "ticker", "amount_jpy", "rule"],
        "additionalProperties": false
      }
    },
    "mode": {"type": "string", "enum": ["normal", "defense"]},
    "reason": {"type": "string"}
  },
  "required": ["trades", "mode", "reason"],
  "additionalProperties": false
}
```
- システムプロンプト要旨: 「あなたは投資憲章の番兵。憲章の発動条件に該当する場合のみ、該当ルールに基づく取引を提案する。該当しなければtradesは空配列。憲章にない取引の提案は禁止。迷ったらホールド」
- API呼び出し失敗時は1回リトライ、それでも失敗なら当日はホールド扱い（エラーはログ＋Telegram）

## 200日線カウンタ
- `portfolio.json` に `above_200dma_streak` / `below_200dma_streak` を保持し、毎営業日更新。
  発動条件2の「3営業日連続」はこのカウンタで判定材料をSonnetに渡す

## portfolio.json スキーマ（例）
```json
{
  "start_date": null,
  "mode": "normal",
  "cash_jpy": 10000000,
  "cash_usd": 0.0,
  "holdings": {"VOO": 12.0},
  "bench_units": 0.0,
  "last_processed_voo_date": null,
  "below_200dma_streak": 0,
  "above_200dma_streak": 0
}
```
- 保有株数は小数可（仮想運用なので端株OK。ベンチマークと条件を揃えるため）

## trades.csv 列
`date,action,ticker,shares,price,currency,fx_rate,amount_jpy,fee_jpy,rule,note`

## history.csv 列
`date,nav_jpy,bench_jpy,diff_jpy,diff_pct,cash_ratio`

## data.json（管理画面用）
```json
{
  "updated_at": "2026-08-06 07:00 JST",
  "start_date": "2026-08-06",
  "nav_jpy": 10000000,
  "bench_jpy": 10000000,
  "diff_jpy": 0,
  "diff_pct": 0.0,
  "mode": "normal",
  "holdings": [{"ticker": "VOO", "name": "S&P500 ETF", "shares": 12.0, "value_jpy": 0, "weight_pct": 0.0}],
  "cash_jpy": 0, "cash_usd": 0.0,
  "history": [{"date": "2026-08-06", "nav": 10000000, "bench": 10000000}],
  "trades": [],
  "monthly": [{"month": "2026-08", "nav": 0, "bench": 0, "result": "win"}]
}
```
- trades は直近50件まで

## index.html（管理画面）
- 静的HTML 1枚。`fetch("data.json?t=" + Date.now())` で読み込み（キャッシュ回避）
- 日本語UI・スマホ表示前提（レスポンシブ）・ダークトーンで見やすく
- 構成（上から）:
  1. タイトル「Fable vs S&P500」と最終更新日時
  2. **対S&P500の勝敗額を最大サイズで表示**（例:「+123,456円」勝ち=緑/負け=赤、下に±%）
  3. 自分の評価額とベンチマーク評価額の並記
  4. 資産推移グラフ（自分vs S&P500の2本線。Chart.js CDN使用可）
  5. 月次勝敗表
  6. 保有一覧（銘柄・株数・評価額・ウェイト）
  7. 取引記録（新しい順）
- 外部依存はChart.js CDNのみ許可

## Telegram報告フォーマット
```
📊 vs S&P500 (8/6)
評価額: ¥10,234,567
対S&P: +34,567円 (+0.34%) 🔴/🟢
売買: なし（ホールド）  ← 売買があれば1行ずつ列挙
📈 https://kimurap-collab.github.io/vs-sp500/
```
- 毎月1日の実行時は先頭に前月の勝敗判定を追加（「7月戦績: 勝ち +XX,XXX円」）
- 送信は `https://api.telegram.org/bot<TOKEN>/sendMessage` にrequestsでPOST

## GitHub / Pages
- `vs-sp500/` ディレクトリ自体をgitリポジトリ化（親のai-hedge-fundとは独立）
- `gh repo create kimurap-collab/vs-sp500 --public --source=. --push`
- Pages有効化: `gh api repos/kimurap-collab/vs-sp500/pages -X POST -f "source[branch]=main" -f "source[path]=/"`
- 公開URL: https://kimurap-collab.github.io/vs-sp500/
- .env と logs/ は必ず gitignore（**公開リポジトリなので秘密情報の混入は絶対禁止**。push前に `git ls-files` で確認すること）

## launchd（毎朝7時）
- `~/Library/LaunchAgents/com.taisho.vs-sp500.plist` を新規作成して `launchctl load`
- ProgramArguments: `which python3` の絶対パス + `daily_run.py` のフルパス
- WorkingDirectory: vs-sp500ディレクトリ
- StartCalendarInterval: Hour 7, Minute 0
- StandardOutPath/StandardErrorPath: `logs/launchd.log` / `logs/launchd.err`

## コーディング規約
- 型ヒント付き・各ファイル400行以内・エラーは握りつぶさずログへ
- 秘密情報のハードコード禁止（.envのみ）
- 依存: yfinance（1.2.0導入済）・anthropic・requests のみ。無いものは `python3 -m pip install --user` で導入可

## 検証手順（実装後に必ず実施し、結果を報告に含める）
1. `python3 daily_run.py --dry-run` : 約定なし・push なし・Telegram なしで全フロー通しレポート文字列をstdoutに出す。エラーゼロを確認
2. ターゲット未記入状態の実運転想定: dry-runで「ホールド（ターゲット未記入）」となること
3. Telegramテスト送信を**1通のみ**: 「🧪 vs-sp500 セットアップ完了テスト」
4. GitHub Pages: リポジトリ作成→push→Pages有効化→`curl -s -o /dev/null -w "%{http_code}" <URL>` が200（反映に数分かかる場合はリトライ）
5. launchd: plist設置→load→`launchctl list | grep vs-sp500` で登録確認
6. ガードレール単体確認: ホワイトリスト外注文・現金マイナス注文が拒否されることをテストコードか対話実行で確認

## 禁止事項
- charter.md / SPEC.md の編集
- robo-taisho・finance-team等、vs-sp500外の既存コードの変更（.envの値の読み取りのみ可）
- Telegramへのテスト送信2通以上
- vs-sp500外へのファイル作成（例外: LaunchAgentsのplist 1個のみ）

## 報告フォーマット（作業完了時）
- 作成ファイル一覧と各行数
- 検証手順1〜6の実行結果（実際の出力を含む）
- Pages URL と HTTPステータス
- 仕様との差分欄: 「SPEC通りの点／SPECに無かったが実施・変更した点」（差分ゼロなら「差分なし」）
