# O'Reilly Japan Ebook Gallery

O'Reilly Japan の電子書籍ページを収集し、一覧しやすいギャラリー形式で公開するプロジェクトです。

- Python スクリプトで書籍情報（タイトル・表紙・価格・発売日・詳細リンク）を取得
- GitHub Actions で自動実行（push）と手動実行（workflow_dispatch）に対応
- 生成した HTML を GitHub Pages に自動デプロイ

## スクリーンショット

![O'Reilly Japan Ebook Gallery のスクリーンショット](assets/oreilly-gallery-hero.png)

## なぜ作ったか

O'Reilly Japan の電子書籍ページは情報量が多く、発売順の確認や表紙ベースでの探索を素早く行うには少し手間がかかると感じました。  
そこで、次の目的でギャラリー化しました。

- 表紙中心で新刊を直感的に確認できる
- タイトル検索で目的の書籍へすぐ到達できる
- 詳細ページへ 1 クリックで遷移できる

## 実装ポイント

1. 収集スクリプト: `oreilly_ebook_to_html.py`
- 一覧ページを解析して書籍メタ情報を抽出
- 詳細ページから表紙画像 URL を収集
- 単一 HTML を生成

2. 画像キャッシュ
- キャッシュ JSON を使い、再実行時の重複アクセスを削減
- `--refresh-images` で全件再取得にも対応

3. 自動デプロイ
- `push` で自動実行
- `workflow_dispatch` で手動実行
- 生成物を GitHub Pages に公開

## 実際に使ってみた感想

良かった点:
- 新刊の把握が速い
- 検索が軽く、欲しい本へすぐ辿り着ける
- 静的ページなので運用コストが低い

改善したい点:
- 元サイトの構造変更にはパーサ更新が必要
- 初回の全件取得は時間がかかる

## GitHub Pages

- 公開ページ: https://lixxlim.github.io/OReilly-Japan-Ebook-Gallery/

## ローカル実行

```bash
python3 oreilly_ebook_to_html.py
```

実行後、以下のファイルが生成されます。

- 生成ページ: `output/oreilly_ebooks.html`
- 画像キャッシュ: `output/oreilly_ebook_image_cache.json`

生成結果を確認するには、`output/oreilly_ebooks.html` をブラウザで開いてください。  
（macOS の場合: `open output/oreilly_ebooks.html`）

オプション例:

```bash
python3 oreilly_ebook_to_html.py --limit 50 --workers 4
python3 oreilly_ebook_to_html.py --refresh-images
```
