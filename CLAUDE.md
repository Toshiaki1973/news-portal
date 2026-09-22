# news-portal

一般ニュース・ゲームニュース（一般/インディー）・映画・はちま起稿・YouTube急上昇（通常/ショート）を1画面にまとめたポータルサイト。PCは5カラム横並びで各カラム独立スクロール、スマホはタブ切り替え。GitHub Pagesで公開し、GitHub Actionsで1時間ごとに再生成する。

## 実行方法

```bash
pip install -r requirements.txt
python fetch_data.py            # output/index.html を生成
python fetch_data.py --open     # 生成後にブラウザで開く
```

## 必要な環境変数

- `YOUTUBE_API_KEY` — YouTube急上昇取得用（未設定でも他セクションは動く。YouTube欄だけ空になる）

## データソース

- 一般ニュース: NHKニュース、Yahoo!ニューストピックス、AI Watch（RSS）
- ゲームニュース: 4Gamer（一般）、AUTOMATON（インディー中心）（RSS）
- 映画: eiga.com（公式RSS/APIが無いためHTMLスクレイピング。program/eiga-movie-infoと同じ正規表現実装）
- はちま起稿: RSS
- YouTube: 公式トレンドチャート（`chart=mostPopular`）を取得し、動画時間3分1秒以下を「ショート」とみなして通常動画と振り分け（program/youtube-rankingと同じ基準）

## デザイン

- 背景は薄いピンク、リンク項目の間に薄い罫線（toshi指定）
- PC（900px以上）: `.portal`をCSS Gridで5カラム、各`.column`が`overflow-y:auto`で独立スクロール
- スマホ（900px未満）: 上部タブバーでカラムを切り替え表示（JSで`.active`クラス付け替え）

## GitHub Pagesでの公開

- `output/`は`.gitignore`対象（Actionsが実行のたびに生成し、`actions/upload-pages-artifact`でそのままデプロイするためリポジトリにはコミットしない）
- ワークフロー: `.github/workflows/deploy.yml`。毎時16分（toshi指定。news-digestのSlack投稿を止める代わりにこのページで確認する運用に切替。当初03分だったが一度もschedule起動が確認できず、動作確認をすぐ取れるよう直近の分に変更）+ `workflow_dispatch`（手動実行）+ masterへのpushをトリガーに、`fetch_data.py`実行→`output/`をPagesにデプロイ
- リポジトリSecrets `YOUTUBE_API_KEY`の設定と、Settings > Pages > Source を「GitHub Actions」にする作業が別途必要

## 既知の制約

- [[project-eiga-movie-info]]と同じくeiga.comのHTML構造が変わると映画欄が壊れる可能性がある
- はちま起稿・AUTOMATON等の非公式/個人メディアRSSは配信停止・URL変更のリスクがある
