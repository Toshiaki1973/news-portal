"""ニュースポータル用データ収集 + 静的HTML生成。

1時間ごとの実行を想定。RSS/APIから各カテゴリの最新トピックを取得し、
そのままブラウザで開ける単一HTMLファイル(output/index.html)を生成する。
PCでは5カラム（一般ニュース/ゲームニュース/IR情報/映画・YouTube/イベント）を
横に並べて各カラム独立スクロール、スマホはタブ切り替え。

必要な環境変数:
- YOUTUBE_API_KEY（YouTube急上昇取得用。無ければYouTube欄は空になる）
"""
import argparse
import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

JST = timezone(timedelta(hours=9))
HEADERS = {"User-Agent": "Mozilla/5.0 (news-portal)"}
ARTICLES_PER_FEED = 10

RSS_FEEDS = {
    "news": [
        {"label": "NHKニュース", "url": "https://www.nhk.or.jp/rss/news/cat0.xml"},
        {"label": "Yahoo!ニューストピックス", "url": "https://news.yahoo.co.jp/rss/topics/top-picks.xml"},
        {"label": "CNN.co.jp（海外）", "url": "https://feeds.cnn.co.jp/rss/cnn/cnn.rdf"},
        {"label": "AI Watch", "url": "https://ai.watch.impress.co.jp/data/rss/1.0/aiw/feed.rdf"},
        {"label": "ギズモード・ジャパン", "url": "https://www.gizmodo.jp/index.xml"},
    ],
    "game": [
        {"label": "GameMakers", "url": "https://gamemakers.jp/category/news/feed/"},
        {"label": "4Gamer（一般）", "url": "https://www.4gamer.net/rss/index.xml"},
        {"label": "AUTOMATON（インディー中心）", "url": "https://automaton-media.com/feed/"},
        {"label": "gamebiz（業界）", "url": "https://gamebiz.jp/feed.rss"},
        {"label": "IGDA日本", "url": "https://igda.jp/feed/"},
        {"label": "Kotaku（海外）", "url": "https://kotaku.com/feed"},
    ],
    "hachima": [
        {"label": "はちま起稿", "url": "http://blog.esuteru.com/index.rdf"},
    ],
    "sports_news": [
        {"label": "Yahoo!スポーツ", "url": "https://news.yahoo.co.jp/rss/categories/sports.xml"},
    ],
    "spice": [
        {"label": "SPICE 音楽", "url": "http://spice-api.eplus.jp/rss/articles/1/latest.xml?encoded=1"},
        {"label": "SPICE イベント", "url": "http://spice-api.eplus.jp/rss/articles/5/latest.xml?encoded=1"},
        {"label": "SPICE スポーツ", "url": "http://spice-api.eplus.jp/rss/articles/7/latest.xml?encoded=1"},
    ],
}

# eiga.comには公式RSS/APIが無いためHTMLスクレイピング（program/eiga-movie-infoと同じ実装）
EIGA_UPCOMING_URL = "https://eiga.com/upcoming/"
EIGA_RANKING_URL = "https://eiga.com/ranking/access/"
EIGA_MOVIE_URL = "https://eiga.com/movie/{id}/"
EIGA_DATE_HEADER_RE = re.compile(r'<span class="icon calendar">([^<]+)</span>（(.)）(?:公開・配信開始|公開)\s*</h2>')
EIGA_UPCOMING_MOVIE_RE = re.compile(
    r'<h3 class="title">\s*<a href="/movie/(\d+)/">([^<]+)</a>.*?<p class="txt">([^<]*)</p>', re.S)
EIGA_RANKING_MOVIE_RE = re.compile(
    r'class="rank-circle[^"]*">(\d+)</span>.*?href="/movie/(\d+)/".*?rating-star small val\d+">([\d.]+)</p>'
    r'.*?<h2 class="title">\s*<a href="/movie/\d+/">([^<]+)</a>', re.S)
EIGA_UPCOMING_LIMIT = 10
EIGA_RANKING_TOP_N = 10

# 配信ランキング: 上記の今週公開/アクセスランキングとは違い、ページ内にschema.orgの
# JSON-LD(ItemList)が埋め込まれているため正規表現でなくそちらをパースする。
EIGA_STREAMING_SERVICES = [
    {"label": "Amazon Prime Video", "url": "https://eiga.com/streaming/amazon/"},
    {"label": "Netflix", "url": "https://eiga.com/streaming/netflix/"},
    {"label": "U-NEXT", "url": "https://eiga.com/streaming/unext/"},
]
EIGA_STREAMING_JSONLD_RE = re.compile(r'<script type="application/ld\+json">(\[.*?\])</script>', re.S)
EIGA_STREAMING_TOP_N = 10

# プロ野球順位表・J1順位表: どちらも公式RSS/APIが無いためYahoo!スポーツをHTMLスクレイピング。
NPB_STANDINGS_URL = "https://baseball.yahoo.co.jp/npb/standings/"
NPB_STANDINGS_ROW_RE = re.compile(
    r'<td class="bb-rankTable__data bb-rankTable__data--rank">([^<]+)</td>\s*'
    r'<td class="bb-rankTable__data bb-rankTable__data--team">\s*'
    r'<a href="[^"]*" class="[^"]*">([^<]+)</a>.*?'
    r'<td class="bb-rankTable__data">\d+</td>\s*'
    r'<td class="bb-rankTable__data">(\d+)</td>\s*'
    r'<td class="bb-rankTable__data">(\d+)</td>\s*'
    r'<td class="bb-rankTable__data">(\d+)</td>\s*'
    r'<td class="bb-rankTable__data">([\d.]+)</td>', re.S)
NPB_TEAMS_PER_LEAGUE = 6  # ページ先頭からセ・リーグ6球団→パ・リーグ6球団の順で並ぶ

J1_STANDINGS_URL = "https://soccer.yahoo.co.jp/jleague/category/j1/standings"
J1_STANDINGS_ROW_RE = re.compile(
    r'<span class="sc-tableValue__rank">([^<]+)</span>.*?'
    r'<a class="sc-tableValue__team"[^>]*>\s*<span[^>]*></span>\s*</a>\s*'
    r'<a class="sc-tableValue__team"[^>]*>([^<]+)</a>\s*'
    r'</td>\s*<td class="sc-tableValue__data">(\d+)</td>', re.S)

# Mリーグ（麻雀）: 公式RSS/APIが無いためHTMLスクレイピング。トップページに順位表が
# 静的HTMLで埋め込まれている。
MLEAGUE_URL = "https://m-league.jp/"
MLEAGUE_RANK_RE = re.compile(
    r'<div class="p-ranking__rank-number is-rank\d+">(\d+)</div>.*?'
    r'<div class="p-ranking__team-name">([^<]+)</div>.*?'
    r'<div class="p-ranking__current-point">([^<]+)</div>', re.S)

