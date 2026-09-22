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


def build_sections():
    print("一般ニュースを取得中...")
    news = fetch_rss_group(RSS_FEEDS["news"])
    print("ゲームニュースを取得中...")
    game = fetch_rss_group(RSS_FEEDS["game"])
    print("映画情報を取得中...")
    movies = fetch_movies()
    print("はちま起稿を取得中...")
    hachima = fetch_rss_group(RSS_FEEDS["hachima"])
    print("YouTube急上昇を取得中...")
    youtube_regular, youtube_shorts = fetch_youtube_trending()

    return [
        {"id": "news", "label": "📰 一般ニュース", "groups": news},
        {"id": "game", "label": "🎮 ゲームニュース", "groups": game},
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
