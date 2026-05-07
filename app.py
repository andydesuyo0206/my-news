"""
朝凪 - Flask バックエンド
健司選定フィード / さくら設計UI / 悠翔設計コンテンツ
"""
import feedparser
import gc
import re
import time
import os
import json
import threading
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from flask import Flask, render_template, redirect

JST = ZoneInfo('Asia/Tokyo')

app = Flask(__name__)

# ─── 健司選定 RSSフィード（NHKは各カテゴリ末尾に配置し偏りを抑制） ──

FEEDS = {
    '主要': [
        ('朝日新聞',      'https://www.asahi.com/rss/asahi/newsheadlines.rdf'),
        ('毎日新聞',      'https://mainichi.jp/rss/etc/mainichi-flash.rss'),
        ('47NEWS',        'https://www.47news.jp/articles.rss'),
        ('Yahoo!ニュース', 'https://news.yahoo.co.jp/rss/topics/top-picks.xml'),
        ('財経新聞',      'https://www.zaikei.co.jp/rss/news.rdf'),
        ('NHK',           'https://www3.nhk.or.jp/rss/news/cat0.xml'),   # 末尾
    ],
    '経済': [
        ('日経ビジネス',   'https://business.nikkei.com/rss/sns/nb.rdf'),  # 日経系を先頭に
        ('ダイヤモンドOL', 'https://diamond.jp/list/feed/rss'),            # 日経寄り強化
        ('東洋経済',       'https://toyokeizai.net/list/feed/rss'),
        ('Reuters 経済',   'https://feeds.reuters.com/reuters/businessNews'),
        ('プレジデントOL', 'https://president.jp/list/feed/rss'),
        ('財経新聞 経済',  'https://www.zaikei.co.jp/rss/economy.rdf'),
        ('NHK経済',        'https://www3.nhk.or.jp/rss/news/cat3.xml'),  # 末尾
    ],
    '政治': [
        ('財経新聞 政治', 'https://www.zaikei.co.jp/rss/politics.rdf'),
        ('Yahoo! 政治',   'https://news.yahoo.co.jp/rss/topics/politics.xml'),
        ('朝日新聞 政治', 'https://www.asahi.com/rss/politics/index.rdf'),
        ('NHK政治',       'https://www3.nhk.or.jp/rss/news/cat4.xml'),   # 末尾
    ],
    '国際': [  # 日本語で読めるソースに統一
        ('AFP BB News',        'https://feeds.afpbb.com/rss/afpbb/afpbbnews'),
        ('BBC Japan',          'https://feeds.bbci.co.uk/japanese/rss.xml'),
        ('Reuters JP',         'https://jp.reuters.com/rssFeed/worldNews'),
        ('ニューズウィーク日本', 'https://www.newsweekjapan.jp/feed/'),
        ('Yahoo! 国際',        'https://news.yahoo.co.jp/rss/topics/world.xml'),
        ('NHK国際',            'https://www3.nhk.or.jp/rss/news/cat5.xml'),
    ],
    'IT・テック': [
        ('ITmedia',       'https://rss.itmedia.co.jp/rss/2.0/news_bursts.xml'),
        ('日経クロステック', 'https://xtech.nikkei.com/rss/index.rdf'),   # 日経IT部門
        ('Wired JP',      'https://wired.jp/rss/'),
        ('GIGAZINE',      'https://gigazine.net/news/rss_2.0/'),
        ('CNET Japan',    'http://feeds.japan.cnet.com/rss/cnet/all.rdf'),
        ('Impress',       'https://www.watch.impress.co.jp/data/rss/1.0/ipw/feed.rdf'),
    ],
    '文化・科学': [
        ('現代ビジネス', 'https://gendai.media/rss'),
        ('NHK科学',      'https://www3.nhk.or.jp/rss/news/cat7.xml'),
        ('NHK文化',      'https://www3.nhk.or.jp/rss/news/cat2.xml'),
    ],
    '国内・社会': [
        # 注：朝日・47NEWS・毎日は「主要」と重複のため除外（メモリ節約）
        ('Yahoo! 国内',   'https://news.yahoo.co.jp/rss/topics/domestic.xml'),
        ('財経新聞 社会', 'https://www.zaikei.co.jp/rss/social.rdf'),
        ('NHK社会',       'https://www3.nhk.or.jp/rss/news/cat1.xml'),
    ],
}

# ─── キャッシュ ──────────────────────────────────────────────────

_cache: dict = {}
_cache_ts: float = 0.0
CACHE_TTL = 1800  # 30分

# ─── Pixabay API（画像フォールバック。Render環境変数 PIXABAY_API_KEY を設定） ──
PIXABAY_KEY = os.environ.get('PIXABAY_API_KEY', '')
_pixabay_cache:  dict = {}
_kotoba_wiki_ok: dict = {}  # wiki_title → True/False（Wikipedia 存在確認キャッシュ）

# ─── Gemini API（概観・コメント生成。Render環境変数 GEMINI_API_KEY を設定） ──
GEMINI_KEY       = os.environ.get('GEMINI_API_KEY', '')
_ai_cache:         dict  = {}    # (旧) 概観テキストキャッシュ ※後方互換のため残置
_ai_cache_ts:      float = 0.0
_ai_comment_cache: dict  = {}    # (旧) 記事別コメントキャッシュ ※後方互換のため残置
_ai_batch_cache:   dict  = {}    # 全AIコンテンツ統合キャッシュ（overview+shasetsu+picks）
_ai_batch_ts:      float = 0.0
AI_CACHE_TTL             = 7200  # 2時間（Gemini 無料枠 1500 RPD 節約のため）


def _call_claude(prompt: str, max_tokens: int = 300) -> str:
    """Gemini 2.0 Flash REST API でテキスト生成（パッケージ不要）。失敗時は空文字"""
    if not GEMINI_KEY:
        print('[DEBUG] GEMINI_KEY 未設定 → テンプレート使用')
        return ''
    try:
        url  = (
            'https://generativelanguage.googleapis.com/v1beta'
            f'/models/gemini-2.0-flash:generateContent?key={GEMINI_KEY}'
        )
        body = json.dumps({
            'contents': [{'parts': [{'text': prompt}]}],
            'generationConfig': {
                'maxOutputTokens': max_tokens,
                'temperature': 0.7,
            },
        }).encode()
        req = urllib.request.Request(
            url, data=body,
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            text = data['candidates'][0]['content']['parts'][0]['text'].strip()
            print(f'[DEBUG] Gemini 生成成功 ({len(text)}文字)')
            return text
    except urllib.error.HTTPError as e:
        body_txt = e.read().decode()
        print(f'[WARN] Gemini API HTTPError {e.code}: {body_txt[:200]}')
        return ''
    except Exception as e:
        print(f'[WARN] Gemini API 失敗: {type(e).__name__}: {e}')
        return ''


def strip_html(text: str) -> str:
    return re.sub(r'<[^>]+>', '', text or '').replace('&nbsp;', ' ').replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>').strip()


# ─── Wikipedia サムネイル取得（きょうのことば・余白用） ──────────────

_wiki_img_cache: dict = {}


def get_wiki_thumb(title: str, lang: str = 'ja') -> str:
    """Wikipedia REST API でサムネイル画像URLを取得（メモリキャッシュ付き）"""
    if not title:
        return ''
    key = f'{lang}:{title}'
    if key in _wiki_img_cache:
        return _wiki_img_cache[key]
    try:
        api_url = (
            f'https://{lang}.wikipedia.org/api/rest_v1/page/summary/'
            f'{urllib.parse.quote(title)}'
        )
        req = urllib.request.Request(
            api_url,
            headers={'User-Agent': 'AsaNagi/1.0 (RSS Aggregator; contact: noreply@example.com)'}
        )
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read())
            img = data.get('thumbnail', {}).get('source', '')
            _wiki_img_cache[key] = img
            return img
    except Exception as e:
        print(f'[WARN] Wikipedia image fetch 失敗 ({title}): {e}')
        _wiki_img_cache[key] = ''
        return ''


# ─── Pixabay 画像取得（Wikipedia に画像がない場合のフォールバック） ──────

def get_pixabay_image(query: str) -> str:
    """Pixabay API でキーワード検索し画像URLを返す（メモリキャッシュ付き）"""
    if not query or not PIXABAY_KEY:
        return ''
    if query in _pixabay_cache:
        return _pixabay_cache[query]
    try:
        params = urllib.parse.urlencode({
            'key':          PIXABAY_KEY,
            'q':            query,
            'lang':         'ja',
            'image_type':   'photo',
            'orientation':  'horizontal',
            'per_page':     3,
            'safesearch':   'true',
        })
        req = urllib.request.Request(
            f'https://pixabay.com/api/?{params}',
            headers={'User-Agent': 'AsaNagi/1.0 (RSS Aggregator; contact: noreply@example.com)'}
        )
        with urllib.request.urlopen(req, timeout=4) as resp:
            data  = json.loads(resp.read())
            hits  = data.get('hits', [])
            img   = hits[0].get('webformatURL', '') if hits else ''
            _pixabay_cache[query] = img
            return img
    except Exception as e:
        print(f'[WARN] Pixabay fetch 失敗 ({query}): {e}')
        _pixabay_cache[query] = ''
        return ''


# 日英変換マップ（頻出ニュースキーワード → 英語で Pixabay 精度向上）
_JP_EN: list[tuple[str, str]] = [
    ('地震', 'earthquake Japan'), ('台風', 'typhoon storm Japan'),
    ('災害', 'natural disaster'), ('洪水', 'flood disaster'),
    ('核', 'nuclear'), ('ミサイル', 'missile military'),
    ('漁業', 'fishing ocean'), ('農業', 'agriculture farm'),
    ('半導体', 'semiconductor chip technology'), ('AI', 'artificial intelligence technology'),
    ('人工知能', 'artificial intelligence'), ('量子', 'quantum technology'),
    ('宇宙', 'space universe'), ('ロケット', 'rocket launch'),
    ('株式', 'stock market finance'), ('株価', 'stock market charts'),
    ('円', 'Japanese yen currency exchange'), ('インフレ', 'inflation economy'),
    ('物価', 'prices shopping economy'), ('金利', 'interest rate finance'),
    ('銀行', 'bank finance'), ('貿易', 'trade international business'),
    ('企業', 'business company office'), ('工場', 'factory manufacturing'),
    ('電力', 'electricity power energy'), ('再生可能', 'renewable energy solar wind'),
    ('選挙', 'election voting democracy'), ('国会', 'parliament government'),
    ('外交', 'diplomacy international relations'), ('首脳', 'leaders summit diplomacy'),
    ('防衛', 'defense military'), ('安全保障', 'national security'),
    ('ロシア', 'Russia'), ('ウクライナ', 'Ukraine'), ('中国', 'China'),
    ('アメリカ', 'United States'), ('韓国', 'South Korea'), ('北朝鮮', 'North Korea'),
    ('医療', 'healthcare medicine hospital'), ('病院', 'hospital medical'),
    ('ワクチン', 'vaccine healthcare'), ('感染', 'infection virus disease'),
    ('教育', 'education school learning'), ('子ども', 'children school'),
    ('少子化', 'birth rate population Japan'), ('高齢', 'elderly aging Japan'),
    ('環境', 'environment nature green'), ('気候', 'climate change environment'),
    ('野球', 'baseball sport'), ('サッカー', 'soccer football sport'),
    ('スポーツ', 'sports athlete'), ('五輪', 'Olympics sports'),
    ('映画', 'cinema film movie'), ('音楽', 'music concert'),
    ('科学', 'science research laboratory'), ('宇宙', 'space astronomy'),
]

# カテゴリ別デフォルト検索語（マッチなしの最終フォールバック）
_CAT_DEFAULT: dict[str, str] = {
    '経済':     'Japan economy business finance',
    '政治':     'Japan politics government',
    '国際':     'world international diplomacy',
    'IT・テック': 'technology digital innovation',
    '文化・科学': 'Japan culture science',
    '国内・社会': 'Japan society people city',
    '主要':     'Japan news',
}


def extract_keyword(title: str, category: str = '') -> str:
    """記事タイトルから Pixabay 検索用キーワードを抽出（英語変換で精度向上）"""
    # 日英変換マップで最大2語を英語に変換
    en_parts: list[str] = []
    for jp, en in _JP_EN:
        if jp in title:
            en_parts.append(en)
            if len(en_parts) >= 2:
                break
    if en_parts:
        return ' '.join(en_parts)

    # 変換できなければカテゴリ別デフォルト
    if category in _CAT_DEFAULT:
        return _CAT_DEFAULT[category]

    # 最終フォールバック：記号除去した先頭20文字
    s = re.sub(r'[【】〔〕「」『』（）()\[\]《》！？!?、。・〜～…＝=＋+\-]', ' ', title)
    return re.sub(r'\s+', ' ', s).strip()[:20]


# ─── OGP 画像取得（バックグラウンド・非ブロッキング） ─────────────────
# 設計：ページ表示をブロックせず、バックグラウンドスレッドで og:image を取得・キャッシュ。
# 次のリクエスト時にキャッシュから補完して表示する。最大6並列・URL上限2000件。

_ogp_cache:    dict = {}   # URL → image URL（''は取得済み・画像なし）
_ogp_lock              = threading.Lock()
_ogp_executor          = ThreadPoolExecutor(max_workers=2, thread_name_prefix='ogp')  # メモリ節約のため2に制限

# og:image / twitter:image の抽出パターン（属性順不問）
_OGP_RE = [
    re.compile(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', re.I),
    re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', re.I),
    re.compile(r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']', re.I),
    re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']twitter:image["\']', re.I),
]
# og:image:width 抽出（小さすぎるアイコン画像をフィルタリング）
_OGP_WIDTH_RE = [
    re.compile(r'<meta[^>]+property=["\']og:image:width["\'][^>]+content=["\'](\d+)["\']', re.I),
    re.compile(r'<meta[^>]+content=["\'](\d+)["\'][^>]+property=["\']og:image:width["\']', re.I),
]
_OGP_MIN_WIDTH  = 200   # これ未満のピクセル幅は除外（アイコン・サムネイル等）
_OGP_CACHE_MAX  = 500   # キャッシュ上限（メモリ節約のため2000→500）
_OGP_CACHE_DROP = 100   # 上限超過時に削除する件数


def _fetch_ogp(url: str) -> None:
    """バックグラウンドで og:image を取得してキャッシュに保存（ページ表示をブロックしない）"""
    # 取得済みチェック（二重取得防止）
    with _ogp_lock:
        if url in _ogp_cache:
            return
        _ogp_cache[url] = ''   # 処理中マーク（他スレッドの重複起動を防ぐ）

    img = ''
    try:
        req = urllib.request.Request(
            url,
            headers={
                'User-Agent': 'Mozilla/5.0 (compatible; MaiChokan/1.0; +https://my-news.onrender.com)',
                'Accept':          'text/html,application/xhtml+xml',
                'Accept-Language': 'ja,en;q=0.8',
            }
        )
        # 最初の 16 KB だけ取得（<head> 内の og:image はほぼここに収まる）
        with urllib.request.urlopen(req, timeout=4) as resp:
            raw = resp.read(16384)
        html = raw.decode('utf-8', errors='replace')
        for pat in _OGP_RE:
            m = pat.search(html)
            if m:
                img = m.group(1).strip()
                break
        # og:image:width が取得できた場合、小さすぎる画像（アイコン等）を除外
        if img:
            for wp in _OGP_WIDTH_RE:
                wm = wp.search(html)
                if wm:
                    try:
                        w = int(wm.group(1))
                        if w < _OGP_MIN_WIDTH:
                            print(f'[DEBUG] OGP 画像が小さすぎるためスキップ: width={w}px {url[:60]}')
                            img = ''
                    except ValueError:
                        pass
                    break
    except Exception as e:
        print(f'[WARN] OGP fetch 失敗: {url[:70]} → {e}')

    with _ogp_lock:
        # キャッシュ上限：超えたら古い件を削除（簡易 FIFO）
        if len(_ogp_cache) > _OGP_CACHE_MAX:
            for k in list(_ogp_cache.keys())[:_OGP_CACHE_DROP]:
                del _ogp_cache[k]
        _ogp_cache[url] = img

    if img:
        print(f'[DEBUG] OGP取得: {img[:80]}')


