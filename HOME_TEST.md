# 自宅PCでの検証手順 (mpk monitor)

    cd <このフォルダ>
    pip install -r requirements.txt
    python -m monitor.probe                  # 接続確認: Yahoo / Netkeiba(出馬表・掲示板) / YouTube
    python -m monitor.run_day --help         # 当日の監視ループ (チェックポイント 20,5分前)

- Netkeiba が [OK] なら、シート書き込みを試す (環境変数 GOOGLE_SERVICE_ACCOUNT_JSON, SHEET_ID)。
- Chrome が入っていること (Selenium)。
- このPC(大学NW)では Netkeiba が接続リセットされた。YouTube は取得OK。
