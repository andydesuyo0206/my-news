# 朝凪（あさなぎ）

> 点と点をつなぐ、毎日の情報整理。

朝日・毎日・NHK・Bloomberg・The Verge など国内外のRSSフィードを集約し、AIによる構造分析・論点整理を加えたパーソナルニュースダッシュボード。Render.com のフリープランで動作する。

🌐 **デモ：** https://my-news-yqvv.onrender.com
> ※ Render フリープランのため、15分間アクセスがないとサーバーがスリープします。初回アクセスから表示まで最大30秒かかる場合があります。

---

## 主な機能

| 機能 | 内容 |
|------|------|
| **ニュース集約** | 7カテゴリ（主要・経済・政治・国際・IT・社会・文化）×複数ソースをRSSで並列取得 |
| **AIによる構造分析** | Gemini 2.5 Flash が「今日を貫く潮流」「カテゴリ横断の関連性」「今後の注目点」を1APIコールで生成 |
| **英語ソース自動翻訳** | Bloomberg・The Verge・DW News・ABC Australia の見出し／要約を自動で日本語化 |
| **ライト／ダークモード** | ワンクリックで切替、`localStorage` で設定を保持（PC・スマホ両対応） |
| **きょうのことば** | 経済・社会用語を1日1語解説。Wikipedia サムネイル付き |
| **余白コラム** | 名言＋ニュースを橋渡しする読み物コーナー |
| **今日は何の日** | 365日分の歴史的出来事データ。Wikipedia リンク付き |
| **画像ハイドレーション** | RSSに画像がない記事はバックグラウンドで `og:image` を取得し、ページをリロードせず差し替え表示 |
| **既読管理・NEWバッジ** | `localStorage` で既読記事を薄く表示、公開2時間以内の記事にNEWバッジ |
| **PWA対応** | ホーム画面に追加してアプリのように起動可能 |
| **レスポンシブ対応** | PC 2カラム・モバイル 1カラム＋ボトムナビで最適表示 |

---

## 技術スタック

```
Backend  : Python 3.x / Flask 3.x
AI       : Google Gemini 2.5 Flash（REST API、429時は flash-lite にフォールバック）
翻訳     : Google翻訳 非公式エンドポイント（APIキー不要）
RSS      : feedparser（ThreadPoolExecutorで並列取得）
画像補完  : Wikipedia REST API / Pixabay API / OGP スクレイピング（バックグラウンド + クライアント側ハイドレーション）
Server   : Gunicorn / Render.com（フリープラン）
Frontend : Jinja2 テンプレート / Vanilla JS / CSS変数ベースのデザインシステム
フォント  : Playfair Display / M PLUS 1p / Noto Sans JP（Google Fonts）
```

外部パッケージは `flask` / `feedparser` / `gunicorn` の3つのみ。画像取得・Gemini API呼び出し・翻訳はすべて標準ライブラリ（`urllib.request`）で実装。

---

## ニュースソース

| カテゴリ | 主なソース |
|---------|-----------|
| 主要 | 朝日新聞・毎日新聞・47NEWS・Yahoo!ニュース・財経新聞・NHK |
| 経済 | 日経ビジネス・ダイヤモンドOL・東洋経済・**Bloomberg**（翻訳）・プレジデントOL・財経新聞・NHK |
| 政治 | 財経新聞・Yahoo!政治・朝日新聞・NHK |
| 国際 | AFP BB News・BBC Japan・**CNN.co.jp**・ニューズウィーク日本版・**DW News**（翻訳）・**ABC Australia**（翻訳）・Yahoo!国際・NHK |
| IT・テック | ITmedia・日経クロステック・Wired JP・GIGAZINE・**The Verge**（翻訳）・**Bloomberg Tech**（翻訳）・CNET Japan・Impress |
| 国内・社会 | Yahoo!国内・財経新聞・NHK |
| 文化・科学 | 現代ビジネス・NHK |

**（翻訳）** の付いたソースは英語配信のため、非公式Google翻訳エンドポイントで自動的に日本語化して表示する。

---

## 環境変数

Render のダッシュボード（Environment）で設定する。

| 変数名 | 必須 | 内容 |
|--------|------|------|
| `GEMINI_API_KEY` | 推奨 | Google AI Studio で取得。未設定時はテンプレート文に fallback |
| `PIXABAY_API_KEY` | 任意 | きょうのことば・余白コラムの画像フォールバック用。未設定時はスキップ |

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
| RSSフィード | 30分 | `_cache` / `_cache_ts`（並列取得、`ThreadPoolExecutor(max_workers=6)`） |
| AI生成コンテンツ | 4時間 | `_ai_batch_cache`（全AIコンテンツを1APIコールで一括生成、ディスクにも永続化） |
| OGP画像 | プロセス存続中 | `_ogp_cache`（上限500件、FIFO） |
| Wikipedia画像 | プロセス存続中 | `_wiki_img_cache` |
| Pixabay画像 | 6時間 | `_pixabay_cache`（URLが約24hで失効するため定期再取得） |
| 翻訳結果 | プロセス存続中 | `_trans_cache`（原文をキーに永続キャッシュ） |

Gemini 2.5 Flash の無料枠を消費しすぎないよう、全AIコンテンツを1回のAPIコールに集約して4時間キャッシュし、429発生時は `gemini-2.5-flash-lite` に自動フォールバックする。

---

## AI分析の仕組み

`_refresh_ai_batch()` が1回のGemini呼び出しで以下をまとめて生成する：

1. **overview** — 今日のニュース全体を構造的に分析（潮流／カテゴリ横断の関連性／今後の注目点）
2. **shasetsu** — 注目記事1本についての論点・意義コメント
3. **picks** — 3記事についての「なぜ重要か」コメント（AIによる視点）

同時リクエストによる重複呼び出しは `threading.Lock` で防止し、API失敗時はテンプレート文へ自動フォールバックする。

---

## デプロイ（Render）

`render.yaml` を使って自動設定される。

```yaml
startCommand: gunicorn app:app --bind 0.0.0.0:$PORT --timeout 120 --workers 1
```

Render フリープランのコールドスタート（15分無操作でスリープ）が気になる場合は、[UptimeRobot](https://uptimerobot.com/) で定期 ping を設定すると解消できる（無料）。

`/health` エンドポイントでキャッシュ状態を確認可能。