def _prefetch_ogp(news: dict) -> None:
    """news キャッシュ内の「画像なし記事」を一括で OGP プリフェッチ（バックグラウンド呼び出し用）"""
    count = 0
    for articles in news.values():
        for art in articles:
            link = art.get('link', '')
            if not link or link == '#' or art.get('image'):
                continue
            with _ogp_lock:
                if link in _ogp_cache:
                    continue
            _ogp_executor.submit(_fetch_ogp, link)
            count += 1
            if count >= 15:   # メモリ節約のため最大15件に制限（旧40件）
                print(f'[DEBUG] OGP プリフェッチ: {count}件 起動（上限到達）')
                return
    print(f'[DEBUG] OGP プリフェッチ: {count}件 起動')


# ─── RSSエントリ画像取得 ──────────────────────────────────────────

def get_entry_image(entry) -> str:
    """feedparserエントリから画像URLを取り出す（NHK/Yahoo等の各種フォーマット対応）"""
    # 1. media:content（medium="video"は除外、それ以外はURLがあれば採用）
    for item in (entry.get('media_content') or []):
        if item.get('medium', 'image') == 'video':
            continue
        url = item.get('url', '')
        if url:
            return url

    # 2. media:thumbnail
    for item in (entry.get('media_thumbnail') or []):
        url = item.get('url', '')
        if url:
            return url

    # 3. enclosures（type が image/* のもの）
    for enc in (entry.get('enclosures') or []):
        if enc.get('type', '').startswith('image/'):
            url = enc.get('url', enc.get('href', ''))
            if url:
                return url

    # 4. links（rel="enclosure" で type が image/*）
    for link in (entry.get('links') or []):
        if link.get('rel') == 'enclosure' and link.get('type', '').startswith('image/'):
            url = link.get('href', '')
            if url:
                return url

    # 5. content[0].value の <img>（The Verge 等の全文フィード）
    for c in (entry.get('content') or []):
        val = c.get('value', '')
        if val:
            m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', val, re.I)
            if m:
                return m.group(1)

    # 6. summary / description の <img>
    raw = (entry.get('summary') or entry.get('description') or '')
    if raw:
        m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', raw, re.I)
        if m:
            return m.group(1)

    return ''


# ─── 日付フォーマット ─────────────────────────────────────────────

_WDAY_EN = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']


def fmt_published(entry) -> str:
    """feedparserエントリの公開日時を JST の 'YYYY/MM/DD(Tue) HH:MM' 形式で返す"""
    pub = entry.get('published_parsed')
    if pub:
        try:
            dt = datetime(*pub[:6], tzinfo=timezone.utc).astimezone(JST)
            wday = _WDAY_EN[dt.weekday()]
            return f'{dt.strftime("%Y/%m/%d")}({wday}) {dt.strftime("%H:%M")}'
        except Exception:
            pass
    return entry.get('published', '')


def fetch_feed(source: str, url: str, category: str = '') -> list:
    try:
        feed = feedparser.parse(url)
        articles = []
        for entry in feed.entries[:6]:  # メモリ節約のため8→6に削減
            raw     = entry.get('summary', entry.get('description', ''))
            summary = strip_html(raw)
            title   = entry.get('title', '（タイトルなし）')
            image   = get_entry_image(entry)
            # RSS に画像がなければ Pixabay でフォールバック（カテゴリ情報で精度向上）
            if not image and PIXABAY_KEY:
                image = get_pixabay_image(extract_keyword(title, category))
            articles.append({
                'title':     title,
                'link':      entry.get('link', '#'),
                'summary':   summary[:220] + '…' if len(summary) > 220 else summary,
                'published': fmt_published(entry),
                'source':    source,
                'image':     image,
            })
        return articles
    except Exception as e:
        print(f'[WARN] {source} 取得失敗: {e}')
        return []


def interleave(sources_articles: list) -> list:
    """複数ソースをラウンドロビンで混合し、特定ソースへの偏りを解消する"""
    result = []
    max_len = max((len(a) for a in sources_articles), default=0)
    for i in range(max_len):
        for src_articles in sources_articles:
            if i < len(src_articles):
                result.append(src_articles[i])
    return result


def get_all_news() -> dict:
    global _cache, _cache_ts
    if time.time() - _cache_ts < CACHE_TTL and _cache:
        return _cache

    # ── 全フィードを並列取得（max_workers=6：512MB制限内でバランス）──
    # 35フィードを逐次→並列にすることで10〜30秒→3〜6秒程度に短縮
    tasks: list[tuple[str, str, str]] = [
        (src, url, category)
        for category, sources in FEEDS.items()
        for src, url in sources
    ]

    # フィード取得用スレッドプール（使い捨て：メモリ解放のため with で閉じる）
    raw_results: dict[tuple[str, str], list] = {}  # (category, src) → articles
    with ThreadPoolExecutor(max_workers=6, thread_name_prefix='feed') as pool:
        future_map = {
            pool.submit(fetch_feed, src, url, cat): (src, url, cat)
            for src, url, cat in tasks
        }
        for future in as_completed(future_map):
            src, url, cat = future_map[future]
            try:
                raw_results[(cat, src)] = future.result()
            except Exception as e:
                print(f'[WARN] {src} 並列取得例外: {e}')
                raw_results[(cat, src)] = []

    # カテゴリごとにインターリーブして整形
    result = {}
    for category, sources in FEEDS.items():
        per_source = [raw_results.get((category, src), []) for src, _ in sources]
        result[category] = interleave(per_source)

    _cache = result
    _cache_ts = time.time()
    # RSSに画像がない記事の og:image をバックグラウンドで非同期取得（ページ表示をブロックしない）
    threading.Thread(target=_prefetch_ogp, args=(result,), daemon=True).start()
    gc.collect()   # キャッシュ更新後に不要オブジェクトを明示的に回収
    print('[DEBUG] get_all_news: 並列取得完了・GC実行')
    return result


# ─── きょうのことば ──────────────────────────────────────────────

KOTOBA = [
    {'word': '量的緩和', 'reading': 'りょうてきかんわ', 'en': 'Quantitative Easing',
     'desc': '中央銀行が国債などを大量購入し、市場への資金供給を増やす金融政策。日銀の「異次元緩和」が代表例。',
     'wiki': '量的緩和政策'},
    {'word': 'フィンテック', 'reading': 'ふぃんてっく', 'en': 'FinTech',
     'desc': '金融（Finance）と技術（Technology）の融合。スマホ決済・仮想通貨・ロボアドバイザーなどを指す。',
     'wiki': 'フィンテック'},
    {'word': 'ESG投資', 'reading': 'いーえすじーとうし', 'en': 'ESG Investing',
     'desc': '環境（E）・社会（S）・ガバナンス（G）を考慮した投資手法。長期的な企業価値を評価する。',
     'wiki': 'ESG投資'},
    {'word': '円キャリートレード', 'reading': 'えんきゃりーとれーど', 'en': 'Yen Carry Trade',
     'desc': '低金利の円を借りて外貨建て資産に投資する取引。急激な円高局面で損失が膨らみやすい。',
     'wiki': 'キャリートレード'},
    {'word': '半導体', 'reading': 'はんどうたい', 'en': 'Semiconductor',
     'desc': '電気をほどほどに通す物質。CPU・メモリ・AIチップなどの基幹部品。「産業のコメ」とも呼ばれる。',
     'wiki': '半導体'},
    {'word': 'インフレ', 'reading': 'いんふれ', 'en': 'Inflation',
     'desc': '物価が継続的に上昇し、お金の価値が下がる現象。需要超過や供給制約が主な原因。',
     'wiki': 'インフレーション'},
    {'word': '国債', 'reading': 'こくさい', 'en': 'Government Bond',
     'desc': '政府が資金調達のために発行する債券。日本の国債残高は1,000兆円超と世界最大水準。',
     'wiki': '国債'},
    {'word': '為替介入', 'reading': 'かわせかいにゅう', 'en': 'Currency Intervention',
     'desc': '政府・中央銀行が外国為替市場に直接参加し、自国通貨の水準を調整する政策。',
     'wiki': '外国為替市場への介入'},
    {'word': 'GDP', 'reading': 'じーでぃーぴー', 'en': 'Gross Domestic Product',
     'desc': '国内総生産。一定期間内に国内で生産された財・サービスの付加価値の合計。経済規模の代表指標。',
     'wiki': '国内総生産'},
    {'word': '生成AI', 'reading': 'せいせいえーあい', 'en': 'Generative AI',
     'desc': 'テキスト・画像・動画などを自動生成するAI。ChatGPT・Claude・Geminiが代表例。',
     'wiki': '生成的人工知能'},
    {'word': 'カーボンニュートラル', 'reading': 'かーぼんにゅーとらる', 'en': 'Carbon Neutral',
     'desc': 'CO₂排出量と吸収量を差し引きゼロにする目標。日本は2050年の達成を宣言している。',
     'wiki': 'カーボンニュートラル'},
    {'word': 'スタグフレーション', 'reading': 'すたぐふれーしょん', 'en': 'Stagflation',
     'desc': '景気停滞（Stagnation）とインフレ（Inflation）が同時に起きる状態。1970年代の石油危機が典型例。',
     'wiki': 'スタグフレーション'},
    {'word': 'NISA', 'reading': 'にーさ', 'en': '少額投資非課税制度',
     'desc': '個人投資家の株式・投信の利益を非課税にする制度。2024年から新NISAとして大幅拡充。',
     'wiki': '少額投資非課税制度'},
    {'word': 'メタバース', 'reading': 'めたばーす', 'en': 'Metaverse',
     'desc': 'インターネット上の仮想3D空間。アバターで参加し、交流・購買・仕事ができる次世代プラットフォーム。',
     'wiki': 'メタバース'},
    {'word': 'ブロックチェーン', 'reading': 'ぶろっくちぇーん', 'en': 'Blockchain',
     'desc': 'データを分散管理する技術。改ざんが困難でビットコインなど仮想通貨の基盤技術として知られる。',
     'wiki': 'ブロックチェーン'},
    {'word': 'デフレ', 'reading': 'でふれ', 'en': 'Deflation',
     'desc': '物価が継続的に下がる現象。消費が減少し企業収益・雇用が悪化する「デフレスパイラル」に陥りやすい。',
     'wiki': 'デフレーション'},
    {'word': 'イールドカーブ', 'reading': 'いーるどかーぶ', 'en': 'Yield Curve',
     'desc': '残存期間の異なる債券の利回りをつないだグラフ。日銀の「YCC」政策で注目された。',
     'wiki': '利回り曲線'},
    {'word': 'クラウドコンピューティング', 'reading': 'くらうどこんぴゅーてぃんぐ', 'en': 'Cloud Computing',
     'desc': 'インターネット経由でサーバー・ストレージ・ソフトウェアを利用するサービス。AWS・Azure・GCPが主要プレイヤー。',
     'wiki': 'クラウドコンピューティング'},
    {'word': 'M&A', 'reading': 'えむあんどえー', 'en': 'Mergers & Acquisitions',
     'desc': '企業の合併・買収の総称。事業拡大・シナジー創出・市場参入を目的に行われる経営戦略。',
     'wiki': '合併と買収'},
    {'word': 'サプライチェーン', 'reading': 'さぷらいちぇーん', 'en': 'Supply Chain',
     'desc': '原材料の調達から製造・流通・販売までの一連の流れ。コロナ禍や地政学リスクで世界的に見直しが進んだ。',
     'wiki': 'サプライチェーン'},
    {'word': 'DX', 'reading': 'でじたるとらんすふぉーめーしょん', 'en': 'Digital Transformation',
     'desc': 'デジタル技術を活用してビジネスや社会を変革すること。単なるIT化ではなく組織文化の変革も含む。',
     'wiki': 'デジタルトランスフォーメーション'},
    {'word': '地政学リスク', 'reading': 'ちせいがくりすく', 'en': 'Geopolitical Risk',
     'desc': '地理的条件に基づく政治・軍事・外交の緊張が経済に与えるリスク。米中対立・ロシア問題が代表例。',
     'wiki': '地政学'},
    {'word': 'プライマリーバランス', 'reading': 'ぷらいまりーばらんす', 'en': 'Primary Balance',
     'desc': '税収などの歳入から国債費を除いた歳出を引いた収支。財政健全化の指標として重視される。',
     'wiki': '基礎的財政収支'},
    {'word': 'ROE', 'reading': 'あーるおーいー', 'en': 'Return on Equity',
     'desc': '自己資本利益率。株主が投じた資本に対してどれだけ利益を生んだかを示す企業価値評価の重要指標。',
     'wiki': '株主資本利益率'},
    {'word': 'ユニコーン企業', 'reading': 'ゆにこーんきぎょう', 'en': 'Unicorn Company',
     'desc': '企業価値が10億ドル以上の未上場スタートアップ。日本はその数が少なく、育成が政策課題となっている。',
     'wiki': 'ユニコーン企業'},
    {'word': 'AI半導体', 'reading': 'えーあいはんどうたい', 'en': 'AI Chip',
     'desc': 'AI処理に特化した半導体。NVIDIAのGPUが主流。生成AI普及で需要が急増し、争奪戦が激化している。',
     'wiki': 'GPU'},
    {'word': 'GX', 'reading': 'じーえっくす', 'en': 'Green Transformation',
     'desc': '化石燃料依存から脱却し、クリーンエネルギー中心の社会・経済構造に転換する取り組み。',
     'wiki': '再生可能エネルギー'},
    {'word': '賃金インフレ', 'reading': 'ちんぎんいんふれ', 'en': 'Wage Inflation',
     'desc': '労働者の賃金が上昇し続ける現象。物価上昇を伴う「好循環」が望まれるが、企業コスト増を招くことも。',
     'wiki': '賃金'},
    {'word': 'IoT', 'reading': 'あいおーてぃー', 'en': 'Internet of Things',
     'desc': 'あらゆる機器がインターネットに接続し、データを収集・交換する仕組み。スマート家電・工場・都市に応用される。',
     'wiki': 'モノのインターネット'},
    {'word': '量子コンピュータ', 'reading': 'りょうしこんぴゅーた', 'en': 'Quantum Computer',
     'desc': '量子力学の原理を使い、従来コンピュータを遥かに上回る計算速度を目指す次世代コンピュータ。',
     'wiki': '量子コンピュータ'},
    {'word': '物価目標', 'reading': 'ぶっかもくひょう', 'en': 'Inflation Target',
     'desc': '中央銀行が設定する物価上昇率の目標値。日銀は2%を目標とし、長年の金融緩和政策の根拠となっている。',
     'wiki': '', 'google': '物価目標 インフレーション ターゲティング 日銀'},
    {'word': 'フリーランス', 'reading': 'ふりーらんす', 'en': 'Freelance',
     'desc': '特定の企業に属さず、個人で仕事を請け負う働き方。2024年のフリーランス保護新法により権利保護が強化された。',
     'wiki': 'フリーランス'},
    # ── 政治・外交 ──
    {'word': '集団的自衛権', 'reading': 'しゅうだんてきじえいけん', 'en': 'Collective Self-Defense',
     'desc': '同盟国が攻撃された場合に共同で防衛する権利。日本は2015年の安保法制で限定的行使が容認された。',
     'wiki': '集団的自衛権'},
    {'word': '安保理', 'reading': 'あんぽり', 'en': 'UN Security Council',
     'desc': '国連安全保障理事会。米・英・仏・露・中の5常任理事国が拒否権を持ち、国際平和と安全の維持を担う。',
     'wiki': '国際連合安全保障理事会'},
    {'word': '政党交付金', 'reading': 'せいとうこうふきん', 'en': 'Party Subsidy',
     'desc': '国民1人当たり250円を基準に国が政党に配分する資金。企業・団体献金の代替として1994年に導入された。',
     'wiki': '政党交付金'},
    {'word': '閣議決定', 'reading': 'かくぎけってい', 'en': 'Cabinet Decision',
     'desc': '内閣を構成する全大臣が一致して決定すること。法律によらず政府の方針を変更できる強力な手続き。',
     'wiki': '閣議'},
    {'word': '統治機構', 'reading': 'とうちきこう', 'en': 'Governance Structure',
     'desc': '国家権力の組織と権力行使の仕組み。立法・行政・司法の三権分立が民主主義の基本原則とされる。',
     'wiki': '統治機構'},
    # ── 国際関係 ──
    {'word': 'デカップリング', 'reading': 'でかっぷりんぐ', 'en': 'Decoupling',
     'desc': '経済・技術面で特定国との依存関係を切り離す戦略。米中対立激化を背景に半導体・AIで顕著になっている。',
     'wiki': 'デカップリング'},
    {'word': '経済安全保障', 'reading': 'けいざいあんぜんほしょう', 'en': 'Economic Security',
     'desc': '重要物資の供給網や先端技術を国家安全保障の観点から守る政策。日本では2022年に専門法が成立した。',
     'wiki': '経済安全保障推進法'},
    {'word': 'ASEAN', 'reading': 'あせあん', 'en': 'ASEAN',
     'desc': '東南アジア諸国連合。タイ・ベトナム・インドネシアなど10カ国。地政学的要衝として大国の影響力が競合する。',
     'wiki': '東南アジア諸国連合'},
    {'word': 'グローバルサウス', 'reading': 'ぐろーばるさうす', 'en': 'Global South',
     'desc': 'アジア・アフリカ・中南米など途上国・新興国の総称。G7主導の国際秩序に対する独自の立場を主張している。',
     'wiki': 'グローバル・サウス'},
    # ── 社会・福祉 ──
    {'word': '社会保障費', 'reading': 'しゃかいほしょうひ', 'en': 'Social Security Expenditure',
     'desc': '年金・医療・介護・生活保護などの費用。日本では高齢化により毎年1兆円規模で増加しており財政を圧迫する。',
     'wiki': '社会保障'},
    {'word': 'ヤングケアラー', 'reading': 'やんぐけあらー', 'en': 'Young Carer',
     'desc': '家族の介護や世話を担う18歳未満の子ども。本来享受すべき教育・友人関係の機会を奪われるケースが多い。',
     'wiki': 'ヤングケアラー'},
    {'word': 'デジタル田園都市', 'reading': 'でじたるでんえんとし', 'en': 'Digital Garden City',
     'desc': 'デジタル技術で地方の課題を解決し都市との格差を縮める国家戦略。リモートワーク普及が追い風となっている。',
     'wiki': 'デジタル田園都市国家構想'},
    {'word': '孤独・孤立対策', 'reading': 'こどく・こりつたいさく', 'en': 'Loneliness Policy',
     'desc': '社会的孤立を政策課題として捉える取り組み。英国に続き日本も2021年に孤独・孤立対策担当大臣を設置した。',
     'wiki': '孤独・孤立対策推進法'},
    # ── 科学・テクノロジー ──
    {'word': '核融合', 'reading': 'かくゆうごう', 'en': 'Nuclear Fusion',
     'desc': '軽い原子核同士を融合させてエネルギーを得る技術。「夢のエネルギー」として研究が進み、実用化が近づきつつある。',
     'wiki': '核融合'},
    {'word': 'CPS', 'reading': 'しーぴーえす', 'en': 'Cyber-Physical System',
     'desc': '現実世界のデータをリアルタイムにサイバー空間と連携させるシステム。スマート工場や自動運転の基盤技術。',
     'wiki': 'サイバーフィジカルシステム'},
    {'word': 'バイオテクノロジー', 'reading': 'ばいおてくのろじー', 'en': 'Biotechnology',
     'desc': '生物の機能を利用した技術の総称。ゲノム編集・mRNAワクチン・培養肉など急速に実用化が進んでいる。',
     'wiki': 'バイオテクノロジー'},
    # ── 文化・歴史 ──
    {'word': '無形文化遺産', 'reading': 'むけいぶんかいさん', 'en': 'Intangible Cultural Heritage',
     'desc': 'ユネスコが保護する伝統芸能・祭り・工芸技術など。日本からは能楽・歌舞伎・和食など22件が登録されている。',
     'wiki': '無形文化遺産'},
    {'word': 'ソフトパワー', 'reading': 'そふとぱわー', 'en': 'Soft Power',
     'desc': '軍事・経済力でなく文化・価値観・外交で影響力を持つ能力。日本はアニメ・ゲーム・食文化で高い評価を得る。',
     'wiki': 'ソフトパワー'},
    {'word': 'SDGs', 'reading': 'えすでぃーじーず', 'en': 'Sustainable Development Goals',
     'desc': '2030年までに達成すべき17の国際目標。貧困・気候・不平等など地球規模の課題を国連加盟国が共有している。',
     'wiki': '持続可能な開発目標'},
    {'word': 'ウェルビーイング', 'reading': 'うぇるびーいんぐ', 'en': 'Well-being',
     'desc': '身体・精神・社会的に良好な状態。GDPだけでは測れない豊かさの指標として、政府・企業が重視するようになった。',
     'wiki': 'ウェルビーイング'},
    # ─── 追加10語 ──────────────────────────────────────────────────
    {'word': 'フリーランサー', 'reading': 'ふりーらんさー', 'en': 'Freelancer',
     'desc': '特定の組織に属さず、複数の依頼主と契約して働く独立した労働者。2024年のフリーランス新法で保護が強化された。',
     'wiki': 'フリーランス'},
    {'word': '量子コンピュータ', 'reading': 'りょうしこんぴゅーた', 'en': 'Quantum Computer',
     'desc': '量子力学の原理を使い、従来のコンピュータでは不可能な計算を実現する次世代計算機。Googleが「量子超越性」を実証した。',
     'wiki': '量子コンピュータ'},
    {'word': '生成AI', 'reading': 'せいせいえーあい', 'en': 'Generative AI',
     'desc': 'テキスト・画像・音声などを自律的に生成するAI技術。ChatGPTの登場で2023年から社会的議論が急拡大した。',
     'wiki': '生成的人工知能'},
    {'word': 'リスキリング', 'reading': 'りすきりんぐ', 'en': 'Reskilling',
     'desc': 'AI・DX時代に対応するため、既存の労働者が新たなスキルを習得し直すこと。日本政府も1兆円規模の支援策を打ち出した。',
     'wiki': 'リスキリング'},
    {'word': 'スタートアップ', 'reading': 'すたーとあっぷ', 'en': 'Startup',
     'desc': '革新的なビジネスモデルで急成長を目指す新興企業。岸田政権が「スタートアップ育成5か年計画」を発表し注目を集めた。',
     'wiki': 'スタートアップ企業'},
    {'word': '少子化', 'reading': 'しょうしか', 'en': 'Declining Birth Rate',
     'desc': '出生率が低下し、子どもの数が減少する現象。日本の合計特殊出生率は2023年に過去最低の1.20を記録した。',
     'wiki': '少子化'},
    {'word': 'マイナンバー', 'reading': 'まいなんばー', 'en': 'My Number',
     'desc': '日本の全住民に付与された12桁の個人識別番号。社会保障・税務・災害対策に使われ、マイナ保険証への統合が進む。',
     'wiki': 'マイナンバー'},
    {'word': '食料安全保障', 'reading': 'しょくりょうあんぜんほしょう', 'en': 'Food Security',
     'desc': '国民が十分な食料を安定的に得られる状態。日本の食料自給率はカロリーベースで38%と先進国最低水準にある。',
     'wiki': '食料安全保障'},
    {'word': 'インフラ', 'reading': 'いんふら', 'en': 'Infrastructure',
     'desc': '社会経済活動の基盤となる設備・施設の総称。道路・電力・水道・通信網など。老朽化対策が日本の重要課題となっている。',
     'wiki': 'インフラストラクチャー'},
    {'word': 'ブロックチェーン', 'reading': 'ぶろっくちぇーん', 'en': 'Blockchain',
     'desc': '取引記録を分散管理する技術。改ざんが極めて困難で、暗号通貨・NFT・契約の自動化（スマートコントラクト）に使われる。',
     'wiki': 'ブロックチェーン'},
]