STEAM_FEATURED_URL = "https://store.steampowered.com/api/featuredcategories"
STEAM_APP_URL = "https://store.steampowered.com/app/{id}/"
STEAM_ITEMS_PER_GROUP = 10

# 東海道新幹線運行状況: 公式サイト(traininfo.jr-central.co.jp)が内部で読みに行っている
# JSONを直接取得する。表示用メッセージのコード→文言変換テーブルまでは追いきれないため、
# 「情報あり件数」「影響を受けている列車数」から簡易な状態表示に留める。
SHINKANSEN_STATUS_URL = "https://traininfo.jr-central.co.jp/shinkansen/var/train_info/service_status.json"
SHINKANSEN_SUSPENSION_URL = "https://traininfo.jr-central.co.jp/shinkansen/var/train_info/suspension_info.json"
SHINKANSEN_INFO_LINK = "https://traininfo.jr-central.co.jp/shinkansen/sp/ja/index.html"

WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
WEATHER_CITIES = [
    {"label": "東京", "lat": 35.6762, "lon": 139.6503, "link": "https://weather.yahoo.co.jp/weather/jp/13/4410.html"},
    {"label": "大阪", "lat": 34.6937, "lon": 135.5023, "link": "https://weather.yahoo.co.jp/weather/jp/27/6200.html"},
]
WEATHER_CODE_JA = {
    0: "快晴", 1: "晴れ", 2: "晴れ時々曇り", 3: "曇り",
    45: "霧", 48: "霧（霜）",
    51: "弱い霧雨", 53: "霧雨", 55: "強い霧雨",
    61: "弱い雨", 63: "雨", 65: "強い雨",
    71: "弱い雪", 73: "雪", 75: "強い雪",
    80: "にわか雨", 81: "にわか雨", 82: "激しいにわか雨",
    95: "雷雨", 96: "雷雨（ひょう）", 99: "雷雨（激しいひょう）",
}

# 株価: Yahoo Financeのグローバル向けchart API（無料・キー不要）。表示リンクは日本語版Yahoo!ファイナンス。
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{code}.T"
YAHOO_QUOTE_URL = "https://finance.yahoo.co.jp/quote/{code}.T"

# IR情報: 各社サイトの実装がバラバラなため、会社ごとに取得方法を変えている。
# - rss_filter: 汎用RSS/ニュースフィードから、リンクに特定文字列を含むものだけをIR情報として抽出
# - rss_direct: IR専用のRSS/カテゴリフィードをそのまま使う
# - bandainamco_archive: /ir/配下はbot対策で403になるため、プレスリリース一覧(/releases/)の
#   月別アーカイブページを新しい月から辿り、data-category="irinfo"の項目だけ拾う
# - konami_newsroom: IRニュースページ自体はJS描画のSPAで静的取得不可だが、実体を書き出す
#   newsRoom.php（JSファイルの中にJSON埋め込み）から直接IRカテゴリの項目を抽出する
IR_COMPANIES = [
    {
        "label": "任天堂", "code": "7974",
        "source": {"type": "rss_filter", "url": "https://www.nintendo.co.jp/news/whatsnew.xml", "must_contain": "/ir/"},
    },
    {
        "label": "カプコン", "code": "9697",
        # news.xml(全社共通フィード)はIR項目のtitleが「プレスリリース 20XX年3月期」の
        # 定型文で全件同じになるため使わず、IRニュース一覧ページを直接スクレイピングする
        "source": {"type": "capcom_ir"},
    },
    {
        "label": "セガサミーHD", "code": "6460",
        "source": {"type": "rss_direct", "url": "https://www.segasammy.co.jp/ja/release/category/ir/feed/"},
    },
    {
        "label": "バンダイナムコHD", "code": "7832",
        "source": {"type": "bandainamco_archive"},
    },
    {
        "label": "コナミグループ", "code": "9766",
        "source": {"type": "konami_newsroom"},
    },
    {
        "label": "スクウェア・エニックスHD", "code": "9684",
        "source": {"type": "sqex_ir"},
    },
]
IR_ARTICLES_PER_COMPANY = 10

CAPCOM_IR_URL = "https://www.capcom.co.jp/ir/news"
CAPCOM_IR_ITEM_RE = re.compile(
    r'<li data-category="[^"]*"><a href="([^"]+)">.*?'
    r'<div class="date">([^<]+)</div>.*?'
    r'<div class="lead">(.*?)</div>', re.S)

BANDAINAMCO_ARCHIVE_XML = "https://www.bandainamco.co.jp/releases/archives.xml"
BANDAINAMCO_MONTH_RE = re.compile(r'<date-group-month[^>]*url="([^"]+)"')
BANDAINAMCO_ITEM_RE = re.compile(
    r'<li class="news-list__item" data-category="([^"]+)"[^>]*>.*?'
    r'<time class="news-list__date" datetime="([^"]+)">.*?'
    r'<a href="([^"]+)"[^>]*>([^<]+)<', re.S)
BANDAINAMCO_MONTHS_TO_SCAN = 6  # 直近何ヶ月分のアーカイブページを遡ってIR項目を探すか

KONAMI_NEWSROOM_URL = "https://www.konami.com/js/common/newsRoom.php?lang=ja&newsType=newsList"
KONAMI_BASE_URL = "https://www.konami.com"

# ニュース一覧は静的HTMLに全カテゴリ分が出力されており、newsBoxIconの文字列が
# 「企業」のものがIR/コーポレート関連(決算・人事・配当など)に該当する
SQEX_IR_URL = "https://www.hd.square-enix.com/jpn/news/"
SQEX_BASE_URL = "https://www.hd.square-enix.com"
SQEX_ITEM_RE = re.compile(
    r'<div class="newsBox">\s*<div class="newsBoxIcon">([^<]+)</div>\s*'
    r'<div class="newsBoxCont">\s*<p class="newsBoxInfo"><span class="date">([^<]+)</span>\s*'
    r'<span class="cat">([^<]*)</span>\s*</p>\s*'
    r'<a class="newsBoxTxt" href="([^"]+)"[^>]*>\s*<span>([^<]+)</span></a>', re.S)

YOUTUBE_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
YOUTUBE_TRENDING_FETCH_COUNT = 30  # 急上昇チャートから取得して通常/ショートに振り分ける件数
YOUTUBE_TRENDING_TOP_N = 10        # 振り分け後、各カテゴリで表示する件数
SHORTS_MAX_SECONDS = 181           # これ以下の長さは「ショート」とみなす近似値（3分1秒。program/youtube-rankingと同じ基準）
DURATION_RE = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")

