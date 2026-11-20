#!/usr/bin/env python3
"""note-wxr-md: note公式エクスポート(WXR)を記事ごとのMarkdownへ一括変換するCLI。

依存は Python 標準ライブラリのみ (Python 3.11+).
入力は note のエクスポート ZIP (WXR XML + 画像の assets フォルダ) が本筋で、
WXR XML 単体も受け付ける。自分のアカウントから取得したエクスポート専用の
オフライン変換ツールで、同梱画像でまかなえない場合のみネットワークを使う。
"""

from __future__ import annotations

import argparse
import hashlib
import html
import io
import os
import re
import sys
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

VERSION = "0.1.0"

NS = {
    "content": "http://purl.org/rss/1.0/modules/content/",
    "wp": "http://wordpress.org/export/1.2/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "excerpt": "http://wordpress.org/export/1.2/excerpt/",
}

USER_AGENT = f"note-wxr-md/{VERSION} (+https://github.com/dgtlchika/note-wxr-md)"


# ---------------------------------------------------------------------------
# WXR パース
# ---------------------------------------------------------------------------

@dataclass
class Article:
    title: str
    link: str
    date: str            # ISO 8601 文字列
    date_ymd: str        # YYYY-MM-DD (ファイル名用)
    note_id: str         # 差分照合キー
    body_html: str
    tags: list[str] = field(default_factory=list)
    price: int | None = None


def _text(item: ET.Element, tag: str) -> str:
    el = item.find(tag, NS)
    if el is None or el.text is None:
        return ""
    return el.text.strip()


def _extract_note_id(link: str, guid: str, post_id: str) -> str:
    """note記事IDを link/guid の末尾セグメントから取る。無ければ post_id を使う。"""
    for url in (link, guid):
        if url:
            seg = url.rstrip("/").rsplit("/", 1)[-1]
            if re.fullmatch(r"n[0-9a-zA-Z]{6,}", seg):
                return seg
    return post_id or hashlib.md5((link or guid).encode()).hexdigest()[:12]


def _extract_date(item: ET.Element) -> tuple[str, str]:
    """(ISO日時, YYYY-MM-DD) を返す。wp:post_date を優先し、無ければ pubDate。"""
    post_date = _text(item, "wp:post_date")
    if post_date:
        iso = post_date.replace(" ", "T", 1)
        return iso, post_date[:10]
    pub = _text(item, "pubDate")
    if pub:
        try:
            dt = parsedate_to_datetime(pub)
            return dt.isoformat(), dt.strftime("%Y-%m-%d")
        except (TypeError, ValueError):
            pass
    return "", "0000-00-00"


def _extract_price(item: ET.Element) -> int | None:
    """wp:postmeta に price を含むキーがあれば数値として拾う。無ければ None。

    noteのエクスポートに価格が含まれない場合は None のまま (frontmatterから省略)。
    """
    for meta in item.findall("wp:postmeta", NS):
        key = _text(meta, "wp:meta_key").lower()
        if "price" in key:
            val = _text(meta, "wp:meta_value")
            if val.isdigit():
                return int(val)
    return None


def parse_wxr(source) -> list[Article]:
    """WXRを読み、記事 (post) のリストを返す。source はパスまたはバイナリファイル。"""
    articles: list[Article] = []
    context = ET.iterparse(source, events=("end",))
    for _event, elem in context:
        if elem.tag != "item":
            continue
        post_type = _text(elem, "wp:post_type")
        if post_type not in ("", "post"):
            elem.clear()
            continue
        link = _text(elem, "link")
        guid_el = elem.find("guid")
        guid = guid_el.text.strip() if guid_el is not None and guid_el.text else ""
        post_id = _text(elem, "wp:post_id")
        date_iso, date_ymd = _extract_date(elem)
        tags: list[str] = []
        for cat in elem.findall("category"):
            if cat.text and cat.text.strip():
                t = cat.text.strip()
                if t not in tags:
                    tags.append(t)
        articles.append(
            Article(
                title=_text(elem, "title") or "(無題)",
                link=link,
                date=date_iso,
                date_ymd=date_ymd,
                note_id=_extract_note_id(link, guid, post_id),
                body_html=_text(elem, "content:encoded"),
                tags=tags,
                price=_extract_price(elem),
            )
        )
        elem.clear()
    return articles