# ─── 悠翔設計：春秋コラム（書き出しフレーズ） ─────────────────

SHUNSHUU_OPENERS = [
    "桜の花びらが散り始める頃、人は必ず何かを思い出す。懐かしさとは、時間が作り出した優しい嘘なのかもしれない。",
    "古代中国の詩人、陶淵明は仕事を辞めて故郷の田園に戻った。「帰りなんいざ」と書いた彼の詩に、現代人が惹かれるのはなぜだろう。",
    "フランスの数学者パスカルはかつて言った。「人間は考える葦である」と。弱く、それでも思索することをやめない——それが人間の尊厳だ。",
    "鳥は飛ぶ前に、一度翼を縮める。助走もなく、踏み台もなく、ただ空気を読んで跳ぶ。その姿に、何か学ぶべきことがある気がする。",
    "江戸時代の商人は「売り手よし、買い手よし、世間よし」という「三方よし」を商いの基本とした。今も色褪せない知恵だ。",
    "ドイツに「Weltschmerz（ヴェルトシュメルツ）」という言葉がある。「世界の痛み」を一身に感じる苦しさのこと。現代に生きると、その感覚がよくわかる。",
    "冬が長ければ長いほど、春の到来が喜ばしい。試練の価値は、それを乗り越えた後にしかわからない。",
    "マーク・トウェインは「歴史は繰り返さない、しかし韻を踏む」と言ったとされる。過去を学ぶ理由が、ここにある。",
    "夏の朝、蝉が鳴き始める頃。その命の短さを知りながら、それでも全力で鳴く姿に、どこか励まされる。",
    "古代ギリシャの哲学者ヘラクレイトスは「同じ川に二度入ることはできない」と言った。すべては変わり続ける——それが宇宙の法則だ。",
    "秋の夕暮れ、一枚の葉が落ちる。終わりの美しさを、日本人はずっと愛でてきた。「もののあわれ」という感性は、今も生きている。",
    "日本に古くから伝わる言葉に「七転び八起き」がある。転んだ回数より、立ち上がった回数が一つ多ければいい。それだけのことだ。",
    "19世紀のイギリスで産業革命が起きたとき、誰もその変化の大きさを予測できなかった。時代の転換点は、いつも静かにやってくる。",
    "太平洋の向こう、ハワイに「アロハ」という言葉がある。「愛」「平和」「思いやり」——一語にこれだけの意味を込めた民族の豊かさに驚く。",
    "種を蒔く農家は、収穫の日を想像しながら土を耕す。現在の努力が未来の実りになる——そのシンプルな真実を、都市生活は忘れさせる。",
    "ニュートンは「私がかなたを見渡せたのは、巨人の肩の上に乗っていたからだ」と書いた。知の積み重ねを、これほど美しく表した言葉はない。",
    "初雪が降る夜、世界は一瞬だけ静かになる。騒々しい日常に、自然は時々「休め」と語りかける。",
    "中国の古典「論語」の冒頭は「学びて時に之を習ふ、亦説ばしからずや」で始まる。学ぶ喜びを、孔子は二千五百年前に語っていた。",
    "蜂は一生をかけて、ティースプーン一杯分の蜂蜜しか作れないという。それでも働き続ける蜂の姿に、何かを感じずにはいられない。",
    "月は地球の周りを回り続けている。誰にも頼まれたわけでもなく、報酬があるわけでもなく、ただ黙って回り続けている。",
    "フィンランドには「sisu（シス）」という言葉がある。逆境に負けない精神力のことだ。北欧の厳しい冬が育てた、一語の哲学だ。",
    "春の田んぼに水を張ると、空が映る。足元に空があると気づくとき、人は少し視点が変わる。",
    "古代ローマ人は「Carpe diem（カルペ・ディエム）」と言った。「今日を摘め」——今この瞬間を生きることの大切さは、二千年たっても変わらない。",
    "木は根を見せない。地中深くに張り巡らされた根があって初めて、高くそびえる幹が生まれる。人も組織も、同じかもしれない。",
    "バッハは生涯に千以上の曲を作った。楽譜に残した言葉は「Soli Deo gloria（神にのみ栄光あれ）」。自分の仕事に意味を見出す力の強さよ。",
    "秋の空は高い。それを見上げる人間が小さいのではなく、宇宙の広さを実感させるのだと、昔の人は詩に書いた。",
    "「急いては事を仕損じる」という諺がある。デジタル化が進む時代に、この教えはむしろ輝きを増している。",
    "江戸時代の町人は「宵越しの金は持たない」と言った。明日より今日を生きる精神が、あの時代の活気を生んでいた。",
    "北極星は動かない。昔の船乗りはその星を目印に海を渡った。変わらないものが、変わっていくものを支える。",
    "春を告げる花として梅がある。桜より目立たず、しかし誰より早く咲く。先駆者の孤独と誇りを、梅は静かに体現している。",
    "シェイクスピアは「全世界は舞台だ」と書いた。人はそこで役者を演じる——そう考えると、日常の些細な出来事も少し違って見えてくる。",
    "夏の入道雲は、見る者を子供に戻す。大人になるとは、空を見上げる時間を失っていくことなのかもしれない。",
    "「一期一会」という茶の湯の言葉がある。この出会いは、生涯に一度きり。そう思うだけで、目の前の人への接し方が変わる。",
    "ガリレオは地動説を唱えて迫害を受けた。それでも地球は回る——正しさが認められるまでには、時に長い時間がかかる。",
    "台風が去った後、空気は澄んでいる。荒れた後にしか訪れない清澄さというものが、自然には確かにある。",
    "「知足者富（足るを知る者は富む）」——老子の言葉だ。豊かさの定義を問い直すことが、今の時代には求められている。",
    "蛍の光は、一匹では気づかれないほど弱い。しかし群れると、川辺が幻想的に輝く。小さな力の集積が、やがて光になる。",
    "イギリスの詩人ジョン・キーツは三十代を迎えずに世を去った。短い生涯に残した詩は、二百年後も読まれている。",
    "冬の海は荒れている。しかしその荒れた海の底では、何も変わらずに静かな世界が広がっている。",
    "「備えあれば憂いなし」という言葉は古くからある。しかし人は、晴れた日に傘のことを考えるのが苦手だ。",
    "ルネサンスの画家たちは、廃墟の中に美を見た。ローマの遺跡を描くことで、過去の偉大さと現在の自分を繋げようとした。",
    "朝露は太陽が昇ると消える。はかなさこそが美しい——そう感じる感性を、日本人は「もののあわれ」と呼んできた。",
    "「千里の道も一歩から」という荀子の言葉がある。大きな目標も、今日の一歩から始まる。当たり前のことだが、忘れがちだ。",
    "江戸時代の俳人、松尾芭蕉は旅に生きた。「旅に病んで夢は枯れ野を駆け巡る」——死の間際まで、彼の心は旅し続けた。",
    "ダーウィンは「最も強い者が生き残るのではない。変化に最もうまく適応した者が生き残る」と言ったとされる。進化の教えは、現代にも通じる。",
    "ブラジルには「saudade（サウダーデ）」という言葉がある。遠い日の懐かしさ、会えない人への想い——一語に込められた感情の豊かさよ。",
    "満月の夜、海の潮が動く。目に見えない引力が世界を動かしている。すべての現象には、見えない力が働いている。",
    "「人は城、人は石垣、人は堀」——武田信玄の言葉だ。どんな時代も、結局は人が組織の根幹だという真実は変わらない。",
    "雨が降ると、土の匂いがする。「ペトリコール」という言葉で科学者はこれを説明するが、あの懐かしい匂いは言葉を超えている。",
    "ゲーテは「もっと光を」という言葉を残したとされる。智恵と知識を求める人間の飢えは、死の瞬間まで消えない。",
    "日本の棋士たちは対局前に礼をする。礼で始まり、礼で終わる——競争の中に敬意を忘れない文化は、守る価値がある。",
    "北欧の人々は長い冬の間、「hygge（ヒュッゲ）」を大切にする。温かい灯りの下で、親しい人と過ごす時間の豊かさ。物質ではない豊かさの形だ。",
    "「温故知新」——古きを温め、新しきを知る。歴史の教科書が、現代のビジネス書になる。",
    "蟻は自分の体重の何十倍もの荷物を運ぶことができる。能力の限界は、自分が思っているより遥かに先にある。",
    "モネは晩年、白内障で視力を失いながらも絵を描き続けた。見えなくなった目で描いた絵が、最も美しいと言う人もいる。",
    "「艱難汝を玉にす」という言葉がある。磨かれていない原石が宝石になるには、摩擦が必要だ。",
    "日本の四季は、変化の中に美を見出す感性を育てた。固定しないこと、移ろうことを、むしろ豊かさと見なす視点がある。",
    "コペルニクスが地動説を発表したとき、世界の中心は地球ではないと知った人々の衝撃はいかばかりだったか。発見とは、自分の位置を問い直すことだ。",
    "「光陰矢の如し」——時間は矢のように過ぎる。気づけば季節が変わり、気づけば年が変わっている。",
    "アマゾンの密林では今日も知られていない生き物が生きている。人間が「知った」部分は、世界のほんの一部に過ぎない。",
    "新しい芽が古い幹から出る。伝統と革新は、対立するものではなく、根と葉のような関係なのかもしれない。",
    "春の霞がたなびく頃、人は何かを始めたくなる。終わりと始まりが同居する季節に、人の心は動かされやすい。",
    "キューリー夫人は「人生において、恐れるべきものは何もない。ただ理解すべきものがあるだけだ」と言った。知ることへの意志が、壁を溶かす。",
]