# イベント情報: ウォーカープラスには公式RSS/APIが無いためHTMLスクレイピング。
# 都道府県別のイベント一覧ページ（開催日が近い順）を上位N件だけ取得する。
WALKERPLUS_BASE_URL = "https://www.walkerplus.com"
EVENT_AREAS = [
    {"label": "東京都", "path": "/event_list/ar0313/"},
    {"label": "大阪府", "path": "/event_list/ar0727/"},
]
EVENT_ITEM_RE = re.compile(
    r'<a href="(/event/[^"]+)">\s*<span class="m-mainlist-item__ttl">([^<]+)</span>\s*</a>\s*'
    r'<p class="m-mainlist-item-event__period">\s*(?:<span[^>]*>[^<]*</span>\s*)?([^<]+?)\s*</p>', re.S)
EVENT_ITEMS_PER_AREA = 10

# J-WAVE TOKIO HOT 100: 公式RSS/APIが無いためHTMLスクレイピング（ページはEUC-JP）。
# 「サブスクで聴く」ボタンはclickfuse経由でApple Music固定にリダイレクトされる仕様なので、
# 代わりに曲名+アーティスト名でSpotifyの検索結果に飛ぶリンクを自前で組み立てる。
JWAVE_CHART_URL = "https://www.j-wave.co.jp/original/tokiohot100/chart/main.htm"
JWAVE_ITEM_RE = re.compile(
    r'<div class="song_rank">(\d+)</div>.*?'
    r'<div class="song_title">(?:<a[^>]*>)?([^<]+?)(?:</a>)?</div>\s*'
    r'<div class="song_artist">([^<]+)</div>', re.S)
JWAVE_TOP_N = 10
SPOTIFY_SEARCH_URL = "https://open.spotify.com/search/{query}"

# GameMakers（gamemakers.jp/event/）のイベントカレンダーはGoogleカレンダーで管理されており、
# サイトのフロントエンドJS(app.bundle.js)に埋め込まれた公開APIキーでGoogle Calendar APIから
# 直接取得できる。summary末尾の【ジャンル】タグ（例:【カンファレンス】）で種別を絞り込む。
# APIキー自体はgamemakers.jp側の公開JSに埋め込まれたものだが、リポジトリに平文で
# 置かないよう環境変数(GAMEMAKERS_CALENDAR_API_KEY)経由で渡す。
GAMEMAKERS_CALENDAR_ID = "n7a7nl4hv6k6b8gdjbjvakj5m4@group.calendar.google.com"
GAMEMAKERS_EVENTS_URL = f"https://www.googleapis.com/calendar/v3/calendars/{quote(GAMEMAKERS_CALENDAR_ID, safe='')}/events"
GAMEMAKERS_EVENT_DAYS = 31  # 今日から何日分を取得するか
GAMEMAKERS_EVENT_GENRES = ("カンファレンス", "展示会")  # toshi指定。他に勉強会・コンテストもある
GAMEMAKERS_EVENT_GENRE_RE = re.compile(r"【([^】]+)】\s*$")


def parse_duration_seconds(iso_duration):
    m = DURATION_RE.match(iso_duration or "")
    if not m:
        return 0
    h, mnt, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mnt * 60 + s

OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "output", "index.html")


def get_with_retry(url, max_retries=3):
    for attempt in range(max_retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
        except requests.exceptions.Timeout:
            if attempt == 0:
                print("  タイムアウトのため1回だけリトライ")
                continue
            raise
        if r.status_code == 429 or r.status_code >= 500:
            wait = (2 ** attempt) * 3
            print(f"  {r.status_code}のため{wait}秒待機してリトライ")
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r
    r.raise_for_status()
    return r


def parse_feed(xml_bytes, max_items=ARTICLES_PER_FEED):
    root = ET.fromstring(xml_bytes)
    articles = []
    for item in root.findall(".//{*}item"):
        title_el = item.find("{*}title")
        link_el = item.find("{*}link")
        title = (title_el.text or "").strip() if title_el is not None else ""
        link = (link_el.text or "").strip() if link_el is not None else ""
        if title and link:
            articles.append({"title": title, "link": link})
        if len(articles) >= max_items:
            break
    return articles


def fetch_rss_group(feeds):
    groups = []
    for feed in feeds:
        try:
            r = get_with_retry(feed["url"])
            articles = parse_feed(r.content)
        except (requests.RequestException, ET.ParseError) as e:
            print(f"  skip {feed['label']}: {e}")
            articles = []
        print(f"  {feed['label']}: {len(articles)}件")
        groups.append({"label": feed["label"], "articles": articles})
    return groups


def fetch_movies():
    try:
        html_text = get_with_retry(EIGA_UPCOMING_URL).text
    except requests.RequestException as e:
        print(f"  skip 映画(今週公開): {e}")
        html_text = ""

    upcoming = []
    headers_found = list(EIGA_DATE_HEADER_RE.finditer(html_text))
    for i, h in enumerate(headers_found):
        chunk_start = h.end()
        chunk_end = headers_found[i + 1].start() if i + 1 < len(headers_found) else len(html_text)
        chunk = html_text[chunk_start:chunk_end]
        date_label = f"{h.group(1)}({h.group(2)})"
        for movie_id, title, _synopsis in EIGA_UPCOMING_MOVIE_RE.findall(chunk):
            upcoming.append({
                "title": f"{title}（{date_label}）",
                "link": EIGA_MOVIE_URL.format(id=movie_id),
            })
    upcoming = upcoming[:EIGA_UPCOMING_LIMIT]
    print(f"  映画(今週公開): {len(upcoming)}件")

    try:
        ranking_html = get_with_retry(EIGA_RANKING_URL).text
    except requests.RequestException as e:
        print(f"  skip 映画(ランキング): {e}")
        ranking_html = ""

    ranking = []
    for rank, movie_id, rating, title in EIGA_RANKING_MOVIE_RE.findall(ranking_html):
        ranking.append({
            "title": f"{rank}位 {title}（評価{rating}）",
            "link": EIGA_MOVIE_URL.format(id=movie_id),
        })
    ranking = ranking[:EIGA_RANKING_TOP_N]
    print(f"  映画(ランキング): {len(ranking)}件")

    return {"upcoming": upcoming, "ranking": ranking}


def fetch_streaming_rankings():
    """Amazon Prime Video/Netflix/U-NEXTの動画配信ランキングを、各ページに埋め込まれた
    schema.orgのJSON-LD(ItemList)から取得する。"""
    groups = []
    for service in EIGA_STREAMING_SERVICES:
        try:
            html_text = get_with_retry(service["url"]).text
        except requests.RequestException as e:
            print(f"  skip 配信ランキング({service['label']}): {e}")
            groups.append({"label": service["label"], "articles": []})
            continue

        articles = []
        m = EIGA_STREAMING_JSONLD_RE.search(html_text)
        if m:
            try:
                data = json.loads(m.group(1))
            except ValueError as e:
                print(f"  skip 配信ランキング({service['label']}): JSON解析失敗 {e}")
                data = []
            itemlist = next((d for d in data if d.get("@type") == "ItemList"), None)
            if itemlist:
                for item in itemlist.get("itemListElement", [])[:EIGA_STREAMING_TOP_N]:
                    articles.append({
                        "title": f"{item['position']}位 {item['name']}",
                        "link": item["url"],
                    })
        print(f"  配信ランキング({service['label']}): {len(articles)}件")
        groups.append({"label": service["label"], "articles": articles})
    return groups


def fetch_youtube_trending():
    """YouTube公式トレンドチャートを取得し、動画時間で通常動画/ショートに振り分ける。"""
    api_key = os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        print("  YOUTUBE_API_KEY未設定のためスキップ")
        return [], []
    params = {
        "part": "snippet,statistics,contentDetails",
        "chart": "mostPopular",
        "regionCode": "JP",
        "maxResults": YOUTUBE_TRENDING_FETCH_COUNT,
        "key": api_key,
    }
    try:
        r = requests.get(YOUTUBE_VIDEOS_URL, params=params, timeout=20)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"  skip YouTube急上昇: {e}")
        return [], []

    regular, shorts = [], []
    for item in r.json().get("items", []):
        views = int(item["statistics"].get("viewCount", 0))
        duration_sec = parse_duration_seconds(item["contentDetails"]["duration"])
        article = {
            "title": f"{item['snippet']['title']}（{views:,}回）",
            "link": f"https://www.youtube.com/watch?v={item['id']}",
            "views": views,
        }
        (shorts if duration_sec <= SHORTS_MAX_SECONDS else regular).append(article)

    # mostPopularは既に急上昇順だが、通常/ショートに分けた後は念のため再生数順に並べ直す
    regular.sort(key=lambda v: v["views"], reverse=True)
    shorts.sort(key=lambda v: v["views"], reverse=True)
    print(f"  YouTube急上昇: 通常{len(regular)}件 / ショート{len(shorts)}件")
    return regular[:YOUTUBE_TRENDING_TOP_N], shorts[:YOUTUBE_TRENDING_TOP_N]


