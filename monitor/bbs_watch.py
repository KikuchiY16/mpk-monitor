"""
Netkeiba掲示板の監視(Selenium)。「暴れ」「イレ込み」「落鉄」等のコメントを拾って印を付ける。
run_day.py から使う。単体でも `python -m monitor.bbs_watch --nk_race_id <12桁> --host nar --debug_html` で試験できる。
掲示板はJS描画のためSelenium必須。コメント本文はログに出さない(公開リポジトリのActionsログ対策)。
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime as _dt, timedelta

import bs4
import pandas as pd
import requests

from monitor import yahoo_lite  # noqa: E402

WORK_DIR = os.environ.get("MONITOR_WORK_DIR", ".monitor_cache")  # 実行時の一時置き場(.gitignore済み)
SCHEDULE_PATH = os.path.join(WORK_DIR, "schedule_today.json")
CACHE_DIR = os.path.join(WORK_DIR, "bbs_cache")
STATE_PATH = os.path.join(CACHE_DIR, "bbs_state.json")
DEBUG_DIR = os.path.join(WORK_DIR, "bbs_debug_html")
OUT_CSV = os.path.join(WORK_DIR, "bbs_flags.csv")
SHEET_TAB = "bbs_flags"

SHUTUBA_URL = "https://{host}.netkeiba.com/race/shutuba.html?race_id={race_id}"  # host: race(JRA)/nar(地方)
HORSE_BOARD_URL = "https://db.netkeiba.com/horse/bbs/{horse_id}/"  # 2026-10-19 Colabで確認済み
RACE_BOARD_URL = "https://{host}.netkeiba.com/race/bbs.html?race_id={race_id}&rf=race_submenu"  # 同上

FIELDS = ["race_id", "source", "phase", "horsenum", "flag", "keywords", "n_hits", "latest_comment", "updated_at"]

# (印, 重み, 正規表現)。重いほど警戒。
KEYWORDS = [
    ("🔴", 3, r"暴れ|立ち上が|放馬|馬房.{0,3}(?:壊|暴)|疝痛|跛行|歩様.{0,3}(?:悪|乱|おかし)|故障|熱発|出走取消|競走除外"),
    ("🟠", 2, r"イレ込|入れ込|いれこ|チャカ(?:つ|ツ)|発汗|汗(?:をかい|びっしょり|だく)|落鉄|蹄鉄|カリカリ|うるさ|テンション.{0,3}高|口を割|引っかか|折り合.{0,4}(?:欠|不安)|ゲート.{0,4}(?:嫌|出遅|立ち上)"),
    ("🟡", 1, r"馬体.{0,3}(?:減|細)|細く見|気配.{0,3}(?:悪|いまいち|冴えない)|元気.{0,3}(?:ない|無い)|覇気.{0,3}(?:ない|無い)|やる気.{0,3}(?:ない|無い)|ガレ"),
]
# キーワード直後(または直前)の否定。例「暴れていない」「イレ込みなし」「落ち着いて」
NEG_AFTER = re.compile(r"^.{0,6}?(?:ていない|てない|なし|無し|ず|なかった|ません|ないよう|ない)")
NEG_BEFORE = re.compile(r"(?:全然|全く|特に|あまり|別に|ほとんど).{0,2}$")


def _classify(text):
    """コメント1件を判定。戻り値: [(印, 重み, ヒット語)]。否定が付いたヒットは除外。"""
    hits = []
    for mark, weight, pat in KEYWORDS:
        for m in re.finditer(pat, text):
            after = text[m.end():m.end() + 8]
            before = text[max(0, m.start() - 4):m.start()]
            if NEG_AFTER.match(after) or NEG_BEFORE.search(before):
                continue
            hits.append((mark, weight, m.group()))
    return hits


class BlockedError(Exception):
    pass


def make_driver():
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    o = Options()
    for arg in ("--headless=new", "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"):
        o.add_argument(arg)
    # 高速化: DOM構築完了で読み込み完了扱い(広告/画像待ちをしない) + 画像を読まない。
    # コメントはJSで#Comment_Listに入るので、読み込み後の待機はfetch_board_html側で行う。
    o.page_load_strategy = "eager"
    o.add_experimental_option("prefs", {"profile.managed_default_content_settings.images": 2})
    return webdriver.Chrome(options=o)


def fetch_board_html(driver, url, debug_name=None, save_debug=False, wait_sec=12):
    """掲示板はJS描画なので、#Comment_List に中身が入るまで待つ(空の掲示板は時間切れで空のまま返す)。"""
    from selenium.common.exceptions import TimeoutException, WebDriverException
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    try:
        driver.get(url)
        try:
            WebDriverWait(driver, wait_sec).until(
                lambda d: d.find_element(By.ID, "Comment_List").text.strip() != "")
        except (TimeoutException, WebDriverException):
            pass
        html = driver.page_source
    except WebDriverException as e:
        raise BlockedError(f"ブラウザ取得失敗: {type(e).__name__}: {str(e)[:100]}")
    if re.search(r"Access Denied|403 Forbidden|アクセスが制限|too many requests", html[:3000], re.I) or len(html) < 1500:
        raise BlockedError(f"ブロックの兆候(HTML長{len(html)}): {url}")
    if save_debug and debug_name:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        with open(os.path.join(DEBUG_DIR, debug_name), "w", encoding="utf-8") as f:
            f.write(html)
    return html


