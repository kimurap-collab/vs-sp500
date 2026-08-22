#!/usr/bin/env python3
"""vs-sp500: 候補憲章の検証・採用器（決定論。LLM判断を含まない）。

使い方:
    python3 tools/validate_and_adopt.py candidates/candidate_YYYYMMDD.md [--dry-run]

検証（1つでも不合格なら理由を出力してexit 1・何も変更しない）:
    1. 冷却期間: 「作成日:」と「表の改訂履歴」の最終日付の遅い方から7日以上経過している
    2. ledger/adoptions.json を見て今月まだ採用しておらず、前回採用から28日以上経過している
    3. 候補の配分表がパース可能（charter.mdと同じ表形式）
    4. 銘柄がホワイトリストのみ／通常・防衛それぞれ合計100±0.5%
    5. 可動域: 株式合計（VOO+QQQ+XLV+1306.T）通常≤90%・防衛≤50%／
       1銘柄≤30%（VOOのみ≤65%）／現金>実行器の下限2%（両モード。同値は実行不能になるため不可）

検証に全て合格した場合のみ、charter.mdの「## ターゲット配分」セクションの表だけを
候補の表で置換し（他セクションは一切変更しない）、改訂履歴に1行追記、候補ファイルの
「状態:」を書換、ledger/adoptions.jsonに記録してgit commit・pushする。
--dry-run 指定時は検証と採用内容の算出のみ行い、ファイル書き込み・gitは一切行わない。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import portfolio  # noqa: E402

STOCK_TICKERS = ("VOO", "QQQ", "XLV", "EWJ")
ROW_ORDER = ["VOO", "QQQ", "GLD", "TLT", "IEF", "XLV", "XLE", "EWJ", "現金"]

SUM_TOLERANCE = 0.005  # ±0.5ポイント
NORMAL_STOCK_MAX = 0.90
DEFENSE_STOCK_MAX = 0.50
SINGLE_TICKER_MAX = 0.30
VOO_MAX = 0.65
# 現金の可動域の下限。実行器の下限(config.MIN_CASH_RATIO=2%)と「同値」を許すと、
# 目標へ収束する買いが下限に触れて弾かれ、採用した途端に沈黙して実行不能になる
# （2026-08-18実測: 現金目標2%の買付は事後1.9664%となり拒否。3%・5%は通る）。
# よって実行器の下限より厳密に大きい値を要求する。上乗せ0.1ptは1回の買付の手数料が
# 現金比率に与える影響（NAV$64,270・手数料$22で0.033pt）を吸収できる幅。
MIN_CASH = config.MIN_CASH_RATIO + 0.001
COOLDOWN_DAYS = 7
# 暦月判定だけでは月境界の連続採用（8/31採用→9/1再採用＝2日で2回）が通る
# （2026-08-20発覚の発見2・2026-08-23修正）。28日は最短の月（平年2月）で、
# 毎月1日の月次レビューでの採用は常に通り、それより速いペースだけを止める。
MIN_ADOPTION_INTERVAL_DAYS = 28

ADOPTIONS_PATH = config.LEDGER_DIR / "adoptions.json"
JST = dt.timezone(dt.timedelta(hours=9))


class ValidationError(Exception):
    pass


def _extract_allocation_table(md_text: str) -> dict[str, dict[str, float]]:
    """Markdown内で最初に現れる「資産|通常モード|防衛モード」形式の表をパースする。

    行のパース（%→小数変換）は portfolio.py の _pct_to_float を流用する。
    """
    lines = md_text.splitlines()
    table_lines: list[str] = []
    in_table = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("|"):
            table_lines.append(stripped)
            in_table = True
        elif in_table:
            break
    if len(table_lines) < 3:
        raise ValidationError("配分表が見つからない、または行数が不足している")

    data_rows = table_lines[2:]  # ヘッダ行・区切り行をスキップ
    targets: dict[str, dict[str, float]] = {}
    for row in data_rows:
        cells = [c.strip() for c in row.strip("|").split("|")]
        if len(cells) < 3:
            continue
        asset, normal_raw, defense_raw = cells[0], cells[1], cells[2]
        if not asset or "記入" in asset:
            raise ValidationError("配分表に未記入セルがある")
        normal = portfolio._pct_to_float(normal_raw)
        defense = portfolio._pct_to_float(defense_raw)
        if normal is None or defense is None:
            raise ValidationError(f"配分表の数値が不正: {row}")
        targets[asset] = {"normal": normal, "defense": defense}

    if not targets:
        raise ValidationError("配分表からデータ行を読み取れなかった")
    return targets


def _latest_table_revision_date(candidate_text: str) -> dt.date | None:
    """「表の改訂履歴」セクションの箇条書き先頭の日付のうち最新を返す。セクションか日付が無ければNone。

    冷却期間の起点は「配分表が最後に変わった日」であるべきで、作成日だけを見ると採用直前に
    表を書き換えても冷却済み扱いになる（2026-08-20発覚・2026-08-22修正）。
    観察ログの追記で冷却が巻き戻らんよう、このセクションの箇条書きだけを数える。
    """
    dates: list[dt.date] = []
    in_section = False
    for line in candidate_text.splitlines():
        stripped = line.strip()
        if "表の改訂履歴" in stripped:
            in_section = True
            continue
        if in_section:
            if stripped.startswith("#"):
                break
            m = re.match(r"-\s*\*{0,2}(\d{4}-\d{2}-\d{2})", stripped)
            if m:
                dates.append(dt.date.fromisoformat(m.group(1)))
    return max(dates) if dates else None


def _check_created_date(candidate_text: str, today: dt.date) -> None:
    m = re.search(r"作成日:\s*(\d{4}-\d{2}-\d{2})", candidate_text)
    if not m:
        raise ValidationError("候補ファイルに「作成日:」が見つからない")
    created = dt.date.fromisoformat(m.group(1))
    revised = _latest_table_revision_date(candidate_text)
    origin = max(created, revised) if revised else created
    elapsed = (today - origin).days
    if elapsed < COOLDOWN_DAYS:
        raise ValidationError(
            f"冷却期間不足（起点{origin.isoformat()}から{elapsed}日。{COOLDOWN_DAYS}日必要）"
        )


def _load_adoptions() -> dict:
    if not ADOPTIONS_PATH.exists():
        return {"adoptions": []}
    with open(ADOPTIONS_PATH, encoding="utf-8") as f:
        return json.load(f)


def _check_monthly_limit(today: dt.date) -> None:
    adoptions = _load_adoptions()["adoptions"]
    this_month = today.strftime("%Y-%m")
    for a in adoptions:
        if a["date"][:7] == this_month:
            raise ValidationError(f"今月は既に採用済み（{a['date']} {a['candidate']}）")
    if adoptions:
        last = max(adoptions, key=lambda a: a["date"])
        elapsed = (today - dt.date.fromisoformat(last["date"])).days
        if elapsed < MIN_ADOPTION_INTERVAL_DAYS:
            raise ValidationError(
                f"前回採用（{last['date']} {last['candidate']}）から{elapsed}日。"
                f"{MIN_ADOPTION_INTERVAL_DAYS}日必要"
            )


def _check_whitelist_and_sums(targets: dict[str, dict[str, float]]) -> None:
    for asset in targets:
        if asset != "現金" and asset not in config.WHITELIST:
            raise ValidationError(f"ホワイトリスト外の資産: {asset}")
    for mode in ("normal", "defense"):
        total = sum(v[mode] for v in targets.values())
        if abs(total - 1.0) > SUM_TOLERANCE:
            raise ValidationError(f"{mode}モードの合計が100%から乖離: {total * 100:.2f}%")


def _check_ranges(targets: dict[str, dict[str, float]]) -> None:
    for mode, stock_max in (("normal", NORMAL_STOCK_MAX), ("defense", DEFENSE_STOCK_MAX)):
        stock_total = sum(targets.get(t, {}).get(mode, 0.0) for t in STOCK_TICKERS)
        if stock_total > stock_max + 1e-9:
            raise ValidationError(
                f"{mode}モードの株式合計が可動域超過: {stock_total * 100:.1f}% > {stock_max * 100:.0f}%"
            )

        for asset, alloc in targets.items():
            if asset == "現金":
                continue
            limit = VOO_MAX if asset == "VOO" else SINGLE_TICKER_MAX
            if alloc[mode] > limit + 1e-9:
                raise ValidationError(
                    f"{mode}モードの{asset}が単一銘柄上限超過: {alloc[mode] * 100:.1f}% > {limit * 100:.0f}%"
                )

        cash = targets.get("現金", {}).get(mode, 0.0)
        if cash < MIN_CASH - 1e-9:
            raise ValidationError(f"{mode}モードの現金比率が下限未満: {cash * 100:.1f}% < {MIN_CASH * 100:.1f}%")


def validate(candidate_path: Path, today: dt.date) -> dict[str, dict[str, float]]:
    candidate_text = candidate_path.read_text(encoding="utf-8")
    _check_created_date(candidate_text, today)
    _check_monthly_limit(today)
    targets = _extract_allocation_table(candidate_text)
    _check_whitelist_and_sums(targets)
    _check_ranges(targets)
    return targets


def _format_table(targets: dict[str, dict[str, float]]) -> str:
    # ROW_ORDER漏れは行の「黙った脱落」＝合計100%未満の憲章を書いてまう事故になるので先に落とす
    missing = [a for a in targets if a not in ROW_ORDER]
    if missing:
        raise ValidationError(f"ROW_ORDERに未登録の資産があるため表を書けない: {missing}")
    lines = ["| 資産 | 通常モード | 防衛モード |", "|---|---|---|"]
    for asset in ROW_ORDER:
        if asset not in targets:
            continue
        v = targets[asset]
        lines.append(f"| {asset} | {v['normal'] * 100:.0f}% | {v['defense'] * 100:.0f}% |")
    return "\n".join(lines)


def _next_version(charter_text: str) -> str:
    versions = re.findall(r"v(\d+)\.(\d+)", charter_text)
    if not versions:
        return "v1.1"
    major, minor = max((int(a), int(b)) for a, b in versions)
    return f"v{major}.{minor + 1}"


def _replace_target_table(charter_text: str, new_table_md: str) -> str:
    """「## ターゲット配分」セクション内の表の行だけを新しい表で置換する。他は一切変更しない。"""
    lines = charter_text.split("\n")
    heading_idx = None
    for i, line in enumerate(lines):
        if line.strip() == "## ターゲット配分":
            heading_idx = i
            break
    if heading_idx is None:
        raise ValidationError("charter.mdに「## ターゲット配分」セクションが見つからない")

    table_start = None
    table_end = None
    for i in range(heading_idx + 1, len(lines)):
        stripped = lines[i].strip()
        if stripped.startswith("|"):
            if table_start is None:
                table_start = i
            table_end = i + 1
        elif table_start is not None:
            break
        elif stripped.startswith("## "):
            break
    if table_start is None:
        raise ValidationError("charter.mdの「## ターゲット配分」セクションに表が見つからない")

    new_lines = lines[:table_start] + new_table_md.split("\n") + lines[table_end:]
    return "\n".join(new_lines)