def fetch_events():
    """ウォーカープラスの都道府県別イベント一覧（開催日が近い順）から上位N件を取得する。"""
    groups = []
    for area in EVENT_AREAS:
        try:
            html_text = get_with_retry(WALKERPLUS_BASE_URL + area["path"]).text
        except requests.RequestException as e:
            print(f"  skip イベント({area['label']}): {e}")
            groups.append({"label": area["label"], "articles": []})
            continue
        articles = [
            {"title": f"{html.unescape(title)}（{period.strip()}）", "link": WALKERPLUS_BASE_URL + link}
            for link, title, period in EVENT_ITEM_RE.findall(html_text)[:EVENT_ITEMS_PER_AREA]
        ]
        print(f"  イベント({area['label']}): {len(articles)}件")
        groups.append({"label": area["label"], "articles": articles})
    return groups


def fetch_jwave_chart():
    """J-WAVE TOKIO HOT 100を取得し、曲名+アーティスト名でSpotify検索リンクを組み立てる。"""
    try:
        r = get_with_retry(JWAVE_CHART_URL)
        r.encoding = "euc-jp"
    except requests.RequestException as e:
        print(f"  skip J-WAVE TOKIO HOT 100: {e}")
        return []

    articles = []
    for rank, title, artist in JWAVE_ITEM_RE.findall(r.text)[:JWAVE_TOP_N]:
        title = html.unescape(title.strip())
        artist = html.unescape(artist.strip())
        link = SPOTIFY_SEARCH_URL.format(query=quote(f"{title} {artist}"))
        articles.append({"title": f"{rank}位 {title} / {artist}", "link": link})
    print(f"  J-WAVE TOKIO HOT 100: {len(articles)}件")
    return articles


def fetch_gamemakers_events():
    """GameMakersイベントカレンダーから今日〜GAMEMAKERS_EVENT_DAYS日後までの、
    カンファレンス・展示会だけを抽出する。"""
    api_key = os.environ.get("GAMEMAKERS_CALENDAR_API_KEY")
    if not api_key:
        print("  GAMEMAKERS_CALENDAR_API_KEY未設定のためスキップ")
        return []
    now = datetime.now(JST)
    params = {
        "key": api_key,
        "timeMin": now.strftime("%Y-%m-%dT00:00:00+09:00"),
        "timeMax": (now + timedelta(days=GAMEMAKERS_EVENT_DAYS)).strftime("%Y-%m-%dT00:00:00+09:00"),
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": 250,
    }
    try:
        r = requests.get(GAMEMAKERS_EVENTS_URL, params=params, timeout=20)
        r.raise_for_status()
        items = r.json().get("items", [])
    except (requests.RequestException, ValueError) as e:
        print(f"  skip GameMakersイベント: {e}")
        return []

    articles = []
    for it in items:
        summary = it.get("summary", "")
        m = GAMEMAKERS_EVENT_GENRE_RE.search(summary)
        if not m or m.group(1) not in GAMEMAKERS_EVENT_GENRES:
            continue
        start = it.get("start", {})
        date_str = start.get("date") or (start.get("dateTime") or "")[:10]
        title = f"{summary}（{date_str}）" if date_str else summary
        articles.append({"title": title, "link": it.get("htmlLink", "")})
    print(f"  GameMakersイベント: {len(articles)}件")
    return articles


def fetch_npb_standings():
    """プロ野球(セ・パ)の順位表をYahoo!スポーツから取得する。優勝が決まった球団は
    順位欄が「優勝」という文字列になるため、その場合は1位として扱う。"""
    try:
        html_text = get_with_retry(NPB_STANDINGS_URL).text
    except requests.RequestException as e:
        print(f"  skip プロ野球順位表: {e}")
        return {"central": [], "pacific": []}

    def to_article(rank_text, team, win, lose, draw, pct):
        rank = "1" if rank_text == "優勝" else rank_text
        return {
            "title": f"{rank}位 {team}（{win}勝{lose}敗{draw}分, 勝率{pct}）",
            "link": NPB_STANDINGS_URL,
        }

    rows = NPB_STANDINGS_ROW_RE.findall(html_text)
    central = [to_article(*r) for r in rows[:NPB_TEAMS_PER_LEAGUE]]
    pacific = [to_article(*r) for r in rows[NPB_TEAMS_PER_LEAGUE:NPB_TEAMS_PER_LEAGUE * 2]]
    print(f"  プロ野球順位表: セ・リーグ{len(central)}件 / パ・リーグ{len(pacific)}件")
    return {"central": central, "pacific": pacific}


