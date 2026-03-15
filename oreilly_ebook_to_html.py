#!/usr/bin/env python3
"""
Scrape O'Reilly Japan ebook + catalog lists and generate two HTML galleries.

Output fields per book:
- title
- image
- price
- release date
- detail page link

Usage:
  python3 oreilly_ebook_to_html.py
  python3 oreilly_ebook_to_html.py --limit 50 --workers 4
  python3 oreilly_ebook_to_html.py --output output/index.html --book-output output/book/index.html
"""

from __future__ import annotations

import argparse
import concurrent.futures
import html
import json
import re
import time
from datetime import date
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, Iterable, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

LIST_URL_EBOOK = "https://www.oreilly.co.jp/ebook/"
LIST_URL_BOOK = "https://www.oreilly.co.jp/catalog/"
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_EBOOK_OUTPUT = BASE_DIR / "output/index.html"
DEFAULT_BOOK_OUTPUT = BASE_DIR / "output/book/index.html"
DEFAULT_EBOOK_CACHE = BASE_DIR / "output/oreilly_ebook_image_cache.json"
DEFAULT_BOOK_CACHE = BASE_DIR / "output/oreilly_catalog_image_cache.json"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


@dataclass
class Book:
    title: str
    price: str
    release_date: str
    detail_url: str
    image_url: str = ""


class EbookTableParser(HTMLParser):
    """Extract rows from the first table in the ebook page."""

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.books: List[Book] = []

        self._found_table = False
        self._in_table = False
        self._table_depth = 0

        self._in_tr = False
        self._in_td = False
        self._td_index = -1
        self._cell_text_parts: List[str] = []
        self._row_cells: List[str] = []

        self._in_title_anchor = False
        self._title_text_parts: List[str] = []
        self._title_href: str = ""

    def handle_starttag(self, tag: str, attrs: List[tuple[str, Optional[str]]]) -> None:
        attrs_dict = dict(attrs)

        if tag == "table":
            if not self._found_table:
                self._found_table = True
                self._in_table = True
                self._table_depth = 1
            elif self._in_table:
                self._table_depth += 1
            return

        if not self._in_table:
            return

        if tag == "tr":
            self._in_tr = True
            self._row_cells = []
            self._td_index = -1
            self._title_href = ""
            self._title_text_parts = []
            return

        if tag == "td" and self._in_tr:
            self._in_td = True
            self._td_index += 1
            self._cell_text_parts = []
            return

        if tag == "a" and self._in_td and self._td_index == 1:
            self._in_title_anchor = True
            self._title_href = attrs_dict.get("href") or self._title_href
            return

    def handle_endtag(self, tag: str) -> None:
        if tag == "table" and self._in_table:
            self._table_depth -= 1
            if self._table_depth <= 0:
                self._in_table = False
            return

        if not self._in_table:
            return

        if tag == "a" and self._in_title_anchor:
            self._in_title_anchor = False
            return

        if tag == "td" and self._in_td:
            text = _clean_text("".join(self._cell_text_parts))
            self._row_cells.append(text)
            self._cell_text_parts = []
            self._in_td = False
            return

        if tag == "tr" and self._in_tr:
            self._consume_row()
            self._in_tr = False
            self._row_cells = []
            self._td_index = -1
            self._title_text_parts = []
            self._title_href = ""

    def handle_data(self, data: str) -> None:
        if self._in_td:
            self._cell_text_parts.append(data)
        if self._in_title_anchor:
            self._title_text_parts.append(data)

    def _consume_row(self) -> None:
        # Expected: ISBN / Title / Price / Release date / Format
        if len(self._row_cells) < 4:
            return

        isbn = self._row_cells[0]
        if not isbn or isbn.upper() == "ISBN":
            return

        title_text = _clean_text("".join(self._title_text_parts)) or self._row_cells[1]
        detail_url = urljoin(self.base_url, self._title_href)
        price = self._row_cells[2]
        release_date = self._row_cells[3]

        if title_text and detail_url:
            self.books.append(
                Book(
                    title=title_text,
                    price=price,
                    release_date=release_date,
                    detail_url=detail_url,
                )
            )


