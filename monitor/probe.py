"""接続確認(最初に1回だけ流す): GitHub Actionsのランナーから各サイトに繋がるかを見る。
ログに出すのはステータスと件数だけ。

  python -m monitor.probe [netkeiba_race_id(12桁)] [horse_id] [youtube_video_id]
"""
import re
import sys
from datetime import datetime

import requests

from monitor import bbs_watch, yahoo_lite, yt_chat

H = {"User-Agent": "Mozilla/5.0", "Accept-Language": "ja"}


def check(name, fn):
    try:
        print(f"[OK ] {name}: {fn()}")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[NG ] {name}: {type(e).__name__}: {str(e)[:150]}")
        return False


def main():
    nk_race = sys.argv[1] if len(sys.argv) > 1 else "202608040711"
    nk_horse = sys.argv[2] if len(sys.argv) > 2 else "2023105825"
    video = sys.argv[3] if len(sys.argv) > 3 else "gFRtAAmiFbE"
    ok = []
    ok.append(check("Yahoo 日程", lambda: f"{len(yahoo_lite.build_schedule(datetime.now().date()))}レース(今日)"))

    def shutuba():
        r = requests.get(bbs_watch.SHUTUBA_URL.format(host="race", race_id=nk_race), headers=H, timeout=20)
        return f"HTTP {r.status_code}, {len(r.text)}文字, 馬リンク{len(re.findall('/horse/', r.text))}"
    ok.append(check("Netkeiba 出馬表(requests)", shutuba))

    def board():
        d = bbs_watch.make_driver()
        try:
            h1 = bbs_watch.fetch_board_html(d, bbs_watch.HORSE_BOARD_URL.format(horse_id=nk_horse), wait_sec=15)
            h2 = bbs_watch.fetch_board_html(d, bbs_watch.RACE_BOARD_URL.format(host="race", race_id=nk_race), wait_sec=15)
        finally:
            d.quit()
        return f"馬掲示板 {len(bbs_watch.parse_comments(h1))}件 / レース掲示板 {len(bbs_watch.parse_comments(h2))}件"
    ok.append(check("Netkeiba 掲示板(Selenium)", board))

    def yt():
        n = 0
        for _ in yt_chat.LiveChat(video).messages():
            n += 1
            if n >= 5:
                break
        return f"{n}件取得"
    ok.append(check("YouTube ライブチャット", yt))
    sys.exit(0 if all(ok) else 1)


if __name__ == "__main__":
    main()