# ---------------------------------------------------------------------------
# frontmatter / ファイル名
# ---------------------------------------------------------------------------

def _yaml_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_frontmatter(art: Article) -> str:
    lines = ["---", f"title: {_yaml_str(art.title)}"]
    if art.date:
        lines.append(f"date: {art.date}")
    lines.append(f"id: {art.note_id}")
    if art.link:
        lines.append(f"url: {art.link}")
    if art.price is not None:
        lines.append(f"price: {art.price}")
    if art.tags:
        lines.append("tags:")
        for t in art.tags:
            lines.append(f"  - {_yaml_str(t)}")
    lines.append("---")
    return "\n".join(lines)


_INVALID_CHARS = re.compile(r'[/\\:*?"<>|\x00-\x1f\x7f]')


def sanitize_title(title: str, max_len: int = 80) -> tuple[str, bool]:
    """ファイル名用にタイトルを整える。(整形後文字列, 切り詰めたか) を返す。"""
    t = unicodedata.normalize("NFC", title)
    t = _INVALID_CHARS.sub("_", t)
    t = re.sub(r"\s+", " ", t).strip().strip(".")
    if not t:
        t = "untitled"
    truncated = len(t) > max_len
    if truncated:
        t = t[:max_len].rstrip()
    return t, truncated


def make_filename(art: Article, mode: str, taken: set[str]) -> str:
    if mode == "id":
        name = f"{art.date_ymd}_{art.note_id}.md"
    else:
        base, truncated = sanitize_title(art.title)
        name = f"{art.date_ymd}_{base}.md"
        if truncated or name in taken:
            name = f"{art.date_ymd}_{base}-{art.note_id[-6:]}.md"
    # それでも衝突する場合は連番を付ける
    stem, counter = name[:-3], 2
    while name in taken:
        name = f"{stem}-{counter}.md"
        counter += 1
    taken.add(name)
    return name


# ---------------------------------------------------------------------------
# HTML -> Markdown 変換 (簡易DOM経由)
# ---------------------------------------------------------------------------

_VOID_TAGS = {"br", "img", "hr", "meta", "link", "input", "wbr", "source", "col"}
_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_BLOCK_TAGS = _HEADINGS | {
    "p", "div", "section", "article", "ul", "ol", "blockquote", "pre",
    "table", "figure", "hr", "iframe", "header", "footer", "main",
    "aside", "nav", "dl", "form", "fieldset",
}


class El:
    __slots__ = ("tag", "attrs", "children")

    def __init__(self, tag: str, attrs: dict | None = None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children: list = []


class _DOMParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = El("#root")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        el = El(tag, dict(attrs))
        self.stack[-1].children.append(el)
        if tag not in _VOID_TAGS:
            self.stack.append(el)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].children.append(El(tag, dict(attrs)))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def parse_html(html_text: str) -> El:
    parser = _DOMParser()
    parser.feed(html_text or "")
    parser.close()
    return parser.root


def _text_content(node) -> str:
    if isinstance(node, str):
        return node
    return "".join(_text_content(c) for c in node.children)


def _serialize(node) -> str:
    """table / iframe など、Markdown化せずHTMLのまま残す要素の直列化。"""
    if isinstance(node, str):
        return html.escape(node, quote=False)
    attrs = "".join(
        f' {k}="{html.escape(str(v), quote=True)}"' if v is not None else f" {k}"
        for k, v in node.attrs.items()
    )
    if node.tag in _VOID_TAGS:
        return f"<{node.tag}{attrs}>"
    inner = "".join(_serialize(c) for c in node.children)
    return f"<{node.tag}{attrs}>{inner}</{node.tag}>"


def collect_image_urls(root: El) -> list[str]:
    urls: list[str] = []

    def walk(node):
        if isinstance(node, str):
            return
        if node.tag == "img":
            src = node.attrs.get("src", "")
            if src and src not in urls:
                urls.append(src)
        for c in node.children:
            walk(c)

    walk(root)
    return urls