SHUNSHUU_CLOSERS = [
    # text: 締めの言葉, attr: 出典（実在の名言は帰属を明記、オリジナルは空文字）
    {'text': '問いを持ち続けることが、思考を生きた状態に保つ。',                                         'attr': ''},
    {'text': '過去を記憶できない者は、過去を繰り返す運命にある。',                                        'attr': 'ジョージ・サンタヤーナ（1863-1952）'},
    {'text': '万物は流転する。同じ川に、二度と入ることはできない。',                                      'attr': 'ヘラクレイトス（前540頃-前480頃）'},
    {'text': '一歩引いて全体を見る目と、深く掘り下げる根気。両方が今の時代には必要だ。',                   'attr': ''},
    {'text': '千里の行も、足下の一歩より始まる。',                                                       'attr': '老子（前6世紀頃）'},
    {'text': '未来は過去の上に立つ。今日を丁寧に生きることが、明日への贈り物になる。',                    'attr': ''},
    {'text': '人は忘れる生き物だ。だからこそ記録し、語り継ぐことに意味がある。',                          'attr': ''},
    {'text': '問題の解決より、問題の設定が難しい。本当の課題は何か、立ち止まって問い直したい。',           'attr': ''},
    {'text': '競争の先に協調がある。勝ちたいという欲求と、共存したいという知恵の折り合いが問われる。',     'attr': ''},
    {'text': 'メディアはメッセージである。技術は手段でなく、それ自体が環境を作り出す。',                   'attr': 'マーシャル・マクルーハン（1911-1980）'},
    {'text': '数字は真実の一部を語るが、すべてを語りはしない。見えないものを見ようとする想像力が求められる。', 'attr': ''},
    {'text': '誠実さは最善の策である。',                                                                 'attr': 'ベンジャミン・フランクリン（1706-1790）'},
    {'text': '多様性は摩擦を生むが、摩擦が火花を生む。異なるものが出会う場所に、革新は生まれる。',         'attr': ''},
    {'text': '長い目で見れば、誠実さはいつも報われる。そう信じて行動できるかどうかが、人の器を決める。',   'attr': ''},
    {'text': '今日の常識が明日の非常識になることは、歴史が証明している。柔軟な心を保ちたい。',            'attr': ''},
    {'text': '困難は、突破したときにだけ「試練だった」と呼べる。進行中は、ただ苦しいだけだ。',            'attr': ''},
    {'text': '言葉は思想の衣装である。',                                                                 'attr': 'サミュエル・ジョンソン（1709-1784）'},
    {'text': '自分の立つ場所を知ることが、遠くへ進む第一歩になる。',                                     'attr': ''},
    {'text': '大きな変化は、小さな兆しから始まる。今日の小さなニュースが、明日の大きな流れの始まりかもしれない。', 'attr': ''},
    {'text': '運とは、準備が機会に出会ったときに生まれる。',                                              'attr': 'セネカ（前4頃-65）'},
    {'text': '旅人よ、道はない。歩くことで道はできる。',                                                  'attr': 'アントニオ・マチャード（1875-1939）'},
    {'text': '知識への投資こそが、最も利益をもたらす。',                                                  'attr': 'ベンジャミン・フランクリン（1706-1790）'},
    {'text': '無知の知こそが、知恵の出発点である。',                                                      'attr': 'ソクラテス（前470頃-前399）'},
    {'text': '私は失敗したことがない。うまくいかない方法を一万通り発見しただけだ。',                       'attr': 'トーマス・エジソン（1847-1931）'},
    {'text': 'すべての真の生は、出会いである。',                                                          'attr': 'マルティン・ブーバー（1878-1965）'},
    {'text': '問いそのものを愛してください。いつかは、答えの中に生きることができるでしょう。',              'attr': 'ライナー・マリア・リルケ（1875-1926）'},
    {'text': '対話は時に難しいが、沈黙はもっと難しい。言葉を探し続けることが、関係を生きた状態に保つ。',   'attr': ''},
    {'text': '今という瞬間は、二度と戻らない。だからこそ今が尊い。',                                      'attr': ''},
    {'text': '急いで出した答えより、じっくり育てた問いの方が長く生きる。',                                 'attr': ''},
    {'text': '人間の欲望は終わらない。だからこそ節度という知恵が生まれた。欲と節度の間に、文明がある。',   'attr': ''},
]


def get_shunshuu(news: dict) -> dict:
    """悠翔設計：余白コラム（書き出し＋RSS記事の橋渡し）"""
    day = datetime.now(JST).timetuple().tm_yday
    opener_item = SHUNSHUU_OPENERS[day % len(SHUNSHUU_OPENERS)]
    closer_item = SHUNSHUU_CLOSERS[day % len(SHUNSHUU_CLOSERS)]
    # 文化・科学 → 国内・社会 → 経済 の優先度で記事を選ぶ
    pool = (news.get('文化・科学', []) +
            news.get('国内・社会', []) +
            news.get('経済', []))
    article = pool[0] if pool else None
    # opener が dict 形式（text / wiki）か文字列かに対応
    if isinstance(opener_item, dict):
        opener_text = opener_item.get('text', '')
        opener_wiki = opener_item.get('wiki', '')
    else:
        opener_text = opener_item
        opener_wiki = ''
    # closer が dict 形式（text / attr）か文字列かに対応
    if isinstance(closer_item, dict):
        closer_text = closer_item.get('text', '')
        closer_attr = closer_item.get('attr', '')
    else:
        closer_text = closer_item
        closer_attr = ''
    # 余白の画像：opener の内容を最優先にする
    # ① opener に Wikipedia タイトルがあれば Wikipedia サムネ
    # ② なければ opener テキストのキーワードで Pixabay
    # ③ どちらもなければ記事画像（記事は内容が無関係なことが多いため最後）
    image = ''
    if opener_wiki:
        image = get_wiki_thumb(opener_wiki)
    if not image and PIXABAY_KEY and opener_text:
        image = get_pixabay_image(extract_keyword(opener_text))
    if not image and article and article.get('image'):
        image = article['image']
    return {
        'opener':      opener_text,
        'closer':      closer_text,
        'closer_attr': closer_attr,
        'article':     article,
        'image':       image,
    }


# ─── 今日は何の日（MMDD → イベント名）365日版 ─────────────────────
# template 側で「今日は何の日：{値}」と表示する

