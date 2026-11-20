"""note_wxr_md のテスト。全てダミーデータで検証する (実在記事は使わない)。"""

import http.server
import io
import re
import sys
import tempfile
import threading
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import note_wxr_md as m

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "sample.wxr"

# 1x1 のダミーPNG
PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6360000000020001e221bc3300000000"
    "49454e44ae426082"
)

WXR_TEMPLATE = """<?xml version="1.0" encoding="UTF-8" ?>
<rss version="2.0"
  xmlns:content="http://purl.org/rss/1.0/modules/content/"
  xmlns:dc="http://purl.org/dc/elements/1.1/"
  xmlns:wp="http://wordpress.org/export/1.2/">
<channel>
  <title>dummy</title>
  <link>https://note.com/dummy_user</link>
{items}
</channel>
</rss>
"""

ITEM_TEMPLATE = """  <item>
    <title>{title}</title>
    <link>https://note.com/dummy_user/n/{nid}</link>
    <pubDate>Sat, 01 Nov 2026 09:00:00 +0900</pubDate>
    <dc:creator>dummy_user</dc:creator>
    <guid isPermaLink="false">https://note.com/dummy_user/n/{nid}</guid>
    <content:encoded><![CDATA[{body}]]></content:encoded>
    <wp:post_id>{pid}</wp:post_id>
    <wp:post_date>2026-11-01 09:00:00</wp:post_date>
    <wp:status>publish</wp:status>
    <wp:post_type>post</wp:post_type>
  </item>"""


def make_wxr(items: list[dict]) -> str:
    body = "\n".join(ITEM_TEMPLATE.format(**it) for it in items)
    return WXR_TEMPLATE.format(items=body)


def run_convert(wxr: Path, out: Path, **kw):
    buf = io.StringIO()
    with redirect_stdout(buf):
        summary = m.convert(wxr, out, **kw)
    return summary, buf.getvalue()


class TestParseFixture(unittest.TestCase):
    def test_counts_and_fields(self):
        arts = m.parse_wxr(FIXTURE)
        self.assertEqual(len(arts), 3)
        a1, a2, a3 = arts
        self.assertEqual(a1.note_id, "ndummy0000001")
        self.assertEqual(a1.date, "2026-11-01T09:00:00")
        self.assertEqual(a1.date_ymd, "2026-11-01")
        self.assertEqual(a1.tags, ["テスト", "サンプル"])
        self.assertIsNone(a1.price)
        self.assertEqual(a2.price, 500)
        self.assertEqual(a3.tags, [])


class TestHtmlToMarkdown(unittest.TestCase):
    def test_basics(self):
        md = m.html_to_markdown(
            "<h2>見出し</h2><p>これは<strong>太字</strong>と"
            '<a href="https://example.com">リンク</a>です。</p>'
        )
        self.assertIn("## 見出し", md)
        self.assertIn("**太字**", md)
        self.assertIn("[リンク](https://example.com)", md)

    def test_code_and_table(self):
        md = m.html_to_markdown(
            '<pre><code class="language-python">print(1)\n</code></pre>'
            "<table><tr><td>x</td></tr></table>"
        )
        self.assertIn("```python", md)
        self.assertIn("print(1)", md)
        # 表はHTMLのまま残る方針
        self.assertIn("<table>", md)

    def test_list_and_blockquote(self):
        md = m.html_to_markdown(
            "<ul><li>one</li><li>two</li></ul>"
            "<blockquote><p>引用</p></blockquote>"
        )
        self.assertIn("- one", md)
        self.assertIn("- two", md)
        self.assertIn("> 引用", md)

    def test_image_default_keeps_url(self):
        md = m.html_to_markdown('<p><img src="https://example.com/a.png" alt="A"></p>')
        self.assertIn("![A](https://example.com/a.png)", md)