class MarkdownRenderer:
    def __init__(self, image_map: dict[str, str] | None = None):
        # 元URL -> 出力Markdownで使うリンク先 (ローカル相対パス等)
        self.image_map = image_map or {}

    # ---- ブロックレベル ----
    def render(self, root: El) -> str:
        blocks = self._blocks(root)
        body = "\n\n".join(b for b in blocks if b.strip())
        return re.sub(r"\n{3,}", "\n\n", body).strip()

    def _blocks(self, node: El) -> list[str]:
        blocks: list[str] = []
        buf: list[str] = []

        def flush():
            text = "".join(buf).strip()
            if text:
                blocks.append(text)
            buf.clear()

        for child in node.children:
            if isinstance(child, str):
                if child.strip():
                    buf.append(self._inline_text(child))
                elif buf:
                    buf.append(" ")
            elif child.tag in _BLOCK_TAGS:
                flush()
                rendered = self._block(child)
                if rendered.strip():
                    blocks.append(rendered)
            else:
                buf.append(self._inline(child))
        flush()
        return blocks

    def _block(self, el: El) -> str:
        tag = el.tag
        if tag in _HEADINGS:
            level = int(tag[1])
            return "#" * level + " " + self._inline_children(el).strip()
        if tag == "p":
            return self._inline_children(el).strip()
        if tag in ("ul", "ol"):
            return self._list(el, depth=0)
        if tag == "blockquote":
            inner = "\n\n".join(self._blocks(el))
            return "\n".join("> " + line if line else ">" for line in inner.split("\n"))
        if tag == "pre":
            return self._pre(el)
        if tag == "table":
            # 表は情報落ちを避けるためHTMLのまま残す (READMEに明記)
            return _serialize(el)
        if tag == "iframe":
            # 埋め込みはHTMLのまま残す (READMEに明記)
            return _serialize(el)
        if tag == "figure":
            return self._figure(el)
        if tag == "hr":
            return "---"
        # div / section 等は透過的に中身をブロックとして扱う
        return "\n\n".join(self._blocks(el))

    def _figure(self, el: El) -> str:
        parts: list[str] = []
        for child in el.children:
            if isinstance(child, El) and child.tag == "img":
                parts.append(self._image(child))
            elif isinstance(child, El) and child.tag == "figcaption":
                caption = self._inline_children(child).strip()
                if caption:
                    parts.append(f"*{caption}*")
            elif isinstance(child, El) and child.tag == "a":
                parts.append(self._inline(child))
        if not parts:
            return "\n\n".join(self._blocks(el))
        return "\n\n".join(parts)

    def _pre(self, el: El) -> str:
        lang = ""
        code_el = next((c for c in el.children if isinstance(c, El) and c.tag == "code"), None)
        if code_el is not None:
            for cls in code_el.attrs.get("class", "").split():
                if cls.startswith("language-"):
                    lang = cls[len("language-"):]
        text = _text_content(el).strip("\n")
        return f"```{lang}\n{text}\n```"

    def _list(self, el: El, depth: int) -> str:
        ordered = el.tag == "ol"
        lines: list[str] = []
        index = 1
        for li in el.children:
            if not (isinstance(li, El) and li.tag == "li"):
                continue
            inline_parts: list[str] = []
            nested: list[str] = []
            for child in li.children:
                if isinstance(child, El) and child.tag in ("ul", "ol"):
                    nested.append(self._list(child, depth + 1))
                elif isinstance(child, El) and child.tag == "p":
                    inline_parts.append(self._inline_children(child))
                elif isinstance(child, str):
                    inline_parts.append(self._inline_text(child))
                else:
                    inline_parts.append(self._inline(child))
            marker = f"{index}. " if ordered else "- "
            lines.append("  " * depth + marker + "".join(inline_parts).strip())
            lines.extend(nested)
            index += 1
        return "\n".join(lines)

    # ---- インライン ----
    def _inline_text(self, text: str) -> str:
        return re.sub(r"\s+", " ", text)

    def _inline_children(self, el: El) -> str:
        parts: list[str] = []
        for child in el.children:
            if isinstance(child, str):
                parts.append(self._inline_text(child))
            else:
                parts.append(self._inline(child))
        return "".join(parts)

    def _image(self, el: El) -> str:
        src = el.attrs.get("src", "")
        alt = el.attrs.get("alt", "")
        target = self.image_map.get(src, src)
        return f"![{alt}]({target})"

    def _inline(self, el: El) -> str:
        tag = el.tag
        if tag in ("strong", "b"):
            inner = self._inline_children(el).strip()
            return f"**{inner}**" if inner else ""
        if tag in ("em", "i"):
            inner = self._inline_children(el).strip()
            return f"*{inner}*" if inner else ""
        if tag in ("del", "s", "strike"):
            inner = self._inline_children(el).strip()
            return f"~~{inner}~~" if inner else ""
        if tag == "code":
            text = _text_content(el)
            if "`" in text:
                return f"`` {text} ``"
            return f"`{text}`"
        if tag == "a":
            inner = self._inline_children(el).strip()
            href = el.attrs.get("href", "")
            if not href:
                return inner
            return f"[{inner}]({href})"
        if tag == "img":
            return self._image(el)
        if tag == "br":
            return "\n"
        # span / u / small 等は透過的に中身だけ出す
        return self._inline_children(el)