def fetch_j1_standings():
    """J1リーグの順位表をYahoo!スポーツから取得する。"""
    try:
        html_text = get_with_retry(J1_STANDINGS_URL).text
    except requests.RequestException as e:
        print(f"  skip J1順位表: {e}")
        return []
    articles = [
        {"title": f"{rank}位 {team}（勝点{points}）", "link": J1_STANDINGS_URL}
        for rank, team, points in J1_STANDINGS_ROW_RE.findall(html_text)
    ]
    print(f"  J1順位表: {len(articles)}件")
    return articles


def fetch_mleague_standings():
    """Mリーグ（麻雀）の順位表を公式サイトのトップページから取得する。"""
    try:
        html_text = get_with_retry(MLEAGUE_URL).text
    except requests.RequestException as e:
        print(f"  skip Mリーグ順位表: {e}")
        return []
    articles = [
        {"title": f"{rank}位 {team}（{points}）", "link": MLEAGUE_URL}
        for rank, team, points in MLEAGUE_RANK_RE.findall(html_text)
    ]
    print(f"  Mリーグ順位表: {len(articles)}件")
    return articles


def fetch_shinkansen_status():
    """東海道新幹線の運行状況(JR東海公式サイトが内部で読む生JSON)を取得する。"""
    try:
        status_data = requests.get(SHINKANSEN_STATUS_URL, headers=HEADERS, timeout=15).json()
        suspension_data = requests.get(SHINKANSEN_SUSPENSION_URL, headers=HEADERS, timeout=15).json()
    except (requests.RequestException, ValueError) as e:
        print(f"  skip 新幹線運行状況: {e}")
        return []

    status_items = status_data.get("serviceStatusInfo", {}).get("data", [])
    bounds = suspension_data.get("suspensionInfo", {}).get("bounds", {})
    affected_trains = sum(len(v) for v in bounds.values())

    if status_items:
        status_text = f"⚠️ 運行情報あり（{len(status_items)}件）"
    elif affected_trains:
        status_text = f"🔶 一部列車に遅延・部分運休の影響あり（{affected_trains}本）"
    else:
        status_text = "🟢 平常運転"

    print(f"  東海道新幹線: {status_text}")
    return [{"title": f"東海道新幹線 {status_text}（詳細はJR東海公式サイトで確認）", "link": SHINKANSEN_INFO_LINK}]


def fetch_weather():
    """Open-Meteo（無料・APIキー不要）で東京/大阪の今日・明日の天気を取得する。"""
    articles = []
    for city in WEATHER_CITIES:
        params = {
            "latitude": city["lat"],
            "longitude": city["lon"],
            "daily": "weather_code,temperature_2m_max,temperature_2m_min",
            "timezone": "Asia/Tokyo",
            "forecast_days": 2,
        }
        try:
            r = requests.get(WEATHER_URL, params=params, timeout=15)
            r.raise_for_status()
            daily = r.json()["daily"]
        except (requests.RequestException, KeyError, ValueError) as e:
            print(f"  skip 天気({city['label']}): {e}")
            continue

        day_labels = ["今日", "明日"]
        for i, label in enumerate(day_labels[:len(daily.get("time", []))]):
            code = daily["weather_code"][i]
            high = daily["temperature_2m_max"][i]
            low = daily["temperature_2m_min"][i]
            desc = WEATHER_CODE_JA.get(code, f"天気コード{code}")
            articles.append({
                "title": f"{city['label']}・{label} {desc} 最高{high:.0f}℃ / 最低{low:.0f}℃",
                "link": city["link"],
            })
    print(f"  天気: {len(articles)}件")
    return articles


def fetch_steam_highlights():
    """Steam公式の非公開だが広く使われているfeaturedcategories APIでセール/新作を取得する。"""
    try:
        r = requests.get(STEAM_FEATURED_URL, params={"cc": "jp", "l": "japanese"}, headers=HEADERS, timeout=20)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        print(f"  skip Steam: {e}")
        return {"specials": [], "new_releases": []}

    def to_articles(key):
        items = data.get(key, {}).get("items", [])[:STEAM_ITEMS_PER_GROUP]
        articles = []
        for it in items:
            name = it.get("name", "")
            discount = it.get("discount_percent", 0)
            final_price = it.get("final_price")
            price_str = f"¥{final_price // 100:,}" if isinstance(final_price, int) else ""
            title = f"{name}（-{discount}% {price_str}）" if discount else f"{name}（{price_str}）"
            articles.append({"title": title, "link": STEAM_APP_URL.format(id=it.get("id"))})
        return articles

    specials = to_articles("specials")
    new_releases = to_articles("new_releases")
    print(f"  Steamセール: {len(specials)}件 / 新作: {len(new_releases)}件")
    return {"specials": specials, "new_releases": new_releases}


def fetch_stock_quote(code):
    try:
        r = requests.get(YAHOO_CHART_URL.format(code=code), headers=HEADERS, timeout=15)
        r.raise_for_status()
        meta = r.json()["chart"]["result"][0]["meta"]
        price = meta.get("regularMarketPrice")
        prev_close = meta.get("chartPreviousClose")
        if price is None:
            return None
        if prev_close:
            diff = price - prev_close
            diff_str = f"（前日比{diff:+,.0f}円）"
        else:
            diff_str = ""
        return {
            "title": f"📈 現在値 {price:,.0f}円{diff_str}",
            "link": YAHOO_QUOTE_URL.format(code=code),
        }
    except (requests.RequestException, KeyError, IndexError, TypeError, ValueError) as e:
        print(f"  skip 株価({code}): {e}")
        return None


def fetch_capcom_ir():
    """IRニュース一覧ページを直接スクレイピングし、見出し(lead)をそのまま使う。"""
    try:
        r = get_with_retry(CAPCOM_IR_URL)
        r.encoding = "utf-8"
    except requests.RequestException as e:
        print(f"  skip カプコンIR: {e}")
        return []

    articles = []
    for link, date, lead in CAPCOM_IR_ITEM_RE.findall(r.text):
        title = re.sub(r"<[^>]+>", " ", lead)
        title = re.sub(r"\s+", " ", title).strip()
        date_clean = date.replace("年", "/").replace("月", "/").replace("日", "")
        articles.append({"title": f"{title}（{date_clean}）", "link": link})
        if len(articles) >= IR_ARTICLES_PER_COMPANY:
            break
    return articles


