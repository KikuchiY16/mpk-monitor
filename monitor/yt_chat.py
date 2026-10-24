"""
yt_chat.py (2026-10-22追加)

YouTubeライブチャットの最小クライアント(InnerTube API直叩き)。

背景: pytchat 0.5.5 / chat-downloader は2026-10-22時点で、チャットが活発な配信でも0件しか
返さなかった(後者は『初期データを解析できない』で例外)。ライブチャットのポップアウトページ
(/live_chat?is_popout=1&v=ID)には ytInitialData として直近の発言が入っており、続きは
/youtubei/v1/live_chat/get_live_chat に continuation を渡して取れるので、それだけを実装した。

  for m in LiveChat(video_id).messages():
      m = {"id", "ts"(datetime, 発言時刻), "author_name", "channel_id", "message", "type", "amount"}

- 発言時刻は YouTube側の timestampUsec を使う(取得時刻ではない)ので、接続前の発言(初期バックログ)も
  正しい時刻で入る。呼び出し側が時間窓で絞ればよい。
- 配信終了(continuationが無くなる)で generator が終わる。通信エラーは自動リトライ。
- YouTube側の非公開APIなので、仕様が変わったら壊れる。0件が続くときは `python yt_chat.py ID --debug`。
"""
import json
import re
import sys
import time
from datetime import datetime

import requests

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
_INIT = re.compile(r'(?:window\["ytInitialData"\]|var ytInitialData)\s*=\s*(\{.*?\});</script>', re.S)


def _text(runs_obj):
    if not runs_obj:
        return ""
    if "simpleText" in runs_obj:
        return runs_obj["simpleText"]
    out = []
    for r in runs_obj.get("runs", []):
        if "text" in r:
            out.append(r["text"])
        elif "emoji" in r:  # カスタム絵文字は shortcut(:name:) か 通常絵文字そのもの
            e = r["emoji"]
            out.append(e.get("shortcuts", [e.get("emojiId", "")])[0] if e.get("isCustomEmoji") else e.get("emojiId", ""))
    return "".join(out)


def _parse_action(a):
    """addChatItemAction -> dict (テキスト/スーパーチャット以外は None)"""
    item = a.get("addChatItemAction", {}).get("item", {})
    for kind in ("liveChatTextMessageRenderer", "liveChatPaidMessageRenderer"):
        r = item.get(kind)
        if r:
            ts = int(r.get("timestampUsec", "0")) / 1e6
            return {"id": r.get("id"), "ts": datetime.fromtimestamp(ts) if ts else datetime.now(),
                    "author_name": _text(r.get("authorName")), "channel_id": r.get("authorExternalChannelId", ""),
                    "message": _text(r.get("message")), "type": "textMessage" if "Text" in kind else "superChat",
                    "amount": _text(r.get("purchaseAmountText"))}
    return None


class LiveChat:
    def __init__(self, video_id, debug=False):
        self.video_id, self.debug = video_id, debug
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "ja"})
        self.seen = set()

    def _init(self):
        t = self.s.get("https://www.youtube.com/live_chat", params={"is_popout": 1, "v": self.video_id}, timeout=20).text
        m = _INIT.search(t)
        if not m:
            raise RuntimeError("ytInitialDataが見つかりません(ライブでない/チャット無効/仕様変更)")
        lc = json.loads(m.group(1)).get("contents", {}).get("liveChatRenderer")
        if not lc:
            raise RuntimeError("liveChatRendererがありません(チャットが無効の配信、または終了済み)")
        self.key = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', t).group(1)
        self.ver = re.search(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"', t).group(1)
        return lc

    def _emit(self, actions):
        for a in actions:
            m = _parse_action(a)
            if m and m["id"] not in self.seen:
                self.seen.add(m["id"])
                yield m

    @staticmethod
    def _next(continuations):
        for c in continuations or []:
            k = next(iter(c.values()))
            if isinstance(k, dict) and k.get("continuation"):
                return k["continuation"], k.get("timeoutMs", 3000)
        return None, 0

    def messages(self, stop=None):
        lc = self._init()
        yield from self._emit(lc.get("actions", []))
        cont, wait = self._next(lc.get("continuations"))
        errors = 0
        while cont and not (stop and stop.is_set()):
            time.sleep(max(1.0, min(wait / 1000, 8)))
            try:
                r = self.s.post(f"https://www.youtube.com/youtubei/v1/live_chat/get_live_chat?key={self.key}&prettyPrint=false",
                                json={"context": {"client": {"clientName": "WEB", "clientVersion": self.ver, "hl": "ja"}},
                                      "continuation": cont}, timeout=20)
                r.raise_for_status()
                c = r.json().get("continuationContents", {}).get("liveChatContinuation", {})
                errors = 0
            except (requests.RequestException, ValueError) as e:
                errors += 1
                print(f"[WARN] チャット取得エラー({errors}): {e}", file=sys.stderr)
                if errors >= 10:
                    return
                wait = 5000
                continue
            yield from self._emit(c.get("actions", []))
            cont, wait = self._next(c.get("continuations"))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    vid = sys.argv[1]
    t0, n = time.time(), 0
    for m in LiveChat(vid).messages():
        n += 1
        print(f"{m['ts']:%H:%M:%S} {m['author_name']}: {m['message']}")
        if "--debug" in sys.argv and time.time() - t0 > 20:
            break
    print("total", n)
