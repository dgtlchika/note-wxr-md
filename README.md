# note-wxr-md

note の公式エクスポート（ZIP）を、記事ごとの **Markdown ファイル** へ一括変換する CLI です。
依存は **Python 標準ライブラリのみ**。定期的な **差分バックアップ** に絞った、軽量な単機能ツールです。

note のエクスポートは「WXR 形式の XML ＋ 画像の assets フォルダ」が入った ZIP で届きます。
そのままでは読めず・検索できず・他所へ移せないので、このツールはそれを frontmatter 付きの
Markdown として記事単位に書き出し直します。他ブログへの移行、Obsidian 等への取り込み、
記事資産の棚卸し・バックアップにそのまま使えます。

## できること

- **一括変換**: エクスポート ZIP（または WXR XML 単体）から、全記事を 1 記事＝1 Markdown ファイルに変換
- **frontmatter**: タイトル・公開日・note 記事 ID・元 URL を YAML で付与（価格・タグについては後述の注意を参照）
- **画像のローカル化**: まず ZIP 同梱の assets から画像を拾い、`assets/` へ配置して相対リンクに書き換え。同梱でまかなえない画像だけ URL からダウンロードする（失敗した画像は元の参照のまま残し、警告を出す）
- **差分バックアップ**: 再実行時は記事 ID をキーに差分検出。新規だけ作成し、変わった記事だけ更新し、変わらない記事は触らない。エクスポートから消えた記事は **削除せず** 一覧で報告する
- **dry-run**: 書き込まずに差分の見通しだけ確認できる

## 動作環境

- Python 3.11 以上
- Windows / macOS / Linux

## インストール

```bash
pip install git+https://github.com/dgtlchika/note-wxr-md
```

インストールすると `note-wxr-md` コマンドが使えます。
リポジトリを clone して `python3 note_wxr_md.py ...` の形で直接実行することもできます（依存ゼロなのでどちらでも動きます）。

## 使い方

note の設定画面からエクスポートした ZIP ファイルを用意し、変換します（WXR XML 単体でも可）。

```bash
# 基本（同梱assetsから画像をローカル化）
note-wxr-md convert note_export.zip --out ./notes

# 画像をローカル化せず、元の参照のままにする
note-wxr-md convert note_export.zip --out ./notes --no-images

# まず差分だけ確認する
note-wxr-md convert note_export.zip --out ./notes --dry-run

# レポートをファイルにも残す
note-wxr-md convert note_export.zip --out ./notes --report report.md
```

実行すると記事ごとに処理結果が表示され、最後にサマリが出ます。

```
[1/3] 新規: 2026-11-01_ダミー記事1：見出しと画像と引用のテスト.md
[2/3] 更新: 2026-11-02_ダミー記事2：コードと表のテスト（有料）.md
[3/3] 変更なし: 2026-11-03_ダミー記事3.md
完了: 新規 1件 / 更新 1件 / 変更なし 1件 / 消失 0件
```

### オプション

| オプション | 説明 |
|---|---|
| `--out <dir>` | 出力先ディレクトリ（必須） |
| `--download-images` | 画像をローカル化する（既定で有効） |
| `--no-images` | 画像のローカル化をしない |
| `--filename title\|id` | ファイル名方式。`title`＝公開日_タイトル（既定）、`id`＝公開日_記事ID |
| `--dry-run` | 書き込まず、差分サマリだけ表示する |
| `--report <file>` | 変換結果・警告一覧をファイルにも出す |

終了コード: 正常 0 / 入力エラー等の致命的失敗は 2。画像の一部失敗は警告扱いで 0 です。

## 出力形式

### ファイル名

- 既定は「公開日_タイトル.md」。日本語タイトルをそのまま使います（UTF-8、NFC 正規化）。
- ファイル名に使えない文字（`/ \ : * ? " < > |` と制御文字）は `_` に置換します。
- 長いタイトルはファイル名のみ切り詰め、記事 ID の短縮サフィックスを付けて衝突を防ぎます。本文・frontmatter のタイトルは原文のままです。

### frontmatter

```yaml
---
title: "記事タイトル"
date: 2026-11-02T10:30:00
id: n1234567890ab
url: https://note.com/your_name/n/n1234567890ab
---
```

- `id` が差分照合のキーです。ファイル名を変えても再実行で正しく照合されます。
- **価格（price）とハッシュタグ（tags）は、note のエクスポートには含まれません**（公式仕様です）。そのため通常は frontmatter にも現れません。WXR 側に該当情報がある形式（WordPress 等の一般的な WXR）では出力されますが、推測で埋めることはしません。同様に、目次・有料記事の試し読みライン・コメント・スキもエクスポート対象外であり、復元できません。

### 本文の変換方針

- 見出し・太字・斜体・打ち消し・リンク・画像・引用・コードブロック（言語指定つき）・箇条書き・番号つきリストは Markdown に変換します。
- **表（table）と埋め込み（iframe）は、情報を落とさないため HTML のまま残します。** 移行先でそのまま表示できる形を優先した判断です。
- 変換できない要素も、テキストとしては可能な範囲で残し、欠落させない方針です。