def fetch_bandainamco_ir():
    """/ir/配下は直接アクセスするとbot対策で弾かれるため、プレスリリース一覧
    (/releases/)の月別アーカイブを新しい月から遡り、経営情報(irinfo)カテゴリの
    項目だけを拾う。"""
    try:
        r = get_with_retry(BANDAINAMCO_ARCHIVE_XML)
        r.encoding = "utf-8"  # charsetヘッダが無くrequestsがISO-8859-1と誤検出するため明示指定
        month_urls = BANDAINAMCO_MONTH_RE.findall(r.text)[:BANDAINAMCO_MONTHS_TO_SCAN]
    except requests.RequestException as e:
        print(f"  skip バンダイナムコIR(archives): {e}")
        return []

    articles = []
    for month_url in month_urls:
        if len(articles) >= IR_ARTICLES_PER_COMPANY:
            break
        try:
            r = get_with_retry(month_url)
            r.encoding = "utf-8"
        except requests.RequestException as e:
            print(f"  skip バンダイナムコIR({month_url}): {e}")
            continue
        for category, date, link, title in BANDAINAMCO_ITEM_RE.findall(r.text):
            if category != "irinfo":
                continue
            articles.append({"title": f"{title.strip()}（{date}）", "link": link})
            if len(articles) >= IR_ARTICLES_PER_COMPANY:
                break
    return articles


def extract_balanced_json(text, start):
    """textのstart位置から始まるJSONオブジェクトを、波括弧の対応を数えて抜き出す。"""
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
    return None


def fetch_konami_ir():
    """IRニュースページ自体はJS描画のSPAで静的には取得できないが、ページが
    document.writeで読み込むnewsRoom.php（実体はJSファイルだがJSONデータを
    含む）から、IR系カテゴリ(ir-*)の項目を直接抜き出す。"""
    try:
        r = get_with_retry(KONAMI_NEWSROOM_URL)
    except requests.RequestException as e:
        print(f"  skip コナミIR: {e}")
        return []

    marker = "newsJson = "
    idx = r.text.find(marker)
    if idx == -1:
        print("  skip コナミIR: newsJsonが見つからない")
        return []
    blob = extract_balanced_json(r.text, idx + len(marker))
    if blob is None:
        print("  skip コナミIR: JSON抽出失敗")
        return []
    try:
        data = json.loads(blob)
    except ValueError as e:
        print(f"  skip コナミIR: JSON解析失敗 {e}")
        return []

    ir_items = []
    for _year, items in data.items():
        for it in items:
            category = it.get("newsCategory", "").split("/")[0]
            if category.startswith("ir-"):
                ir_items.append(it)
    ir_items.sort(key=lambda x: x.get("newsDate", ""), reverse=True)

    articles = []
    for it in ir_items[:IR_ARTICLES_PER_COMPANY]:
        link = it.get("newsLink", "").strip()
        if link.startswith("/"):
            link = KONAMI_BASE_URL + link
        articles.append({"title": it.get("newsTitle", "").strip(), "link": link})
    return articles


def fetch_sqex_ir():
    """ニュース一覧ページは全カテゴリが静的HTMLに出力されており、
    newsBoxIconが「企業」の項目がIR/コーポレート関連にあたる。"""
    try:
        r = get_with_retry(SQEX_IR_URL)
        r.encoding = "utf-8"
    except requests.RequestException as e:
        print(f"  skip スクエニIR: {e}")
        return []

    articles = []
    for icon, date, _cat, link, title in SQEX_ITEM_RE.findall(r.text):
        if icon != "企業":
            continue
        if link.startswith("/"):
            link = SQEX_BASE_URL + link
        articles.append({"title": f"{title.strip()}（{date.replace('.', '/')}）", "link": link})
        if len(articles) >= IR_ARTICLES_PER_COMPANY:
            break
    return articles


def fetch_ir_news(source):
    t = source["type"]
    if t == "capcom_ir":
        return fetch_capcom_ir()
    if t == "bandainamco_archive":
        return fetch_bandainamco_ir()
    if t == "konami_newsroom":
        return fetch_konami_ir()
    if t == "sqex_ir":
        return fetch_sqex_ir()
    try:
        r = get_with_retry(source["url"])
        max_items = 100 if t == "rss_filter" else IR_ARTICLES_PER_COMPANY
        articles = parse_feed(r.content, max_items=max_items)
    except (requests.RequestException, ET.ParseError) as e:
        print(f"  skip IR({source['url']}): {e}")
        return []
    if t == "rss_filter":
        articles = [a for a in articles if source["must_contain"] in a["link"]]
        date_re = source.get("date_from_link_re")
        if date_re:
            pattern = re.compile(date_re)
            for a in articles:
                m = pattern.search(a["link"])
                if m:
                    yy, mm, dd = m.groups()
                    a["title"] = f"{a['title']}（20{yy}/{mm}/{dd}）"
    return articles[:IR_ARTICLES_PER_COMPANY]


def fetch_ir_info():
    """ゲーム大手5社の株価(Yahoo!ファイナンス)と最新IR情報をまとめる。"""
    groups = []
    for company in IR_COMPANIES:
        quote = fetch_stock_quote(company["code"])
        ir_articles = fetch_ir_news(company["source"])
        print(f"  {company['label']}: 株価{'取得' if quote else '失敗'} / IR{len(ir_articles)}件")
        groups.append({
            "label": company["label"],
            "articles": ([quote] if quote else []) + ir_articles,
        })
    return groups