class TestConvert(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "out"

    def tearDown(self):
        self.tmp.cleanup()

    def test_convert_no_images(self):
        summary, _ = run_convert(FIXTURE, self.out, download_images=False)
        self.assertEqual(len(summary.created), 3)
        files = sorted(p.name for p in self.out.glob("*.md"))
        self.assertEqual(len(files), 3)
        # 記事3はファイル名に使えない文字 (ASCIIの / * 等) が _ に置換される
        for f in files:
            for ch in '/*?"<>|':
                self.assertNotIn(ch, f)
        a1 = next(p for p in self.out.glob("*記事1*"))
        text = a1.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---"))
        self.assertIn("id: ndummy0000001", text)
        self.assertIn('title: "ダミー記事1：見出しと画像と引用のテスト"', text)
        self.assertIn("## はじめに", text)
        # 画像DLなしなので元URLのまま
        self.assertIn("https://example.com/images/dummy1.png", text)
        a2 = next(p for p in self.out.glob("*記事2*"))
        self.assertIn("price: 500", a2.read_text(encoding="utf-8"))

    def test_idempotent_rerun(self):
        run_convert(FIXTURE, self.out, download_images=False)
        mtimes = {p.name: p.stat().st_mtime_ns for p in self.out.glob("*.md")}
        summary, _ = run_convert(FIXTURE, self.out, download_images=False)
        self.assertEqual(len(summary.unchanged), 3)
        self.assertEqual(len(summary.created), 0)
        self.assertEqual(len(summary.updated), 0)
        for p in self.out.glob("*.md"):
            self.assertEqual(p.stat().st_mtime_ns, mtimes[p.name])

    def test_update_detection(self):
        run_convert(FIXTURE, self.out, download_images=False)
        wxr2 = Path(self.tmp.name) / "v2.wxr"
        xml = FIXTURE.read_text(encoding="utf-8").replace("締めの段落です", "書き換えた段落です")
        wxr2.write_text(xml, encoding="utf-8")
        summary, _ = run_convert(wxr2, self.out, download_images=False)
        self.assertEqual(len(summary.updated), 1)
        self.assertEqual(len(summary.unchanged), 2)
        a1 = next(p for p in self.out.glob("*記事1*"))
        self.assertIn("書き換えた段落です", a1.read_text(encoding="utf-8"))

    def test_missing_reported_not_deleted(self):
        run_convert(FIXTURE, self.out, download_images=False)
        wxr3 = Path(self.tmp.name) / "v3.wxr"
        xml = make_wxr([
            dict(title="ダミー記事1：見出しと画像と引用のテスト", nid="ndummy0000001",
                 pid=101, body="<p>本文</p>"),
        ])
        wxr3.write_text(xml, encoding="utf-8")
        summary, _ = run_convert(wxr3, self.out, download_images=False)
        self.assertEqual(len(summary.missing), 2)
        # 消えた記事のファイルは残っている (削除しない)
        self.assertEqual(len(list(self.out.glob("*.md"))), 3)

    def test_dry_run_writes_nothing(self):
        summary, out_text = run_convert(
            FIXTURE, self.out, download_images=False, dry_run=True
        )
        self.assertEqual(len(summary.created), 3)
        self.assertFalse(self.out.exists())
        self.assertIn("新規", out_text)

    def test_filename_id_mode(self):
        summary, _ = run_convert(
            FIXTURE, self.out, download_images=False, filename_mode="id"
        )
        names = sorted(p.name for p in self.out.glob("*.md"))
        self.assertIn("2026-11-01_ndummy0000001.md", names)


class TestImageDownload(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "out"
        handler = type(
            "H", (http.server.BaseHTTPRequestHandler,), {
                "do_GET": lambda s: (
                    s.send_response(200),
                    s.send_header("Content-Type", "image/png"),
                    s.end_headers(),
                    s.wfile.write(PNG_BYTES),
                ),
                "log_message": lambda *a: None,
            }
        )
        self.server = http.server.HTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.tmp.cleanup()

    def test_download_success_and_rewrite(self):
        url = f"http://127.0.0.1:{self.port}/img/pic.png"
        wxr = Path(self.tmp.name) / "img.wxr"
        wxr.write_text(make_wxr([
            dict(title="画像テスト", nid="ndummy0000101", pid=201,
                 body=f'<p><img src="{url}" alt="pic"></p>'),
        ]), encoding="utf-8")
        summary, _ = run_convert(wxr, self.out, download_images=True)
        self.assertEqual(summary.warnings, [])
        assets = list((self.out / "assets").glob("*"))
        self.assertEqual(len(assets), 1)
        self.assertEqual(assets[0].suffix, ".png")
        md = next(self.out.glob("*.md")).read_text(encoding="utf-8")
        self.assertIn(f"![pic](assets/{assets[0].name})", md)
        # 再実行で変更なしになる (冪等)
        summary2, _ = run_convert(wxr, self.out, download_images=True)
        self.assertEqual(len(summary2.unchanged), 1)

    def test_download_failure_keeps_url(self):
        url = "http://127.0.0.1:1/nonexistent.png"  # 接続できないポート
        wxr = Path(self.tmp.name) / "imgfail.wxr"
        wxr.write_text(make_wxr([
            dict(title="画像失敗テスト", nid="ndummy0000102", pid=202,
                 body=f'<p><img src="{url}" alt="x"></p>'),
        ]), encoding="utf-8")
        summary, _ = run_convert(wxr, self.out, download_images=True)
        self.assertEqual(len(summary.warnings), 1)
        md = next(self.out.glob("*.md")).read_text(encoding="utf-8")
        self.assertIn(f"![x]({url})", md)


class TestFilename(unittest.TestCase):
    def test_long_title_truncated_with_id_suffix(self):
        art = m.Article(
            title="あ" * 120, link="", date="2026-11-01T09:00:00",
            date_ymd="2026-11-01", note_id="ndummy9999999", body_html="",
        )
        name = m.make_filename(art, "title", set())
        self.assertTrue(name.endswith("-999999.md"))
        self.assertLessEqual(len(name), 110)

    def test_sanitize(self):
        t, truncated = m.sanitize_title('a/b:c*d?e"f<g>h|i')
        self.assertEqual(t, "a_b_c_d_e_f_g_h_i")
        self.assertFalse(truncated)


class TestZipInput(unittest.TestCase):
    """noteエクスポートの実態であるZIP入力 (WXR + 同梱assets) の検証。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "out"
        xml = make_wxr([
            dict(title="ZIP記事1", nid="ndummy0000201", pid=301,
                 body='<p><img src="assets/dummy1.png" alt="同梱1"></p>'),
            dict(title="ZIP記事2", nid="ndummy0000202", pid=302,
                 body='<p><img src="https://assets.note.com/img/photo2.jpg" alt="同梱2"></p>'),
        ])
        self.zip_path = Path(self.tmp.name) / "export.zip"
        with zipfile.ZipFile(self.zip_path, "w") as zf:
            zf.writestr("export.xml", xml)
            zf.writestr("assets/dummy1.png", PNG_BYTES)
            zf.writestr("assets/photo2.jpg", b"\xff\xd8\xff\xe0dummy-jpeg")

    def tearDown(self):
        self.tmp.cleanup()

    def test_bundled_assets_localized(self):
        summary, _ = run_convert(self.zip_path, self.out, download_images=True)
        self.assertEqual(summary.warnings, [])
        self.assertEqual(len(summary.created), 2)
        assets = sorted(p.name for p in (self.out / "assets").glob("*"))
        self.assertEqual(assets, ["ndummy0000201-1.png", "ndummy0000202-1.jpg"])
        md1 = next(self.out.glob("*ZIP記事1*")).read_text(encoding="utf-8")
        self.assertIn("![同梱1](assets/ndummy0000201-1.png)", md1)
        # CDN風URLでもファイル名で同梱assetsに一致する
        md2 = next(self.out.glob("*ZIP記事2*")).read_text(encoding="utf-8")
        self.assertIn("![同梱2](assets/ndummy0000202-1.jpg)", md2)
        # 同梱バイトがそのまま書き出されている
        self.assertEqual((self.out / "assets" / "ndummy0000201-1.png").read_bytes(), PNG_BYTES)

    def test_zip_idempotent(self):
        run_convert(self.zip_path, self.out, download_images=True)
        summary, _ = run_convert(self.zip_path, self.out, download_images=True)
        self.assertEqual(len(summary.unchanged), 2)

    def test_zip_dry_run_writes_nothing(self):
        summary, _ = run_convert(self.zip_path, self.out, download_images=True, dry_run=True)
        self.assertEqual(len(summary.created), 2)
        self.assertFalse(self.out.exists())

    def test_zip_no_images_keeps_src(self):
        run_convert(self.zip_path, self.out, download_images=False)
        md1 = next(self.out.glob("*ZIP記事1*")).read_text(encoding="utf-8")
        self.assertIn("![同梱1](assets/dummy1.png)", md1)
        self.assertFalse((self.out / "assets").exists())


class TestCli(unittest.TestCase):
    def test_missing_input_returns_2(self):
        rc = m.main(["convert", "/nonexistent/export.wxr", "--out", "/tmp/x"])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
