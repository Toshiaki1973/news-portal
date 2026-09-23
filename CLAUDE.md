# news-portal

一般ニュース・ゲームニュース（一般/インディー）・IR情報（大手ゲーム会社）・映画・YouTube急上昇（通常/ショート）・東京都/大阪府のイベント情報を1画面にまとめたポータルサイト。映画とYouTubeは1カラムにまとめて表示。PCは5カラム横並びで各カラム独立スクロール、スマホはタブ切り替え。GitHub Pagesで公開し、GitHub Actionsで1時間ごとに再生成する。

## 実行方法

```bash
pip install -r requirements.txt
python fetch_data.py            # output/index.html を生成
python fetch_data.py --open     # 生成後にブラウザで開く
```

## 必要な環境変数

- `YOUTUBE_API_KEY` — YouTube急上昇取得用（未設定でも他セクションは動く。YouTube欄だけ空になる）
- `GAMEMAKERS_CALENDAR_API_KEY` — GameMakersイベントカレンダー取得用のGoogle Calendar APIキー（未設定でも他セクションは動く。イベントカレンダー欄だけ空になる）。値自体はgamemakers.jp側の公開フロントエンドJSに埋め込まれているものだが、リポジトリに平文で置かないよう環境変数経由にしている

## データソース

- 一般ニュース: NHKニュース、Yahoo!ニューストピックス、CNN.co.jp（海外ニュース）、AI Watch（RSS）
  + プロ野球順位表（セ・パ）、J1順位表: どちらも公式RSS/APIが無いためYahoo!スポーツ（baseball.yahoo.co.jp / soccer.yahoo.co.jp）をHTMLスクレイピング。Yahoo!ニューストピックスの直下に配置
  + Mリーグ（麻雀）順位表: m-league.jpのトップページに埋め込まれた順位表をHTMLスクレイピング。J1順位表の直下に配置
  + スポーツニュース: Yahoo!ニュース スポーツカテゴリ（RSS）。順位表の下に配置
- ゲームニュース: GameMakers（開発者向け）、4Gamer（一般）、AUTOMATON（インディー中心）（RSS）
  + GameMakersイベントカレンダー（gamemakers.jp/event/、カンファレンス・展示会のみ）: Google Calendar APIから直接取得（APIキーは`GAMEMAKERS_CALENDAR_API_KEY`環境変数）。今日から31日分、summary末尾の【ジャンル】タグで絞り込み
- 映画: eiga.com（公式RSS/APIが無いためHTMLスクレイピング。program/eiga-movie-infoと同じ正規表現実装）
  + 動画配信ランキング（Amazon Prime Video/Netflix/U-NEXT、eiga.com/streaming/配下）: 各ページに埋め込まれたschema.orgのJSON-LD(ItemList)から取得。こちらは正規表現ではなくJSON解析
- はちま起稿: RSS
- YouTube: 公式トレンドチャート（`chart=mostPopular`）を取得し、動画時間3分1秒以下を「ショート」とみなして通常動画と振り分け（program/youtube-rankingと同じ基準）
- イベント: ウォーカープラス（walkerplus.com）の都道府県別イベント一覧ページ（公式RSS/APIが無いためHTMLスクレイピング）。東京都・大阪府それぞれ開催日が近い順で上位10件
  + SPICE（spice.eplus.jp、イープラス運営）の音楽・イベント・スポーツRSS（公式配信、`spice-api.eplus.jp/rss/articles/{1,5,7}/latest.xml`）
  + J-WAVE TOKIO HOT 100（公式RSS/APIが無いためHTMLスクレイピング、ページはEUC-JP）上位10件。公式の「サブスクで聴く」ボタンはclickfuse経由でApple Music固定リダイレクトのため、代わりに曲名+アーティスト名でSpotify検索結果へのリンクを自前で組み立てている

## デザイン

- 背景は薄いピンク、リンク項目の間に薄い罫線（toshi指定）
- PC（900px以上）: `.portal`をCSS Gridで5カラム、各`.column`が`overflow-y:auto`で独立スクロール
- スマホ（900px未満）: 上部タブバーでカラムを切り替え表示（JSで`.active`クラス付け替え）
- スライドショーモード: ヘッダーの「▶ スライドショー」ボタンで全画面表示に切り替え、全カラム・全グループの見出しを1件ずつ自動送りする。スマホ充電中やEcho Show等での「ながら見」用途。通常4.5秒間隔だが、順位表・ランキング・Steam商品名・TOKIO HOT 100など名詞情報だけのグループ（`build_slideshow_items()`の`SLIDESHOW_FAST_GROUP_KEYWORDS`で判定）は1.5秒間隔。スライドショー中は5分おきに自動で`location.reload()`し（ページ自体は1時間おきの再生成のため）、`sessionStorage`でスライドショー中フラグを持たせてリロード後も自動再開する
  - 手動操作: 画面を横方向に3分割し、左1/3タップ/クリックで前へ、右1/3タップ/クリックで次へ、中央タップ/クリックで終了（通常表示に戻る）。PCは←→キーでも前後移動可能。手動操作すると自動送りタイマーはその時点でリセットされる

## GitHub Pagesでの公開

- `output/`は`.gitignore`対象（Actionsが実行のたびに生成し、`actions/upload-pages-artifact`でそのままデプロイするためリポジトリにはコミットしない）
- ワークフロー: `.github/workflows/deploy.yml`。毎時16分（toshi指定。news-digestのSlack投稿を止める代わりにこのページで確認する運用に切替。当初03分だったが一度もschedule起動が確認できず、動作確認をすぐ取れるよう直近の分に変更）+ `workflow_dispatch`（手動実行）+ masterへのpushをトリガーに、`fetch_data.py`実行→`output/`をPagesにデプロイ
- リポジトリSecrets `YOUTUBE_API_KEY`・`GAMEMAKERS_CALENDAR_API_KEY`の設定と、Settings > Pages > Source を「GitHub Actions」にする作業が別途必要

## 既知の制約

- [[project-eiga-movie-info]]と同じくeiga.comのHTML構造が変わると映画欄が壊れる可能性がある
- はちま起稿・AUTOMATON等の非公式/個人メディアRSSは配信停止・URL変更のリスクがある
- ウォーカープラスも公式APIではなくHTML構造依存のスクレイピングのため、サイト側の構造変更でイベント欄が壊れる可能性がある
- GameMakersイベントカレンダーのAPIキーはサイト側の公開JSから抜き出したもので、非公式利用。キー失効やsummaryの【ジャンル】タグ表記変更で欄が壊れる可能性がある。ローカル実行時は`GAMEMAKERS_CALENDAR_API_KEY`を環境変数に設定しないとこの欄だけ空になる
- プロ野球・J1順位表もYahoo!スポーツのHTML構造依存のスクレイピングのため、サイト側の構造変更で壊れる可能性がある
- Mリーグ順位表もm-league.jpのトップページのHTML構造依存のスクレイピングのため、サイト側の構造変更で壊れる可能性がある
- J-WAVE TOKIO HOT 100もHTML構造依存のスクレイピングのため壊れる可能性がある。またSpotifyリンクは検索結果への遷移であり、公式が紐付けた正確な楽曲ページへの直リンクではない（曲名・アーティスト名の表記揺れで違う曲がヒットする可能性がある）
- `get_with_retry()`は接続タイムアウトの場合、初回に限り1回だけリトライする（GitHub Actions環境からGameMakers RSSへの接続が一時的にタイムアウトした事例への対応）。2回目もタイムアウトした場合や429/5xx以外のエラーはリトライせずそのままスキップ扱いになる