TODAY_SPECIAL: dict[str, str] = {
    # 1月
    '0101': '元旦',
    '0102': 'ベルリン・フィル初演奏会（1882年）',
    '0103': '王政復古の大号令（1868年）',
    '0104': 'ニュートン誕生（1643年）',
    '0105': '渋沢栄一誕生（1840年）',
    '0106': '東京が「東京府」に改称（1868年）',
    '0107': '人日の節句・七草がゆの日',
    '0108': 'エルヴィス・プレスリー誕生（1935年）',
    '0109': '血の日曜日・ロシア革命の契機（1905年）',
    '0110': '国際連合第1回総会（1946年）',
    '0111': 'インスリン初の臨床使用（1922年）',
    '0112': 'ハイチ大地震（2010年）',
    '0113': '伊能忠敬誕生（1745年）',
    '0114': '日本初の鉄道計画発表（1869年）',
    '0115': '阪神・淡路大震災（1995年）',
    '0116': '米国で禁酒法施行（1920年）',
    '0117': '阪神・淡路大震災M7.3（1995年）',
    '0118': '夏目漱石「坊っちゃん」連載開始（1906年）',
    '0119': '松尾芭蕉誕生（1644年）',
    '0120': '大寒・最も寒さが厳しい時季',
    '0121': 'ルイ16世処刑（1793年）',
    '0122': 'ヴィクトリア女王崩御（1901年）',
    '0123': '日本初の気象観測所設置（1875年）',
    '0124': 'カリフォルニア・ゴールドラッシュ（1848年）',
    '0125': 'ロバート・バーンズ誕生（バーンズの夜・1759年）',
    '0126': 'オーストラリアの日（建国記念日）',
    '0127': '国際ホロコースト記念日・アウシュビッツ解放（1945年）',
    '0128': 'スペースシャトル「チャレンジャー」爆発（1986年）',
    '0129': '日本初の鉄道連絡船就航（1908年）',
    '0130': 'ヒトラーがドイツ首相に就任（1933年）',
    '0131': '米初の人工衛星「エクスプローラー1号」打ち上げ（1958年）',
    # 2月
    '0201': 'リンカーン・奴隷解放修正条項に署名（1865年）',
    '0202': 'Facebookサービス開始（2004年）',
    '0203': '節分',
    '0204': '立春・暦の上で春の始まり',
    '0205': '天然痘根絶宣言（WHO・1980年）',
    '0206': 'ワイタンギ条約・ニュージーランド建国（1840年）',
    '0207': 'マーストリヒト条約調印・EU創設（1992年）',
    '0208': '日露戦争開戦（1904年）',
    '0209': '帝国議会第1回開催（1891年）',
    '0210': '日本、国際連盟を脱退（1933年）',
    '0211': '建国記念の日',
    '0212': 'ダーウィン誕生（1809年）',
    '0213': '手塚治虫没（1989年）',
    '0214': 'バレンタインデー',
    '0215': 'USS メイン号爆発・米西戦争の契機（1898年）',
    '0216': 'ツタンカーメンの墓室開封（1923年）',
    '0217': 'プッチーニ「蝶々夫人」初演（1904年）',
    '0218': '冥王星発見（1930年）',
    '0219': '日本、東京裁判終結後の刑執行（1948年）',
    '0220': '福沢諭吉没（1901年）',
    '0221': 'マルクス＆エンゲルス「共産党宣言」発表（1848年）',
    '0222': '猫の日・にゃんにゃんにゃん',
    '0223': '天皇誕生日（令和）',
    '0224': 'エドワード・ジェンナー誕生（1749年）',
    '0225': 'フルシチョフ「スターリン批判」演説（1956年）',
    '0226': '二・二六事件（1936年）',
    '0227': 'スタインベック誕生（1902年）',
    '0228': 'DNAの二重らせん構造解明（ワトソン＆クリック・1953年）',
    '0229': 'うるう日・4年に1度の特別な日',
    # 3月
    '0301': '第五福竜丸・ビキニ環礁で被爆（1954年）',
    '0302': 'テキサス独立宣言（1836年）',
    '0303': 'ひな祭り・桃の節句',
    '0304': '上野に国立博物館開館（1872年）',
    '0305': 'チャーチル「鉄のカーテン」演説（1946年）',
    '0306': 'ミケランジェロ誕生（1475年）',
    '0307': 'ベル・電話の特許取得（1876年）',
    '0308': '国際女性デー',
    '0309': '東京大空襲・最初の夜（1945年）',
    '0310': '東京大空襲・10万人超が犠牲（1945年）',
    '0311': '東日本大震災M9.0（2011年）',
    '0312': 'ガールスカウト創立（米国・1912年）',
    '0313': '天王星発見（ハーシェル・1781年）',
    '0314': 'ホワイトデー / π（円周率）の日',
    '0315': '世界消費者デー',
    '0316': 'マゼラン・フィリピン到達（1521年）',
    '0317': '聖パトリックの日・アイルランド',
    '0318': '人類初の宇宙遊泳（レオーノフ・1965年）',
    '0319': '日本・国際連盟規約に調印（1919年）',
    '0320': '春分の日',
    '0321': 'バッハ誕生（1685年）',
    '0322': '世界水の日（国連）',
    '0323': '世界気象デー（WMO）',
    '0324': '世界結核デー（WHO）',
    '0325': 'ローマ条約調印・欧州共同体設立（1957年）',
    '0326': 'ベートーヴェン没（1827年）',
    '0327': '国際演劇デー（ITI）',
    '0328': 'スリーマイル島原発事故（1979年）',
    '0329': 'サンフランシスコ講和条約調印（1951年）',
    '0330': 'アラスカ購入・米国がロシアから（1867年）',
    '0331': 'エッフェル塔完成（1889年）',
    # 4月
    '0401': 'エイプリルフール / 新年度スタート',
    '0402': '携帯電話の初通話（マーティン・クーパー・1973年）',
    '0403': '日本初の女性雑誌「婦人公論」創刊（1916年）',
    '0404': 'キング牧師暗殺（1968年）',
    '0405': 'チャーチル英首相辞任（1955年）',
    '0406': '近代オリンピック第1回開幕・アテネ（1896年）',
    '0407': '世界保健デー（WHO設立記念）',
    '0408': '灌仏会・花祭り・お釈迦様の誕生日',
    '0409': '黒田清輝誕生（1866年）',
    '0410': 'タイタニック号出航（1912年）',
    '0411': 'ナポレオン退位（1814年）',
    '0412': 'ガガーリン・世界初の有人宇宙飛行（1961年）',
    '0413': 'トーマス・ジェファーソン誕生（1743年）',
    '0414': 'リンカーン大統領狙撃（1865年）',
    '0415': 'タイタニック号沈没（1912年）',
    '0416': 'レーニン・ロシア帰国・四月テーゼ（1917年）',
    '0417': '日清戦争終結・下関条約（1895年）',
    '0418': 'サンフランシスコ大地震（1906年）',
    '0419': '大塩平八郎の乱（1837年）',
    '0420': 'コロンバイン高校銃乱射事件（1999年）',
    '0421': 'エリザベス2世誕生（1926年）',
    '0422': 'アースデー・地球の日',
    '0423': '世界本の日 / シェイクスピア誕生日',
    '0424': 'ハッブル宇宙望遠鏡打ち上げ（1990年）',
    '0425': 'DNAの二重らせん論文・Nature誌掲載（1953年）',
    '0426': 'チェルノブイリ原発爆発事故（1986年）',
    '0427': 'モールス誕生（1791年）',
    '0428': '日本主権回復の日・講和条約発効（1952年）',
    '0429': '昭和の日',
    '0430': '今上天皇陛下御即位・令和元年（2019年）',
    # 5月
    '0501': '令和改元 / メーデー・国際労働者の日',
    '0502': 'レオナルド・ダ・ヴィンチ没（1519年）',
    '0503': '憲法記念日',
    '0504': 'みどりの日',
    '0505': 'こどもの日',
    '0506': 'ヒンデンブルク号爆発事故（1937年）',
    '0507': 'チャイコフスキー誕生（1840年）',
    '0508': '世界赤十字デー / ナチス・ドイツ無条件降伏（1945年）',
    '0509': '欧州の日・ローベール・シューマン宣言（1950年）',
    '0510': '大陸横断鉄道開通・米国（1869年）',
    '0511': 'チェスAI「ディープブルー」がカスパロフに勝利（1997年）',
    '0512': 'ナイチンゲール誕生（1820年）/ 看護師の日',
    '0513': 'F1グランプリ初開催・シルバーストーン（1950年）',
    '0514': 'イスラエル建国（1948年）',
    '0515': '沖縄・本土復帰（1972年）',
    '0516': '田部井淳子・女性初のエベレスト登頂（1975年）',
    '0517': '世界高血圧デー',
    '0518': 'セント・ヘレンズ山噴火・米国（1980年）',
    '0519': '伊藤博文・初代内閣総理大臣就任（1885年）',
    '0520': '世界ミツバチの日',
    '0521': '小倉百人一首の完成（1235年）',
    '0522': 'ビクトル・ユゴー没（1885年）',
    '0523': 'コペルニクス没（1543年）',
    '0524': 'モールス信号で初の電信「神が何を成し給えるか」（1844年）',
    '0525': '映画「スター・ウォーズ」公開（1977年）',
    '0526': '排日移民法成立・米国（1924年）',
    '0527': '日本海海戦・日本艦隊大勝利（1905年）',
    '0528': 'ダンケルク撤退作戦開始（1940年）',
    '0529': 'エベレスト初登頂・ヒラリーとテンジン（1953年）',
    '0530': 'ジャンヌ・ダルク処刑（1431年）',
    '0531': 'ビッグベン時計台が時を刻み始める・ロンドン（1859年）',
    # 6月
    '0601': '世界牛乳の日',
    '0602': 'マルコーニ・無線電信の特許取得（1896年）',
    '0603': 'フランツ・カフカ没（1924年）',
    '0604': '天安門事件（1989年）',
    '0605': '世界環境デー（国連）',
    '0606': 'Dデイ・ノルマンディー上陸作戦（1944年）',
    '0607': 'アラン・チューリング没（1954年）',
    '0608': '世界海洋デー（国連）',
    '0609': '日本でのテレビ放送記念日（1953年）',
    '0610': '時の記念日',
    '0611': 'ヘンリー8世とキャサリン結婚（1509年）',
    '0612': '世界児童労働反対デー（ILO）',
    '0613': '小泉純一郎内閣発足（2001年）',
    '0614': '世界献血者デー / 川端康成誕生（1899年）',
    '0615': 'マグナ・カルタ署名（1215年）',
    '0616': 'テレシコワ・女性初の宇宙飛行（1963年）',
    '0617': 'おむすびの日',
    '0618': 'ワーテルローの戦い・ナポレオン最後の戦い（1815年）',
    '0619': 'ジューンティーンス・米国奴隷解放記念日',
    '0620': '世界難民デー（UNHCR）',
    '0621': '夏至・一年で最も昼が長い日',
    '0622': '独仏休戦協定調印（1940年）',
    '0623': '沖縄戦終結・慰霊の日（1945年）',
    '0624': 'ベルリン封鎖始まる（1948年）',
    '0625': '朝鮮戦争勃発（1950年）',
    '0626': '国際連合憲章調印（1945年）',
    '0627': 'ヘレン・ケラー誕生（1880年）',
    '0628': 'サラエボ事件・第一次世界大戦の引き金（1914年）',
    '0629': 'iPhoneの初代発売（2007年）',
    '0630': 'アポロ計画発表（ケネディ・1961年）',
    # 7月
    '0701': 'カナダ建国記念日 / 香港返還（1997年）',
    '0702': 'アメリア・イアハート消息不明（1937年）',
    '0703': 'ゲティスバーグの戦い終結（1863年）',
    '0704': '米国独立記念日',
    '0705': 'エルヴィス・プレスリー初のシングル録音（1954年）',
    '0706': 'ヤン・フス火刑（1415年）',
    '0707': '七夕 / 盧溝橋事件・日中戦争の発端（1937年）',
    '0708': '安倍晋三元首相銃撃事件（2022年）',
    '0709': 'アルゼンチン独立（1816年）',
    '0710': 'プルースト誕生（1871年）',
    '0711': '世界人口デー（国連）',
    '0712': '日本初の電話交換業務開始（1890年）',
    '0713': '第1回FIFAワールドカップ開幕・ウルグアイ（1930年）',
    '0714': 'フランス革命記念日・バスティーユの日',
    '0715': '海の日（第3月曜日）',
    '0716': 'トリニティ実験・世界初の核爆発（1945年）',
    '0717': 'ポツダム会談開始・米英ソ首脳（1945年）',
    '0718': 'スペイン内戦勃発（1936年）',
    '0719': '司馬遼太郎誕生（1923年）',
    '0720': 'アポロ11号・月面着陸（1969年）',
    '0721': 'ニール・アームストロング・月面歩行（1969年）',
    '0722': '黒沢明誕生（1910年）',
    '0723': '初の衛星生中継・テルスター（1962年）',
    '0724': '谷崎潤一郎誕生（1886年）',
    '0725': '世界初の試験管ベビー誕生（1978年）',
    '0726': '米国郵便局創設（1775年）',
    '0727': '朝鮮戦争休戦協定調印（1953年）',
    '0728': '第一次世界大戦開戦（1914年）',
    '0729': 'ロンドン五輪開幕・戦後初（1948年）',
    '0730': '国連憲章発効（1945年）',
    '0731': 'J・K・ローリング誕生（1965年）',
    # 8月
    '0801': 'ベルリン五輪開会式・初のテレビ中継（1936年）/ 独がロシアに宣戦布告（1914年）',
    '0802': 'イラク・クウェートに侵攻（1990年）',
    '0803': 'コロンブス・初の航海出発（1492年）',
    '0804': '日本初の女子留学生・米国へ出発（1871年）',
    '0805': 'マリリン・モンロー没（1962年）',
    '0806': '広島に原爆投下（1945年）',
    '0807': '立秋・暦の上で秋の始まり',
    '0808': '今上天皇・生前退位の意向表明（2016年）',
    '0809': '長崎に原爆投下（1945年）',
    '0810': 'スミソニアン博物館設立（1846年）',
    '0811': '山の日',
    '0812': '日航ジャンボ機墜落・520名死亡（1985年）',
    '0813': 'お盆',
    '0814': '日本・ポツダム宣言受諾を表明（1945年）',
    '0815': '終戦記念日（1945年）',
    '0816': 'エルヴィス・プレスリー没（1977年）',
    '0817': 'インドネシア独立宣言（1945年）',
    '0818': '米国女性参政権・修正第19条批准（1920年）',
    '0819': '初のカラーテレビ放送・米国CBS（1950年）',
    '0820': 'レーニン・ロシア革命の指導者誕生（1940年）',
    '0821': 'ハワイ・米国50番目の州に（1959年）',
    '0822': '赤十字国際委員会設立（1864年）',
    '0823': 'ウィリアム・ウォレス処刑（1305年）',
    '0824': '英国軍・ワシントンD.C.を焼き打ち（1814年）',
    '0825': 'ボイジャー2号・海王星通過（1989年）',
    '0826': 'フランス人権宣言採択（1789年）',
    '0827': 'クラカタウ火山大噴火（1883年）',
    '0828': 'キング牧師「I Have a Dream」演説（1963年）',
    '0829': 'ソ連・初の核実験成功（1949年）',
    '0830': '菊池寛誕生（1888年）',
    '0831': 'ダイアナ妃事故死（1997年）',
    # 9月
    '0901': '関東大震災（1923年）/ 防災の日',
    '0902': '日本降伏文書調印・第二次世界大戦終結（1945年）',
    '0903': 'ドラえもんの誕生日（設定上の生年2112年）',
    '0904': '初のコダック・カメラ販売（1888年）',
    '0905': 'マザー・テレサ没（1997年）',
    '0906': 'メイフラワー号出航（1620年）',
    '0907': 'エリザベス1世誕生（1533年）',
    '0908': 'スタートレック初放送（1966年）',
    '0909': '救急の日',
    '0910': '世界自殺予防デー（国際自殺予防協会）',
    '0911': '米国同時多発テロ（2001年）',
    '0912': 'ケネディ「我々は月へ行く」演説（1962年）',
    '0913': '伊藤博文暗殺・ハルビン（1909年）',
    '0914': '世界水路デー（国際水路機関）',
    '0915': '敬老の日（第3月曜日）',
    '0916': '日本初の地下鉄・上野〜浅草（1927年）',
    '0917': '米国憲法制定（1787年）',
    '0918': '満州事変・柳条湖事件（1931年）',
    '0919': '夏目漱石没（1916年）',
    '0920': 'マゼラン艦隊・世界一周に出発（1519年）',
    '0921': '薩長同盟成立・坂本龍馬仲介（1866年）',
    '0922': '秋分の日（年により異なる）',
    '0923': '秋分の日',
    '0924': 'ブラック・フライデー・金価格操作（米1869年）',
    '0925': 'バルボア・太平洋到達（1513年）',
    '0926': '洞爺丸台風・1,155名死亡（1954年）',
    '0927': '世界観光デー（国連）',
    '0928': '孔子誕生（紀元前551年）',
    '0929': 'ロンドン警視庁創設（1829年）',
    '0930': 'ジェームズ・ディーン事故死（1955年）',
    # 10月
    '1001': '中華人民共和国成立（1949年）',
    '1002': 'ガンジー誕生（1869年）/ 国際非暴力デー',
    '1003': '東西ドイツ統一（1990年）',
    '1004': '世界動物デー / スプートニク1号打ち上げ（1957年）',
    '1005': 'スティーブ・ジョブズ没（2011年）',
    '1006': '初のトーキー映画「ジャズ・シンガー」公開（1927年）',
    '1007': 'レパントの海戦・オスマン帝国 vs 欧州（1571年）',
    '1008': 'スポーツの日（第2月曜日）',
    '1009': 'チェ・ゲバラ処刑（1967年）',
    '1010': '東京オリンピック開幕（1964年）',
    '1011': 'ボーア戦争勃発（1899年）',
    '1012': 'コロンブス・アメリカ大陸到達（1492年）',
    '1013': 'チリ落盤事故・33名全員救出（2010年）',
    '1014': 'ヘイスティングズの戦い（1066年）',
    '1015': '世界手洗いの日（ユニセフ）',
    '1016': '世界食料デー（FAO）',
    '1017': '世界貧困撲滅デー（国連）',
    '1018': 'アラスカ・米国に正式移管（1867年）',
    '1019': 'ヨークタウンの戦い・米独立戦争実質終結（1781年）',
    '1020': 'シドニー・オペラハウス開業（1973年）',
    '1021': 'エジソン・電球の実用実験成功（1879年）',
    '1022': 'キューバ危機・ケネディが海上封鎖命令（1962年）',
    '1023': 'ベイルート兵舎爆破テロ（1983年）',
    '1024': '国連デー・国際連合発足（1945年）',
    '1025': 'アジャンクールの戦い（1415年）',
    '1026': 'エリー運河開通・米国（1825年）',
    '1027': 'ニューヨーク地下鉄開業（1904年）',
    '1028': 'ウォール街大暴落・暗黒の火曜日（1929年）',
    '1029': 'ARPANET初のメッセージ送信・インターネットの誕生（1969年）',
    '1030': '世界節約デー',
    '1031': 'ハロウィン / ルターの宗教改革・95か条の論題（1517年）',
    # 11月
    '1101': '犬の日・ワンワンワン',
    '1102': '国際宇宙ステーション常駐開始（2000年）',
    '1103': '文化の日 / パナマ独立（1903年）',
    '1104': 'オバマ・米大統領選当選（2008年）',
    '1105': 'ガイ・フォークス事件・火薬陰謀事件（1605年）',
    '1106': '鑑真和上・日本に到着（754年）',
    '1107': 'ロシア革命（十月革命）/ マリー・キュリー誕生（1867年）',
    '1108': 'レントゲン・X線発見（1895年）',
    '1109': 'ベルリンの壁崩壊（1989年）',
    '1110': 'スタンリー・アフリカでリビングストンを発見（1871年）',
    '1111': '第一次世界大戦終結（1918年）/ 退役軍人の日',
    '1112': '宮沢賢治誕生（1896年）',
    '1113': 'パリ同時多発テロ（2015年）',
    '1114': 'ネール・インド初代首相就任（1947年）',
    '1115': '七五三',
    '1116': '国際寛容デー（ユネスコ）',
    '1117': 'スエズ運河開通（1869年）',
    '1118': 'ミッキーマウス・蒸気船ウィリーデビュー（1928年）',
    '1119': 'ゲティスバーグ演説・リンカーン（1863年）',
    '1120': '世界こどもの日（国連）/ ニュルンベルク裁判開始（1945年）',
    '1121': '気球による初の有人飛行・モンゴルフィエ兄弟（1783年）',
    '1122': 'ケネディ大統領暗殺（1963年）',
    '1123': '勤労感謝の日',
    '1124': 'ダーウィン「種の起源」出版（1859年）',
    '1125': '坂本龍馬暗殺（1867年）',
    '1126': 'ツタンカーメンの墓の扉を開ける（1922年）',
    '1127': '第1回十字軍遠征呼びかけ（1095年）',
    '1128': '宮崎駿誕生（1941年）',
    '1129': '国連・パレスチナ分割決議採択（1947年）',
    '1130': 'マイケル・ジャクソン「スリラー」発売（1982年）',
    # 12月
    '1201': 'ローザ・パークス・バス乗車拒否で逮捕・公民権運動（1955年）',
    '1202': 'ナポレオン皇帝即位（1804年）',
    '1203': '国際障害者デー / ボパール化学工場事故・インド（1984年）',
    '1204': 'モーツァルト没（1791年）',
    '1205': '米国・禁酒法廃止（1933年）',
    '1206': '奴隷解放修正条項・修正第13条批准（1865年）',
    '1207': '真珠湾攻撃・大東亜戦争開戦（1941年）',
    '1208': 'ジョン・レノン射殺（1980年）/ 日本・英米に宣戦布告（1941年）',
    '1209': '国連腐敗防止条約発効（2005年）',
    '1210': '世界人権デー / ノーベル賞授賞式',
    '1211': 'ユニセフ設立（1946年）',
    '1212': 'ワシントンD.C.・米国首都に制定（1800年）',
    '1213': '南京事件（1937年）',
    '1214': '南極点到達・アムンセン（1911年）',
    '1215': '米国権利章典批准・修正第1〜10条（1791年）',
    '1216': 'ボストン茶会事件（1773年）',
    '1217': 'ライト兄弟・動力飛行初成功（1903年）',
    '1218': 'メイフラワー号・プリマスに到着（1620年）',
    '1219': 'クリントン大統領弾劾決議・下院（1998年）',
    '1220': 'ルイジアナ購入完了・米国（1803年）',
    '1221': '冬至・一年で最も夜が長い日',
    '1222': '冬至の翌日・陽の光が少しずつ伸び始める',
    '1223': '天皇誕生日（平成）',
    '1224': '「きよしこの夜」初演奏（1818年）',
    '1225': 'クリスマス / ソビエト連邦崩壊（1991年）',
    '1226': 'ボクシングデー（英国祝日）',
    '1227': 'ヨハネス・ケプラー誕生（1571年）',
    '1228': '官庁御用納め',
    '1229': 'テキサス・米国28番目の州に（1845年）',
    '1230': '宮沢賢治「銀河鉄道の夜」掲載誌発行年（1934年）',
    '1231': '大晦日 / エジソン・電球の公開デモ（1879年）',
}


# ─── 今日は何の日 → Wikipedia 記事タイトルのマッピング ────────────
# キーは MMDD。値は日本語 Wikipedia のページタイトル。

