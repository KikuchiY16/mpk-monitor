"""
flag_danger.py

ライブチャットから「誰が見ても危険そうな馬」(暴れている・入れ込み・発汗・
放馬・落馬など)を拾う。

方針:
  - 危険語を含むコメントを、馬名/愛称/騎手名/丸数字/「N番」で馬に紐付ける。
  - 馬名が書かれていないコメントは、直前 --context-seconds 秒以内に最後に
    話題になった馬へ『推定』で紐付ける(重み0.5、表示は[推定])。
  - 「誰が見ても」を担保するため、ユニーク投稿者数 >= --min-users で初めて
    フラグを立てる。1人の連投・荒らしでは立たない。

使い方:
    python flag_danger.py \
        --racecard racecards/2026-08-22_niigata10.json \
        --chat output/g5KT20-oWGM.csv \
        --start 2026-08-22T17:00:00 --end 2026-08-22T17:15:00

--end には発走時刻を指定すること(発走後のコメントはレース結果を含む)。
パドック〜返し馬〜ゲート入りの時間帯だけを見たい場合は --start を絞る。
"""

import argparse
import csv
import re
from datetime import datetime, timedelta
from pathlib import Path

from monitor.predict_race import build_lookup, load_json, CIRCLED

# 強い危険語: 1コメントで1.0点
STRONG = [
    "暴れ", "大暴れ", "放馬", "落馬", "振り落", "立ち上が", "立ち上り", "後ろ脚",
    "蹴っ", "蹴り", "暴走", "ゲート入り嫌", "ゲートに入らな", "入りたがらな",
    "駄々", "騎手が降り", "下馬", "競走中止", "取消", "除外", "外傷", "ケガ", "怪我",
    "跛行", "歩様", "脚元", "故障",
]
# 弱い危険語: 1コメントで0.5点(入れ込み・発汗は単独だと軽症もある)
WEAK = [
    "イレ込", "入れ込", "いれこ", "発汗", "汗びっしょり", "汗だく", "白い汗", "汗かい",
    "カリカリ", "チャカつ", "ちゃかつ", "テンション高", "興奮", "うるさい馬",
    "嫌がっ", "嫌がる", "言うこと聞", "行きたがっ", "引っかか", "落ち着かな",
    "落ち着きな", "ソワソワ", "そわそわ", "怖がっ", "ビビっ", "びびっ", "パニック",
]
# 否定・誤爆の除外(これを含むコメントは危険語として数えない)
NEGATE = re.compile(
    r"暴れ(ん坊|馬だけど強|てほしい)|落ち着いて(る|いる|います)|落ち着きがある|"
    r"汗(かいてない|なし|ない)|入れ込んでない|イレ込んでない|問題な|大丈夫|"
    r"(観客|ファン|客|乃木坂).{0,6}うるさい|うるさい.{0,4}(観客|ファン|客)|"
    r"暴れ.{0,3}(たい|ろ$)|ゲームの|ガチャ"
)

_BAN = re.compile(r"(\d{1,2})\s*番(?!人気|手|目)")


def danger_score(message):
    """(点数, ヒットした語リスト)。否定語を含むなら 0"""
    if NEGATE.search(message):
        return 0.0, []
    hits = [w for w in STRONG if w in message]
    score = float(len(set(hits))) if hits else 0.0
    weak = [w for w in WEAK if w in message]
    score += 0.5 * len(set(weak))
    return min(score, 1.5), hits + weak  # 1コメントの上限は1.5点


def mentioned_horses(message, lookup, max_umaban):
    """曖昧な素の数字は使わず、名前・丸数字・「N番」だけで馬番を拾う"""
    found = set()
    for term, umaban in lookup.items():
        if term in message:
            found.add(umaban)
    for ch in message:
        idx = CIRCLED.find(ch)
        if idx != -1 and idx + 1 <= max_umaban:
            found.add(idx + 1)
    for m in _BAN.finditer(message):
        n = int(m.group(1))
        if 1 <= n <= max_umaban:
            found.add(n)
    return found


