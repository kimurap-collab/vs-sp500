# 自律ループ実装指示書（Fable起草 2026-08-07・SPEC.md追補第2弾）

vs-sp500に「毎朝の自律ループ」「緊急停止・アラート」「候補憲章の検証・採用機構」を追加する。

## 仕様の出典（大将の発言・承認「いけ」2026-08-07取得済み）
- 「俺は君に任せてるし、君に自律的に動いてほしいし、進化してほしいのだ。勝利の為に」
- 「loopという概念があるそうだが、勝手に回ってほしいのだよこのシステムは」
- 「codexの提言も含め、君が考えるべきタイミングで自律的に考えてくれたらいい」
- 統治設計（観察は毎日・採用は月次・冷却期間・採点基準は変更不可）は外部レビュー（Codex）の指摘を反映してFableが決定、大将が「いけ」で承認

## 対象
- 修正: `daily_run.py`（異常停止＋アラート）、`report.py`（アラート文言、必要なら）
- 新規: `tools/validate_and_adopt.py`、`loop_run.sh`、`ledger/adoptions.json`（初期値 `{"adoptions": []}`）、`logs/loop/`（ディレクトリ）
- 新規（vs-sp500外の唯一の例外）: `~/Library/LaunchAgents/com.taisho.vs-sp500-loop.plist`
- 参照のみ（編集禁止）: `LOOP_PROMPT.md`、`candidates/candidate_20260807.md`、`charter.md`、`SPEC*.md`、`theses.json`

## 1. daily_run.py: 異常停止＋アラート

### 異常停止（該当したら売買せずホールド固定・台帳の評価記録は行う・Telegramに⚠️通知）
- 市場データの日付が3営業日以上前のまま（データ鮮度異常）
- VOOまたはUSDJPYの前日比が±12%超（データ異常の疑い）
- NAV計算結果が前回比±20%超（整合性異常）
- 停止時のTelegram: 先頭に「🛑 異常停止: <理由>。売買を止めて観察のみ実施」

### アラート（売買は通常通り・Telegram報告の先頭に警告行を追加）
- VOO前日比 -3.5%以下 →「⚠️ 急落検知」
- VOO 5営業日で -7%以下 →「⚠️ 続落検知」
- モード切替（通常⇔防衛）が発生 →「⚠️ モード切替」
- 対S&P差が5営業日で2ポイント以上悪化 →「⚠️ 乖離拡大」
- 文言例: 「⚠️ 緊急レビュー推奨: 急落検知（VOO -4.1%）」

## 2. tools/validate_and_adopt.py（決定論の検証器・採用器）

使い方: `python3 tools/validate_and_adopt.py candidates/candidate_YYYYMMDD.md`

### 検証（1つでも不合格なら理由を出力してexit 1・何も変更しない）
1. 候補ファイルの「作成日:」から**7日以上**経過しとる（冷却期間）
2. `ledger/adoptions.json` を見て**今月まだ採用していない**（暦月で月1回まで）
3. 候補の配分表がパース可能（charter.mdと同じ表形式・portfolio.pyのパーサ流用）
4. 銘柄がホワイトリストのみ／通常・防衛それぞれ合計100±0.5%
5. 可動域: 株式合計（VOO+QQQ+XLV+1306.T）通常≤90%・防衛≤50%／1銘柄≤30%（VOOのみ≤65%）／現金≥2%（両モード）
6. 変更されるのは「## ターゲット配分」セクションの表のみであることを保証する実装にする（他セクションへの影響ゼロ）

### 採用処理（検証合格時のみ）
- charter.mdのターゲット配分表を候補の表で置換
- charter.md改訂履歴に1行追記（vX.Y、日付、候補ファイル名、一言）※バージョンは現行+0.1
- 候補ファイルの「状態:」を「採用済み(YYYY-MM-DD)」に書換
- adoptions.jsonに記録（日付・候補ファイル名）
- git add -A && commit（メッセージに候補名）&& push
- 出力: 採用内容の要約（呼び出し元のループがTelegram報告に使う）

## 3. loop_run.sh（毎朝7:15の自律ループ起動）
- `claude -p "$(cat LOOP_PROMPT.md)"` をvs-sp500ディレクトリで実行
- 権限: `--allowedTools "Read" "Glob" "Grep" "Bash" "Write" "Edit"` を付与（dangerously-skip-permissionsは**使用禁止**）
  - ヘッドレスでの権限指定方法はclaude CLIの現行仕様を`claude --help`で確認して合わせること
- 標準出力・エラーを `logs/loop/run_YYYYMMDD.log` に保存
- タイムアウト15分（`timeout` コマンド等で強制終了）
- 失敗時（exit非0）: report.pyのsend_telegram_messageで「⚠️ 自律ループ実行失敗」を送る（bashからpython3 -c経由）

## 4. launchd: com.taisho.vs-sp500-loop.plist
- 毎朝7:15、WorkingDirectory=vs-sp500、loop_run.shを実行
- StandardOut/ErrorPath: logs/loop/launchd.log / launchd.err
- 設置後 `launchctl load` し、`launchctl list | grep vs-sp500-loop` で登録確認

## 検証手順（全て実施し結果を報告）
1. validate_and_adopt.py: （a）冷却期間不足の候補（candidate_20260807.md、作成当日）で**却下される**こと（b）テスト用に作成日を8日前にした一時候補ファイル（tmp_test_candidate.md等、検証後削除）で**合格し採用処理が走る**こと。※(b)は`--dry-run`フラグを実装してcharter.md非破壊で確認する
2. 可動域違反（株式95%等）のテスト候補が却下されること
3. daily_run.py --dry-run が正常（既存フロー非破壊）
4. アラート判定ロジックの単体確認（閾値をまたぐ疑似データで発火・非発火を確認）
5. loop_run.sh を手動実行1回 → claudeヘッドレスが起動し、logs/loop/に日誌が生成されることを確認（この時のTelegram送信は日誌上の判断次第だが、月曜・1日でない限り送信なしのはず）
6. launchd登録確認
7. push後、`git ls-files`で.env等の秘密が含まれないこと

## 禁止事項
- LOOP_PROMPT.md・candidates/・charter.md・theses.json・SPEC*.mdの編集
- dangerously-skip-permissionsの使用
- Telegramへの送信は検証手順5で自然発生する分以外0通
- vs-sp500外への変更（plist 1個のみ例外）

## 報告フォーマット
- 変更・新規ファイル一覧と行数／検証1〜7の実出力／仕様との差分欄（ゼロなら「差分なし」）