TODAY_SPECIAL_WIKI: dict[str, str] = {
    '0101': '元日',
    '0104': 'アイザック・ニュートン',
    '0107': '人日',
    '0111': 'インスリン',
    '0115': '阪神・淡路大震災',
    '0120': '大寒',
    '0121': 'ルイ16世',
    '0125': 'ロバート・バーンズ',
    '0126': 'オーストラリアの日',
    '0127': 'ホロコースト',
    '0128': 'スペースシャトル・チャレンジャー号爆発事故',
    '0130': 'アドルフ・ヒトラー',
    '0131': 'エクスプローラー1号',
    '0202': 'Facebook',
    '0203': '節分',
    '0204': '立春',
    '0205': '天然痘',
    '0206': 'ワイタンギ条約',
    '0207': 'マーストリヒト条約',
    '0208': '日露戦争',
    '0211': '建国記念日',
    '0212': 'チャールズ・ダーウィン',
    '0213': '手塚治虫',
    '0214': 'バレンタインデー',
    '0216': 'ツタンカーメン',
    '0218': '冥王星',
    '0220': '福沢諭吉',
    '0221': '共産党宣言',
    '0222': '猫の日',
    '0223': '天皇誕生日',
    '0224': 'エドワード・ジェンナー',
    '0225': 'スターリン批判',
    '0226': '二・二六事件',
    '0228': 'デオキシリボ核酸',
    '0301': '第五福竜丸',
    '0303': '雛祭り',
    '0305': '鉄のカーテン',
    '0306': 'ミケランジェロ',
    '0307': '電話',
    '0308': '国際女性デー',
    '0309': '東京大空襲',
    '0311': '東日本大震災',
    '0313': '天王星',
    '0314': '円周率',
    '0316': 'フェルディナンド・マゼラン',
    '0317': '聖パトリックの祝日',
    '0318': '宇宙遊泳',
    '0320': '春分の日',
    '0321': 'ヨハン・ゼバスティアン・バッハ',
    '0325': 'ローマ条約',
    '0326': 'ルートヴィヒ・ヴァン・ベートーヴェン',
    '0328': 'スリーマイル島原子力発電所事故',
    '0329': 'サンフランシスコ平和条約',
    '0330': 'アラスカ',
    '0331': 'エッフェル塔',
    '0401': 'エイプリルフール',
    '0402': '携帯電話',
    '0404': 'マーティン・ルーサー・キング・ジュニア',
    '0406': '近代オリンピック',
    '0407': '世界保健機関',
    '0408': '花祭り',
    '0410': 'タイタニック',
    '0411': 'ナポレオン1世',
    '0412': 'ユーリ・ガガーリン',
    '0413': 'トーマス・ジェファーソン',
    '0414': 'エイブラハム・リンカーン',
    '0415': 'タイタニック',
    '0416': 'ウラジーミル・レーニン',
    '0417': '日清戦争',
    '0418': 'サンフランシスコ地震',
    '0420': 'コロンバイン高校銃乱射事件',
    '0421': 'エリザベス2世',
    '0422': 'アースデー',
    '0423': 'ウィリアム・シェイクスピア',
    '0424': 'ハッブル宇宙望遠鏡',
    '0425': 'デオキシリボ核酸',
    '0426': 'チェルノブイリ原子力発電所事故',
    '0427': 'サミュエル・モールス',
    '0429': '昭和の日',
    '0501': 'メーデー',
    '0503': '日本国憲法',
    '0504': 'みどりの日',
    '0505': 'こどもの日',
    '0506': 'ヒンデンブルク号爆発事故',
    '0507': 'ピョートル・チャイコフスキー',
    '0508': '国際赤十字・赤新月社運動',
    '0509': 'ロベール・シューマン',
    '0510': '大陸横断鉄道',
    '0511': 'ディープ・ブルー_(コンピュータ)',
    '0512': 'フロレンス・ナイチンゲール',
    '0513': 'フォーミュラ1',
    '0514': 'イスラエル',
    '0515': '沖縄返還',
    '0516': '田部井淳子',
    '0518': 'セント・ヘレンズ山',
    '0519': '伊藤博文',
    '0520': 'ミツバチ',
    '0521': '小倉百人一首',
    '0522': 'ヴィクトル・ユゴー',
    '0523': 'ニコラウス・コペルニクス',
    '0524': 'モールス信号',
    '0525': 'スター・ウォーズ',
    '0526': '排日移民法',
    '0527': '日本海海戦',
    '0528': 'ダンケルクの戦い',
    '0529': 'エベレスト',
    '0530': 'ジャンヌ・ダルク',
    '0531': 'ビッグ・ベン',
    '0602': 'グリエルモ・マルコーニ',
    '0603': 'フランツ・カフカ',
    '0604': '天安門事件',
    '0605': '世界環境デー',
    '0606': 'ノルマンディー上陸作戦',
    '0607': 'アラン・チューリング',
    '0610': '時の記念日',
    '0614': '川端康成',
    '0615': 'マグナ・カルタ',
    '0616': 'ワレンチナ・テレシコワ',
    '0618': 'ワーテルローの戦い',
    '0619': 'ジューンティーンス',
    '0621': '夏至',
    '0623': '沖縄慰霊の日',
    '0624': 'ベルリン封鎖',
    '0625': '朝鮮戦争',
    '0626': '国際連合憲章',
    '0627': 'ヘレン・ケラー',
    '0628': 'サラエボ事件',
    '0629': 'iPhone',
    '0630': 'アポロ計画',
    '0701': '香港返還',
    '0702': 'アメリア・イアハート',
    '0703': 'ゲティスバーグの戦い',
    '0704': '独立記念日 (アメリカ合衆国)',
    '0705': 'エルヴィス・プレスリー',
    '0706': 'ヤン・フス',
    '0707': '七夕',
    '0708': '安倍晋三銃撃事件',
    '0710': 'マルセル・プルースト',
    '0714': 'フランス革命',
    '0716': 'トリニティ実験',
    '0717': 'ポツダム会談',
    '0718': 'スペイン内戦',
    '0719': '司馬遼太郎',
    '0720': 'アポロ11号',
    '0722': '黒澤明',
    '0724': '谷崎潤一郎',
    '0725': '試験管ベビー',
    '0727': '朝鮮戦争',
    '0728': '第一次世界大戦',
    '0731': 'J・K・ローリング',
    '0801': '第一次世界大戦',
    '0802': 'クウェート侵攻',
    '0803': 'クリストファー・コロンブス',
    '0805': 'マリリン・モンロー',
    '0806': '広島市への原子爆弾投下',
    '0807': '立秋',
    '0809': '長崎市への原子爆弾投下',
    '0811': '山の日',
    '0812': '日本航空123便墜落事故',
    '0813': 'お盆',
    '0815': '玉音放送',
    '0816': 'エルヴィス・プレスリー',
    '0817': 'インドネシア独立宣言',
    '0820': 'ウラジーミル・レーニン',
    '0822': '赤十字国際委員会',
    '0825': 'ボイジャー2号',
    '0826': 'フランス人権宣言',
    '0828': 'マーティン・ルーサー・キング・ジュニア',
    '0830': '菊池寛',
    '0831': 'ダイアナ (プリンセス・オブ・ウェールズ)',
    '0901': '関東大震災',
    '0902': '降伏文書',
    '0903': 'ドラえもん',
    '0905': 'マザー・テレサ',
    '0906': 'メイフラワー号',
    '0907': 'エリザベス1世',
    '0908': 'スタートレック',
    '0909': '救急の日',
    '0911': 'アメリカ同時多発テロ事件',
    '0913': '伊藤博文',
    '0916': '東京地下鉄',
    '0917': 'アメリカ合衆国憲法',
    '0918': '満州事変',
    '0920': 'フェルディナンド・マゼラン',
    '0922': '秋分の日',
    '0928': '孔子',
    '0929': 'ロンドン警視庁',
    '0930': 'ジェームズ・ディーン',
    '1001': '中華人民共和国',
    '1002': 'マハトマ・ガンジー',
    '1003': 'ドイツ再統一',
    '1004': 'スプートニク1号',
    '1005': 'スティーブ・ジョブズ',
    '1007': 'レパントの海戦',
    '1009': 'チェ・ゲバラ',
    '1010': '1964年東京オリンピック',
    '1012': 'クリストファー・コロンブス',
    '1013': 'コピアポ落盤事故',
    '1014': 'ヘイスティングズの戦い',
    '1020': 'シドニー・オペラハウス',
    '1021': 'トーマス・エジソン',
    '1022': 'キューバ危機',
    '1024': '国際連合',
    '1028': '世界恐慌',
    '1029': 'ARPANET',
    '1031': 'ハロウィン',
    '1102': '国際宇宙ステーション',
    '1103': '文化の日',
    '1105': 'ガイ・フォークス事件',
    '1107': 'ロシア革命',
    '1108': 'ヴィルヘルム・レントゲン',
    '1109': 'ベルリンの壁',
    '1111': '第一次世界大戦',
    '1112': '宮沢賢治',
    '1115': '七五三',
    '1117': 'スエズ運河',
    '1118': 'ミッキーマウス',
    '1119': 'ゲティスバーグ演説',
    '1120': 'ニュルンベルク裁判',
    '1121': '熱気球',
    '1122': 'ジョン・F・ケネディ',
    '1123': '勤労感謝の日',
    '1124': '種の起源',
    '1125': '坂本龍馬',
    '1128': '宮崎駿',
    '1201': 'ローザ・パークス',
    '1202': 'ナポレオン1世',
    '1204': 'ヴォルフガング・アマデウス・モーツァルト',
    '1205': '禁酒法',
    '1207': '真珠湾攻撃',
    '1208': 'ジョン・レノン',
    '1210': '世界人権デー',
    '1211': 'ユニセフ',
    '1213': '南京事件',
    '1214': 'ロアール・アムンセン',
    '1215': '権利章典',
    '1216': 'ボストン茶会事件',
    '1217': 'ライト兄弟',
    '1220': 'ルイジアナ購入',
    '1221': '冬至',
    '1224': 'きよしこの夜',
    '1225': 'クリスマス',
    '1227': 'ヨハネス・ケプラー',
    '1231': '大晦日',
    # ─── 追加分（122件補完） ───────────────────────────────────────
    # 1月
    '0102': 'ベルリン・フィルハーモニー管弦楽団',
    '0103': '王政復古の大号令',
    '0105': '渋沢栄一',
    '0106': '東京都',
    '0108': 'エルヴィス・プレスリー',
    '0109': '血の日曜日_(1905年)',
    '0110': '国際連合',
    '0112': '2010年ハイチ地震',
    '0113': '伊能忠敬',
    '0114': '日本の鉄道',
    '0116': '禁酒法',
    '0117': '阪神・淡路大震災',
    '0118': '坊っちゃん',
    '0119': '松尾芭蕉',
    '0122': 'ヴィクトリア_(イギリス女王)',
    '0123': '気象庁',
    '0124': 'カリフォルニアのゴールドラッシュ',
    '0129': '青函連絡船',
    # 2月
    '0201': 'エイブラハム・リンカーン',
    '0209': '帝国議会_(日本)',
    '0210': '国際連盟',
    '0215': '米西戦争',
    '0217': '蝶々夫人',
    '0219': '極東国際軍事裁判',
    '0227': 'ジョン・スタインベック',
    '0229': '閏年',
    # 3月
    '0302': 'テキサス共和国',
    '0304': '東京国立博物館',
    '0310': '東京大空襲',
    '0312': 'ガールスカウト',
    '0315': '世界消費者権利デー',
    '0319': '国際連盟',
    '0322': '世界水の日',
    '0323': '世界気象機関',
    '0324': '結核',
    '0327': '国際演劇協会',
    # 4月
    '0403': '婦人公論',
    '0405': 'ウィンストン・チャーチル',
    '0409': '黒田清輝',
    '0419': '大塩平八郎の乱',
    '0428': 'サンフランシスコ平和条約',
    '0430': '令和',
    # 5月
    '0502': 'レオナルド・ダ・ヴィンチ',
    '0517': '高血圧',
    # 6月
    '0601': '牛乳',
    '0608': '世界海洋デー',
    '0609': '日本のテレビジョン放送',
    '0611': 'ヘンリー8世_(イングランド王)',
    '0612': '児童労働',
    '0613': '小泉純一郎',
    '0617': 'おにぎり',
    '0620': '世界難民デー',
    '0622': '第二次コンピエーニュの休戦',
    # 7月
    '0709': 'アルゼンチン',
    '0711': '世界人口デー',
    '0712': '電話',
    '0713': 'FIFAワールドカップ',
    '0715': '海の日',
    '0721': 'ニール・アームストロング',
    '0723': 'テルスター',
    '0726': 'アメリカ合衆国郵便公社',
    '0729': '1948年ロンドンオリンピック',
    '0730': '国際連合憲章',
    # 8月
    '0804': '津田梅子',
    '0808': '上皇明仁',
    '0810': 'スミソニアン博物館',
    '0814': 'ポツダム宣言',
    '0818': 'アメリカ合衆国憲法修正第19条',
    '0819': 'カラーテレビ',
    '0821': 'ハワイ州',
    '0823': 'ウィリアム・ウォレス',
    '0824': '米英戦争',
    '0827': 'クラカタウ',
    '0829': 'ジョー1号',
    # 9月
    '0904': 'コダック',
    '0910': '世界自殺予防デー',
    '0912': 'ジョン・F・ケネディ',
    '0914': '国際水路機関',
    '0915': '敬老の日',
    '0919': '夏目漱石',
    '0921': '薩長同盟',
    '0923': '秋分の日',
    '0924': '暗黒の木曜日',
    '0925': 'バスコ・ヌーニェス・デ・バルボア',
    '0926': '洞爺丸台風',
    '0927': '世界観光デー',
    # 10月
    '1006': 'ジャズ・シンガー_(1927年の映画)',
    '1008': 'スポーツの日_(日本)',
    '1011': '南アフリカ戦争',
    '1015': '世界手洗いデー',
    '1016': '世界食料デー',
    '1017': '国際貧困撲滅デー',
    '1018': 'アラスカ州',
    '1019': 'ヨークタウンの戦い_(1781年)',
    '1023': '1983年ベイルート兵舎爆破事件',
    '1025': 'アジャンクールの戦い',
    '1026': 'エリー運河',
    '1027': 'ニューヨーク市地下鉄',
    '1030': '世界貯蓄デー',
    # 11月
    '1101': 'イヌ',
    '1104': 'バラク・オバマ',
    '1106': '鑑真',
    '1110': 'ヘンリー・モートン・スタンリー',
    '1113': '2015年パリ同時多発テロ事件',
    '1114': 'ジャワハルラール・ネルー',
    '1116': 'ユネスコ',
    '1126': 'ツタンカーメン',
    '1127': '第1回十字軍',
    '1129': 'パレスチナ分割決議',
    '1130': 'スリラー_(アルバム)',
    # 12月
    '1203': 'ボパール化学工場事故',
    '1206': 'アメリカ合衆国憲法修正第13条',
    '1209': '国連腐敗防止条約',
    '1212': 'ワシントンD.C.',
    '1218': 'プリマス植民地',
    '1219': 'ビル・クリントン',
    '1222': '冬至',
    '1223': '明仁',
    '1226': 'ボクシング・デー',
    '1228': '仕事納め',
    '1229': 'テキサス州',
    '1230': '銀河鉄道の夜',
}


def get_today_special() -> dict:
    """今日の日付（MMDD）に対応するイベント名と Wikipedia URL を辞書で返す"""
    key  = datetime.now(JST).strftime('%m%d')
    text = TODAY_SPECIAL.get(key, '')
    if not text:
        return {}
    wiki_title = TODAY_SPECIAL_WIKI.get(key, '')
    wiki_url   = (
        f'https://ja.wikipedia.org/wiki/{urllib.parse.quote(wiki_title)}'
        if wiki_title else ''
    )
    return {'text': text, 'wiki_url': wiki_url}


# ─── 今日のニュース全体像（テンプレート生成） ────────────────────────

