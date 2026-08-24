# IndieGem

Steam 隠れた名作 / 急上昇インディー ゲーム 自動収集 + Gemini 日本語要約.

GitHub Pages + GitHub Actions (毎日自動更新).

## 必要要件

- Python 3.10+
- STEAM_API_KEY (任意: https://steamcommunity.com/dev/apikey )
- GEMINI_API_KEY (任意: https://aistudio.google.com/apikey )

キーが無くても appdetails / reviews / CCU とフォールバック要約でサイトは生成されます。

## 初期シード（検証用）

| Game | AppID |
| --- | --- |
| Balatro | 2379780 |
| Vampire Survivors | 1794680 |
| Slay the Spire | 646570 |
| Schedule I | 3164500 |

## ローカルでの動かし方

```bash
cd IndieGem
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python scripts/fetch_and_summarize.py --seed-only
```

`docs/index.html` をブラウザで開いて確認します。

```bash
python scripts/fetch_and_summarize.py --skip-gemini
python scripts/fetch_and_summarize.py --max-games 8
python scripts/fetch_and_summarize.py --app-ids 2379780,1794680
```

## GitHub 自動運用

1. Secrets and variables → Actions
   - `STEAM_API_KEY`
   - `GEMINI_API_KEY`
2. Settings → Pages
   - Source: Deploy from a branch
   - Branch: `main`
   - Folder: `/docs`
3. Actions: `.github/workflows/update.yml`
   - cron: `0 0 * * *` (UTC 0:00 = JST 09:00)
   - `docs/` + `data/games.json` を commit & push

公開問い合わせ: `https://<user>.github.io/IndieGem/`

## サイト構成

```
docs/index.html            top cards + live sort
docs/posts/[app_id].html   article
docs/assets/               css/js/favicon
data/games.json            catalog (CCU delta)
```

Sort: CCU rising / positive % / discount %.

Article: header, AI 3-line summary, swamp points, caveats, specs/language, Steam + affiliate slot, AdSense slot, OGP, breadcrumb.

## 運用上の注意

- Steam API: `time.sleep` + retry. Control volume with `MAX_GAMES`.
- Gemini reuse: `SUMMARY_TTL_DAYS` (default 7). Price/CCU/rating still refresh daily.
- Affiliate / AdSense snippets can be pasted into the template slots.
- Unofficial fan media. Data is as of fetch time.