def html_to_markdown(html_text: str, image_map: dict[str, str] | None = None) -> str:
    return MarkdownRenderer(image_map).render(parse_html(html_text))


# ---------------------------------------------------------------------------
# 入力 (ZIP / WXR単体) と同梱 assets の解決
# ---------------------------------------------------------------------------

class BundledAssets:
    """エクスポートZIP内の同梱ファイル (主に assets フォルダの画像) の索引。

    本文中の img src の表記ゆれ (相対パス / ファイル名のみ / CDN URL) に
    対応するため、フルパス・パス末尾・ファイル名の3段階で照合する。
    """

    def __init__(self, zf: zipfile.ZipFile):
        self.zf = zf
        self.by_path: dict[str, str] = {}
        self.by_basename: dict[str, str] = {}
        for name in zf.namelist():
            if name.endswith("/"):
                continue
            norm = name.replace("\\", "/")
            self.by_path.setdefault(norm, name)
            self.by_basename.setdefault(norm.rsplit("/", 1)[-1], name)

    def resolve(self, src: str) -> str | None:
        """img src に対応するZIP内エントリ名を返す。無ければ None。"""
        s = urllib.parse.unquote(src).replace("\\", "/")
        s = s.lstrip("./").lstrip("/")
        if not s:
            return None
        entry = self.by_path.get(s)
        if entry is None:
            for path, cand in self.by_path.items():
                if path.endswith("/" + s):
                    entry = cand
                    break
        if entry is None:
            entry = self.by_basename.get(s.rsplit("/", 1)[-1])
        if entry is not None and Path(entry).suffix.lower() in _KNOWN_EXTS:
            return entry
        return None

    def read(self, entry: str) -> bytes:
        return self.zf.read(entry)


def load_input(input_path: Path):
    """入力を開き (XMLソース, BundledAssets|None, ZipFile|None) を返す。

    ZIP の場合は中の WXR (.wxr 優先、無ければ最大の .xml) を XML ソースにし、
    同梱ファイルを BundledAssets として返す。WXR 単体の場合は assets なし。
    """
    if zipfile.is_zipfile(input_path):
        zf = zipfile.ZipFile(input_path)
        names = [n for n in zf.namelist() if not n.endswith("/")]
        wxr = [n for n in names if n.lower().endswith(".wxr")]
        xmls = [n for n in names if n.lower().endswith(".xml")]
        if wxr:
            target = wxr[0]
        elif xmls:
            target = max(xmls, key=lambda n: zf.getinfo(n).file_size)
        else:
            zf.close()
            raise ValueError("ZIP内にWXR (XML) ファイルが見つかりません")
        return io.BytesIO(zf.read(target)), BundledAssets(zf), zf
    return input_path, None, None


# ---------------------------------------------------------------------------
# 画像のローカル化
# ---------------------------------------------------------------------------

_EXT_BY_CONTENT_TYPE = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
}
_KNOWN_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}