def load_chat(csv_path, start, end):
    rows = []
    with open(csv_path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            try:
                ts = datetime.fromisoformat(row["timestamp"])
            except ValueError:
                continue
            if start <= ts <= end:
                rows.append((ts, row["channel_id"], row["message"]))
    return rows


def analyze_chat(chat, lookup, max_umaban, context_seconds):
    """chat=[(ts, channel_id, message)] を時刻順に読み、馬番ごとの危険コメント集計を返す。
    戻り値: (stats, unattributed)。stats[umaban] = {"users": {channel: 重み}, "direct": n,
    "inferred": n, "samples": [...]}。watch_live.py からも呼ぶため関数化(2026-10-22)。"""
    # umaban -> {"users": {channel: weight}, "direct": n, "inferred": n, "samples": []}
    stats = {}
    unattributed = []
    last_topic = None  # (ts, umaban)

    for ts, channel, message in chat:
        horses = mentioned_horses(message, lookup, max_umaban)
        score, words = danger_score(message)

        if score > 0:
            targets, kind, weight = [], "direct", 1.0
            if len(horses) == 1:
                targets = list(horses)
            elif len(horses) == 0 and last_topic and \
                    (ts - last_topic[0]).total_seconds() <= context_seconds:
                targets, kind, weight = [last_topic[1]], "inferred", 0.5
            # 複数馬が同時に書かれたコメントは、どの馬のことか曖昧なので捨てる

            if not targets:
                unattributed.append((ts, message))
            for u in targets:
                s = stats.setdefault(u, {"users": {}, "direct": 0, "inferred": 0, "samples": []})
                s["users"][channel] = max(s["users"].get(channel, 0), score * weight)
                s[kind] += 1
                tag = "" if kind == "direct" else "[推定] "
                s["samples"].append(f"{ts:%H:%M:%S} {tag}{message}  <{','.join(words)}>")

        if len(horses) == 1:
            last_topic = (ts, next(iter(horses)))
    return stats, unattributed


def main():
    ap = argparse.ArgumentParser(description="チャットから危険そうな馬を拾う")
    ap.add_argument("--racecard", required=True)
    ap.add_argument("--chat", required=True)
    ap.add_argument("--alias", default=str(Path(__file__).parent / "alias_dictionary.json"))
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True, help="発走時刻。これより後は読まない")
    ap.add_argument("--min-users", type=int, default=3,
                    help="フラグを立てるのに必要なユニーク投稿者数 (default 3)")
    ap.add_argument("--context-seconds", type=float, default=20.0,
                    help="馬名なしコメントを直前の話題馬に紐付ける秒数")
    ap.add_argument("--show-unattributed", action="store_true",
                    help="どの馬にも紐付かなかった危険コメントも表示")
    args = ap.parse_args()

    racecard = load_json(args.racecard)
    lookup = build_lookup(racecard, load_json(args.alias))
    max_umaban = max(e["umaban"] for e in racecard["entries"])
    by_umaban = {e["umaban"]: e for e in racecard["entries"]}

    chat = load_chat(args.chat, datetime.fromisoformat(args.start),
                     datetime.fromisoformat(args.end))

    stats, unattributed = analyze_chat(chat, lookup, max_umaban, args.context_seconds)

    print(f"{racecard['date']} {racecard['track']}{racecard['race_number']}R "
          f"{racecard['race_name']} (発走 {racecard['post_time']})")
    print(f"チャット窓: {args.start} 〜 {args.end}  対象 {len(chat)} 件")
    print(f"フラグ条件: ユニーク投稿者 >= {args.min_users}")
    print()

    if not stats:
        print("危険語を含む馬別コメントはありませんでした。")
    else:
        ranked = sorted(stats.items(),
                        key=lambda kv: (len(kv[1]["users"]), sum(kv[1]["users"].values())),
                        reverse=True)
        for umaban, s in ranked:
            e = by_umaban[umaban]
            n_users = len(s["users"])
            flag = "【要警戒】" if n_users >= args.min_users else "  (様子見)"
            print(f"{flag} {umaban}番 {e['horse']} ({e['jockey']}, {e['popularity']}人気 "
                  f"{e['odds']}倍)  投稿者{n_users}人 / 直接{s['direct']}件 推定{s['inferred']}件")
            for line in s["samples"][:5]:
                print(f"      {line}")
            if len(s["samples"]) > 5:
                print(f"      ... 他 {len(s['samples']) - 5} 件")

    if args.show_unattributed and unattributed:
        print()
        print("馬に紐付かなかった危険コメント:")
        for ts, m in unattributed[:20]:
            print(f"  {ts:%H:%M:%S} {m}")

    print()
    print("注意: 『要警戒』はチャット上で複数人が危険を指摘したという観測であり、")
    print("      実際の状態(映像)や着順の予測ではない。[推定]は直前話題の馬への紐付けで誤りうる。")


if __name__ == "__main__":
    main()
