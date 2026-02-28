# O'Reilly Japan Ebook Gallery

O'Reilly Japan の電子書籍ページを収集し、一覧しやすいギャラリー形式で公開するプロジェクトです。

- Python スクリプトで書籍情報（タイトル・表紙・価格・発売日・詳細リンク）を取得
- GitHub Actions で自動実行（push）と手動実行（workflow_dispatch）に対応
- 生成した HTML を GitHub Pages に自動デプロイ

## スクリーンショット

> GitHub README 上でも表示されるよう、リポジトリ内の画像を参照しています。

![O'Reilly Japan Ebook Gallery のスクリーンショット](assets/oreilly-gallery-hero.png)

## GitHub Pages

- 公開ページ: https://lixxlim.github.io/OReilly-Japan-Ebook-Gallery/

## ローカル実行

```bash
python3 oreilly_ebook_to_html.py
```

オプション例:

```bash
python3 oreilly_ebook_to_html.py --limit 50 --workers 4
python3 oreilly_ebook_to_html.py --refresh-images
```

## 補足

Pages の公開設定は GitHub 側で `Settings > Pages > Source = GitHub Actions` にしてください。
