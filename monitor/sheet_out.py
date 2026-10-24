"""スプレッドシート出力。認証情報は環境変数(GitHub Secrets)から読む。コードに鍵・シートIDは書かない。
  GOOGLE_SERVICE_ACCOUNT_JSON : サービスアカウント鍵のJSON文字列
  SHEET_ID                    : 出力先スプレッドシートのID
どちらかが無ければ書き込まず False を返す(=ドライラン扱い)。"""
import json
import os

import pandas as pd


def configured():
    return bool(os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON") and os.environ.get("SHEET_ID"))


def merge_write(df, race_ids, source, fields, tab):
    """(race_id, source)単位のread-merge-write。他レース・他ソースの行は残す。"""
    import gspread
    gc = gspread.service_account_from_dict(json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]))
    sh = gc.open_by_key(os.environ["SHEET_ID"])
    try:
        ws = sh.worksheet(tab)
        old = pd.DataFrame(ws.get_all_records())
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=tab, rows=500, cols=len(fields))
        old = pd.DataFrame(columns=fields)
    if not old.empty:
        old["race_id"] = old["race_id"].astype(str)
        if "source" not in old.columns:
            old["source"] = "netkeiba"
        old["source"] = old["source"].replace("", "netkeiba")
        old = old[~(old["race_id"].isin(set(race_ids)) & (old["source"] == source))]
    merged = pd.concat([old, df], ignore_index=True).reindex(columns=fields, fill_value="")
    ws.clear()
    ws.update([fields] + merged.astype(str).values.tolist(), "A1")