def build_sections():
    print("東海道新幹線運行状況を取得中...")
    shinkansen = fetch_shinkansen_status()
    print("天気を取得中...")
    weather = fetch_weather()
    print("一般ニュースを取得中...")
    news = fetch_rss_group(RSS_FEEDS["news"])
    print("プロ野球順位表を取得中...")
    npb = fetch_npb_standings()
    print("J1順位表を取得中...")
    j1 = fetch_j1_standings()
    print("Mリーグ順位表を取得中...")
    mleague = fetch_mleague_standings()
    print("スポーツニュースを取得中...")
    sports_news = fetch_rss_group(RSS_FEEDS["sports_news"])
    print("ゲームニュースを取得中...")
    game = fetch_rss_group(RSS_FEEDS["game"])
    print("GameMakersイベントカレンダーを取得中...")
    gamemakers_events = fetch_gamemakers_events()
    print("Steam情報を取得中...")
    steam = fetch_steam_highlights()
    print("映画情報を取得中...")
    movies = fetch_movies()
    print("動画配信ランキングを取得中...")
    streaming_groups = fetch_streaming_rankings()
    print("はちま起稿を取得中...")
    hachima = fetch_rss_group(RSS_FEEDS["hachima"])
    print("IR情報を取得中...")
    ir_groups = fetch_ir_info()
    print("YouTube急上昇を取得中...")
    youtube_regular, youtube_shorts = fetch_youtube_trending()
    print("イベント情報を取得中...")
    event_groups = fetch_events() + fetch_rss_group(RSS_FEEDS["spice"])
    print("J-WAVE TOKIO HOT 100を取得中...")
    jwave_chart = fetch_jwave_chart()
    event_groups.append({"label": "J-WAVE TOKIO HOT 100", "articles": jwave_chart})

    news_groups = [
        {"label": "東海道新幹線 運行状況", "articles": shinkansen},
        {"label": "今日・明日の天気", "articles": weather},
        news[0],  # NHKニュース
        news[1],  # Yahoo!ニューストピックス
    ] + news[2:] + [  # news[2:] = CNN.co.jp（海外）、AI Watch
        {"label": "プロ野球順位表：セ・リーグ", "articles": npb["central"]},
        {"label": "プロ野球順位表：パ・リーグ", "articles": npb["pacific"]},
        {"label": "J1順位表", "articles": j1},
        {"label": "Mリーグ順位表", "articles": mleague},
    ] + sports_news  # スポーツ関連は末尾
    game_groups = [
        game[0],  # GameMakers
        {"label": "GameMakers イベントカレンダー（カンファレンス・展示会）", "articles": gamemakers_events},
        game[1],  # 4Gamer
        {"label": "Steamセール", "articles": steam["specials"]},
        {"label": "Steam新作", "articles": steam["new_releases"]},
        game[2],  # AUTOMATON
    ] + hachima  # はちま起稿はゲームニュース欄の最後に表示

    return [
        {"id": "news", "label": "📰 一般ニュース", "groups": news_groups},
        {"id": "game", "label": "🎮 ゲームニュース", "groups": game_groups},
        {"id": "ir", "label": "💹 IR情報（大手ゲーム会社）", "groups": ir_groups},
        {"id": "movie-youtube", "label": "🎬 映画・YouTube", "groups": [
            {"label": "映画：今週公開", "articles": movies["upcoming"]},
            {"label": "映画：アクセスランキング", "articles": movies["ranking"]},
        ] + [
            {"label": f"配信ランキング：{g['label']}", "articles": g["articles"]} for g in streaming_groups
        ] + [
            {"label": "YouTube急上昇：通常動画", "articles": youtube_regular},
            {"label": "YouTube急上昇：ショート（推定）", "articles": youtube_shorts},
        ]},
        {"id": "event", "label": "🎪 イベント（東京・大阪）", "groups": event_groups},
    ]


SLIDESHOW_NORMAL_MS = 4500
SLIDESHOW_FAST_MS = 1500  # 順位・ランキング・商品名など、読むのに時間がかからない名詞情報向け
SLIDESHOW_FAST_GROUP_KEYWORDS = ("順位", "ランキング", "Steamセール", "Steam新作", "TOKIO HOT 100")


def build_slideshow_items(sections):
    """スライドショー用に、全カラム・全グループの記事を1件ずつのフラットな配列にする。
    グループ名に順位表・ランキング系のキーワードが含まれる場合は表示時間を短くする。"""
    items = []
    for s in sections:
        for g in s["groups"]:
            is_fast = any(kw in g["label"] for kw in SLIDESHOW_FAST_GROUP_KEYWORDS)
            duration = SLIDESHOW_FAST_MS if is_fast else SLIDESHOW_NORMAL_MS
            for a in g["articles"]:
                items.append({
                    "category": s["label"], "group": g["label"], "title": a["title"], "duration": duration,
                })
    return items


def render_articles(articles):
    if not articles:
        return '<p class="empty">取得できませんでした</p>'
    items = "\n".join(
        f'<li><a href="{html.escape(a["link"])}" target="_blank" rel="noopener">{html.escape(a["title"])}</a></li>'
        for a in articles
    )
    return f"<ul>{items}</ul>"


def render_section(section):
    groups_html = "\n".join(
        f'<h3>{html.escape(g["label"])}</h3>\n{render_articles(g["articles"])}'
        for g in section["groups"]
    )
    return f'''
    <section class="column" data-id="{section["id"]}">
      <h2>{html.escape(section["label"])}</h2>
      {groups_html}
    </section>'''


