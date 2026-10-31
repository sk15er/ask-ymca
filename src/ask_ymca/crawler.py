import hashlib
import io
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx
import pymupdf
import yaml
from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.orm import Session

from ask_ymca.assistant import AgentUnavailable, generate_embeddings
from ask_ymca.config import get_settings
from ask_ymca.models import Document, DocumentChunk, DocumentVersion, IngestionLog

logger = logging.getLogger(__name__)
USER_AGENT = "AskYMCAUniversityInfoBot/1.0 (+https://jcboseust.ac.in/)"
BLOCKED_PATHS = ("/app", "/login", "/erp", "/student-portal")
RELEVANT = re.compile(
    r"exam|date.?sheet|result|re.?evaluat|attendance|academic|syllabus|scheme|"
    r"ordinance|calendar|fee|scholarship|hostel|placement|internship|"
    r"admission|student|library|notice|degree|certificate|course|paper",
    re.I,
)
EXCLUDED = re.compile(r"tender|recruit|procurement|purchase|vendor", re.I)
WORD = re.compile(r"\S+")
MAX_EMBEDDED_CHUNKS_PER_DOCUMENT = 128


class CrawlError(Exception):
    pass


@dataclass
class CrawlSummary:
    sources: int = 0
    seen: int = 0
    changed: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class FetchedPage:
    url: str
    body: bytes
    content_type: str
    etag: str | None
    last_modified: str | None


