#!/usr/bin/env python3
"""vs-sp500: 毎朝実行のエントリポイント。"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import subprocess
import sys

import yfinance as yf

import account_state
import broker
import config
import jp_market
import jp_rsi_daily
import jp_rsi_ledger
import market
import portfolio
import report
import rsi_daily
import rsi_ledger
from advisor import get_trade_decision

JST = dt.timezone(dt.timedelta(hours=9))

# --- 異常停止・アラートの閾値（SPEC_LOOP.md準拠） ---
HALT_STALE_BUSINESS_DAYS = 3
HALT_PRICE_MOVE_THRESHOLD = 0.12
HALT_NAV_CONSISTENCY_THRESHOLD = 0.20
# 保有銘柄の「実在しない変動幅」。サーキットブレーカー（最大-20%で取引停止）より上に置き、
# 本物の暴落日には掛からず、配信データの破損だけを捕まえる水準にする。
HALT_IMPLAUSIBLE_MOVE_THRESHOLD = 0.30
ALERT_VOO_DROP_1D = -0.035
ALERT_VOO_DROP_5D = -0.07
ALERT_DIFF_PCT_DETERIORATION_5D = 2.0


def setup_logging() -> logging.Logger:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("vs-sp500")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        fh = logging.FileHandler(config.LOG_DIR / "daily_run.log", encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(fh)
    return logger


def git_commit_and_push(now_jst: dt.datetime, logger: logging.Logger) -> None:
    date_str = now_jst.strftime("%Y-%m-%d")
    try:
        add_paths = ["ledger/", "data.json"]
        if config.RSI_UNIVERSE_PATH.exists():
            add_paths.append("universe.json")
        if config.RSI_JP_LOTSIZE_CACHE_PATH.exists():
            add_paths.append("jp_lotsize.json")
        subprocess.run(["git", "add", *add_paths], cwd=config.BASE_DIR, check=True, capture_output=True)
        result = subprocess.run(
            ["git", "commit", "-m", f"update {date_str}"],
            cwd=config.BASE_DIR, capture_output=True, text=True,
        )
        if result.returncode != 0 and "nothing to commit" not in result.stdout:
            logger.warning("git commit警告: %s", result.stdout + result.stderr)
        subprocess.run(["git", "push"], cwd=config.BASE_DIR, check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        logger.error("git commit/push失敗: %s", e)


def _business_days_between(start: dt.date, end: dt.date) -> int:
    """startからendまでの営業日数（土日を除く単純カウント。祝日は考慮しない）。"""
    if end <= start:
        return 0
    count = 0
    d = start
    while d < end:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            count += 1
    return count


def _recent_closes(ticker: str, bars: int) -> list[float]:
    """直近bars本の終値（古い→新しい順）。データ不足時は取れた分だけ返す。

    market.pyは直近終値1件のみを公開しているため、複数本の終値が要る
    異常停止・アラート判定用にここで直接yfinanceを呼ぶ（market.py自体は変更しない）。
    """
    try:
        hist = yf.Ticker(ticker).history(period="2mo", auto_adjust=False)
    except Exception:
        return []
    if hist.empty:
        return []
    return [float(c) for c in hist["Close"].tail(bars)]


def check_halt(state: dict, snapshots: dict, voo_snap, today: dt.date) -> str | None:
    """異常停止条件に該当するか判定する。該当すれば理由文字列、なければNone。"""
    market_date = dt.date.fromisoformat(voo_snap.date)
    stale_days = _business_days_between(market_date, today)
    if stale_days >= HALT_STALE_BUSINESS_DAYS:
        return f"データ鮮度異常（市場データが{stale_days}営業日前のまま）"

    voo_closes = _recent_closes(config.BENCHMARK_TICKER, 2)
    if len(voo_closes) == 2 and voo_closes[-2] != 0:
        voo_chg = (voo_closes[-1] - voo_closes[-2]) / voo_closes[-2]
        if abs(voo_chg) > HALT_PRICE_MOVE_THRESHOLD:
            return f"VOO前日比異常（{voo_chg:+.1%}）"

    fx_closes = _recent_closes(config.FX_TICKER, 2)
    if len(fx_closes) == 2 and fx_closes[-2] != 0:
        fx_chg = (fx_closes[-1] - fx_closes[-2]) / fx_closes[-2]
        if abs(fx_chg) > HALT_PRICE_MOVE_THRESHOLD:
            return f"USDJPY前日比異常（{fx_chg:+.1%}）"

    # 各銘柄の配信データ破損検知。VOOとUSDJPYしか見ていなかった穴を塞ぐ。
    # 2026-03-30・03-31、yfinanceが1306.Tの終値を実勢の約1/10（37円台 vs 前後380円台）で
    # 配信していた（分割記録なし＝純粋なデータ破損）。10%枠の銘柄が1/10になってもNAV変化は
    # 約-9%で整合性しきい値20%に掛からず、リバランス（±5pt乖離）だけが発動して幻の価格で
    # 評価額の約9%を買い付ける。翌日に価格が戻ると保有株数が10倍のまま評価され、
    # 実在しない利益が台帳に固定される——単日で最も重い破損経路であり、勝敗記録そのものを汚す。
    for ticker in sorted(snapshots):
        closes = _recent_closes(ticker, 2)
        if len(closes) == 2 and closes[-2] != 0:
            chg = (closes[-1] - closes[-2]) / closes[-2]
            if abs(chg) > HALT_IMPLAUSIBLE_MOVE_THRESHOLD:
                return f"{ticker}の価格データ異常（前日比{chg:+.1%}・実在しない変動幅）"

    history_rows = portfolio.read_history_rows()
    if history_rows:
        prev_nav = float(history_rows[-1]["nav_usd"])
        if prev_nav != 0:
            current_nav = portfolio.compute_nav_usd(state, snapshots)
            nav_chg = (current_nav - prev_nav) / prev_nav
            if abs(nav_chg) > HALT_NAV_CONSISTENCY_THRESHOLD:
                return f"NAV整合性異常（前回比{nav_chg:+.1%}）"

    return None


def check_alerts(
    prev_mode: str,
    new_mode: str,
    history_rows_before: list[dict],
    diff_pct_today: float,
) -> list[str]:
    """アラート条件（売買は通常通り・報告に警告行を追加するだけ）を判定する。"""
    alerts: list[str] = []

    voo_closes = _recent_closes(config.BENCHMARK_TICKER, 6)
    if len(voo_closes) >= 2 and voo_closes[-2] != 0:
        chg1 = (voo_closes[-1] - voo_closes[-2]) / voo_closes[-2]
        if chg1 <= ALERT_VOO_DROP_1D:
            alerts.append(f"⚠️ 急落検知（VOO {chg1:+.1%}）")
    if len(voo_closes) >= 6 and voo_closes[0] != 0:
        chg5 = (voo_closes[-1] - voo_closes[0]) / voo_closes[0]
        if chg5 <= ALERT_VOO_DROP_5D:
            alerts.append(f"⚠️ 続落検知（VOO 5営業日 {chg5:+.1%}）")

    if prev_mode != new_mode:
        alerts.append(f"⚠️ モード切替（{prev_mode}→{new_mode}）")

    if len(history_rows_before) >= 5:
        old_diff_pct = float(history_rows_before[-5]["diff_pct"])
        deterioration = old_diff_pct - diff_pct_today
        if deterioration >= ALERT_DIFF_PCT_DETERIORATION_5D:
            alerts.append(f"⚠️ 乖離拡大（5営業日で{deterioration:.1f}pt悪化）")

    return alerts


def _run_jp_rsi_safe(
    jp_state: dict, dry_run: bool, logger: logging.Logger,
) -> tuple[dict, list[dict], list[str], float, dict]:
    """日本株RSI枠の実行を例外から守る（2026-08-24追加。3本目の戦略枠。

    Fableが自分で決めた安全策（大将の指示にはない）: この枠だけmoomoo発注が無い実験的な
    追加のため、ここで例外が漏れるとdaily_run.py全体のtry/exceptに巻き込まれ、本体・
    米国RSI枠のdata.json更新やTelegram送信まで止まってしまう。それを避けるため、
    ここだけ個別にtry/exceptで包み、失敗時は前回のjp_stateをそのまま返してログにだけ残す。
    """
    try:
        trading_date = jp_market.get_jp_trading_date()
    except Exception as e:  # noqa: BLE001 - yfinance内部の例外型は不定
        logger.exception("JP RSI枠: 取引日の取得に失敗した")
        try:
            nav_jpy, market_snap = jp_rsi_daily.compute_snapshot_only_jp(jp_state)
        except Exception:
            logger.exception("JP RSI枠: スナップショット計算にも失敗した")
            nav_jpy, market_snap = 0.0, {}
        return jp_state, [], [f"[JP] 取引日取得エラーのため売買スキップ: {e}"], nav_jpy, market_snap

    try:
        return jp_rsi_daily.run_jp(jp_state, trading_date, dry_run)
    except Exception as e:  # noqa: BLE001 - JP枠の障害で本体・米国RSI枠の報告まで止めないため
        logger.exception("JP RSI枠の実行中に例外が発生した")
        try:
            nav_jpy, market_snap = jp_rsi_daily.compute_snapshot_only_jp(jp_state)
        except Exception:
            logger.exception("JP RSI枠: スナップショット計算にも失敗した")
            nav_jpy, market_snap = 0.0, {}
        return jp_state, [], [f"[JP] 実行エラーのため売買スキップ: {e}"], nav_jpy, market_snap


def run(dry_run: bool = False, report_only: bool = False) -> str:
    logger = setup_logging()
    now_jst = dt.datetime.now(JST)
    report_lines: list[str] = []

    def log_and_report(msg: str) -> None:
        logger.info(msg)
        report_lines.append(msg)

    try:
        state = portfolio.load_portfolio()
        rsi_state = rsi_ledger.load_portfolio()
        jp_state = jp_rsi_ledger.load_portfolio()
        log_and_report(
            f"[1] 台帳読み込み完了。mode={state['mode']} start_date={state['start_date']} "
            f"RSI枠start_date={rsi_state['start_date']} JP RSI枠start_date={jp_state['start_date']}"
        )

        tickers_to_fetch = sorted(set(list(config.WHITELIST.keys()) + list(state["holdings"].keys())))
        snapshots = market.get_snapshots(tickers_to_fetch)
        usdjpy_snap = market.get_usdjpy_mid()
        usdjpy_mid = usdjpy_snap.close
        voo_snap = snapshots[config.BENCHMARK_TICKER]
        log_and_report(f"[2] 市場データ取得完了。VOO終値={voo_snap.close}({voo_snap.date}) USDJPY={usdjpy_mid}")

        if report_only:
            # --report-only: 台帳読み込み→市場データ取得→NAV計算→data.json再生成→git push のみ。
            # Sonnet判断・約定・history.csv追記・Telegram送信・portfolio.json保存は行わない。
            nav_usd = portfolio.compute_nav_usd(state, snapshots)
            bench_usd = portfolio.compute_bench_nav_usd(state, voo_snap.close)
            log_and_report(f"[3] NAV計算: NAV=${nav_usd:,.2f} ベンチマーク=${bench_usd:,.2f}")
            rsi_nav, rsi_bench, rsi_market = rsi_daily.compute_snapshot_only(rsi_state, voo_snap)
            rsi_block = report.build_rsi_block(rsi_state, rsi_market, rsi_nav, rsi_bench, [])
            try:
                rsi_jp_nav, rsi_jp_market = jp_rsi_daily.compute_snapshot_only_jp(jp_state)
            except Exception:
                logger.exception("JP RSI枠: --report-onlyのスナップショット計算に失敗した")
                rsi_jp_nav, rsi_jp_market = 0.0, {}
            rsi_jp_block = report.build_rsi_jp_block(jp_state, rsi_jp_market, rsi_jp_nav, [])
            data = report.build_data_json(
                state, snapshots, nav_usd, bench_usd, [], now_jst, rsi_block=rsi_block, rsi_jp_block=rsi_jp_block,
            )
            report.save_data_json(data)
            log_and_report("[4] data.json更新完了（--report-onlyのためhistory.csv追記・portfolio.json保存はスキップ）")
            git_commit_and_push(now_jst, logger)
            log_and_report("[5] git commit & push完了（--report-onlyのためTelegram送信はスキップ）")
            log_and_report("[6] 正常終了（--report-only）")
            return "\n".join(report_lines)

        # 異常停止判定（市場データ取得直後、初回構築・配当処理・売買判断の前に行う）
        halt_reason = check_halt(state, snapshots, voo_snap, now_jst.date())
        if halt_reason:
            log_and_report(f"[2c] 異常停止判定: 該当（{halt_reason}）→ 売買せずホールド固定")
            nav_usd = portfolio.compute_nav_usd(state, snapshots)
            bench_usd = portfolio.compute_bench_nav_usd(state, voo_snap.close)
            diff_usd = nav_usd - bench_usd
            cash_ratio = portfolio.compute_cash_ratio(state, nav_usd) if nav_usd else 0.0
            log_and_report(
                f"[7] 評価額計算（異常停止時）: NAV=${nav_usd:,.2f} ベンチマーク=${bench_usd:,.2f} 差額=${diff_usd:,.2f}"
            )
            state["last_processed_voo_date"] = voo_snap.date
            if not dry_run:
                portfolio.append_history_row({
                    "date": voo_snap.date,
                    "nav_usd": round(nav_usd, 2),
                    "bench_usd": round(bench_usd, 2),
                    "diff_usd": round(diff_usd, 2),
                    "diff_pct": round(diff_usd / bench_usd * 100, 4) if bench_usd else 0.0,
                    "cash_ratio": round(cash_ratio, 4),
                })
                portfolio.save_portfolio(state)
            # RSI枠も異常停止時は売買せず評価のみ行う（本体と同じ縮退方針）
            rsi_nav, rsi_bench, rsi_market = rsi_daily.compute_snapshot_only(rsi_state, voo_snap)
            if not dry_run:
                rsi_diff = rsi_nav - rsi_bench
                rsi_ledger.append_history_row({
                    "date": voo_snap.date,
                    "nav_usd": round(rsi_nav, 2),
                    "bench_usd": round(rsi_bench, 2),
                    "diff_usd": round(rsi_diff, 2),
                    "diff_pct": round(rsi_diff / rsi_bench * 100, 4) if rsi_bench else 0.0,
                    "cash_ratio": round(rsi_ledger.compute_cash_ratio(rsi_state, rsi_nav), 4) if rsi_nav else 0.0,
                    "open_lots": len(rsi_ledger.open_lots(rsi_state)),
                })
            rsi_block = report.build_rsi_block(rsi_state, rsi_market, rsi_nav, rsi_bench, [])

            # JP RSI枠も異常停止時は売買せず評価のみ行う（本体・米国RSI枠と同じ縮退方針。
            # 米国市場の異常停止判定に日本株枠を巻き込むこと自体は大将の指示に無いFableの判断だが、
            # 「売買を止めて観察のみ」という全体方針に合わせるのが最も安全と考えた）
            try:
                rsi_jp_nav, rsi_jp_market = jp_rsi_daily.compute_snapshot_only_jp(jp_state)
                if not dry_run:
                    jp_trading_date = jp_market.get_jp_trading_date()
                    jp_principal = config.RSI_JP_INITIAL_CAPITAL_JPY
                    jp_diff = rsi_jp_nav - jp_principal
                    jp_rsi_ledger.append_history_row({
                        "date": jp_trading_date,
                        "nav_jpy": round(rsi_jp_nav, 0),
                        "principal_jpy": round(jp_principal, 0),
                        "diff_jpy": round(jp_diff, 0),
                        "diff_pct": round(jp_diff / jp_principal * 100, 4) if jp_principal else 0.0,
                        "cash_ratio": round(jp_rsi_ledger.compute_cash_ratio(jp_state, rsi_jp_nav), 4) if rsi_jp_nav else 0.0,
                        "open_lots": len(jp_rsi_ledger.open_lots(jp_state)),
                    })
            except Exception:
                logger.exception("JP RSI枠: 異常停止時のスナップショット計算に失敗した")
                rsi_jp_nav, rsi_jp_market = 0.0, {}
            rsi_jp_block = report.build_rsi_jp_block(jp_state, rsi_jp_market, rsi_jp_nav, [])

            data = report.build_data_json(
                state, snapshots, nav_usd, bench_usd, [], now_jst, rsi_block=rsi_block, rsi_jp_block=rsi_jp_block,
            )
            if not dry_run:
                report.save_data_json(data)
                git_commit_and_push(now_jst, logger)
                log_and_report("[8] data.json更新・台帳保存・git push完了（異常停止時）")
            else:
                log_and_report("[8] dry-runのためdata.json保存・git push・台帳保存はスキップ")

            halt_line = f"🛑 異常停止: {halt_reason}。売買を止めて観察のみ実施"
            message = report.build_telegram_message(data, [], now_jst, alert_lines=[halt_line])
            if dry_run:
                log_and_report("[9] dry-runのためTelegram送信はスキップ。送信予定文面:\n" + message)
            else:
                ok = report.send_telegram_message(message)
                log_and_report(f"[9] Telegram送信{'成功' if ok else '失敗'}（異常停止アラート）")
            log_and_report("[10] 正常終了（異常停止）")
            return "\n".join(report_lines)

        charter_text = portfolio.load_charter_text()
        charter_targets = portfolio.parse_charter_targets(charter_text)
        voo_technicals = market.get_voo_technicals(voo_snap.close)
        log_and_report(
            f"[2b] VOOテクニカル取得完了。MA200={voo_technicals.ma200:.2f} "
            f"RSI14={voo_technicals.rsi14:.1f} 52w高値={voo_technicals.high_52w:.2f}"
        )

        prev_mode = state["mode"]
        history_rows_before = portfolio.read_history_rows()

        # 3. 初回判定
        if state["start_date"] is None and charter_targets is not None:
            state["start_date"] = voo_snap.date
            state["bench_units"] = config.INITIAL_CAPITAL_USD / voo_snap.close
            log_and_report(f"[3] 初回構築: start_date={state['start_date']} bench_units={state['bench_units']:.6f}")
        else:
            log_and_report("[3] 初回構築スキップ（既に開始済み、またはターゲット未記入）")

        # 3b. moomoo可用性確認（保有照合は廃止。各枠は自分のpending_ordersだけを見る。2026-08-18仕様変更）
        moomoo_available = broker.is_available()
        moomoo_skip_reason: str | None = None
        moomoo_alert_lines: list[str] = []
        if not moomoo_available:
            moomoo_skip_reason = "moomoo未接続"
            moomoo_alert_lines.append("⚠️ moomoo未接続（売買停止・評価のみ）")
            log_and_report("[3b] moomoo未接続。売買停止・評価のみ実施")
        else:
            log_and_report("[3b] moomoo接続確認OK")
        can_trade = moomoo_available

        # 3b-2. 発注前の市場状態確認（2026-08-18 修正2）。moomooへの問い合わせは1回の実行につき
        # 1回だけ行い、本体（execute_trades）・RSI枠（損切り・利確・買い増し・新規エントリー）の
        # 全発注経路で使い回す（発注ごとに毎回叩くとAPI呼び出し回数が増えるため）。
        # AFTERNOON（米国の通常取引時間）でなければ各発注経路が個別に見送る。問い合わせ自体が
        # 失敗した場合（None）はフェイルオープンで発注を許可する（一時的な問い合わせ失敗で
        # 売買が黙って止まる害の方が大きく、時間外に出てしまった注文は未決注文の自己解決
        # ＝settle_pending_ordersが回収するため）。
        market_us_state = broker.get_market_us_state() if moomoo_available else None
        if not moomoo_available:
            log_and_report("[3b-2] moomoo未接続のため市場状態確認スキップ")
        elif market_us_state is None:
            log_and_report("[3b-2] 市場状態確認: 取得失敗（フェイルオープンで発注は許可）")
        elif market_us_state != broker.MARKET_US_OPEN_STATE:
            log_and_report(
                f"[3b-2] 市場状態確認: market_us={market_us_state}（AFTERNOONではないため本日の新規発注は見送り）"
            )
        else:
            log_and_report(f"[3b-2] 市場状態確認: market_us={market_us_state}（発注許可）")

        # 手数料逆算用の口座全体チェックポイント（両枠のpending決済より前・本日の新規発注より前に読む。
        # こうすることで「前回記録からの現金差」が今回決済したpending注文だけに帰属する（修正3）。
        account_cash_snapshot = broker.get_cash() if moomoo_available else None
        if moomoo_available and account_cash_snapshot is None:
            logger.warning("口座全体の現金残高取得に失敗したため、今回は手数料逆算をスキップする")

        # 3c. 未決注文の決済（本体）。moomoo接続時のみ。他の判断より前に行う
        accepted_trades: list[dict] = []
        if moomoo_available:
            state, settled_trades, settle_warnings, settle_resolved = portfolio.settle_pending_orders(
                state, voo_snap.date, dry_run=dry_run,
            )
            for t in settled_trades:
                if not dry_run:
                    portfolio.append_trade_row(t)
            accepted_trades.extend(settled_trades)
            for w in settle_warnings:
                logger.warning(w)
            log_and_report(f"[3c] 本体pending決済: 確定{len(settled_trades)}件 警告{len(settle_warnings)}件")
        else:
            settled_trades, settle_resolved = [], []
            log_and_report("[3c] moomoo未接続のためpending決済スキップ")

        # 3d. 未決注文の決済（RSI枠）。本体決済の直後に行う（手数料逆算を両枠合算で行うため、
        # RSI枠の売買判断より前にここで決済を済ませておく。2026-08-18 修正3）
        if moomoo_available:
            rsi_state, rsi_settled_trades, rsi_settle_warnings, rsi_settle_resolved = rsi_daily.settle_pending_orders(
                rsi_state, voo_snap.date, dry_run=dry_run,
            )
            for t in rsi_settled_trades:
                if not dry_run:
                    rsi_ledger.append_trade_row(t)
            for w in rsi_settle_warnings:
                logger.warning(w)
            log_and_report(f"[3d] RSI枠pending決済: 確定{len(rsi_settled_trades)}件 警告{len(rsi_settle_warnings)}件")
        else:
            rsi_settled_trades, rsi_settle_resolved = [], []
            log_and_report("[3d] moomoo未接続のためRSI枠pending決済スキップ")

        # 3e. 手数料逆算（moomoo APIに手数料そのものを返す経路が無いため、口座全体の現金増減から
        #      逆算する。両枠合算・額面比で按分してcash_usdから差し引く。2026-08-18 修正3）
        pending_resolution_lines = settle_resolved + rsi_settle_resolved
        if moomoo_available and account_cash_snapshot is not None:
            prev_account_cash = account_state.load_account_cash()
            fees_by_book, fee_logs = account_state.reconcile_fees(
                prev_account_cash, account_cash_snapshot,
                {"main": settled_trades, "rsi": rsi_settled_trades},
            )
            state["cash_usd"] -= fees_by_book.get("main", 0.0)
            rsi_state["cash_usd"] -= fees_by_book.get("rsi", 0.0)
            for line in fee_logs:
                log_and_report(f"[3e] {line}")
        else:
            log_and_report("[3e] moomoo未接続のため手数料逆算はスキップ")

        # 200日線カウンタ更新
        if voo_technicals.above_200dma_today:
            state["above_200dma_streak"] += 1
            state["below_200dma_streak"] = 0
        elif voo_technicals.below_200dma_today:
            state["below_200dma_streak"] += 1
            state["above_200dma_streak"] = 0

        # 5. 売買判断のスキップ条件
        skip_reason = None
        if charter_targets is None:
            skip_reason = "ターゲット未記入"
        elif state["start_date"] is None:
            skip_reason = "start_dateがnull"
        elif state["last_processed_voo_date"] == voo_snap.date:
            skip_reason = "休場（VOO終値日付が前回処理日から進んでいない）"
        elif moomoo_skip_reason:
            skip_reason = moomoo_skip_reason

        rejected_trades: list[dict] = []
        queued_trades: list[dict] = []
        stop_loss_report_lines: list[str] = []

        # 5a. 損切り判定（Sonnetの判断より前に強制執行。ルール由来の強制執行であり番兵の裁量ではない）
        already_processed_today = state["last_processed_voo_date"] == voo_snap.date
        if already_processed_today:
            log_and_report("[5a] 損切り判定スキップ（休場・本日処理済み）")
        elif not can_trade:
            log_and_report(f"[5a] 損切り判定スキップ（売買停止中: {moomoo_skip_reason}）")
        else:
            stop_loss_orders = portfolio.check_stop_losses(state, snapshots)
            if not stop_loss_orders:
                log_and_report("[5a] 損切り判定: 該当なし")
            elif dry_run:
                log_and_report(f"[5a] 損切り判定: {len(stop_loss_orders)}件該当（dry-runのため約定はスキップ）")
            else:
                trade_date = voo_snap.date
                state, stop_loss_accepted, stop_loss_rejected, stop_loss_queued = portfolio.execute_trades(
                    stop_loss_orders, state, snapshots, charter_targets, trade_date, market_us_state,
                )
                for t in stop_loss_accepted:
                    portfolio.append_trade_row(t)
                accepted_trades.extend(stop_loss_accepted)
                rejected_trades.extend(stop_loss_rejected)
                queued_trades.extend(stop_loss_queued)
                loss_pct_by_ticker = {o["ticker"]: o["loss_pct"] for o in stop_loss_orders}
                stop_loss_report_lines = [
                    f"🔻 損切り: {t['ticker']} ({loss_pct_by_ticker[t['ticker']]:+.1%})"
                    for t in stop_loss_accepted
                ]
                log_and_report(
                    f"[5a] 損切り約定完了: 約定{len(stop_loss_accepted)}件 拒否{len(stop_loss_rejected)}件 "
                    f"未決{len(stop_loss_queued)}件"
                )
                for r in stop_loss_rejected:
                    logger.warning("拒否された損切り注文: %s", r)
                for q in stop_loss_queued:
                    logger.info("未決キューに追加された損切り注文: %s", q)

        if skip_reason:
            log_and_report(f"[5] 売買判断スキップ: ホールド（{skip_reason}）")
        else:
            decision = get_trade_decision(charter_text, state, snapshots, voo_technicals)
            decision_reason = decision.get("reason", "")
            proposed_trades = decision.get("trades", [])
            state["mode"] = decision.get("mode", state["mode"])
            log_and_report(
                f"[5] Sonnet判断: mode={state['mode']} trades={len(proposed_trades)}件 reason={decision_reason}"
            )

            if dry_run:
                log_and_report("[6] dry-runのため約定処理はスキップ")
            else:
                trade_date = voo_snap.date
                state, sonnet_accepted, sonnet_rejected, sonnet_queued = portfolio.execute_trades(
                    proposed_trades, state, snapshots, charter_targets, trade_date, market_us_state,
                )
                for t in sonnet_accepted:
                    portfolio.append_trade_row(t)
                accepted_trades.extend(sonnet_accepted)
                rejected_trades.extend(sonnet_rejected)
                queued_trades.extend(sonnet_queued)
                log_and_report(
                    f"[6] 約定処理完了: 約定{len(sonnet_accepted)}件 拒否{len(sonnet_rejected)}件 "
                    f"未決{len(sonnet_queued)}件"
                )
                for r in sonnet_rejected:
                    logger.warning("拒否された注文: %s", r)
                for q in sonnet_queued:
                    logger.info("未決キューに追加された注文: %s", q)

        state["last_processed_voo_date"] = voo_snap.date

        # 7. 評価・記録
        nav_usd = portfolio.compute_nav_usd(state, snapshots)
        bench_usd = portfolio.compute_bench_nav_usd(state, voo_snap.close)
        diff_usd = nav_usd - bench_usd
        diff_pct_today = round(diff_usd / bench_usd * 100, 4) if bench_usd else 0.0
        cash_ratio = portfolio.compute_cash_ratio(state, nav_usd) if nav_usd else 0.0
        log_and_report(
            f"[7] 評価額計算: NAV=${nav_usd:,.2f} ベンチマーク=${bench_usd:,.2f} 差額=${diff_usd:,.2f}"
        )

        if not dry_run:
            portfolio.append_history_row({
                "date": voo_snap.date,
                "nav_usd": round(nav_usd, 2),
                "bench_usd": round(bench_usd, 2),
                "diff_usd": round(diff_usd, 2),
                "diff_pct": diff_pct_today,
                "cash_ratio": round(cash_ratio, 4),
            })
            portfolio.save_portfolio(state)

        # 7c. RSI-30枠（本体の後に実行。本体の判断・台帳には一切触れない。SPEC_RSI30.md準拠。
        #     pending決済は3dで済ませてあるためrsi_stateは既に決済済み）
        rsi_already_processed = rsi_state["last_processed_date"] == voo_snap.date
        rsi_state, rsi_accepted_trades, rsi_log_lines, rsi_nav, rsi_bench, rsi_market = rsi_daily.run(
            rsi_state, voo_snap, can_trade, rsi_already_processed, dry_run, voo_snap.date, market_us_state,
        )
        rsi_accepted_trades = rsi_settled_trades + rsi_accepted_trades
        for line in rsi_log_lines:
            log_and_report(line)
        rsi_block = report.build_rsi_block(rsi_state, rsi_market, rsi_nav, rsi_bench, rsi_accepted_trades)

        # 7d. 日本株RSI枠（3本目の戦略枠。米国RSI枠の後に実行。moomoo発注は一切行わない
        #     台帳のみの仮想売買のため、moomoo可用性(can_trade)には依存せず常に試みる。
        #     この枠の障害で本体・米国RSI枠の報告まで止めないよう_run_jp_rsi_safeで保護する）
        jp_state, rsi_jp_accepted_trades, rsi_jp_log_lines, rsi_jp_nav, rsi_jp_market = _run_jp_rsi_safe(
            jp_state, dry_run, logger,
        )
        for line in rsi_jp_log_lines:
            log_and_report(line)
        rsi_jp_block = report.build_rsi_jp_block(jp_state, rsi_jp_market, rsi_jp_nav, rsi_jp_accepted_trades)

        # 3f. 口座全体の現金残高を記録（次回の手数料逆算の基準値。本日の全取引が終わった後に記録する。
        #     dry-runでは記録しない（触ってはいけない実運用のチェックポイントを汚さないため）
        if not dry_run and moomoo_available:
            final_account_cash = broker.get_cash()
            if final_account_cash is not None:
                account_state.save_account_cash(final_account_cash, voo_snap.date)
                log_and_report(f"[3f] 口座全体の現金残高を記録: ${final_account_cash:,.2f}")
            else:
                log_and_report("[3f] 口座全体の現金残高取得に失敗したため記録をスキップ")

        # アラート判定（売買は通常通り。報告の先頭に警告行を追加するのみ）
        alert_lines = (
            moomoo_alert_lines + stop_loss_report_lines
            + check_alerts(prev_mode, state["mode"], history_rows_before, diff_pct_today)
        )
        if alert_lines:
            log_and_report(f"[7b] アラート検知: {alert_lines}")

        # 8. data.json再生成
        data = report.build_data_json(
            state, snapshots, nav_usd, bench_usd, accepted_trades, now_jst,
            rsi_block=rsi_block, rsi_jp_block=rsi_jp_block,
        )
        if not dry_run:
            report.save_data_json(data)
            log_and_report("[8] data.json更新・台帳保存完了")
            git_commit_and_push(now_jst, logger)
            log_and_report("[8] git commit & push完了")
        else:
            log_and_report("[8] dry-runのためdata.json保存・git push・台帳保存はスキップ")

        # 9. Telegram送信
        prev_month_line = None
        if now_jst.day == 1:
            prev_month_dt = now_jst.replace(day=1) - dt.timedelta(days=1)
            prev_month_key = prev_month_dt.strftime("%Y-%m")
            for m in data["monthly"]:
                if m["month"] == prev_month_key:
                    result_jp = "勝ち" if m["result"] == "win" else "負け"
                    diff = m["nav"] - m["bench"]
                    prev_month_line = (
                        f"{prev_month_dt.month}月戦績: {result_jp} "
                        f"{'+' if diff >= 0 else ''}${diff:,.2f}"
                    )
                    break

        message = report.build_telegram_message(
            data, accepted_trades, now_jst, prev_month_line, alert_lines,
            resolved_pending_lines=pending_resolution_lines,
            rsi_accepted_trades=rsi_accepted_trades, rsi_jp_accepted_trades=rsi_jp_accepted_trades,
        )
        if dry_run:
            log_and_report("[9] dry-runのためTelegram送信はスキップ。送信予定文面:\n" + message)
        else:
            ok = report.send_telegram_message(message)
            log_and_report(f"[9] Telegram送信{'成功' if ok else '失敗'}")

        log_and_report("[10] 正常終了")

    except Exception as e:  # noqa: BLE001 - 実行中の例外は握りつぶさず記録・通知する
        logger.exception("daily_run実行中に例外が発生")
        error_line = f"⚠️ vs-sp500 実行エラー: {e}"
        report_lines.append(error_line)
        if not dry_run:
            try:
                report.send_telegram_message(error_line)
            except Exception:
                logger.exception("エラー通知のTelegram送信にも失敗")
        else:
            report_lines.append("(dry-runのためエラー時Telegram送信はスキップ)")

    return "\n".join(report_lines)


def launch_autonomous_loop() -> None:
    """自律ループ（loop_run.sh）をこのプロセスの子として起動する。

    launchdから /bin/bash で直接起動すると、macOSのTCC（~/Documents保護）により
    EPERM（Operation not permitted / exit 126）で落ちる。ディスクアクセス権を持つ
    このPythonプロセスから起動することで権限を継承させる。
    （2026-08-13: ループが8/8以降一度も自動起動していなかった問題の対策）
    """
    script = config.BASE_DIR / "loop_run.sh"
    if not script.exists():
        return
    logger = logging.getLogger("vs-sp500")
    try:
        proc = subprocess.run(
            ["/bin/bash", str(script)],
            cwd=str(config.BASE_DIR),
            capture_output=True,
            text=True,
            timeout=1200,
        )
        logger.info("[11] 自律ループ実行完了 exit=%s", proc.returncode)
    except subprocess.TimeoutExpired:
        logger.error("[11] 自律ループがタイムアウトした")
    except Exception:
        logger.exception("[11] 自律ループの起動に失敗")


def main() -> None:
    parser = argparse.ArgumentParser(description="vs-sp500 daily run")
    parser.add_argument("--dry-run", action="store_true", help="約定・push・Telegram送信を行わず全フローを検証する")
    parser.add_argument(
        "--report-only", action="store_true",
        help="NAV計算とdata.json再生成・pushのみ行う（Sonnet判断・約定・Telegram送信は行わない）",
    )
    parser.add_argument(
        "--no-loop", action="store_true",
        help="実行後の自律ループ起動を行わない（手動実行時の暴発防止）",
    )
    args = parser.parse_args()
    output = run(dry_run=args.dry_run, report_only=args.report_only)
    print(output)
    # 執行の後に必ず自律ループを回す（executionが例外で落ちた日も観測は行う）
    if not (args.dry_run or args.report_only or args.no_loop):
        launch_autonomous_loop()


if __name__ == "__main__":
    main()