def fetch_shutuba(race_id_nk, session, save_debug=False, host="race"):
    try:
        res = session.get(SHUTUBA_URL.format(host=host, race_id=race_id_nk), headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    except (requests.ConnectionError, requests.Timeout) as e:
        raise BlockedError(f"接続失敗: {type(e).__name__}: {e}")
    if res.status_code in (403, 429, 500, 503):
        raise BlockedError(f"HTTP {res.status_code}: 出馬表")
    res.raise_for_status()
    res.encoding = res.apparent_encoding
    if save_debug:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        with open(os.path.join(DEBUG_DIR, f"shutuba_{race_id_nk}.html"), "w", encoding="utf-8") as f:
            f.write(res.text)
    return res.text


def _extract_horses(html):
    """出馬表HTMLから {馬番: {"id": horse_id, "name": 馬名}}。馬番セルが取れなければ
    出馬表の並び順(1始まり)で代用する(Colabで馬リンクの抽出自体は確認済み)。"""
    soup = bs4.BeautifulSoup(html, "html.parser")
    out, order = {}, []
    for tr in soup.select("tr"):
        a = tr.select_one("a[href*='db.netkeiba.com/horse/']")
        m = re.search(r"/horse/(\d{8,12})", a.get("href", "")) if a else None
        if not m or m.group(1) in order:
            continue
        order.append(m.group(1))
        num = None
        cell = tr.select_one("td[class*=Umaban]")
        if cell:
            mm = re.search(r"\d+", cell.get_text(strip=True))
            num = int(mm.group()) if mm else None
        out[len(order) if num is None else num] = {"id": m.group(1), "name": a.get_text(strip=True)}
    return out


_AGE_UNIT_MIN = {"秒": 1 / 60, "分": 1, "時間": 60, "日": 1440}
# 馬の掲示板は「8分前 0 非表示・報告」、レース掲示板は「2時間前 いいね! 0」(2026-10-19 Colab出力で確認)
_TAIL = re.compile(r"(\d+)\s*(秒|分|時間|日)前\s*(?:いいね!?\s*)?\d*\s*(?:非表示・報告)?\s*$")
_HEAD = re.compile(r"^.*?さん\s+\S+\s+(?:非表示・報告\s*)?(?:フォローする\s*)?")


def parse_comments(html):
    """#Comment_List を '[番号]' で分割。戻り値: [{"no", "text", "age_min"}]。
    age_min は「N分前」等の相対表記から(絶対日付表記など解釈不能ならNone=古い扱い)。"""
    soup = bs4.BeautifulSoup(html, "html.parser")
    box = soup.find(id="Comment_List")
    if not box:
        return []
    raw = " ".join(box.stripped_strings)
    parts = re.split(r"\[(\d+)\]", raw)  # [前置き, no, 本文, no, 本文, ...]
    out = []
    for i in range(1, len(parts) - 1, 2):
        seg = parts[i + 1].strip()
        age = None
        t = _TAIL.search(seg)
        if t:
            age = int(t.group(1)) * _AGE_UNIT_MIN[t.group(2)]
            seg = seg[:t.start()].strip()
        out.append({"no": parts[i], "text": _HEAD.sub("", seg, count=1).strip(), "age_min": age})
    return out


def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def select_due(schedule, checkpoints, done, tol_min, now=None):
    """発走N分前(N in checkpoints)を過ぎていて、まだ実行しておらず、tol_min分以内のものを返す。
    1分おきの繰り返しタスクから呼ばれる前提(取りこぼし防止のため許容幅tol_minを持つ)。"""
    now = now or _dt.now()
    out = []
    for e in schedule:
        post = _dt.fromisoformat(e["post_dt"])
        for n in checkpoints:
            t = post - timedelta(minutes=n)
            key = f"{e['race_id']}@{n}"
            if key not in done and t <= now < min(t + timedelta(minutes=tol_min), post):
                out.append({**e, "phase": n, "done_key": key})
    return out


class RunLock:
    """多重起動防止(1分おき起動で前の巡が長引いた場合に重ならないように)。10分超の古いロックは無効。"""
    path = os.path.join(CACHE_DIR, "run.lock")

    def __enter__(self):
        os.makedirs(CACHE_DIR, exist_ok=True)
        if os.path.exists(self.path) and time.time() - os.path.getmtime(self.path) > 600:
            os.remove(self.path)
        try:
            os.close(os.open(self.path, os.O_CREAT | os.O_EXCL))
        except FileExistsError:
            raise SystemExit("前回の巡がまだ実行中のためスキップ")
        return self

    def __exit__(self, *a):
        try:
            os.remove(self.path)
        except OSError:
            pass


def _ingest(st, comments, names, now, args):
    """未読コメントだけ判定して st["hits"] に積む。names={馬番: 馬名} があればレース掲示板扱いで、
    本文に馬名が出てくる馬へ帰属させる(戻り値: {馬番: [hit]})。無ければ単一板として st に積むだけ。"""
    known = set(st["ids"])
    new_hits = []
    for c in comments:
        if c["no"] in known:
            continue
        known.add(c["no"])
        if c["age_min"] is None or c["age_min"] > args.max_age_min:
            continue  # 古い/日時不明のコメントは印にしない
        hits = _classify(c["text"])
        if hits:
            best = max(hits, key=lambda h: h[1])
            new_hits.append({"no": c["no"], "text": c["text"][:200], "marks": [best[0]],
                             "words": [h[2] for h in hits], "weight": max(h[1] for h in hits),
                             "t": (now - timedelta(minutes=c["age_min"])).isoformat(timespec="seconds"),
                             "text_full": c["text"]})
    st["ids"] = sorted(known, key=int)
    return new_hits


def watch_race(entry, session, driver, state, args, budget):
    """1レース分を処理。戻り値: {馬番: [直近max_age_min以内のhit]}。"""
    rid = entry["race_id"]
    nk_id = entry.get("nk_race_id") or yahoo_lite.yahoo_to_netkeiba_race_id(
        rid, _dt.strptime(entry["date"], "%Y-%m-%d").date())
    host = args.host
    cache_path = os.path.join(CACHE_DIR, f"horses_{nk_id}.json")
    horses = {int(k): v for k, v in _load_json(cache_path, {}).items()}
    if not horses:
        budget.spend()
        horses = _extract_horses(fetch_shutuba(nk_id, session, args.debug_html, host))
        print(f"[{rid}] 出馬表: {len(horses)}頭")
        if horses:
            _save_json(cache_path, {str(k): v for k, v in horses.items()})
        time.sleep(args.interval)
    if not horses:
        return {}

    now = _dt.now()
    st_race = state.setdefault(nk_id, {})
    per_horse = {hn: st_race.setdefault(f"h{hn}", {"ids": [], "hits": []}) for hn in horses}
    rb = st_race.setdefault("race", {"ids": [], "hits": []})

    # 各馬の掲示板
    for hn, h in sorted(horses.items()):
        budget.spend()
        html = fetch_board_html(driver, HORSE_BOARD_URL.format(horse_id=h["id"]), f"horse_{h['id']}.html", args.debug_html)
        per_horse[hn]["hits"] += _ingest(per_horse[hn], parse_comments(html), None, now, args)
        time.sleep(args.interval)

    # レース掲示板: 本文に馬名が出る馬へ帰属
    budget.spend()
    html = fetch_board_html(driver, RACE_BOARD_URL.format(host=host, race_id=nk_id), f"race_{nk_id}.html", args.debug_html)
    for hit in _ingest(rb, parse_comments(html), None, now, args):
        for hn, h in horses.items():
            if len(h["name"]) >= 2 and h["name"] in hit["text_full"]:
                per_horse[hn]["hits"].append(hit)
    time.sleep(args.interval)

    cutoff = (now - timedelta(minutes=args.max_age_min)).isoformat(timespec="seconds")
    result = {}
    for hn, st in per_horse.items():
        st["hits"] = [x for x in st["hits"] if x["t"] >= cutoff]  # 古くなった印は落とす
        if st["hits"]:
            result[hn] = st["hits"]
    return result


class Budget:
    def __init__(self, limit):
        self.limit, self.used = limit, 0

    def spend(self):
        if self.used >= self.limit:
            raise BlockedError(f"1巡の最大リクエスト数({self.limit})に到達、打ち切り")
        self.used += 1


def to_rows(entry, per_horse, now_iso):
    rows = []
    for hn, hits in sorted(per_horse.items()):
        hits = sorted(hits, key=lambda h: h["t"])
        top = max(hits, key=lambda h: h["weight"])
        words = sorted({w for h in hits for w in h["words"]})
        rows.append({"race_id": entry["race_id"], "source": "netkeiba", "phase": entry.get("phase", ""), "horsenum": hn, "flag": top["marks"][0],
                     "keywords": "/".join(words), "n_hits": len(hits),
                     "latest_comment": hits[-1]["text"], "updated_at": now_iso})
    return rows


def write_sheet(df, race_ids, source="netkeiba"):
    """(race_id, source)単位のread-merge-write(他レース・他ソース(youtube等)の既存行は保持)。"""
    from monitor.sheet_out import merge_write
    merge_write(df, race_ids, source, FIELDS, SHEET_TAB)


def main():
    sys.stdout.reconfigure(encoding="utf-8")  # 印の絵文字がcp932コンソールで落ちるのを防ぐ
    ap = argparse.ArgumentParser()
    ap.add_argument("--race_id", default=None, help="Yahoo形式のrace_id。省略時はschedule_todayの窓内の全レース")
    ap.add_argument("--nk_race_id", default=None,
                    help="Netkeiba形式のrace_idを直接指定(スケジュール不要・シート書込なしの試験用。地方は--host nar)")
    ap.add_argument("--host", default="race", choices=["race", "nar"], help="race=JRA / nar=地方競馬")
    ap.add_argument("--checkpoints", default="20,5",
                    help="発走の何分前に確認するか(カンマ区切り)。既定: 20=パドック終わり頃, 5=返し馬後・締切前。"
                         "パドック終了時刻は場/レースで違うので実際に合わせて調整すること")
    ap.add_argument("--tol_min", type=int, default=4, help="チェックポイントを過ぎてから何分以内なら実行するか(取りこぼし許容)")
    ap.add_argument("--interval", type=float, default=1.0, help="リクエスト間の秒数(ブラウザ読み込み自体に1〜2秒かかるので実効間隔はこれより長い)")
    ap.add_argument("--max_requests", type=int, default=150, help="1巡の最大リクエスト数")
    ap.add_argument("--max_age_min", type=int, default=360, help="これより古いコメントは印にしない(分)")
    ap.add_argument("--dry_run", action="store_true", help="シートに書かない")
    ap.add_argument("--debug_html", action="store_true", help="生HTMLをbbs_debug_html/に保存")
    args = ap.parse_args()

    if args.nk_race_id:
        args.dry_run = True  # 試験用: 本番シートには書かない
        targets = [{"race_id": args.nk_race_id, "nk_race_id": args.nk_race_id}]
    else:
        schedule = _load_json(SCHEDULE_PATH, [])
        if not schedule:
            raise SystemExit(f"{SCHEDULE_PATH} が無い/空です。先に python -m monitor.run_day --make_schedule_only を実行してください。")
        if args.race_id:  # 手動指定は時刻に関係なく即実行
            targets = [e for e in schedule if e["race_id"] == args.race_id]
        else:
            done_keys = set(_load_json(STATE_PATH, {}).get("_done", []))
            cps = [int(x) for x in args.checkpoints.split(",") if x.strip()]
            targets = select_due(schedule, cps, done_keys, args.tol_min)
    if not targets:
        print("今回のチェックポイントに該当するレースなし。終了。")
        return

    lock = RunLock().__enter__()
    state = _load_json(STATE_PATH, {})
    session, budget = requests.Session(), Budget(args.max_requests)
    now_iso = _dt.now().isoformat(timespec="seconds")
    rows, done = [], []
    driver = None
    try:
        driver = make_driver()
        for entry in targets:
            per_horse = watch_race(entry, session, driver, state, args, budget)
            done.append(entry["race_id"])
            if entry.get("done_key"):
                state.setdefault("_done", []).append(entry["done_key"])
            rows += to_rows(entry, per_horse, now_iso)
            print(f"[{entry['race_id']}] 印あり: "
                  f"{ {hn: hits[-1]['marks'][0] for hn, hits in per_horse.items()} or 'なし'}")
    except BlockedError as e:
        print(f"[中断] ブロック/上限の兆候: {e}\n  → 取得済みレース分のみ反映して終了(再実行は時間を空けること)")
    finally:
        if driver:
            driver.quit()
        _save_json(STATE_PATH, state)
        lock.__exit__()

    df = pd.DataFrame(rows, columns=FIELDS)
    if not df.empty or done:
        df.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    if args.dry_run:
        print(df.to_string(index=False) if not df.empty else "(印なし)")
        print("(dry_run: シートには書き込んでいません)")
    elif done:
        write_sheet(df, done)
        print(f"{SHEET_TAB}タブ更新: {len(done)}レース, 印{len(df)}頭")


if __name__ == "__main__":
    main()
