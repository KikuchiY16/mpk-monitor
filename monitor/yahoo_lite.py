"""Yahoo!スポーツ競馬の軽量パーサ(開催日程・出馬表)。標準ライブラリ+bs4のみ。
live_predict/common.py(molpk_Racevision_AI)から、監視に必要な部分だけを切り出したもの。"""
import re
import urllib.request
from datetime import datetime

import bs4

USER_AGENT = "Mozilla/5.0"


def fetch_html(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="replace")


def get_daycodes_for_date(target_date):
    url = f"https://sports.yahoo.co.jp/keiba/schedule/monthly/?year={target_date.year}&month={target_date.month}"
    soup = bs4.BeautifulSoup(fetch_html(url), "html.parser")
    out, current = [], None
    for cell in soup.select("td.hr-tableSchedule__data--date"):
        m = re.match(r"(\d+)日", cell.get_text(" ", strip=True))
        if m:
            current = datetime(target_date.year, target_date.month, int(m.group(1))).date()
        link = cell.select_one("a")
        if link and current == target_date:
            m2 = re.search(r"/keiba/race/list/(\d{8})", link.get("href", "").replace("\\", "/"))
            if m2:
                out.append({"daycode": m2.group(1), "label": link.get_text(strip=True)})
    return out


def parse_day_schedule(daycode):
    soup = bs4.BeautifulSoup(fetch_html(f"https://sports.yahoo.co.jp/keiba/race/list/{daycode}"), "html.parser")
    out = []
    for cell in soup.select("td.hr-tableSchedule__data--date"):
        m = re.match(r"(\d+)R\s*(\d{1,2}:\d{2})", cell.get_text(" ", strip=True))
        if m:
            out.append((int(m.group(1)), m.group(2)))
    return out


def build_schedule(target_date):
    """schedule_today.json 相当: [{race_id, label, racenum, date, post_dt}]"""
    plan = []
    for e in get_daycodes_for_date(target_date):
        for racenum, hhmm in parse_day_schedule(e["daycode"]):
            post = datetime.combine(target_date, datetime.strptime(hhmm, "%H:%M").time())
            plan.append({"race_id": f"{e['daycode']}{racenum:02d}", "label": e["label"], "racenum": racenum,
                         "date": target_date.isoformat(), "post_dt": post.isoformat()})
    return plan


def yahoo_to_netkeiba_race_id(race_id_yahoo, race_date):
    """Yahoo(YY+場+回+日+R=10桁) -> netkeiba(YYYY+場+回+日+R=12桁)"""
    y, place, kai, day, rn = race_id_yahoo[0:2], race_id_yahoo[2:4], race_id_yahoo[4:6], race_id_yahoo[6:8], race_id_yahoo[8:10]
    return f"{race_date.year:04d}{place}{kai}{day}{rn}"


def fetch_racecard(race_id, entry):
    """flag_danger/predict_race が読む形式の出走表(umaban/horse/jockey)を出馬表ページから作る。"""
    soup = bs4.BeautifulSoup(fetch_html(f"https://sports.yahoo.co.jp/keiba/race/denma/{race_id}"), "html.parser")
    table = soup.select_one("table")
    entries = []
    for tr in (table.select("tr.hr-table__row") if table else []):
        nums = tr.select("td.hr-table__data--number")
        names = tr.select("td.hr-table__data--name")
        if not nums or len(names) < 2:
            continue
        h, j = names[0].select_one("a"), names[1].select_one("a")
        if h and j:
            # 騎手名は「三浦 皇成」のように空白が入る。チャットでは空白なしなので除去(苗字照合のため)
            entries.append({"umaban": int(nums[-1].get_text(strip=True)), "horse": h.get_text(strip=True),
                            "jockey": re.sub(r"[\s　]+", "", j.get_text(strip=True))})
    return {"race_id": race_id, "date": entry["date"], "track": entry.get("label", ""),
            "race_number": entry.get("racenum"), "entries": entries}
