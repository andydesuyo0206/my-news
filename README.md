# 朝凪（あさなぎ）

> 点と点をつなぐ、毎日の情報整理。

朝日・毎日・Reuters・NHK・東洋経済など複数のRSSフィードを集約し、AIによる要約・論点整理を加えたパーソナルニュースダッシュボード。Render.com のフリープランで動作する。

🌐 **デモ：** https://my-news.onrender.com
> ※ Render フリープランのため、15分間アクセスがないとサーバーがスリープします。初回アクセスから表示まで最大30秒かかる場合があります。

---

## 主な機能

| 機能 | 内容 |
|------|------|
| **ニュース集約** | 7カテゴリ（経済・政治・国際・IT・社会・文化・主要）×複数ソースをRSSで取得 |
| **AIによる分析** | Gemini 2.0 Flash がニュース全体像・論点コメント・注目記事の視点を1APIコールで生成 |
| **きょうのことば** | 経済・社会用語を1日1語解説。Wikipedia サムネイル付き |
| **余白コラム** | 名言＋ニュースを橋渡しする読み物コーナー |
| **今日は何の日** | 365日分の歴史的出来事データ。Wikipedia リンク付き |
| **OGP画像取得** | RSSに画像がない記事を、バックグラウンドで `og:image` をスクレイピングして補完 |
| **レスポンシブ対応** | PC 2カラム・モバイル 1カラムで最適表示 |

---

## 技術スタック

```
Backend  : Python 3.x / Flask 3.x
AI       : Google Gemini 2.0 Flash（REST API、SDKなし）
RSS      : feedparser
画像補完  : Wikipedia REST API / Pixabay API / OGP スクレイピング（バックグラウンド）
Server   : Gunicorn / Render.com（フリープラン）
Frontend : Jinja2 テンプレート / Vanilla JS / CSS変数ベースのデザインシステム
フォント  : Playfair Display / M PLUS 1p / Noto Sans JP（Google Fonts）
```

外部パッケージは `flask` / `feedparser` / `gunicorn` の3つのみ。画像取得やGemini API呼び出しはすべて標準ライブラリ（`urllib.request`）で実装。

---

## ニュースソース

| カテゴリ | 主なソース |
|---------|-----------|
| 主要 | 朝日新聞・毎日新聞・47NEWS・Yahoo!ニュース・NHK |
| 経済 | 日経ビジネス・東洋経済・ダイヤモンド・Reuters・プレジデント |
| 政治 | 朝日新聞・Yahoo!政治・財経新聞・NHK |
| 国際 | AFP BB News・BBC Japan・Reuters・ニューズウィーク日本版 |
| IT・テック | ITmedia・日経クロステック・Wired JP・GIGAZINE |
| 社会 | 朝日新聞・毎日新聞・47NEWS・Yahoo!国内 |
| 文化・科学 | 現代ビジネス・NHK |

---

## 環境変数

Render のダッシュボード（Environment）で設定する。

| 変数名 | 必須 | 内容 |
|--------|------|------|
| `GEMINI_API_KEY` | 推奨 | Google AI Studio で取得。未設定時はテンプレート文に fallback |
| `PIXABAY_API_KEY` | 任意 | Pixabay の画像フォールバック用。未設定時はスキップ |

---

## ローカル起動

```bash
git clone https://github.com/andydesuyo0206/my-news.git
cd my-news

pip install -r requirements.txt

# 環境変数を設定（任意）
export GEMINI_API_KEY=your_key_here
export PIXABAY_API_KEY=your_key_here

flask run
# → http://localhost:5000
```

---

## キャッシュ設計

| 対象 | TTL | 内容 |
|------|-----|------|
| RSSフィード | 30分 | `_cache` / `_cache_ts` |
| AI生成コンテンツ | 2時間 | `_ai_batch_cache`（全AIコンテンツを1APIコールで一括生成） |
| OGP画像 | プロセス存続中 | `_ogp_cache`（上限2000件、FIFO） |
| Wikipedia画像 | プロセス存続中 | `_wiki_img_cache` |

Gemini のフリープラン（1500 RPD）を消費しすぎないよう、全AIコンテンツを1回のAPIコールに集約し、2時間キャッシュしている。

---

## デプロイ（Render）

`render.yaml` を使って自動設定される。

```yaml
startCommand: gunicorn app:app --bind 0.0.0.0:$PORT --timeout 120 --workers 1
```

Render フリープランのコールドスタート（15分無操作でスリープ）が気になる場合は、[UptimeRobot](https://uptimerobot.com/) で定期 ping を設定すると解消できる（無料）。