def _guess_ext(url: str, content_type: str | None) -> str:
    suffix = Path(url.split("?")[0]).suffix.lower()
    if suffix in _KNOWN_EXTS:
        return suffix
    if content_type:
        ext = _EXT_BY_CONTENT_TYPE.get(content_type.split(";")[0].strip())
        if ext:
            return ext
    return ".png"


def download_image(url: str, dest_no_ext: Path) -> Path:
    """画像をダウンロードして保存し、実際の保存パスを返す。失敗時は例外。"""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = resp.read()
        content_type = resp.headers.get_content_type()
    dest = dest_no_ext.with_suffix(_guess_ext(url, content_type))
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return dest


# ---------------------------------------------------------------------------
# 差分バックアップ
# ---------------------------------------------------------------------------

_ID_RE = re.compile(r"^id:\s*(\S+)\s*$", re.MULTILINE)


def scan_existing(out_dir: Path) -> dict[str, tuple[Path, str]]:
    """出力先の既存 .md を走査し、frontmatter の id -> (パス, 本文) を返す。"""
    found: dict[str, tuple[Path, str]] = {}
    if not out_dir.is_dir():
        return found
    for md in sorted(out_dir.glob("*.md")):
        try:
            content = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        m = _ID_RE.search(content[:2000])
        if m and m.group(1) not in found:
            found[m.group(1)] = (md, content)
    return found


@dataclass
class Summary:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def convert(
    wxr_path: Path,
    out_dir: Path,
    *,
    download_images: bool = True,
    filename_mode: str = "title",
    dry_run: bool = False,
) -> Summary:
    xml_source, bundled, zf = load_input(wxr_path)
    try:
        articles = parse_wxr(xml_source)
        return _convert_articles(
            articles,
            out_dir,
            bundled,
            download_images=download_images,
            filename_mode=filename_mode,
            dry_run=dry_run,
        )
    finally:
        if zf is not None:
            zf.close()


def _convert_articles(
    articles: list[Article],
    out_dir: Path,
    bundled: BundledAssets | None,
    *,
    download_images: bool,
    filename_mode: str,
    dry_run: bool,
) -> Summary:
    existing = scan_existing(out_dir)
    summary = Summary()
    taken: set[str] = set()
    image_cache: dict[str, str] = {}  # 元URL -> 相対リンク (成功/計画済み)
    seen_ids: set[str] = set()

    total = len(articles)
    for i, art in enumerate(articles, 1):
        seen_ids.add(art.note_id)
        dom = parse_html(art.body_html)

        # 画像リンクの決定: 同梱assets優先、次にURLダウンロード、無理なら元の参照
        image_map: dict[str, str] = {}
        if download_images:
            per_article_index = 0
            for url in collect_image_urls(dom):
                if url in image_cache:
                    image_map[url] = image_cache[url]
                    continue
                per_article_index += 1
                entry = bundled.resolve(url) if bundled is not None else None
                if entry is not None:
                    ext = Path(entry).suffix.lower() or ".png"
                    rel = f"assets/{art.note_id}-{per_article_index}{ext}"
                    if not dry_run:
                        dest = out_dir / rel
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_bytes(bundled.read(entry))
                    image_map[url] = rel
                    image_cache[url] = rel
                elif url.startswith(("http://", "https://")):
                    rel = f"assets/{art.note_id}-{per_article_index}{_guess_ext(url, None)}"
                    if dry_run:
                        image_map[url] = rel
                        image_cache[url] = rel
                        continue
                    dest_no_ext = out_dir / "assets" / f"{art.note_id}-{per_article_index}"
                    try:
                        saved = download_image(url, dest_no_ext)
                        rel = f"assets/{saved.name}"
                        image_map[url] = rel
                        image_cache[url] = rel
                    except Exception as e:  # 失敗しても変換は止めない
                        msg = f"画像DL失敗 (記事 {art.note_id}): {url} ({e})"
                        summary.warnings.append(msg)
                        image_map[url] = url
                else:
                    image_map[url] = url

        body_md = MarkdownRenderer(image_map).render(dom)
        content = build_frontmatter(art) + "\n\n" + body_md + "\n"
        filename = make_filename(art, filename_mode, taken)
        dest = out_dir / filename

        prev = existing.get(art.note_id)
        if prev is None:
            summary.created.append(filename)
            if not dry_run:
                out_dir.mkdir(parents=True, exist_ok=True)
                dest.write_text(content, encoding="utf-8")
            print(f"[{i}/{total}] 新規: {filename}")
        elif prev[1] == content and prev[0] == dest:
            summary.unchanged.append(filename)
            print(f"[{i}/{total}] 変更なし: {filename}")
        else:
            summary.updated.append(filename)
            if not dry_run:
                out_dir.mkdir(parents=True, exist_ok=True)
                dest.write_text(content, encoding="utf-8")
                if prev[0] != dest and prev[0].exists():
                    prev[0].unlink()  # タイトル変更等で旧ファイル名が残らないように
            print(f"[{i}/{total}] 更新: {filename}")

    for old_id, (old_path, _c) in existing.items():
        if old_id not in seen_ids:
            summary.missing.append(old_path.name)
            print(f"消失 (削除はしません): {old_path.name}")

    return summary


