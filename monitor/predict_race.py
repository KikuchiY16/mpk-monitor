"""
predict_race.py

出走表(racecard JSON)とライブチャットログ(collect_chat.pyの出力CSV)から、
馬番ごとの「チャット言及バズ度」を定量集計し、市場人気(オッズ由来の
確率)と並べて比較する。

これは「当たる予想」を作るものではない。これまで手作業でやっていた
「馬名・騎手名の言及を数える」「終盤の言及を重めに見る」という作業を、
毎回同じ基準で再現できるようにしただけのもの。市場(オッズ)を上回る
優位性がある根拠は無いので、あくまで観測結果の整理として使うこと。

使い方:
    python predict_race.py \
        --racecard racecards/2026-08-22_niigata10.json \
        --chat output/g5KT20-oWGM.csv \
        --start 2026-08-22T17:05:00 \
        --end   2026-08-22T17:15:00

--end には必ずレースの発走時刻(またはそれ以前)を指定すること。
発走後のチャットまで含めると結果が漏れて「予測」にならない。
"""

import argparse
import csv
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

POSITIVE_WORDS = [
    "頑張れ", "がんばれ", "応援", "いいね", "よき", "強い", "つよい", "気合",
    "期待", "勝て", "来い", "こい", "軸", "本命", "単勝", "複勝",
    "仕上がっ", "良い", "いい", "楽しみ", "狙い", "抜けてる", "たのむ", "頼む",
]
NEGATIVE_WORDS = [
    "ダメ", "だめ", "切る", "弱い", "よわい", "消える", "やめとけ", "やめとこ",
    "不安", "心配", "無理", "飛ぶ", "飛べ", "ハズレ", "失速", "垂れた", "厳しい",
]

CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱"
_NUMBER = re.compile(r"(\d{1,2})")
_EXCLUDE_TAIL = re.compile(r"^(円|万|倍|時|分|人気|人|着|号機|R|レース|階|回|着差|馬身)")


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def build_lookup(racecard, alias_dict):
    """表記(馬名・騎手名・愛称)-> {umaban,...} のマップを作る。
    複数馬に一致する曖昧な表記は採用しない(誤爆を避けるため)。"""
    raw = {}

    def add(term, umaban):
        if not term:
            return
        raw.setdefault(term, set()).add(umaban)

    horse_by_name = {e["horse"]: e["umaban"] for e in racecard["entries"]}
    jockey_by_name = {e["jockey"]: e["umaban"] for e in racecard["entries"]}

    for e in racecard["entries"]:
        add(e["horse"], e["umaban"])
        add(e["jockey"], e["umaban"])

    # 騎手の苗字(先頭2文字)を自動フォールバックとして追加。
    # 同じ2文字が同一レース内で複数騎手にかぶる場合は追加しない。
    surname2 = {}
    for e in racecard["entries"]:
        s = e["jockey"][:2]
        surname2.setdefault(s, set()).add(e["umaban"])
    for s, umabans in surname2.items():
        if len(umabans) == 1:
            add(s, next(iter(umabans)))

    # alias_dictionary.json から、このレースの登録馬・登録騎手に一致する
    # canonical のエイリアスだけを取り込む。
    for entry in alias_dict.get("horses", []):
        if entry["canonical"] in horse_by_name:
            umaban = horse_by_name[entry["canonical"]]
            for alias in entry["aliases"]:
                add(alias, umaban)

    for entry in alias_dict.get("jockeys", []):
        if entry["canonical"] in jockey_by_name:
            umaban = jockey_by_name[entry["canonical"]]
            for alias in entry["aliases"]:
                add(alias, umaban)

    # 曖昧(複数馬に一致)な語は除外し、単純な文字列->単一馬番のdictにする
    lookup = {term: next(iter(umabans)) for term, umabans in raw.items() if len(umabans) == 1}
    # 短すぎる語(1文字)は誤爆しやすいので除外
    lookup = {term: umaban for term, umaban in lookup.items() if len(term) >= 2}
    return lookup


def find_hits(message, lookup, max_umaban):
    """1メッセージから (umaban, confidence) のリストを返す"""
    hits = []

    for term, umaban in lookup.items():
        if term in message:
            hits.append((umaban, 1.0, "name"))

    for ch in message:
        idx = CIRCLED.find(ch)
        if idx != -1:
            n = idx + 1
            if n <= max_umaban:
                hits.append((n, 0.8, "circled"))

    for m in _NUMBER.finditer(message):
        n = int(m.group(1))
        if not (1 <= n <= max_umaban):
            continue
        tail = message[m.end():m.end() + 3]
        if _EXCLUDE_TAIL.match(tail):
            continue
        hits.append((n, 0.3, "digit"))

    return hits


def sentiment_score(message):
    pos = sum(1 for w in POSITIVE_WORDS if w in message)
    neg = sum(1 for w in NEGATIVE_WORDS if w in message)
    return pos - neg