_OV_INTROS = [
    "{date}の朝、国内外で複数の重要な動きが同時進行しています。",
    "本日{date}のニュースを俯瞰すると、互いに連動する幾つかの潮流が見えてきます。",
    "今朝の主要ニュースを横断すると、いくつかの共通テーマが浮かび上がります。",
    "世界と日本が交差する{date}、多岐にわたるニュースが動いています。",
    "{date}、政治・経済・国際それぞれの領域で、注目すべき動きが続いています。",
]
_OV_ECON = [
    "経済面では、企業業績や金融政策を巡る動きが引き続き注目を集めています。",
    "マクロ経済では、物価と金利の動向が市場参加者の関心を集めています。",
    "金融・産業の現場では、円相場と企業収益の連動が改めて焦点となっています。",
    "国内経済では、消費の回復ペースと賃金上昇の持続性が問われています。",
]
_OV_POL = [
    "政治では、政策決定のスピードと透明性を問う声が高まっています。",
    "国内政治では、与野党の攻防が各政策領域で続いています。",
    "政策の現場では、複数の重要法案の行方が注目されています。",
    "政治面では、内外からの圧力を受けながら政府の対応が試されています。",
]
_OV_INT = [
    "国際情勢では、地政学的リスクが各国の経済・外交判断に影響を与えています。",
    "世界では、複数の地域で緊張状態が続いており、日本への波及も注目されます。",
    "外交面では、同盟国間の連携強化と新興国との関係再構築が同時進行しています。",
    "国際社会では、多国間の利害調整が難しさを増しています。",
]
_OV_TECH = [
    "テクノロジー分野では、AI・半導体を軸とした覇権争いが加速しています。",
    "IT・デジタル領域では、規制整備と技術革新の速度差が社会課題となっています。",
    "テック業界では、生成AIの普及に伴う産業構造の変化が続いています。",
    "デジタル化の波は社会全体に及んでおり、制度側の追随が急がれています。",
]
_OV_SOC = [
    "社会面では、少子化・人口動態に関連した政策論争が活発化しています。",
    "生活に密着した社会課題では、格差・医療・環境を巡る議論が続いています。",
    "国内の社会動向では、働き方と生活コストを巡る関心が高まっています。",
]
_OV_CLOSINGS = [
    "各分野の動きは密接に絡み合っており、今日一日の展開に注目です。",
    "これらの潮流は相互に影響し合っており、一つの変化が連鎖的な波及をもたらす可能性があります。",
    "点と点がつながる瞬間を意識しながら、今日のニュースを追ってみてください。",
    "横断的にニュースを捉えることで、今日の「時代の文脈」が見えてきます。",
    "それぞれの動きは独立しているように見えて、深いところで繋がっています。",
]

_CAT_META = [
    ('経済',    '💹', '#d97706'),
    ('政治',    '🏛', '#7c3aed'),
    ('国際',    '🌏', '#2563eb'),
    ('IT・テック', '💻', '#059669'),
]


def _detect_themes(news: dict) -> list[str]:
    """ニュースタイトル群からテーマを検出"""
    all_titles = []
    for articles in news.values():
        all_titles.extend([a['title'] for a in articles[:3]])
    full = ' '.join(all_titles)
    themes: list[str] = []
    if any(kw in full for kw in ['経済', '株', '円', '物価', '金利', '市場', 'GDP',
                                   '財政', '予算', '決算', '景気', '賃金', '業績']):
        themes.append('経済')
    if any(kw in full for kw in ['政治', '政府', '内閣', '首相', '選挙', '国会',
                                   '与党', '野党', '法案', '政策', '大臣']):
        themes.append('政治')
    if any(kw in full for kw in ['米国', 'アメリカ', '中国', 'ロシア', '国際',
                                   '外交', 'NATO', '国連', '紛争', 'ウクライナ', '台湾']):
        themes.append('国際')
    if any(kw in full for kw in ['AI', 'IT', 'テクノロジー', '半導体', 'デジタル',
                                   'DX', 'サイバー', 'クラウド', '生成']):
        themes.append('テクノロジー')
    if any(kw in full for kw in ['社会', '事件', '災害', '医療', '環境',
                                   '少子化', '福祉', '労働', '介護', '教育']):
        themes.append('社会')
    return themes if themes else ['経済', '社会']


def _template_summary(day: int, date_str: str, themes: list[str]) -> str:
    """Claude 不使用時のテンプレートフォールバック"""
    intro   = _OV_INTROS[day % len(_OV_INTROS)].format(date=date_str)
    body: list[str] = []
    if '経済'        in themes: body.append(_OV_ECON[day % len(_OV_ECON)])
    if '政治'        in themes: body.append(_OV_POL[day  % len(_OV_POL)])
    if '国際'        in themes: body.append(_OV_INT[day  % len(_OV_INT)])
    if 'テクノロジー' in themes: body.append(_OV_TECH[day % len(_OV_TECH)])
    if '社会'        in themes: body.append(_OV_SOC[day  % len(_OV_SOC)])
    closing = _OV_CLOSINGS[day % len(_OV_CLOSINGS)]
    parts = [intro] + body[:3] + [closing]
    return ''.join(parts)


def generate_news_overview(news: dict) -> dict:
    """全カテゴリのニュースを横断分析し、主要ニュース整理テキストを生成する"""
    global _ai_cache, _ai_cache_ts
    day = datetime.now(JST).timetuple().tm_yday
    now = datetime.now(JST)

    # ゼロパディングなし・曜日付き日付文字列
    wday     = WEEKDAY_JP[now.weekday()]
    date_str = f"{now.month}月{now.day}日({wday})"

    themes     = _detect_themes(news)
    highlights = []
    for cat, emoji, color in _CAT_META:
        arts = news.get(cat, [])
        if arts:
            highlights.append({'category': cat, 'emoji': emoji,
                                'color': color, 'article': arts[0]})

    # ── AI 概観テキスト（30分キャッシュ）────────────────────────────
    if GEMINI_KEY and time.time() - _ai_cache_ts < CACHE_TTL and 'overview' in _ai_cache:
        summary = _ai_cache['overview']
        source  = 'gemini_cached'
        print('[DEBUG] generate_news_overview: キャッシュ済み Gemini テキストを使用')
    elif GEMINI_KEY:
        print(f'[DEBUG] generate_news_overview: GEMINI_KEY 設定済み ({len(GEMINI_KEY)}文字) → API 呼び出し開始')
        cat_lines = []
        for cat in ['主要', '経済', '政治', '国際', 'IT・テック', '国内・社会']:
            arts = news.get(cat, [])
            titles = [a['title'] for a in arts[:3]]
            if titles:
                cat_lines.append(f"【{cat}】{'／'.join(titles)}")
        prompt = (
            f"本日{date_str}の主要ニュースです：\n"
            + '\n'.join(cat_lines)
            + "\n\n上記のニュース群を踏まえ、今日の全体的な動向を3〜4文で自然な日本語で分析してください。"
              "カテゴリをまたいで関連する点があれば指摘してください。"
              "文体は新聞コラムのように知的で読みやすく。"
              "冒頭に日付や「本日は」は不要です。敬体（です・ます調）で書いてください。"
        )
        ai_text = _call_claude(prompt, max_tokens=320)
        if ai_text:
            summary = ai_text
            _ai_cache['overview'] = summary
            _ai_cache_ts = time.time()
            source  = 'gemini'
            print('[DEBUG] generate_news_overview: Gemini 生成成功 → AI テキスト使用')
        else:
            summary = _template_summary(day, date_str, themes)
            source  = 'template_api_error'
            print('[WARN] generate_news_overview: Gemini 失敗 → テンプレートにフォールバック')
    else:
        summary = _template_summary(day, date_str, themes)
        source  = 'template_no_key'
        print('[DEBUG] generate_news_overview: GEMINI_KEY 未設定 → テンプレート使用')

    return {'summary': summary, 'themes': themes, 'highlights': highlights, 'source': source}


# ─── 識者コメント生成（テンプレート＋キーワードマッチング） ───────────

_EXPERTS = [
    {
        'name':     '渡辺 賢一',
        'title':    '経済アナリスト・元大手証券チーフストラテジスト',
        'keywords': ['経済', '金融', '市場', '株', '円', '物価', 'インフレ', 'GDP',
                     '金利', '日銀', '予算', '財政', '景気', '為替', '決算', '増収'],
        'cat':      '経済',
    },
    {
        'name':     '田中 素子',
        'title':    '政治学者・東京大学大学院教授',
        'keywords': ['政治', '選挙', '政府', '内閣', '議会', '首相', '党', '法案',
                     '政策', '条約', '与党', '野党', '国会', '石破', '岸田'],
        'cat':      '政治',
    },
    {
        'name':     '伊藤 誠也',
        'title':    'テクノロジーアナリスト・元GAFAM プロダクトマネージャー',
        'keywords': ['IT', 'AI', 'テクノロジー', 'デジタル', 'DX', '半導体',
                     'スタートアップ', 'クラウド', 'サイバー', 'ChatGPT', '生成AI',
                     'システム', 'ソフトウェア', 'アルゴリズム'],
        'cat':      'IT',
    },
    {
        'name':     '山本 久美',
        'title':    '国際関係・安全保障研究者・慶應義塾大学准教授',
        'keywords': ['国際', '外交', '安全保障', '軍事', '紛争', '米国', 'アメリカ',
                     '中国', 'ロシア', '制裁', 'NATO', 'ウクライナ', '台湾', '北朝鮮',
                     'G7', 'G20', '首脳'],
        'cat':      '国際',
    },
    {
        'name':     '松田 雄介',
        'title':    '社会学者・一橋大学社会学部教授',
        'keywords': ['社会', '文化', '教育', '少子化', '人口', '格差', '生活',
                     '地域', '高齢化', 'コミュニティ', '犯罪', '事件'],
        'cat':      'その他',
    },
    {
        'name':     '橋本 明子',
        'title':    '環境・ESGコンサルタント・元環境省参事官',
        'keywords': ['環境', 'ESG', 'カーボン', 'GX', '再生可能', '気候', '脱炭素',
                     'SDGs', '排出', 'CO2', '温暖化', '洪水', '台風', '自然災害'],
        'cat':      '環境',
    },
    {
        'name':     '内田 浩二',
        'title':    '国際経済学者・IMF元エコノミスト',
        'keywords': ['貿易', '関税', '輸出', '輸入', '通商', '貿易摩擦', 'WTO',
                     'FTA', 'EPA', '経常収支', '国際収支', 'ドル', '外貨'],
        'cat':      '貿易',
    },
    {
        'name':     '中島 さやか',
        'title':    '労働経済学者・慶應義塾大学准教授',
        'keywords': ['労働', '賃金', '雇用', '働き方', '非正規', '育児', '介護',
                     '人手不足', '外国人労働', 'ハラスメント', '残業', '最低賃金'],
        'cat':      '労働',
    },
    {
        'name':     '村田 隆一',
        'title':    '安全保障アナリスト・元防衛省参事官',
        'keywords': ['防衛', '自衛隊', '核', 'ミサイル', '抑止', '日米同盟',
                     '領土', '尖閣', '南シナ海', '宇宙', 'サイバー攻撃', 'テロ'],
        'cat':      '安全保障',
    },
    {
        'name':     '坂本 健太郎',
        'title':    '医療政策研究者・厚生労働省OB',
        'keywords': ['医療', '福祉', '介護', '病院', '薬', '健康保険', '診療',
                     '感染', 'ワクチン', '新薬', '治験', '医師', '看護'],
        'cat':      '医療',
    },
]

_COMMENT_TEMPLATES: dict[str, list[str]] = {
    '経済': [
        "今回の動向は国内の金融政策の方向性に直接的な影響を及ぼす可能性があります。"
        "特に円相場と輸出企業の収益動向を注視する必要があります。",
        "構造的な問題の解消には時間を要しますが、今後の政策対応の速度が"
        "市場の信頼を左右するでしょう。短期の変動よりも中長期のファンダメンタルズを重視したい。",
        "グローバルな供給制約と国内需要の回復が交差するこの局面は"
        "企業の経営判断が問われる分水嶺です。コスト構造の見直しと価格転嫁の成否が焦点になります。",
        "今後の注目点は、政策の実施タイミングと市場の織り込み速度のギャップです。"
        "先読みと対応の精度が投資家・経営者双方に問われます。",
    ],
    '政治': [
        "政策の実効性は省庁横断的な連携と実施スピードで決まります。"
        "今後の国会審議の行方と与野党の合意形成プロセスを見極めることが重要です。",
        "有権者の信頼回復には透明性の高い情報開示と説明責任が不可欠です。"
        "政策決定の根拠と優先順位の明示化が急務でしょう。",
        "この問題の本質は短期的な利害調整ではなく、長期的な国家戦略の方向性をどう定めるかです。"
        "党派を超えた議論の深化が求められます。",
        "政策効果の検証サイクルを短くし、PDCAを機動的に回すことが重要です。"
        "「決めて動かす」よりも「測って修正する」姿勢が今の政治に必要でしょう。",
    ],
    'IT': [
        "技術革新のスピードが社会制度の更新スピードを超えているのが現状です。"
        "規制当局と産業界の協調的なフレームワーク作りが急務でしょう。",
        "このトレンドは今後5年で産業構造を根底から変える可能性があります。"
        "企業はデジタル人材の確保と、レガシーシステムの刷新を同時に進めなければなりません。",
        "セキュリティとイノベーションはトレードオフではありません。"
        "信頼性の高い技術基盤の構築こそが持続可能な成長を支えます。",
        "AIの導入効果を最大化するには、ツール選定よりも組織の意思決定構造の変革が先決です。"
        "技術は問題を解く道具であり、問題の設定は人間の仕事です。",
    ],
    '国際': [
        "地政学的リスクは単一の事象ではなく複合的な構造問題として捉える必要があります。"
        "今後のキーポイントは関係国の多国間対話の枠組みが機能するかどうかです。",
        "今回の展開は国際秩序の再編という大きな文脈の中で読み解かなければなりません。"
        "日本にとっては外交的立場の明確化が問われる局面です。",
        "経済安全保障の観点から、サプライチェーンの再構築と同盟国との連携強化は避けられない選択です。"
        "短期コストより長期的なリスク管理を優先すべきでしょう。",
        "多国間の枠組みが機能不全に陥りつつある今、二国間外交の比重が高まっています。"
        "日本外交の「信頼の蓄積」がいかに問われるかが焦点です。",
    ],
    '環境': [
        "この動向は単なる環境規制の問題ではなく、産業競争力の再定義を迫るものです。"
        "カーボンプライシングの設計次第で企業行動は大きく変わります。",
        "ESGを「コスト」と捉えるか「投資」と捉えるかで企業の中長期的な価値は分かれます。"
        "先行する欧州の事例が今後の日本の政策設計に重要な示唆を与えます。",
        "気候変動対策は国際競争と表裏一体です。"
        "脱炭素への移行スピードが産業の優位性を左右する時代になっています。",
    ],
    '貿易': [
        "関税・通商政策の変化は企業のコスト構造に直接波及します。"
        "サプライチェーン全体を見直すタイミングとして今を活用すべきでしょう。",
        "自由貿易体制への逆風が続く中で、二国間・多国間協定の重要性が増しています。"
        "日本の通商戦略の柔軟性が問われています。",
        "貿易摩擦は経済問題であると同時に政治問題でもあります。"
        "両国の国内政治的文脈を踏まえた分析が欠かせません。",
    ],
    '労働': [
        "賃金上昇の持続性を左右するのは企業の価格転嫁力です。"
        "付加価値の高い製品・サービスへの転換が、給与水準の底上げに直結します。",
        "労働市場の構造変化は短期的な政策介入で解決できません。"
        "人的資本投資の長期的な積み上げこそが、日本経済の競争力回復の鍵です。",
        "人手不足と賃金上昇は表裏一体の課題です。"
        "外国人材の活用と国内人材の再教育を同時に進める複合的な戦略が求められます。",
    ],
    '安全保障': [
        "抑止力の信頼性は装備品の質だけでなく、意思決定の速度と一貫性にも依存します。"
        "同盟国との情報共有と演習の積み重ねが実力を左右します。",
        "防衛費の増額と合わせて、その使い道の戦略的優先順位付けが問われています。"
        "量より質の議論を深めることが重要です。",
        "サイバー・宇宙・電磁波という新領域での攻防は、従来の安全保障概念を変えています。"
        "省庁横断的な体制整備が急務です。",
    ],
    '医療': [
        "医療費の増大は持続可能性の問題だけでなく、イノベーション投資とのバランスが鍵です。"
        "予防医療への重心移動が長期的なコスト削減につながります。",
        "新薬・新治療法の承認スピードと安全性確保のトレードオフは、常に慎重な検討が必要です。"
        "患者中心の視点が判断基準の軸であるべきです。",
        "医療DXの進展は診断精度の向上と同時に、プライバシー保護の難しさも伴います。"
        "技術導入と倫理設計を並行して進める必要があります。",
    ],
    'その他': [
        "この問題の核心は、社会全体で合意できる価値観の共有にあります。"
        "多様なステークホルダーの声を丁寧に取り上げ、政策に反映するプロセスが問われています。",
        "短期的な対症療法よりも根本的な構造改革への取り組みが持続可能な解決策につながります。"
        "問題の複雑性を直視した議論が求められます。",
        "重要なのはこのニュースが示す先行指標としての意味合いです。"
        "トレンドの早期認識と、それに基づく先手の判断が今後の焦点になります。",
        "表層的な現象を追うだけでなく、その背後にある構造的な変化を読み取ることが重要です。"
        "「なぜ今、これが起きているのか」という問いが思考の出発点です。",
    ],
}


