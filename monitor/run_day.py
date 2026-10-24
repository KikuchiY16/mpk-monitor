"""
1開催日ぶんの監視を1ジョブで回す(GitHub Actions用)。cronは遅延するので、朝に起動して
最終レース(または --until)まで自分でループし、各レースの発走N分前(既定 20,5)に1回ずつ判定する。

  python -m monitor.run_day --dry_run                       # 今日(JST)、シートに書かない
  python -m monitor.run_day --until 13:00 --min_racenum 1   # 午前だけ
  python -m monitor.run_day --youtube_video_id XXXXXXXXXXX  # YouTubeチャットも併用

シート出力は環境変数 GOOGLE_SERVICE_ACCOUNT_JSON / SHEET_ID があるときだけ(無ければドライラン)。
ログにはコメント本文を出さない(公開リポジトリ)。印(馬番+記号)だけ出す。
"""
import argparse
import os
import time
from argparse import Namespace
from datetime import datetime, timedelta

import pandas as pd
import requests

from monitor import bbs_watch, sheet_out, yahoo_lite, youtube_watch


def jst_now():
    return datetime.now()  # ワークフローで TZ=Asia/Tokyo を設定する前提


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="YYYY-MM-DD(省略時は今日)")
    ap.add_argument("--until", default=None, help="HH:MM。これを過ぎたら終了(6時間制限の分割用)")
    ap.add_argument("--since", default=None,
                    help="HH:MM。この時刻より前のチェックポイントは扱わず、それまで待つ(--untilの後半ジョブ用)")
    ap.add_argument("--min_racenum", type=int, default=1)
    ap.add_argument("--checkpoints", default="20,5")
    ap.add_argument("--tol_min", type=int, default=3)
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--max_requests", type=int, default=150)
    ap.add_argument("--max_age_min", type=int, default=360)
    ap.add_argument("--youtube_video_id", default=os.environ.get("YOUTUBE_VIDEO_ID", ""))
    ap.add_argument("--no_netkeiba", action="store_true")
    ap.add_argument("--dry_run", action="store_true")
    ap.add_argument("--make_schedule_only", action="store_true")
    ap.add_argument("--debug_html", action="store_true")
    args = ap.parse_args()

    today = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else jst_now().date()
    os.makedirs(bbs_watch.WORK_DIR, exist_ok=True)
    schedule = [e for e in yahoo_lite.build_schedule(today) if e["racenum"] >= args.min_racenum]
    bbs_watch._save_json(bbs_watch.SCHEDULE_PATH, schedule)
    print(f"{today}: {len(schedule)}レース (R{args.min_racenum}以上)")
    if args.make_schedule_only or not schedule:
        return
    cps = [int(x) for x in args.checkpoints.split(",")]
    until = datetime.combine(today, datetime.strptime(args.until, "%H:%M").time()) if args.until else None
    since = datetime.combine(today, datetime.strptime(args.since, "%H:%M").time()) if args.since else None
    last_end = max(datetime.fromisoformat(e["post_dt"]) for e in schedule)
    dry = args.dry_run or not sheet_out.configured()
    print(f"チェックポイント={cps}分前 / シート書込={'しない(dry)' if dry else 'する'} / "
          f"YouTube={'あり' if args.youtube_video_id else 'なし'} / 開始={since or '今'} / 終了={until or last_end}")

    yt = None
    if args.youtube_video_id:
        yt = youtube_watch.ChatBuffer(args.youtube_video_id)
        yt.start()
    driver, session, state, done, blocked = None, requests.Session(), {}, set(), 0
    nb_args = Namespace(host="race", debug_html=args.debug_html, interval=args.interval,
                        max_age_min=args.max_age_min)
    if since:
        # --since より前のチェックポイントは前半ジョブ(--until)の担当なので最初から済み扱い
        for e in schedule:
            for n in cps:
                if datetime.fromisoformat(e["post_dt"]) - timedelta(minutes=n) < since:
                    done.add(f"{e['race_id']}@{n}")
    try:
        while True:
            now = jst_now()
            if now > last_end + timedelta(minutes=2) or (until and now >= until):
                break
            if since and now < since:
                time.sleep(15)
                continue
            for e in bbs_watch.select_due(schedule, cps, done, args.tol_min, now):
                done.add(e["done_key"])  # 失敗しても再試行しない(アクセス過多を避ける)
                post = datetime.fromisoformat(e["post_dt"])
                tag = f"[{e['race_id']} 発走{e['phase']}分前]"
                if not args.no_netkeiba and blocked < 3:
                    try:
                        driver = driver or bbs_watch.make_driver()
                        per_horse = bbs_watch.watch_race(e, session, driver, state, nb_args,
                                                         bbs_watch.Budget(args.max_requests))
                        rows = bbs_watch.to_rows(e, per_horse, now.isoformat(timespec="seconds"))
                        print(f"{tag} netkeiba 印: { {r['horsenum']: r['flag'] for r in rows} or 'なし' }")
                        if not dry:
                            bbs_watch.write_sheet(pd.DataFrame(rows, columns=bbs_watch.FIELDS), [e["race_id"]], "netkeiba")
                        blocked = 0
                    except bbs_watch.BlockedError as ex:
                        blocked += 1
                        print(f"{tag} netkeiba 取得失敗/ブロック兆候({blocked}回目): {str(ex)[:120]}")
                    except Exception as ex:  # noqa: BLE001
                        print(f"{tag} netkeiba 例外: {type(ex).__name__}: {str(ex)[:120]}")
                if yt:
                    try:
                        card = yahoo_lite.fetch_racecard(e["race_id"], e)
                        win = yt.window(post - timedelta(minutes=15 + e["phase"]),
                                        post - timedelta(minutes=e["phase"]) + timedelta(minutes=args.tol_min))
                        judged = youtube_watch.judge(win, card)
                        rows = [{"race_id": e["race_id"], "source": "youtube", "phase": e["phase"],
                                 "updated_at": now.isoformat(timespec="seconds"), **j} for j in judged]
                        print(f"{tag} youtube 窓{len(win)}件 印: { {r['horsenum']: r['flag'] for r in rows} or 'なし' }")
                        if not dry:
                            bbs_watch.write_sheet(pd.DataFrame(rows, columns=bbs_watch.FIELDS), [e["race_id"]], "youtube")
                    except Exception as ex:  # noqa: BLE001
                        print(f"{tag} youtube 例外: {type(ex).__name__}: {str(ex)[:120]}")
            time.sleep(15)
    finally:
        if yt:
            yt.stop.set()
        if driver:
            driver.quit()
    print("終了")


if __name__ == "__main__":
    main()
