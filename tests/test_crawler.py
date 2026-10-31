from contextlib import nullcontext
from datetime import date
from unittest.mock import Mock

import httpx
import pytest

from ask_ymca.crawler import (
    CrawlError,
    UniversityCrawler,
    _embedding_indices,
    _make_chunks,
    _parse_date,
)


@pytest.fixture
def crawler(tmp_path):
    config = tmp_path / "sources.yaml"
    config.write_text(
        "allowed_hosts:\n"
        "  - jcboseust.ac.in\n"
        "max_documents_per_source: 5\n"
        "request_delay_seconds: 0\n"
        "sources:\n"
        "  - name: test\n"
        "    url: https://jcboseust.ac.in/\n"
        "    category: notices\n"
        "    priority: 1\n"
        "    enabled: true\n"
        "    interval_hours: 24\n",
        encoding="utf-8",
    )
    instance = UniversityCrawler(config)
    yield instance
    instance.close()


def test_crawler_rejects_external_hosts_http_and_login_routes(crawler):
    with pytest.raises(CrawlError):
        crawler._normalize_url("https://example.com/notice.pdf")
    with pytest.raises(CrawlError):
        crawler._normalize_url("http://jcboseust.ac.in/notices")
    with pytest.raises(CrawlError):
        crawler._normalize_url("https://jcboseust.ac.in/app")


def test_request_retries_redirect_without_location(crawler, monkeypatch):
    url = "https://jcboseust.ac.in/assets/files/calendar.pdf"
    request = httpx.Request("GET", url)
    redirected = httpx.Response(302, request=request)
    successful = httpx.Response(200, content=b"pdf", request=request)
    stream = Mock(
        side_effect=[nullcontext(redirected), nullcontext(successful)]
    )
    monkeypatch.setattr(crawler._client, "stream", stream)
    monkeypatch.setattr("ask_ymca.crawler.time.sleep", lambda _: None)

    response, body = crawler._request(url)

    assert response.status_code == 200
    assert body == b"pdf"
    assert stream.call_count == 2


def test_crawler_extracts_page_text_and_relevant_pdf_links(crawler):
    html = b"""
    <html><head><title>University updates</title></head>
      <body><nav>Navigation is not page content</nav>
        <main><h1>Examination notices</h1>
          <p>Official examination information.</p>
          <a href="/assets/files/Dec_2026_exam_datesheet.pdf">Final exam date sheet</a>
          <a href="/assets/files/purchase_tender.pdf">Purchase tender</a>
          <a href="https://example.com/attendance.pdf">External attendance</a>
        </main>
      </body>
    </html>
    """

    title, text, links = crawler._extract_html(html, "https://jcboseust.ac.in/coe-office")
    candidates = crawler._relevant_links(links, "https://jcboseust.ac.in/coe-office")

    assert title == "Examination notices"
    assert "Official examination information." in text
    assert "Navigation is not page content" not in text
    assert len(candidates) == 1
    assert candidates[0][0] == "Final exam date sheet"


def test_chunks_overlap_and_keep_page_numbers():
    words = " ".join(f"w{index}" for index in range(1100))

    chunks = _make_chunks("", [(2, words)])

    assert [page for _, page in chunks] == [2, 2, 2]
    assert len(chunks[0][0].split()) == 500
    assert chunks[0][0].split()[-70:] == chunks[1][0].split()[:70]


def test_embedding_indices_sample_large_documents_across_the_full_text():
    indices = _embedding_indices(1000, limit=4)

    assert indices == [0, 333, 666, 999]
    assert _embedding_indices(3, limit=4) == [0, 1, 2]


def test_parse_publication_dates():
    assert _parse_date("Published Tue, 14 Oct 2026") == date(2026, 10, 14)
    assert _parse_date("Last date 30.10.2026") == date(2026, 10, 30)
    assert _parse_date("date not stated") is None