def print_summary(summary: Summary, dry_run: bool) -> None:
    prefix = "[dry-run] " if dry_run else ""
    print(
        f"{prefix}完了: 新規 {len(summary.created)}件 / 更新 {len(summary.updated)}件 / "
        f"変更なし {len(summary.unchanged)}件 / 消失 {len(summary.missing)}件"
    )
    for w in summary.warnings:
        print(f"警告: {w}", file=sys.stderr)


def write_report(summary: Summary, report_path: Path, dry_run: bool) -> None:
    lines = [
        "# note-wxr-md 変換レポート",
        "",
        f"- モード: {'dry-run' if dry_run else '通常'}",
        f"- 新規: {len(summary.created)}件",
        f"- 更新: {len(summary.updated)}件",
        f"- 変更なし: {len(summary.unchanged)}件",
        f"- 消失 (削除はしていません): {len(summary.missing)}件",
        "",
    ]
    for label, items in (
        ("新規", summary.created),
        ("更新", summary.updated),
        ("消失", summary.missing),
        ("警告", summary.warnings),
    ):
        if items:
            lines.append(f"## {label}")
            lines.extend(f"- {x}" for x in items)
            lines.append("")
    report_path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="note-wxr-md",
        description="note公式エクスポート(WXR)を記事ごとのMarkdownへ一括変換します",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    conv = sub.add_parser("convert", help="WXRファイルを変換する")
    conv.add_argument("wxr", type=Path, help="note公式エクスポート (ZIP または WXRファイル)")
    conv.add_argument("--out", type=Path, required=True, help="出力先ディレクトリ")
    conv.add_argument(
        "--download-images",
        dest="download_images",
        action="store_true",
        default=True,
        help="画像をローカルにダウンロードして相対リンク化する (既定: 有効)",
    )
    conv.add_argument(
        "--no-images",
        dest="download_images",
        action="store_false",
        help="画像のローカル化をしない (元URLのまま)",
    )
    conv.add_argument(
        "--filename",
        choices=("title", "id"),
        default="title",
        help="ファイル名方式: title=公開日_タイトル / id=公開日_記事ID (既定: title)",
    )
    conv.add_argument(
        "--dry-run",
        action="store_true",
        help="書き込まず、差分サマリだけ表示する",
    )
    conv.add_argument("--report", type=Path, help="変換結果をファイルにも出す")

    args = parser.parse_args(argv)

    if args.command == "convert":
        if not args.wxr.is_file():
            print(f"エラー: 入力ファイルが見つかりません: {args.wxr}", file=sys.stderr)
            return 2
        try:
            summary = convert(
                args.wxr,
                args.out,
                download_images=args.download_images,
                filename_mode=args.filename,
                dry_run=args.dry_run,
            )
        except ET.ParseError as e:
            print(f"エラー: WXRの解析に失敗しました: {e}", file=sys.stderr)
            return 2
        except (ValueError, zipfile.BadZipFile) as e:
            print(f"エラー: 入力ファイルを開けませんでした: {e}", file=sys.stderr)
            return 2
        print_summary(summary, args.dry_run)
        if args.report:
            write_report(summary, args.report, args.dry_run)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