### 画像のローカル化

- 画像はまず **ZIP 同梱の assets** から探します。本文中の参照（相対パス／ファイル名／URL）のいずれの表記でも、ファイル名で照合して拾います。
- 同梱にない画像だけ、URL からダウンロードします（XML 単体を入力した場合も同様）。
- ローカル化した画像は出力先の `assets/` に「記事ID-連番.拡張子」で保存し、Markdown 内のリンクを相対パスに書き換えます。同じ画像は 1 回だけ処理し、複数記事で使い回します。
- 取得できなかった画像は元の参照のまま残り、最後に警告として一覧表示されます（変換全体は止まりません）。

## 差分バックアップの仕様

出力先に既存ファイルがある状態で再実行した場合:

| 状態 | 動作 |
|---|---|
| 新規記事 | ファイルを作成 |
| 変更あり記事 | そのファイルだけ更新（ファイル名が変わる場合は旧ファイルを付け替える） |
| 変更なし記事 | 触らない（更新日時も変わらない） |
| エクスポートから消えた記事 | **削除しない**。ファイル名を一覧で報告するだけ |

## 注意

- **自分のアカウントから取得したエクスポートファイル専用** のツールです。他人の記事の取得・スクレイピングには使えません（そもそも入力は手元のファイルだけです）。
- ログイン情報や API キーは扱いません。ネットワークを使うのは、同梱 assets でまかなえない画像をダウンロードするときだけです。
- エクスポートに含まれない情報（価格・ハッシュタグ・目次など）は復元できません。
- 変換後の Markdown を他サービスへ投稿する作業は、このツールの範囲外です。

## 類似ツールと、このツールの位置づけ

正直に書いておくと、note WXR を扱う先行ツールはすでにあります。

- [y-marui/python-note-wxr-tools](https://github.com/y-marui/python-note-wxr-tools) — 一括変換・画像・frontmatter・差分に加えて **Markdown → WXR の逆変換** まで備えた高機能なツールです。機能の網羅性ではこちらが上です。
- [hiyori-akane/note-wxr-to-markdown](https://github.com/hiyori-akane/note-wxr-to-markdown) — ブラウザだけで完結する Web ツール。手軽さではこちらに分があります（画像ローカル化・差分には非対応）。

そのうえで note-wxr-md が目指すのは、機能の多さではなく **届け方** です。

- **pip で入る軽量さ**: 依存ゼロの単一ファイル構成で、`pip install` 一発で導入できます。
- **バックアップ特化の単機能**: 「エクスポート ZIP を渡すと、差分バックアップとして Markdown が積み上がる」ことだけに絞っています。定期実行して記事資産を守る用途が主眼です。
- **日本語の作り**: ドキュメントも出力サマリも日本語で、note を書く人（非エンジニア含む）が迷わない形を目指しています。

高機能な変換が欲しい場合は、先行ツールの方が合うかもしれません。「軽い方」が欲しい場合の選択肢として作っています。

## テスト

ダミーの WXR フィクスチャで検証できます（実在の記事は含まれません）。

```bash
python3 -m unittest discover -s tests -v
```

## ライセンス

MIT License

---

## English summary

**note-wxr-md** converts a note (note.com) official export ZIP — a WXR XML plus a
bundled `assets` folder with images — into one Markdown file per article, with YAML
frontmatter (title, date, article ID, source URL). Note that note exports do not
contain price or hashtag data, so those fields are only emitted when present in
the source WXR. Images are taken from the bundled assets first (matched by file
name) and downloaded only when not bundled; localized images are rewritten as
relative links. Re-running the tool performs an incremental backup: it uses the
article ID to create only new files, update only changed ones, leave unchanged
files untouched, and report (never delete) articles that disappeared from the
export. Tables and iframes are kept as raw HTML to avoid information loss.
Pure Python 3.11+ standard library, no dependencies, installable via pip.
MIT License.

Full-featured alternatives already exist (e.g. y-marui/python-note-wxr-tools,
which even converts back from Markdown to WXR). This tool deliberately focuses
on one thing — a lightweight, Japanese-documented, backup-oriented converter
that is easy to install and re-run on a schedule.

This tool works **only on your own export file**; it never logs in or scrapes.

---

## 作者について

このツールは「デジタル地下室」が、自分のブログ運用で実際に困ったことを解消するために作ったものです。同じ困りごとの解説や、作った過程は note に書いています。

- note（デジタル地下室・記事目次）: https://note.com/sumaho_lab/n/n96ded41773ed
- BOOTH（デジタル地下室）: https://digital-basement.booth.pm/
- X: https://x.com/dgtl_chika
- GitHub: https://github.com/dgtlchika

不具合報告・要望は Issue へどうぞ。確認して対応します。
