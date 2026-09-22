"""ニュースポータル用データ収集 + 静的HTML生成。

1時間ごとの実行を想定。RSS/APIから各カテゴリの最新トピックを取得し、
そのままブラウザで開ける単一HTMLファイル(output/index.html)を生成する。
PCでは5カラムを横に並べて各カラム独立スクロール、スマホはタブ切り替え。

必要な環境変数:
- YOUTUBE_API_KEY（YouTube急上昇取得用。無ければYouTube欄は空になる）
"""
import argparse
import html
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

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
        {"label": "AI Watch", "url": "https://ai.watch.impress.co.jp/data/rss/1.0/aiw/feed.rdf"},
    ],
    "game": [
        {"label": "4Gamer（一般）", "url": "https://www.4gamer.net/rss/index.xml"},
        {"label": "AUTOMATON（インディー中心）", "url": "https://automaton-media.com/feed/"},
    ],
    "hachima": [
        {"label": "はちま起稿", "url": "http://blog.esuteru.com/index.rdf"},
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

STEAM_FEATURED_URL = "https://store.steampowered.com/api/featuredcategories"
STEAM_APP_URL = "https://store.steampowered.com/app/{id}/"
STEAM_ITEMS_PER_GROUP = 8

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

YOUTUBE_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
YOUTUBE_TRENDING_FETCH_COUNT = 30  # 急上昇チャートから取得して通常/ショートに振り分ける件数
YOUTUBE_TRENDING_TOP_N = 10        # 振り分け後、各カテゴリで表示する件数
SHORTS_MAX_SECONDS = 181           # これ以下の長さは「ショート」とみなす近似値（3分1秒。program/youtube-rankingと同じ基準）
DURATION_RE = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def parse_duration_seconds(iso_duration):
    m = DURATION_RE.match(iso_duration or "")
    if not m:
        return 0
    h, mnt, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mnt * 60 + s

OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "output", "index.html")


def get_with_retry(url, max_retries=3):
    for attempt in range(max_retries):
        r = requests.get(url, headers=HEADERS, timeout=20)
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


def build_sections():
    print("天気を取得中...")
    weather = fetch_weather()
    print("一般ニュースを取得中...")
    news = fetch_rss_group(RSS_FEEDS["news"])
    print("ゲームニュースを取得中...")
    game = fetch_rss_group(RSS_FEEDS["game"])
    print("Steam情報を取得中...")
    steam = fetch_steam_highlights()
    print("映画情報を取得中...")
    movies = fetch_movies()
    print("はちま起稿を取得中...")
    hachima = fetch_rss_group(RSS_FEEDS["hachima"])
    print("YouTube急上昇を取得中...")
    youtube_regular, youtube_shorts = fetch_youtube_trending()

    news_groups = [{"label": "今日・明日の天気", "articles": weather}] + news
    game_groups = [
        game[0],
        {"label": "Steamセール", "articles": steam["specials"]},
        {"label": "Steam新作", "articles": steam["new_releases"]},
        game[1],
    ]

    return [
        {"id": "news", "label": "📰 一般ニュース", "groups": news_groups},
        {"id": "game", "label": "🎮 ゲームニュース", "groups": game_groups},
        {"id": "hachima", "label": "🗨️ はちま起稿", "groups": hachima},
        {"id": "movie", "label": "🎬 映画", "groups": [
            {"label": "今週公開", "articles": movies["upcoming"]},
            {"label": "アクセスランキング", "articles": movies["ranking"]},
        ]},
        {"id": "youtube", "label": "▶️ YouTube急上昇（日本）", "groups": [
            {"label": "通常動画", "articles": youtube_regular},
            {"label": "ショート（推定）", "articles": youtube_shorts},
        ]},
    ]


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
  <h1>ニュースポータル</h1>
  <span class="updated">更新: {updated_at}</span>
</header>
<nav class="tabbar">
{tabs_html}
</nav>
<main class="portal">
{sections_html}
</main>
<script>
  const tabs = document.querySelectorAll(".tab");
  const columns = document.querySelectorAll(".column");
  function activate(id) {{
    tabs.forEach(t => t.classList.toggle("active", t.dataset.target === id));
    columns.forEach(c => c.classList.toggle("active", c.dataset.id === id));
  }}
  tabs.forEach(t => t.addEventListener("click", () => activate(t.dataset.target)));
  if (tabs.length) activate(tabs[0].dataset.target);
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