class CoverImageParser(HTMLParser):
    """Extract og:image first, then fallback to cover photo image."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.og_image: str = ""
        self.cover_image: str = ""

    def handle_starttag(self, tag: str, attrs: List[tuple[str, Optional[str]]]) -> None:
        attrs_dict = dict(attrs)
        if tag == "meta":
            prop = (attrs_dict.get("property") or "").strip().lower()
            if prop == "og:image" and attrs_dict.get("content"):
                self.og_image = attrs_dict["content"] or ""
            return

        if tag == "img" and not self.cover_image:
            alt = (attrs_dict.get("alt") or "").strip()
            src = (attrs_dict.get("src") or "").strip()
            if src and alt == "[cover photo]":
                self.cover_image = src


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _parse_release_date(value: str) -> Optional[date]:
    match = re.search(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", value or "")
    if not match:
        return None
    year, month, day = (int(part) for part in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _sort_books_by_release_date(books: List[Book]) -> None:
    def sort_key(book: Book) -> tuple[bool, date]:
        parsed = _parse_release_date(book.release_date)
        if parsed is None:
            return (False, date.min)
        return (True, parsed)

    books.sort(key=sort_key, reverse=True)


def fetch_text(url: str, timeout: int, retries: int) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    delay = 1.0
    last_error: Optional[Exception] = None
    for _ in range(retries + 1):
        try:
            with urlopen(request, timeout=timeout) as response:
                content_type = response.headers.get_content_charset() or "utf-8"
                return response.read().decode(content_type, errors="replace")
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            last_error = exc
            time.sleep(delay)
            delay *= 1.8
    raise RuntimeError(f"Failed to fetch {url}: {last_error}")


def parse_books_from_list(html_text: str, list_url: str) -> List[Book]:
    parser = EbookTableParser(base_url=list_url)
    parser.feed(html_text)
    return parser.books


def extract_image_url(detail_html: str, detail_url: str) -> str:
    parser = CoverImageParser()
    parser.feed(detail_html)
    url = parser.og_image or parser.cover_image
    if not url:
        return ""
    return urljoin(detail_url, url)


def load_cache(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    return {}


def save_cache(path: Path, cache: Dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def scrape_image(detail_url: str, timeout: int, retries: int, delay: float) -> str:
    if delay > 0:
        time.sleep(delay)
    detail_html = fetch_text(detail_url, timeout=timeout, retries=retries)
    return extract_image_url(detail_html, detail_url)


def enrich_images(
    books: Iterable[Book],
    cache: Dict[str, str],
    timeout: int,
    retries: int,
    workers: int,
    delay: float,
    refresh_images: bool,
) -> None:
    books_list = list(books)
    targets = [
        book
        for book in books_list
        if refresh_images or not cache.get(book.detail_url)
    ]

    if not targets:
        for book in books_list:
            book.image_url = cache.get(book.detail_url, "")
        print("All images loaded from cache.")
        return

    print(f"Fetching cover images for {len(targets)} books...")

    if workers <= 1:
        for idx, book in enumerate(targets, start=1):
            try:
                image_url = scrape_image(book.detail_url, timeout, retries, delay)
            except Exception as exc:  # noqa: BLE001
                image_url = ""
                print(f"[{idx}/{len(targets)}] Failed: {book.detail_url} ({exc})")
            else:
                print(f"[{idx}/{len(targets)}] OK: {book.detail_url}")
            cache[book.detail_url] = image_url
        for book in books_list:
            book.image_url = cache.get(book.detail_url, "")
        return

    completed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(scrape_image, book.detail_url, timeout, retries, delay): book.detail_url
            for book in targets
        }
        for future in concurrent.futures.as_completed(future_map):
            detail_url = future_map[future]
            completed += 1
            try:
                image_url = future.result()
            except Exception as exc:  # noqa: BLE001
                image_url = ""
                print(f"[{completed}/{len(targets)}] Failed: {detail_url} ({exc})")
            else:
                print(f"[{completed}/{len(targets)}] OK: {detail_url}")
            cache[detail_url] = image_url

    for book in books_list:
        book.image_url = cache.get(book.detail_url, "")


def _book_card_html(book: Book, index: int) -> str:
    title = html.escape(book.title)
    price = html.escape(book.price)
    release_date = html.escape(book.release_date or "-")
    detail_url = html.escape(book.detail_url)
    data_title = html.escape(book.title.lower())

    if book.image_url:
        image_part = (
            f'<img loading="lazy" src="{html.escape(book.image_url)}" '
            f'alt="{title} cover">'
        )
    else:
        image_part = '<div class="cover-missing">NO COVER</div>'

    delay_index = index % 16
    return f"""