class UniversityCrawler:
    def __init__(self, config_path: Path | None = None):
        self.config_path = config_path or Path.cwd() / "config" / "sources.yaml"
        with self.config_path.open(encoding="utf-8") as registry:
            config = yaml.safe_load(registry)
        self.allowed_hosts = set(config["allowed_hosts"])
        self.sources = [source for source in config["sources"] if source["enabled"]]
        self.max_documents = int(config.get("max_documents_per_source", 6))
        self.request_delay = float(config.get("request_delay_seconds", 0.25))
        self.max_response_bytes = int(config.get("max_response_bytes", 15_000_000))
        self._last_request: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser | None] = {}
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            timeout=httpx.Timeout(20, connect=10),
            follow_redirects=False,
        )

    def close(self) -> None:
        self._client.close()

    def crawl(self, db: Session) -> CrawlSummary:
        summary = CrawlSummary(sources=len(self.sources))
        for source in sorted(self.sources, key=lambda item: item["priority"]):
            source_log = IngestionLog(
                source_name=source["name"],
                status="RUNNING",
                items_seen=0,
                items_changed=0,
            )
            db.add(source_log)
            db.commit()
            errors_before = len(summary.errors)
            try:
                before_seen, before_changed = summary.seen, summary.changed
                self._crawl_source(db, source, summary, source_log)
                source_log.status = (
                    "PARTIAL" if len(summary.errors) > errors_before else "SUCCEEDED"
                )
                source_log.items_seen = summary.seen - before_seen
                source_log.items_changed = summary.changed - before_changed
            except Exception as exc:
                logger.exception("Unable to crawl source %s", source["name"])
                source_log.status = "FAILED"
                source_log.error_message = str(exc)[:2000]
                summary.errors.append(f"{source['name']}: {exc}")
            finally:
                if not source_log.error_message and len(summary.errors) > errors_before:
                    source_log.error_message = "\n".join(
                        summary.errors[errors_before:]
                    )[:2000]
                source_log.completed_at = datetime.now(UTC)
                db.commit()
        return summary

    def _crawl_source(
        self,
        db: Session,
        source: dict,
        summary: CrawlSummary,
        source_log: IngestionLog,
    ) -> None:
        source_url = self._normalize_url(source["url"])
        source_url = self._validated_url(source_url)
        page = self._fetch(source_url, db)
        if not page.body:
            summary.skipped += 1
            self._update_progress(db, source_log, changed=False)
            return
        if not self._is_pdf(page):
            title, text, links = self._extract_html(page.body, page.url)
            self._save_document(db, page, title or source["name"], source["category"], text)
            summary.seen += 1
            summary.changed += int(self._last_saved_changed)
            self._update_progress(db, source_log, self._last_saved_changed)
            attachments = self._relevant_links(links, page.url)
            for title, url, link_text in attachments[: self.max_documents]:
                try:
                    document_page = self._fetch(url, db)
                    if not document_page.body:
                        summary.skipped += 1
                        continue
                    if not self._is_pdf(document_page):
                        continue
                    text, pages = self._extract_pdf(document_page.body)
                    if not text.strip():
                        summary.skipped += 1
                        continue
                    self._save_document(
                        db,
                        document_page,
                        title,
                        source["category"],
                        text,
                        page_texts=pages,
                        published_date=_parse_date(link_text),
                    )
                    summary.seen += 1
                    summary.changed += int(self._last_saved_changed)
                    self._update_progress(db, source_log, self._last_saved_changed)
                except Exception as exc:
                    logger.warning("Skipped university attachment %s: %s", url, exc)
                    summary.errors.append(f"{url}: {exc}")
        else:
            title = unquote(Path(urlsplit(page.url).path).stem).replace("_", " ")
            text, pages = self._extract_pdf(page.body)
            self._save_document(
                db, page, title, source["category"], text, page_texts=pages
            )
            summary.seen += 1
            summary.changed += int(self._last_saved_changed)
            self._update_progress(db, source_log, self._last_saved_changed)

    @staticmethod
    def _update_progress(
        db: Session, source_log: IngestionLog, changed: bool
    ) -> None:
        source_log.items_seen += 1
        source_log.items_changed += int(changed)
        db.commit()

    def _normalize_url(self, url: str) -> str:
        parts = urlsplit(url)
        if not parts.scheme:
            url = urljoin("https://jcboseust.ac.in/", url)
            parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.hostname.lower() not in self.allowed_hosts
            or parts.username is not None
            or parts.password is not None
            or parts.port not in (None, 443)
        ):
            raise CrawlError(f"URL is outside the approved HTTPS university hosts: {url}")
        path = re.sub(r"/+", "/", parts.path or "/")
        if any(segment in path.casefold() for segment in BLOCKED_PATHS):
            raise CrawlError(f"Login/ERP URL is not crawlable: {url}")
        return urlunsplit(("https", parts.netloc.lower(), path, parts.query, ""))

    def _validated_url(self, url: str) -> str:
        validated = self._normalize_url(url)
        if not self._allowed_by_robots(validated):
            raise CrawlError(f"robots.txt disallows URL: {validated}")
        return validated

    def _allowed_by_robots(self, url: str) -> bool:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if host not in self._robots:
            robots_url = f"https://{host}/robots.txt"
            parser = RobotFileParser(robots_url)
            try:
                response, body = self._request(robots_url)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    parser.parse([])
                    self._robots[host] = parser
                    return True
                self._robots[host] = None
                return False
            except (httpx.HTTPError, CrawlError):
                self._robots[host] = None
                return False
            if response.status_code != 200:
                self._robots[host] = None
                return False
            parser.parse(body.decode("utf-8", "replace").splitlines())
            self._robots[host] = parser
        parser = self._robots[host]
        return parser is not None and parser.can_fetch(USER_AGENT, url)

    def _request(
        self, url: str, headers: dict[str, str] | None = None
    ) -> tuple[httpx.Response, bytes]:
        for attempt in range(3):
            current_url = url
            missing_location_status: int | None = None
            try:
                for _ in range(5):
                    current_url = self._normalize_url(current_url)
                    host = urlsplit(current_url).hostname or ""
                    delay = self.request_delay - (
                        time.monotonic() - self._last_request.get(host, 0)
                    )
                    if delay > 0:
                        time.sleep(delay)
                    with self._client.stream(
                        "GET", current_url, headers=headers
                    ) as response:
                        self._last_request[host] = time.monotonic()
                        if response.is_redirect:
                            location = response.headers.get("location")
                            if not location:
                                missing_location_status = response.status_code
                                break
                            current_url = urljoin(current_url, location)
                            continue
                        response.raise_for_status()
                        body = bytearray()
                        for content in response.iter_bytes():
                            body.extend(content)
                            if len(body) > self.max_response_bytes:
                                raise CrawlError(
                                    f"Response exceeds configured size limit: {current_url}"
                                )
                        return response, bytes(body)
                if missing_location_status is not None:
                    if attempt == 2:
                        raise CrawlError(
                            f"HTTP {missing_location_status} redirect from "
                            f"{current_url} has no location after retries"
                        )
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise CrawlError(f"Too many redirects for {url}")
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code < 500 or attempt == 2:
                    raise
            except httpx.TransportError:
                if attempt == 2:
                    raise
            time.sleep(0.5 * (attempt + 1))
        raise CrawlError(f"Request failed after retries: {url}")

    def _fetch(self, url: str, db: Session) -> FetchedPage:
        safe_url = self._validated_url(url)
        existing = db.scalar(
            select(Document).where(Document.canonical_url == safe_url)
        )
        current_version = (
            db.get(DocumentVersion, existing.current_version_id)
            if existing and existing.current_version_id
            else None
        )
        headers: dict[str, str] = {}
        if current_version:
            if current_version.etag:
                headers["If-None-Match"] = current_version.etag
            if current_version.last_modified:
                headers["If-Modified-Since"] = current_version.last_modified
        try:
            response, body = self._request(safe_url, headers=headers)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 304 and existing and current_version:
                return FetchedPage(
                    url=safe_url,
                    body=b"",
                    content_type="",
                    etag=current_version.etag,
                    last_modified=current_version.last_modified,
                )
            raise
        return FetchedPage(
            url=self._normalize_url(str(response.url)),
            body=body,
            content_type=response.headers.get("content-type", "").split(";")[0].lower(),
            etag=response.headers.get("etag"),
            last_modified=response.headers.get("last-modified"),
        )

    @staticmethod
    def _is_pdf(page: FetchedPage) -> bool:
        return page.content_type == "application/pdf" or page.url.casefold().endswith(".pdf")

    @staticmethod
    def _extract_html(
        body: bytes, url: str
    ) -> tuple[str, str, list[tuple[str, str, str]]]:
        soup = BeautifulSoup(body, "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        for unwanted in soup.select("script, style, nav, footer, header, noscript, iframe"):
            unwanted.decompose()
        main = soup.select_one("main, #main, #content, .main-content") or soup.body or soup
        headings = main.find_all(["h1", "h2", "h3", "h4"])
        page_heading = " ".join(headings[0].stripped_strings) if headings else ""
        content_nodes = main.find_all(["h1", "h2", "h3", "h4", "p", "li", "tr"])
        text = "\n".join(
            " ".join(node.stripped_strings)
            for node in content_nodes
            if " ".join(node.stripped_strings)
        )
        if len(text) < 100:
            text = main.get_text("\n", strip=True)
        links: list[tuple[str, str, str]] = []
        for anchor in soup.select("a[href]"):
            label = " ".join(anchor.get_text(" ", strip=True).split())
            href = anchor.get("href", "").strip()
            if not label or not href or href.startswith(("mailto:", "tel:", "javascript:")):
                continue
            absolute = urljoin(url, href)
            listing_row = anchor.find_parent(["li", "tr"])
            link_context = (
                " ".join(listing_row.get_text(" ", strip=True).split())
                if listing_row
                else label
            )
            links.append((label[:500], absolute, link_context[:1000]))
        return page_heading or title, text[:1_000_000], links

    @staticmethod
    def _extract_pdf(body: bytes) -> tuple[str, list[tuple[int, str]]]:
        pages: list[tuple[int, str]] = []
        with pymupdf.open(stream=io.BytesIO(body), filetype="pdf") as pdf:
            for index, page in enumerate(pdf, start=1):
                text = page.get_text("text", sort=True).strip()
                if text:
                    pages.append((index, text))
        return "\n\n".join(f"[Page {number}]\n{text}" for number, text in pages), pages

    def _relevant_links(
        self, links: list[tuple[str, str, str]], parent_url: str
    ) -> list[tuple[str, str, str]]:
        candidates: list[tuple[str, str, str]] = []
        seen: set[str] = set()
        for label, raw_url, link_text in links:
            try:
                url = self._normalize_url(urljoin(parent_url, raw_url))
            except CrawlError:
                continue
            if url in seen or not urlsplit(url).path.casefold().endswith(".pdf"):
                continue
            seen.add(url)
            descriptor = f"{label} {url}"
            if EXCLUDED.search(descriptor) or not RELEVANT.search(descriptor):
                continue
            candidates.append((label or Path(urlsplit(url).path).stem, url, link_text))
        return candidates

    @property
    def _last_saved_changed(self) -> bool:
        return getattr(self, "__last_saved_changed", False)

    @_last_saved_changed.setter
    def _last_saved_changed(self, value: bool) -> None:
        self.__last_saved_changed = value

    def _save_document(
        self,
        db: Session,
        page: FetchedPage,
        title: str,
        category: str,
        text: str,
        *,
        page_texts: list[tuple[int, str]] | None = None,
        published_date: date | None = None,
    ) -> None:
        if not page.body:
            self._last_saved_changed = False
            return
        digest = hashlib.sha256(page.body).hexdigest()
        document = db.scalar(
            select(Document).where(Document.canonical_url == page.url)
        )
        if document and document.current_version_id:
            current = db.get(DocumentVersion, document.current_version_id)
            if current and current.sha256 == digest:
                self._last_saved_changed = False
                return
        if not document:
            document = Document(
                source_name="J.C. Bose University official website",
                source_url=page.url,
                canonical_url=page.url,
                title=title[:500],
                category=category[:80],
                status="NEEDS_REVIEW",
                published_date=published_date,
                extra_metadata={"publisher": "J.C. Bose University of Science and Technology"},
            )
            db.add(document)
            db.flush()
        else:
            document.title = title[:500]
            document.category = category[:80]
            document.status = "NEEDS_REVIEW"
            if published_date:
                document.published_date = published_date

        previous_version = db.scalar(
            select(DocumentVersion)
            .where(DocumentVersion.document_id == document.id)
            .order_by(DocumentVersion.version_number.desc())
        )
        version = DocumentVersion(
            document_id=document.id,
            version_number=(previous_version.version_number + 1 if previous_version else 1),
            sha256=digest,
            etag=page.etag,
            last_modified=page.last_modified,
            extracted_text=text,
            extra_metadata={"content_type": page.content_type},
        )
        db.add(version)
        db.flush()
        document.current_version_id = version.id
        chunks = _make_chunks(text, page_texts)
        embedding_indices = _embedding_indices(len(chunks))
        embeddings: dict[int, list[float]] = {}
        try:
            for start in range(0, len(embedding_indices), 16):
                batch_indices = embedding_indices[start : start + 16]
                batch_embeddings = generate_embeddings(
                    [chunks[index][0] for index in batch_indices],
                    get_settings(),
                )
                embeddings.update(zip(batch_indices, batch_embeddings, strict=True))
        except AgentUnavailable as exc:
            logger.warning(
                "Saved %s without vector embeddings; lexical retrieval remains available: %s",
                page.url,
                exc,
            )
        for chunk_index, (content, page_number) in enumerate(chunks):
            db.add(
                DocumentChunk(
                    document_version_id=version.id,
                    chunk_index=chunk_index,
                    content=content,
                    page_number=page_number,
                    token_count=len(WORD.findall(content)),
                    embedding=embeddings.get(chunk_index),
                    extra_metadata={"source_url": page.url, "category": category},
                )
            )
        db.commit()
        self._last_saved_changed = True


def _make_chunks(
    text: str, page_texts: list[tuple[int, str]] | None = None
) -> list[tuple[str, int | None]]:
    source_pages = page_texts or [(None, text)]
    chunks: list[tuple[str, int | None]] = []
    max_words = 500
    overlap = 70
    for page_number, page_text in source_pages:
        words = WORD.findall(page_text)
        if not words:
            continue
        start = 0
        while start < len(words):
            stop = min(start + max_words, len(words))
            chunks.append((" ".join(words[start:stop]), page_number))
            if stop == len(words):
                break
            start = stop - overlap
    return chunks


def _embedding_indices(
    chunk_count: int,
    limit: int = MAX_EMBEDDED_CHUNKS_PER_DOCUMENT,
) -> list[int]:
    if chunk_count <= limit:
        return list(range(chunk_count))
    if limit == 1:
        return [chunk_count // 2]
    return [
        round(index * (chunk_count - 1) / (limit - 1))
        for index in range(limit)
    ]


def _parse_date(text: str) -> date | None:
    match = re.search(r"\b(\d{1,2})\s+([A-Za-z]{3,9})\s+(20\d{2})\b", text)
    if match:
        for date_format in ("%d %B %Y", "%d %b %Y"):
            try:
                return datetime.strptime(match.group(0), date_format).date()
            except ValueError:
                continue
    match = re.search(r"\b(\d{1,2})[./-](\d{1,2})[./-](20\d{2})\b", text)
    if match:
        try:
            return date(int(match.group(3)), int(match.group(2)), int(match.group(1)))
        except ValueError:
            pass
    return None


_crawl_lock = threading.Lock()


def crawl_university(db: Session) -> CrawlSummary:
    if not _crawl_lock.acquire(blocking=False):
        raise CrawlError("A university source crawl is already running")
    crawler: UniversityCrawler | None = None
    try:
        crawler = UniversityCrawler()
        return crawler.crawl(db)
    finally:
        if crawler:
            crawler.close()
        _crawl_lock.release()
