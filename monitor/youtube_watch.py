"""YouTubeライブチャットの取り込み(別スレッド)と、出走表に対する危険馬判定。"""
import json
import re
import threading
import time
from pathlib import Path

from monitor import flag_danger, yt_chat
from monitor.predict_race import build_lookup

ALIAS_PATH = Path(__file__).parent / "alias_dictionary.json"


class ChatBuffer:
    def __init__(self, video_id):
        self.video_id = video_id
        self.rows, self.lock, self.stop = [], threading.Lock(), threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def _run(self):
        while not self.stop.is_set():
            try:
                for m in yt_chat.LiveChat(self.video_id).messages(self.stop):
                    with self.lock:
                        self.rows.append((m["ts"], m["channel_id"], m["message"]))
                print("[youtube] 配信終了または切断。取り込みを停止")
                return
            except Exception as e:  # noqa: BLE001
                print(f"[youtube] 接続エラー: {type(e).__name__}。10秒後に再接続")
                time.sleep(10)

    def window(self, start, end):
        with self.lock:
            return [r for r in self.rows if start <= r[0] <= end]


def judge(chat, card, min_users=3, context_seconds=20.0):
    """[{horsenum, flag, keywords, n_hits, latest_comment}]。1人だけの指摘では印を付けない。"""
    if not card["entries"]:
        return []
    alias = json.loads(ALIAS_PATH.read_text(encoding="utf-8"))
    lookup = build_lookup(card, alias)
    max_umaban = max(e["umaban"] for e in card["entries"])
    stats, _ = flag_danger.analyze_chat(chat, lookup, max_umaban, context_seconds)
    out = []
    for umaban, s in sorted(stats.items()):
        n = len(s["users"])
        if n < max(min_users - 1, 2):
            continue
        words = sorted({w for smp in s["samples"] for w in re.findall(r"<(.*?)>$", smp)[0].split(",")})
        out.append({"horsenum": umaban, "flag": "🔴" if n >= min_users else "🟠", "keywords": "/".join(words),
                    "n_hits": s["direct"] + s["inferred"],
                    "latest_comment": f"({n}人) " + re.sub(r"\s*<.*?>$", "", s["samples"][-1])[:180]})
    return out