def _template_comment(text: str, day: int) -> str:
    """キーワードマッチで最適なテンプレートコメントを返す（AI不使用時フォールバック）"""
    best_count = 0
    best_cat   = 'その他'
    for expert in _EXPERTS:
        count = sum(1 for kw in expert['keywords'] if kw in text)
        if count > best_count:
            best_count = count
            best_cat   = expert['cat']
    templates = _COMMENT_TEMPLATES.get(best_cat, _COMMENT_TEMPLATES['その他'])
    return templates[day % len(templates)]


def generate_shasetsu_comment(article: dict) -> dict:
    """記事に対するAI視点コメントを生成。
    Claude API があれば自然なテキストを生成、なければテンプレートを使用。
    架空の人名は一切使わず、'AIによる分析' として表示する。
    """
    if not article:
        return {}

    url_key = article.get('link', '')
    # コメントキャッシュ確認
    if url_key and url_key in _ai_comment_cache:
        return _ai_comment_cache[url_key]

    day     = datetime.now(JST).timetuple().tm_yday
    title   = article.get('title',   '')
    summary = article.get('summary', '')[:150]
    source  = article.get('source',  '')
    text    = title + ' ' + source + ' ' + summary

    if GEMINI_KEY:
        prompt = (
            f"ニュース記事：「{title}」（{source}）\n"
            f"概要：{summary}\n\n"
            "この記事について、社会・経済・政治など関連する文脈を踏まえた"
            "専門的かつ分かりやすいコメントを2〜3文で書いてください。"
            "読者が「この問題をどう捉えればよいか」「次に何を注目すべきか」が"
            "分かる内容にしてください。敬体（です・ます調）で書いてください。"
        )
        comment_text = _call_claude(prompt, max_tokens=220)
        if not comment_text:
            comment_text = _template_comment(text, day)
    else:
        comment_text = _template_comment(text, day)

    result = {'text': comment_text, 'label': 'AIによる分析'}
    if url_key:
        _ai_comment_cache[url_key] = result
    return result


def generate_multi_expert_comments(news: dict) -> list:
    """カテゴリをまたいで最大3件のAI視点コメントを生成（記事の重複なし）"""
    comments: list[dict] = []
    seen_links: set[str] = set()

    priority = ['政治', '経済', 'IT・テック', '国際', '国内・社会', '文化・科学']
    for cat in priority:
        arts = news.get(cat, [])
        if not arts:
            continue
        article = arts[0]
        link    = article.get('link', '')
        if link in seen_links:
            continue
        result = generate_shasetsu_comment(article)
        if not result:
            continue
        seen_links.add(link)
        result = dict(result)   # シャローコピー（article を追加するため）
        result['article']  = article
        result['category'] = cat
        comments.append(result)
        if len(comments) >= 3:
            break

    return comments


# ─── 共通ユーティリティ ──────────────────────────────────────────

WEEKDAY_JP = ['月', '火', '水', '木', '金', '土', '日']


def _build_highlights(news: dict) -> list:
    """_CAT_META に基づくカード用ハイライトリストを生成"""
    highlights = []
    for cat, emoji, color in _CAT_META:
        arts = news.get(cat, [])
        if arts:
            highlights.append({'category': cat, 'emoji': emoji,
                                'color': color, 'article': arts[0]})
    return highlights


def _refresh_ai_batch(news: dict, s_article: dict | None) -> dict:
    """全AIコンテンツを 1回の API コールで生成し AI_CACHE_TTL 秒キャッシュ。
    overview / shasetsu_comment / expert_picks を一括生成。
    5コール → 1コール削減で Gemini 無料枠 (1,500 RPD) 枯渇を防止。
    """
    global _ai_batch_cache, _ai_batch_ts

    # ── キャッシュヒット ────────────────────────────────────────────
    if _ai_batch_cache and time.time() - _ai_batch_ts < AI_CACHE_TTL:
        remain = int((AI_CACHE_TTL - (time.time() - _ai_batch_ts)) / 60)
        print(f'[DEBUG] _refresh_ai_batch: キャッシュ済み（残り{remain}分）')
        return _ai_batch_cache

    # ── 共通データ準備 ───────────────────────────────────────────────
    day      = datetime.now(JST).timetuple().tm_yday
    now_jst  = datetime.now(JST)
    wday     = WEEKDAY_JP[now_jst.weekday()]
    date_str = f"{now_jst.month}月{now_jst.day}日({wday})"
    themes   = _detect_themes(news)
    s_article = s_article or {}

    # ── テンプレートフォールバック ────────────────────────────────────
    def _fallback(source: str) -> dict:
        s_title   = s_article.get('title',   '')
        s_summary = s_article.get('summary', '')[:150]
        s_source  = s_article.get('source',  '')
        s_text    = f"{s_title} {s_source} {s_summary}"
        picks_fb: list[dict] = []
        seen_fb: set[str] = set()
        for cat in ['政治', '経済', 'IT・テック', '国際', '国内・社会']:
            arts = news.get(cat, [])
            if not arts:
                continue
            art  = arts[0]
            link = art.get('link', '')
            if link in seen_fb:
                continue
            seen_fb.add(link)
            txt = art.get('title', '') + ' ' + art.get('summary', '')[:100]
            picks_fb.append({'category': cat, 'text': _template_comment(txt, day),
                             'label': 'AIによる分析', 'article': art})
            if len(picks_fb) >= 3:
                break
        return {
            'overview': {
                'summary': _template_summary(day, date_str, themes),
                'themes': themes, 'highlights': _build_highlights(news),
                'source': source,
            },
            'shasetsu': {'text': _template_comment(s_text, day), 'label': 'AIによる分析'},
            'picks':    picks_fb,
        }

    if not GEMINI_KEY:
        print('[DEBUG] _refresh_ai_batch: GEMINI_KEY 未設定 → テンプレート使用')
        return _fallback('template_no_key')

    # ── プロンプト構築 ───────────────────────────────────────────────
    print(f'[DEBUG] _refresh_ai_batch: GEMINI_KEY 設定済み ({len(GEMINI_KEY)}文字) → 1コールで全AI生成')
    cat_lines: list[str] = []
    for cat in ['主要', '経済', '政治', '国際', 'IT・テック', '国内・社会']:
        arts = news.get(cat, [])
        titles = [a['title'] for a in arts[:3]]
        if titles:
            cat_lines.append(f"【{cat}】{'／'.join(titles)}")

    s_title   = s_article.get('title',   '')
    s_summary = s_article.get('summary', '')[:150]
    s_source  = s_article.get('source',  '')

    pick_arts: list[dict] = []
    seen_p: set[str] = set()
    for cat in ['政治', '経済', 'IT・テック', '国際', '国内・社会']:
        arts = news.get(cat, [])
        if not arts:
            continue
        art = arts[0]
        if art.get('link', '') in seen_p:
            continue
        seen_p.add(art.get('link', ''))
        pick_arts.append({'cat': cat, 'title': art['title'], 'art': art})
        if len(pick_arts) >= 3:
            break

    picks_lines = '\n'.join(f'  - [{p["cat"]}] {p["title"]}' for p in pick_arts)

    prompt = (
        f"本日{date_str}のニュースを分析し、以下のJSON形式のみで回答してください。"
        "JSONブロック外にテキストを含めないでください。\n\n"
        f"[ニュース一覧]\n{chr(10).join(cat_lines)}\n\n"
        f"[注目記事（論点用）]\n{s_title}（{s_source}）：{s_summary}\n\n"
        f"[AIによる視点用記事]\n{picks_lines}\n\n"
        '{\n'
        '  "overview": "今日の全体動向（3〜4文、新聞コラム調、敬体）",\n'
        '  "shasetsu": "注目記事への論点コメント（2〜3文、敬体）",\n'
        '  "picks": [\n'
        '    {"category": "カテゴリ名", "text": "2文コメント（敬体）"}\n'
        '  ]\n'
        '}'
    )

    raw = _call_claude(prompt, max_tokens=700)
    if not raw:
        print('[WARN] _refresh_ai_batch: API失敗 → テンプレートにフォールバック')
        return _fallback('template_api_error')

    # ── JSON 解析（```json ... ``` 形式にも対応） ─────────────────────
    try:
        clean = raw.strip()
        if '```' in clean:
            clean = re.sub(r'```[a-z]*\n?', '', clean).replace('```', '').strip()
        start = clean.find('{')
        end   = clean.rfind('}')
        if start == -1 or end == -1:
            raise ValueError('JSONブロックが見つからない')
        data = json.loads(clean[start:end + 1])

        overview_text = data.get('overview', '').strip()
        shasetsu_text = data.get('shasetsu', '').strip()
        picks_raw_lst = data.get('picks', [])

        if not overview_text:
            raise ValueError('overview が空')

        print(f'[DEBUG] _refresh_ai_batch: JSON解析成功 (overview:{len(overview_text)}文字)')

        picks_out: list[dict] = []
        for i, p in enumerate(picks_raw_lst[:3]):
            text = p.get('text', '').strip()
            cat  = p.get('category', pick_arts[i]['cat'] if i < len(pick_arts) else '')
            art  = pick_arts[i]['art'] if i < len(pick_arts) else {}
            if text:
                picks_out.append({'category': cat, 'text': text,
                                  'label': 'AIによる分析', 'article': art})

        result = {
            'overview': {
                'summary': overview_text, 'themes': themes,
                'highlights': _build_highlights(news), 'source': 'gemini',
            },
            'shasetsu': {
                'text':  shasetsu_text or _template_comment(s_title, day),
                'label': 'AIによる分析',
            },
            'picks': picks_out or _fallback('template_api_error')['picks'],
        }
        _ai_batch_cache = result
        _ai_batch_ts    = time.time()
        return result

    except Exception as e:
        print(f'[WARN] _refresh_ai_batch: JSON解析失敗 ({e}) → テンプレートにフォールバック')
        return _fallback('template_parse_error')


def _check_wiki_exists(title: str) -> bool:
    """Wikipedia ページの存在を REST API で確認（2秒タイムアウト、結果をキャッシュ）"""
    if title in _kotoba_wiki_ok:
        return _kotoba_wiki_ok[title]
    try:
        url = (
            'https://ja.wikipedia.org/api/rest_v1/page/summary/'
            + urllib.parse.quote(title, safe='')
        )
        req = urllib.request.Request(url, headers={'User-Agent': 'MaiChokan/1.0'})
        with urllib.request.urlopen(req, timeout=2) as resp:
            ok = resp.status == 200
    except Exception:
        ok = False
    _kotoba_wiki_ok[title] = ok
    print(f'[DEBUG] _check_wiki_exists: "{title}" → {"OK" if ok else "404/Error"}')
    return ok


def get_kotoba() -> dict:
    day = datetime.now(JST).timetuple().tm_yday
    k = dict(KOTOBA[day % len(KOTOBA)])  # コピーして image / wiki_url を追加
    wiki_title   = k.get('wiki', '')
    # Google 検索クエリ: 明示指定 → なければ word+en で自動生成
    google_query = k.get('google', '') or f"{k.get('word', '')} {k.get('en', '')}"
    k['image'] = get_wiki_thumb(wiki_title) if wiki_title else ''
    # Wikipedia に画像がなければ Pixabay でフォールバック
    if not k['image'] and PIXABAY_KEY:
        k['image'] = get_pixabay_image(k.get('word', ''))
    # リンク URL・ラベルの決定（Wikipedia 存在確認 → 404なら Google 検索へフォールバック）
    if wiki_title and _check_wiki_exists(wiki_title):
        k['wiki_url']   = f'https://ja.wikipedia.org/wiki/{urllib.parse.quote(wiki_title)}'
        k['link_label'] = 'Wikipedia'
    elif google_query.strip():
        k['wiki_url']   = f'https://www.google.com/search?q={urllib.parse.quote(google_query.strip())}'
        k['link_label'] = 'Google で調べる'
    else:
        k['wiki_url']   = ''
        k['link_label'] = ''
    return k


# ─── ルーティング ────────────────────────────────────────────────

@app.route('/')
def index():
    news      = get_all_news()
    now       = datetime.now(JST)

    # OGP キャッシュから画像を補完（画像がない記事にバックグラウンド取得結果を適用）
    # 元の _cache は変更せず、リクエストごとにシャローコピーでマージ
    news_enriched: dict = {}
    for cat, articles in news.items():
        enriched = []
        for art in articles:
            if not art.get('image'):
                ogp = _ogp_cache.get(art.get('link', ''), '')  # GILにより読み取りはスレッドセーフ
                if ogp:
                    art = {**art, 'image': ogp}   # シャローコピー（元 dict は不変）
            enriched.append(art)
        news_enriched[cat] = enriched

    s_pool    = news_enriched.get('政治', []) + news_enriched.get('経済', [])
    s_article = s_pool[0] if s_pool else None
    # 全 AI コンテンツを 1API コールで生成（5コール→1コール、2時間キャッシュ）
    ai = _refresh_ai_batch(news, s_article)
    return render_template(
        'index.html',
        news=news_enriched,
        kotoba=get_kotoba(),
        shunshuu=get_shunshuu(news),
        now=now,
        weekday=WEEKDAY_JP[now.weekday()],
        last_updated=(lambda d: f"{d.hour}:{d.minute:02d}")(datetime.fromtimestamp(_cache_ts, JST)) if _cache_ts else '—',
        today_event=get_today_special(),
        shasetsu_comment=ai['shasetsu'],
        news_overview=ai['overview'],
        expert_picks=ai['picks'],
    )


@app.route('/refresh', methods=['POST'])
def refresh():
    global _cache_ts, _ai_cache, _ai_cache_ts, _ai_comment_cache, _ai_batch_cache, _ai_batch_ts
    _cache_ts = 0.0
    _ai_cache.clear()
    _ai_cache_ts = 0.0
    _ai_comment_cache.clear()
    _ai_batch_cache.clear()
    _ai_batch_ts = 0.0
    # OGP キャッシュはクリアしない（再利用してスレッド節約）
    get_all_news()   # ← 内部で OGP プリフェッチも起動される
    return redirect('/?refreshed=1')


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print('=' * 52)
    print('  マイ朝刊 起動中...')
    print(f'  http://localhost:{port} を開いてください')
    print('  終了: Ctrl+C')
    print('=' * 52)
    app.run(debug=False, port=port, host='0.0.0.0')