<article class="book-card" data-title="{data_title}" style="--delay:{delay_index};">
  <a class="cover" href="{detail_url}" target="_blank" rel="noopener noreferrer">
    {image_part}
  </a>
  <div class="book-meta">
    <h2>{title}</h2>
    <p class="price">{price} yen</p>
    <p class="release-date">Release: {release_date}</p>
  </div>
</article>
""".strip()


def _page_links(page_key: str) -> tuple[str, str]:
    if page_key == "book":
        return ("../index.html", "./index.html")
    return ("./index.html", "book/index.html")


def build_html(books: List[Book], page_title: str, page_key: str, count_label: str) -> str:
    cards = "\n".join(_book_card_html(book, idx) for idx, book in enumerate(books))
    total = len(books)
    ebook_href, book_href = _page_links(page_key)
    title_href = book_href if page_key == "ebook" else ebook_href
    display_title = page_title
    if page_key == "ebook":
        display_title = page_title.replace(
            " Ebook ", '&nbsp;<span class="title-em">Ebook</span>&nbsp;'
        )
        if display_title == page_title:
            display_title = page_title.replace(
                "Ebook", '&nbsp;<span class="title-em">Ebook</span>&nbsp;'
            )
    elif page_key == "book":
        display_title = page_title.replace(
            " Book ", '&nbsp;<span class="title-em">Book</span>&nbsp;'
        )
        if display_title == page_title:
            display_title = page_title.replace(
                "Book", '&nbsp;<span class="title-em">Book</span>&nbsp;'
            )

    if page_key == "ebook":
        title_fill = "#f15a4c"
        outline_soft = "rgba(241, 90, 76, 0.35)"
    else:
        title_fill = "#4aa6ff"
        outline_soft = "rgba(74, 166, 255, 0.35)"

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{page_title}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+JP:wght@400;600&family=Shippori+Mincho+B1:wght@600;700&display=swap" rel="stylesheet">
  <style>
    :root {{
      --bg: #f6f1e7;
      --surface: rgba(255, 255, 255, 0.76);
      --ink: #1f2421;
      --muted: #5f6259;
      --accent: #0e7a5a;
      --accent-2: #c9673d;
      --title-outline: #1f2421;
      --title-outline-soft: {outline_soft};
      --title-fill: {title_fill};
      --line: rgba(31, 36, 33, 0.15);
      --shadow: 0 16px 40px rgba(14, 20, 18, 0.12);
    }}
    * {{
      box-sizing: border-box;
    }}
    body {{
      margin: 0;
      font-family: "IBM Plex Sans JP", "Hiragino Kaku Gothic ProN", Meiryo, sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at 8% 12%, rgba(201, 103, 61, 0.25), transparent 34%),
        radial-gradient(circle at 88% 6%, rgba(14, 122, 90, 0.18), transparent 36%),
        linear-gradient(180deg, #fcf8f2 0%, #f2ede2 100%);
      min-height: 100vh;
    }}
    .wrap {{
      width: min(1280px, 94vw);
      margin: 16px auto 44px;
    }}
    .topbar {{
      --title-size: 1.26rem;
      position: sticky;
      top: 12px;
      z-index: 20;
      border: 1px solid var(--line);
      border-radius: 20px;
      padding: 10px 14px;
      background: var(--surface);
      backdrop-filter: blur(6px);
      box-shadow: var(--shadow);
      display: flex;
      align-items: center;
      gap: 12px;
      flex-wrap: nowrap;
      overflow-x: auto;
      overflow-y: visible;
      white-space: nowrap;
    }}
    .topbar::-webkit-scrollbar {{
      height: 6px;
    }}
    .topbar::-webkit-scrollbar-thumb {{
      background: rgba(31, 36, 33, 0.28);
      border-radius: 999px;
    }}
    .top-title {{
      margin: 0;
      font-family: "Shippori Mincho B1", "Hiragino Mincho ProN", serif;
      letter-spacing: 0.01em;
      font-size: var(--title-size);
      line-height: 1.1;
      flex: 0 0 auto;
    }}
    .top-title a {{
      color: inherit;
      text-decoration: none;
      padding: 4px 6px;
      border-radius: 12px;
      display: inline-flex;
      align-items: center;
    }}
    .title-em {{
      color: var(--title-fill);
      font-weight: 700;
      letter-spacing: 0.02em;
      -webkit-text-stroke: 0.6px var(--title-outline);
      text-shadow: 0 0 2px rgba(31, 36, 33, 0.2);
      padding: 0 4px;
    }}
    .top-title a:hover {{
      background: rgba(14, 122, 90, 0.08);
    }}
    .top-title a:focus-visible {{
      outline: 2px solid rgba(14, 122, 90, 0.6);
      outline-offset: 3px;
    }}
    .top-count {{
      font-size: calc(var(--title-size) * 0.5);
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 5px 10px;
      background: rgba(255, 255, 255, 0.72);
      color: var(--muted);
      flex: 0 0 auto;
    }}
    .top-count strong {{
      color: var(--accent);
    }}
    .search {{
      flex: 0 1 380px;
      min-width: 200px;
      max-width: 460px;
      min-height: 38px;
      margin-left: auto;
      border-radius: 12px;
      border: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.92);
      padding: 0 14px;
      font-size: 0.92rem;
      color: var(--ink);
      outline: none;
    }}
    .search:focus {{
      border-color: rgba(14, 122, 90, 0.75);
      box-shadow: 0 0 0 3px rgba(14, 122, 90, 0.15);
    }}
    .grid {{
      margin-top: 18px;
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(210px, 1fr));
      gap: 18px;
    }}
    .book-card {{
      border: 1px solid var(--line);
      border-radius: 16px;
      background: rgba(255, 255, 255, 0.92);
      overflow: hidden;
      box-shadow: 0 10px 28px rgba(31, 36, 33, 0.08);
      transform: translateY(14px);
      opacity: 0;
      animation: card-in 520ms ease forwards;
      animation-delay: calc(var(--delay) * 35ms);
    }}
    .book-card:hover {{
      box-shadow: 0 16px 30px rgba(31, 36, 33, 0.14);
      transform: translateY(-3px);
      transition: transform 180ms ease, box-shadow 180ms ease;
    }}
    .cover {{
      display: block;
      aspect-ratio: 3 / 4;
      background: linear-gradient(160deg, #e9e5dd, #f8f5ef);
      border-bottom: 1px solid var(--line);
      text-decoration: none;
    }}
    .cover img {{
      width: 100%;
      height: 100%;
      object-fit: cover;
      display: block;
    }}
    .cover-missing {{
      width: 100%;
      height: 100%;
      display: grid;
      place-items: center;
      font-size: 0.82rem;
      color: var(--muted);
      letter-spacing: 0.1em;
    }}
    .book-meta {{
      padding: 12px 12px 14px;
    }}
    .book-meta h2 {{
      margin: 0;
      font-family: "Shippori Mincho B1", "Hiragino Mincho ProN", serif;
      font-size: 1rem;
      line-height: 1.38;
      min-height: 2.76em;
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }}
    .price {{
      margin: 10px 0 0;
      color: var(--accent-2);
      font-weight: 600;
      font-size: 0.95rem;
    }}
    .release-date {{
      margin: 5px 0 0;
      color: var(--muted);
      font-size: 0.84rem;
      line-height: 1.35;
    }}
    [hidden] {{
      display: none !important;
    }}
    @keyframes card-in {{
      to {{
        transform: translateY(0);
        opacity: 1;
      }}
    }}
    @media (max-width: 700px) {{
      .wrap {{
        width: min(96vw, 1200px);
        margin-top: 12px;
      }}
      .topbar {{
        --title-size: 1.14rem;
        top: 8px;
        gap: 10px;
        padding: 9px 10px;
      }}
      .search {{
        min-height: 36px;
        min-width: 170px;
        flex-basis: 190px;
        display: none;
      }}
      .grid {{
        gap: 14px;
        grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
      }}
    }}
  </style>
</head>
<body data-page="{page_key}">
  <main class="wrap">
    <section class="topbar">
      <h1 class="top-title"><a href="{title_href}">{display_title}</a></h1>
      <span class="top-count">{count_label}: <strong>{total}</strong></span>
      <input id="q" class="search" type="search" placeholder="Filter by title...">
    </section>

    <section class="grid" id="book-grid">
      {cards}
    </section>
  </main>

  <script>
    (() => {{
      const input = document.getElementById("q");
      const cards = Array.from(document.querySelectorAll(".book-card"));

      function applyFilter() {{
        const q = input.value.trim().toLowerCase();
        for (const card of cards) {{
          const title = card.dataset.title || "";
          const show = !q || title.includes(q);
          card.hidden = !show;
        }}
      }}

      input.addEventListener("input", applyFilter);
      applyFilter();
    }})();
  </script>
</body>
</html>
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Scrape https://www.oreilly.co.jp/ebook/ and https://www.oreilly.co.jp/catalog/ "
            "and generate two HTML galleries with title/image/price/release-date/detail link."
        )
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_EBOOK_OUTPUT,
        help="Ebook HTML path",
    )
    parser.add_argument(
        "--book-output",
        type=Path,
        default=DEFAULT_BOOK_OUTPUT,
        help="Book HTML path",
    )
    parser.add_argument(
        "--cache",
        type=Path,
        default=DEFAULT_EBOOK_CACHE,
        help="Ebook image cache JSON path",
    )
    parser.add_argument(
        "--book-cache",
        type=Path,
        default=DEFAULT_BOOK_CACHE,
        help="Book image cache JSON path",
    )
    parser.add_argument("--timeout", type=int, default=25, help="HTTP timeout seconds")
    parser.add_argument("--retries", type=int, default=2, help="HTTP retries per URL")
    parser.add_argument("--workers", type=int, default=4, help="Concurrent workers for detail pages")
    parser.add_argument("--delay", type=float, default=0.0, help="Delay seconds before each detail-page request")
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Limit number of books to process (0 = all)",
    )
    parser.add_argument(
        "--refresh-images",
        action="store_true",
        help="Ignore cache and re-fetch all detail-page images",
    )
    return parser.parse_args()


def _fetch_books(list_url: str, timeout: int, retries: int) -> List[Book]:
    print(f"Fetching list page: {list_url}")
    list_html = fetch_text(list_url, timeout=timeout, retries=retries)
    books = parse_books_from_list(list_html, list_url)
    if not books:
        raise RuntimeError("No books found. The page structure may have changed.")
    return books


def main() -> None:
    args = parse_args()
    ebook_books = _fetch_books(LIST_URL_EBOOK, timeout=args.timeout, retries=args.retries)
    book_books = _fetch_books(LIST_URL_BOOK, timeout=args.timeout, retries=args.retries)
    _sort_books_by_release_date(book_books)

    if args.limit > 0:
        ebook_books = ebook_books[: args.limit]
        book_books = book_books[: args.limit]

    print(f"Ebooks parsed: {len(ebook_books)}")
    print(f"Books parsed: {len(book_books)}")

    ebook_cache = load_cache(args.cache)
    enrich_images(
        books=ebook_books,
        cache=ebook_cache,
        timeout=args.timeout,
        retries=args.retries,
        workers=max(1, args.workers),
        delay=max(0.0, args.delay),
        refresh_images=args.refresh_images,
    )
    save_cache(args.cache, ebook_cache)

    book_cache = load_cache(args.book_cache)
    enrich_images(
        books=book_books,
        cache=book_cache,
        timeout=args.timeout,
        retries=args.retries,
        workers=max(1, args.workers),
        delay=max(0.0, args.delay),
        refresh_images=args.refresh_images,
    )
    save_cache(args.book_cache, book_cache)

    ebook_html = build_html(
        ebook_books,
        page_title="O'Reilly Japan Ebook Gallery",
        page_key="ebook",
        count_label="Ebooks",
    )
    book_html = build_html(
        book_books,
        page_title="O'Reilly Japan Book Gallery",
        page_key="book",
        count_label="Books",
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(ebook_html, encoding="utf-8")

    args.book_output.parent.mkdir(parents=True, exist_ok=True)
    args.book_output.write_text(book_html, encoding="utf-8")

    print(f"Ebook HTML written: {args.output}")
    print(f"Ebook cache written: {args.cache}")
    print(f"Book HTML written: {args.book_output}")
    print(f"Book cache written: {args.book_cache}")


if __name__ == "__main__":
    main()
