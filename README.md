# mpk — 競馬 掲示板/YouTubeチャット監視 (GitHub Actions)

Netkeiba掲示板と YouTube ライブチャットから「暴れている」「イレ込み」「発汗」「落鉄」などのコメントを拾い、
各レースの**発走20分前(パドック終わり頃)**と**5分前(返し馬後・締切前)**に馬ごとの印(🔴🟠🟡)を
スプレッドシートの `bbs_flags` タブへ書く。予測ボード側は (race_id, horsenum) で結合して表示する。

## 構成
- `monitor/run_day.py` … 1開催日分を1ジョブでループ(cronは遅延するため、起動後は自分で時刻を見て動く)
- `monitor/bbs_watch.py` … Netkeiba掲示板(JS描画のためSelenium)。否定表現(「暴れていない」)は除外、6時間より古いコメントは無視
- `monitor/youtube_watch.py`, `yt_chat.py`, `flag_danger.py` … YouTubeチャット(pytchatは壊れていたため自前クライアント)。ユニーク投稿者3人以上で🔴、2人で🟠
- `monitor/yahoo_lite.py` … 開催日程・出馬表(Yahoo!スポーツ競馬)
- `monitor/sheet_out.py` … シート出力。**認証は環境変数のみ**(コードに鍵・IDを書かない)

## 最初にやること
1. **Actions → probe → Run workflow** : ランナーのIPから Yahoo / Netkeiba / YouTube に繋がるか確認。
   `[NG]` が出た先は、その経路ではブロックされている(Netkeibaが通らなければActionsでは不可)
2. 通ったら **Actions → monitor → Run workflow**(`dry_run=true`、当日の開催日)で通しのドライラン
3. シートに書くなら Settings → Secrets and variables → Actions に登録:
   - Secret `GOOGLE_SERVICE_ACCOUNT_JSON` : サービスアカウント鍵のJSON全文(シートを編集者として共有しておく)
   - Secret `SHEET_ID` : スプレッドシートID
   - (任意) Variable `YOUTUBE_VIDEO_ID` : 監視する配信の動画ID(配信ごとに更新。手動実行なら入力欄でも可)
   Secrets が無い間は書き込まずログだけ出す(ドライラン扱い)。
4. 定期実行(毎日 09:00 / 12:50 JST に起動、開催日でなければ数十秒で終了)は mainブランチで自動で有効。
   ランナーは1ジョブ6時間までなので、09:00起動の午前ジョブは14:30まで(`--until 14:30`)、
   12:50起動の午後ジョブは14:30まで待ってから最終レースまで(`--since 14:30`)担当する。
   午後ジョブを早めに起動するのは、cron の遅れ(数十分〜)で取りこぼさないため。

## 注意
- **公開リポジトリ**: Actionsのログも公開される。コメント本文はログに出さない設計(印と馬番のみ)。鍵は必ず Secrets に。
- アクセス制限: 1リクエスト間 1秒 + ブラウザ読み込み、1巡 最大150リクエスト、ブロック兆候が3回続いたらNetkeiba側は当日停止。
- 地方競馬は未対応(Yahoo日程がJRAのみ)。`python -m monitor.bbs_watch --nk_race_id <12桁> --host nar --dry_run` で単発の試験は可能。
- 印は「掲示板/チャットでそう言われている」という観測であり、実際の状態や着順の保証ではない。
- 未検証: Netkeiba掲示板のパーサ(Colabでの観測に基づく)を実ページで通しで動かした記録はまだ無い。probe → dry_run で確認すること。