def adopt(candidate_path: Path, targets: dict[str, dict[str, float]], today: dt.date, dry_run: bool) -> str:
    charter_text = portfolio.load_charter_text()
    new_table_md = _format_table(targets)
    version = _next_version(charter_text)
    date_str = today.isoformat()

    new_charter_text = _replace_target_table(charter_text, new_table_md)
    revision_line = f"- {version} ({date_str}): {candidate_path.name}を採用。決定論の検証器（tools/validate_and_adopt.py）で自動採用。"
    new_charter_text = new_charter_text.rstrip("\n") + "\n" + revision_line + "\n"

    candidate_text = candidate_path.read_text(encoding="utf-8")
    new_candidate_text = re.sub(r"状態:.*", f"状態: 採用済み({date_str})", candidate_text, count=1)

    adoptions = _load_adoptions()
    adoptions["adoptions"].append({"date": date_str, "candidate": candidate_path.name})

    summary = f"候補 {candidate_path.name} を採用（{version}）。新ターゲット配分:\n{new_table_md}"

    if dry_run:
        return "[dry-run] charter.md等への書き込み・gitは行っていない\n" + summary

    config.CHARTER_PATH.write_text(new_charter_text, encoding="utf-8")
    candidate_path.write_text(new_candidate_text, encoding="utf-8")
    with open(ADOPTIONS_PATH, "w", encoding="utf-8") as f:
        json.dump(adoptions, f, ensure_ascii=False, indent=2)

    subprocess.run(["git", "add", "-A"], cwd=config.BASE_DIR, check=True)
    subprocess.run(
        ["git", "commit", "-m", f"adopt: {candidate_path.name} ({version})"],
        cwd=config.BASE_DIR, check=True,
    )
    subprocess.run(["git", "push"], cwd=config.BASE_DIR, check=True)

    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="vs-sp500 候補憲章の検証・採用器")
    parser.add_argument("candidate", help="候補ファイルパス（例: candidates/candidate_20260807.md）")
    parser.add_argument("--dry-run", action="store_true", help="charter.md等を書き換えずに採用内容だけ確認する")
    args = parser.parse_args()

    candidate_path = Path(args.candidate)
    if not candidate_path.exists():
        print(f"候補ファイルが見つからない: {candidate_path}", file=sys.stderr)
        sys.exit(1)

    today = dt.datetime.now(JST).date()

    try:
        targets = validate(candidate_path, today)
    except ValidationError as e:
        print(f"却下: {e}")
        sys.exit(1)

    summary = adopt(candidate_path, targets, today, args.dry_run)
    print(summary)


if __name__ == "__main__":
    main()