def load_chat(csv_path, start, end):
    rows = []
    with open(csv_path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                ts = datetime.fromisoformat(row["timestamp"])
            except ValueError:
                continue
            if start <= ts <= end:
                rows.append((ts, row["message"]))
    return rows


def main():
    ap = argparse.ArgumentParser(description="チャット言及バズ度 vs 市場人気の定量比較")
    ap.add_argument("--racecard", required=True)
    ap.add_argument("--chat", required=True)
    ap.add_argument("--alias", default=str(Path(__file__).parent / "alias_dictionary.json"))
    ap.add_argument("--start", required=True, help="例: 2026-08-22T17:05:00")
    ap.add_argument("--end", required=True, help="発走時刻。これより後のチャットは読まない")
    ap.add_argument("--final-window-minutes", type=float, default=2.0,
                     help="発走直前、何分間を『終盤』として重み付けするか")
    ap.add_argument("--final-weight", type=float, default=3.0,
                     help="終盤メッセージの重み倍率")
    args = ap.parse_args()

    racecard = load_json(args.racecard)
    alias_dict = load_json(args.alias)
    lookup = build_lookup(racecard, alias_dict)
    max_umaban = max(e["umaban"] for e in racecard["entries"])

    start = datetime.fromisoformat(args.start)
    end = datetime.fromisoformat(args.end)
    final_cutoff = end - timedelta(minutes=args.final_window_minutes)

    chat = load_chat(args.chat, start, end)

    buzz = {e["umaban"]: 0.0 for e in racecard["entries"]}
    sentiment = {e["umaban"]: 0.0 for e in racecard["entries"]}
    raw_count = {e["umaban"]: 0 for e in racecard["entries"]}

    for ts, message in chat:
        time_weight = args.final_weight if ts >= final_cutoff else 1.0
        net = sentiment_score(message)
        for umaban, confidence, _kind in find_hits(message, lookup, max_umaban):
            w = confidence * time_weight
            buzz[umaban] += w
            sentiment[umaban] += net * confidence
            raw_count[umaban] += 1

    total_buzz = sum(buzz.values()) or 1.0
    total_inv_odds = sum(1.0 / e["odds"] for e in racecard["entries"])

    results = []
    for e in racecard["entries"]:
        umaban = e["umaban"]
        market_prob = (1.0 / e["odds"]) / total_inv_odds
        chat_share = buzz[umaban] / total_buzz
        results.append({
            "umaban": umaban,
            "horse": e["horse"],
            "jockey": e["jockey"],
            "popularity": e["popularity"],
            "odds": e["odds"],
            "market_prob": market_prob,
            "raw_mentions": raw_count[umaban],
            "chat_buzz": buzz[umaban],
            "chat_share": chat_share,
            "sentiment": sentiment[umaban],
            "diff": chat_share - market_prob,
        })

    results.sort(key=lambda r: r["chat_share"], reverse=True)

    print(f"{racecard['date']} {racecard['track']}{racecard['race_number']}R "
          f"{racecard['race_name']} (発走 {racecard['post_time']})")
    print(f"チャット窓: {args.start} 〜 {args.end}"
          f"（終盤{args.final_window_minutes:.0f}分は重み{args.final_weight:.1f}倍）")
    print(f"対象チャット件数: {len(chat)}")
    print()
    header = f"{'馬番':>3} {'馬名':<10} {'騎手':<8} {'人気':>4} {'オッズ':>7} " \
             f"{'市場%':>7} {'言及数':>6} {'buzz':>6} {'buzz%':>7} {'乖離':>7} {'感情':>5}"
    print(header)
    print("-" * len(header))
    for r in results:
        print(f"{r['umaban']:>3} {r['horse']:<10} {r['jockey']:<8} "
              f"{r['popularity']:>4} {r['odds']:>7.1f} "
              f"{r['market_prob']*100:>6.1f}% {r['raw_mentions']:>6} "
              f"{r['chat_buzz']:>6.1f} {r['chat_share']*100:>6.1f}% "
              f"{r['diff']*100:>+6.1f}% {r['sentiment']:>+5.1f}")

    print()
    print("見方:")
    print("  市場% = オッズから逆算した、控除率を除いた市場の暗黙確率")
    print("  buzz% = チャット言及の加重シェア(終盤ほど重み大)")
    print("  乖離  = buzz% - 市場%。プラスなら市場よりチャットで話題、マイナスなら逆")
    print("  感情  = ポジティブ語-ネガティブ語の加重合計(プラスが多いほど好意的)")
    print()
    print("注意: buzz%はあくまで『チャットでの話題の量』であって、的中率の推定値ではない。")
    print("      市場を上回る優位性がある根拠は無い。参考程度に。")


if __name__ == "__main__":
    main()