def generate_html(sections):
    updated_at = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    tabs_html = "\n".join(
        f'<button class="tab" data-target="{s["id"]}">{html.escape(s["label"])}</button>'
        for s in sections
    )
    sections_html = "\n".join(render_section(s) for s in sections)
    slideshow_json = json.dumps(build_slideshow_items(sections), ensure_ascii=False).replace("</", "<\\/")

    return f'''<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ニュースポータル</title>
<style>
  :root {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    font-family: -apple-system, "Segoe UI", "Hiragino Kaku Gothic ProN", sans-serif;
    background: #fdf2f5;
    color: #1a1a1a;
  }}
  header {{
    padding: 10px 16px;
    background: #20232a;
    color: #fff;
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: 4px;
  }}
  header h1 {{ font-size: 1.1rem; margin: 0; }}
  header .updated {{ font-size: 0.8rem; opacity: 0.8; }}
  header .header-left {{ display: flex; align-items: center; gap: 10px; }}
  header .slideshow-btn {{
    background: #3a3f4b;
    color: #fff;
    border: 1px solid #565c6a;
    border-radius: 4px;
    padding: 4px 10px;
    font-size: 0.8rem;
    cursor: pointer;
  }}
  header .slideshow-btn:hover {{ background: #4a505e; }}

  .slideshow {{
    display: none;
    position: fixed;
    inset: 0;
    background: #14151a;
    color: #fff;
    z-index: 1000;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    text-align: center;
    padding: 24px;
    cursor: pointer;
  }}
  .slideshow.active {{ display: flex; }}
  .slideshow .ss-category {{ font-size: 1.1rem; opacity: 0.7; margin-bottom: 8px; }}
  .slideshow .ss-group {{ font-size: 1.3rem; opacity: 0.85; margin-bottom: 24px; }}
  .slideshow .ss-title {{ font-size: 2rem; line-height: 1.5; max-width: 90vw; font-weight: bold; }}
  .slideshow .ss-hint {{ position: absolute; bottom: 24px; font-size: 0.8rem; opacity: 0.5; }}
  .tabbar {{ display: none; }}
  .portal {{
    display: grid;
    grid-template-columns: repeat(5, 1fr);
    height: calc(100vh - 52px);
  }}
  .column {{
    overflow-y: auto;
    padding: 12px 14px;
    border-right: 1px solid #f0d3dc;
    background: #fdf2f5;
  }}
  .column:last-child {{ border-right: none; }}
  .column h2 {{
    font-size: 1rem;
    margin: 0 0 10px;
    position: sticky;
    top: -12px;
    background: #fdf2f5;
    padding: 6px 0;
  }}
  .column h3 {{ font-size: 0.85rem; color: #666; margin: 14px 0 6px; }}
  .column ul {{ list-style: none; margin: 0; padding: 0; }}
  .column li {{
    padding: 8px 0;
    line-height: 1.4;
    border-bottom: 1px solid #eeced5;
  }}
  .column li:last-child {{ border-bottom: none; }}
  .column a {{ color: #1a4fd6; text-decoration: none; font-size: 0.9rem; }}
  .column a:hover {{ text-decoration: underline; }}
  .empty {{ color: #999; font-size: 0.85rem; }}

  @media (prefers-color-scheme: dark) {{
    body {{ background: #16171a; color: #eee; }}
    .column {{ background: #1e2025; border-right-color: #333; }}
    .column h2 {{ background: #1e2025; }}
    .column h3 {{ color: #aaa; }}
    .column a {{ color: #7aa2ff; }}
  }}

  @media (max-width: 900px) {{
    .tabbar {{
      display: flex;
      overflow-x: auto;
      background: #2a2d35;
    }}
    .tab {{
      flex: 0 0 auto;
      padding: 10px 14px;
      background: none;
      border: none;
      color: #ccc;
      font-size: 0.85rem;
      white-space: nowrap;
      cursor: pointer;
    }}
    .tab.active {{ color: #fff; border-bottom: 2px solid #7aa2ff; }}
    .portal {{
      display: block;
      height: calc(100vh - 52px - 42px);
    }}
    .column {{ display: none; height: 100%; border-right: none; }}
    .column.active {{ display: block; }}
  }}
</style>
</head>
<body>
<header>
  <div class="header-left">
    <h1>ニュースポータル</h1>
    <button class="slideshow-btn" id="slideshowStart">▶ スライドショー</button>
  </div>
  <span class="updated">更新: {updated_at}</span>
</header>
<nav class="tabbar">
{tabs_html}
</nav>
<main class="portal">
{sections_html}
</main>
<div class="slideshow" id="slideshow">
  <div class="ss-category" id="ssCategory"></div>
  <div class="ss-group" id="ssGroup"></div>
  <div class="ss-title" id="ssTitle"></div>
  <div class="ss-hint">← 前へ ｜ 中央タップ/クリックで終了 ｜ 次へ →　（PCは←→キーでも操作可）</div>
</div>
<script type="application/json" id="slideshowData">{slideshow_json}</script>
<script>
  const tabs = document.querySelectorAll(".tab");
  const columns = document.querySelectorAll(".column");
  function activate(id) {{
    tabs.forEach(t => t.classList.toggle("active", t.dataset.target === id));
    columns.forEach(c => c.classList.toggle("active", c.dataset.id === id));
  }}
  tabs.forEach(t => t.addEventListener("click", () => activate(t.dataset.target)));
  if (tabs.length) activate(tabs[0].dataset.target);

  const slideshowItems = JSON.parse(document.getElementById("slideshowData").textContent);
  const slideshowEl = document.getElementById("slideshow");
  const ssCategory = document.getElementById("ssCategory");
  const ssGroup = document.getElementById("ssGroup");
  const ssTitle = document.getElementById("ssTitle");
  const SS_RELOAD_MS = 5 * 60 * 1000;  // ページは1時間おきに再生成されるため、5分おきに再読込して最新の見出しに切り替える
  let ssIndex = 0;
  let ssTimer = null;
  let ssReloadTimer = null;

  function currentItem() {{
    const len = slideshowItems.length;
    return slideshowItems[((ssIndex % len) + len) % len];
  }}

  function renderSlide() {{
    if (slideshowItems.length === 0) {{
      ssCategory.textContent = "";
      ssGroup.textContent = "";
      ssTitle.textContent = "表示できる見出しがありません";
      return;
    }}
    const item = currentItem();
    ssCategory.textContent = item.category;
    ssGroup.textContent = item.group;
    ssTitle.textContent = item.title;
  }}

  function scheduleNext() {{
    if (slideshowItems.length === 0) return;
    clearTimeout(ssTimer);
    ssTimer = setTimeout(() => {{
      ssIndex++;
      renderSlide();
      scheduleNext();
    }}, currentItem().duration);
  }}

  function goNext() {{
    ssIndex++;
    renderSlide();
    scheduleNext();
  }}

  function goPrev() {{
    ssIndex--;
    renderSlide();
    scheduleNext();
  }}

  function startSlideshow() {{
    slideshowEl.classList.add("active");
    ssIndex = 0;
    renderSlide();
    scheduleNext();
    ssReloadTimer = setInterval(() => location.reload(), SS_RELOAD_MS);
    try {{ sessionStorage.setItem("slideshowMode", "1"); }} catch (e) {{}}
  }}

  function stopSlideshow() {{
    slideshowEl.classList.remove("active");
    clearTimeout(ssTimer);
    clearInterval(ssReloadTimer);
    try {{ sessionStorage.removeItem("slideshowMode"); }} catch (e) {{}}
  }}

  document.getElementById("slideshowStart").addEventListener("click", startSlideshow);

  slideshowEl.addEventListener("click", (e) => {{
    const x = e.clientX / window.innerWidth;
    if (x < 1 / 3) {{
      goPrev();
    }} else if (x > 2 / 3) {{
      goNext();
    }} else {{
      stopSlideshow();
    }}
  }});

  document.addEventListener("keydown", (e) => {{
    if (!slideshowEl.classList.contains("active")) return;
    if (e.key === "ArrowRight") goNext();
    else if (e.key === "ArrowLeft") goPrev();
  }});

  try {{
    if (sessionStorage.getItem("slideshowMode") === "1") startSlideshow();
  }} catch (e) {{}}
</script>
</body>
</html>
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--open", action="store_true", help="生成後にブラウザで開く")
    args = parser.parse_args()

    sections = build_sections()
    output = generate_html(sections)

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(output)
    print(f"\n生成しました: {OUTPUT_PATH}")

    if args.open:
        os.startfile(OUTPUT_PATH)


if __name__ == "__main__":
    main()
