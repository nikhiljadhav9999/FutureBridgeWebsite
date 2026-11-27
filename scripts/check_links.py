#!/usr/bin/env python3
"""Check links found on https://www.futurebridge.com.

Crawls at most 10 pages on www.futurebridge.com, then checks every discovered
link with a HEAD request, falling back to GET. Flags HTTP 4xx/5xx responses,
timeouts, request failures, and internal links that redirect to the homepage.

Results are written to link_check.log. Exits with status 1 when issues are found.

Usage:
    python scripts/check_links.py [--max-pages N] [--start-url URL] [--output FILE]

Requires: requests, beautifulsoup4
"""

import argparse
import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

START_URL = "https://www.futurebridge.com/"
CRAWL_HOST = "www.futurebridge.com"
INTERNAL_HOSTS = {"www.futurebridge.com", "futurebridge.com"}
MAX_PAGES = 10
TIMEOUT = 15
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 "
    "FutureBridgeLinkChecker/1.0"
)

log = logging.getLogger("link_check")


@dataclass
class LinkResult:
    url: str
    status: int | None
    final_url: str | None
    error: str | None
    method: str


def normalize(url):
    return urldefrag(url)[0].strip()


def host_of(url):
    return (urlparse(url).hostname or "").lower()


def is_internal(url):
    return host_of(url) in INTERNAL_HOSTS


def is_crawlable(url):
    return host_of(url) == CRAWL_HOST


def is_homepage(url):
    return urlparse(url).path in ("", "/")


def extract_links(html, page_url):
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href or href.startswith("#"):
            continue
        link = normalize(urljoin(page_url, href))
        if urlparse(link).scheme not in ("http", "https"):
            continue
        if link:
            links.append(link)
    return links


def check_link(session, url):
    """HEAD first, then GET when HEAD fails or is rejected."""
    try:
        response = session.head(url, timeout=TIMEOUT, allow_redirects=True)
        if response.status_code < 400:
            return LinkResult(url, response.status_code, response.url, None, "HEAD")
    except requests.exceptions.RequestException:
        pass

    try:
        with session.get(url, timeout=TIMEOUT, allow_redirects=True, stream=True) as response:
            return LinkResult(url, response.status_code, response.url, None, "GET")
    except requests.exceptions.Timeout:
        return LinkResult(url, None, None, "timeout", "GET")
    except requests.exceptions.RequestException as exc:
        return LinkResult(url, None, None, type(exc).__name__, "GET")


def find_issues(result):
    issues = []
    if result.error == "timeout":
        issues.append("TIMEOUT")
    elif result.error:
        issues.append("REQUEST_ERROR")
    elif result.status is not None and result.status >= 400:
        issues.append("HTTP_ERROR")

    if (
        is_internal(result.url)
        and not is_homepage(result.url)
        and result.final_url
        and is_internal(result.final_url)
        and is_homepage(result.final_url)
    ):
        issues.append("HOMEPAGE_REDIRECT")
    return issues


def crawl(session, start_url, max_pages):
    """Return {link: source_page} for links found in the crawled pages."""
    queue = deque([normalize(start_url)])
    queued = set(queue)
    crawled = []
    links = {}

    while queue and len(crawled) < max_pages:
        page_url = queue.popleft()
        try:
            with session.get(page_url, timeout=TIMEOUT, allow_redirects=True) as response:
                status = response.status_code
                final_url = response.url
                content_type = response.headers.get("Content-Type", "").lower()
                if status >= 400:
                    log.info("PAGE ERROR %s %s (final: %s)", status, page_url, final_url)
                    crawled.append(page_url)
                    continue
                if "html" not in content_type:
                    log.info("PAGE SKIP non-HTML %s (%s)", page_url, content_type or "no content type")
                    crawled.append(page_url)
                    continue
                html = response.text
        except requests.exceptions.Timeout:
            log.info("PAGE TIMEOUT %s", page_url)
            crawled.append(page_url)
            continue
        except requests.exceptions.RequestException as exc:
            log.info("PAGE REQUEST_ERROR %s %s", type(exc).__name__, page_url)
            crawled.append(page_url)
            continue

        crawled.append(page_url)
        log.info("PAGE %s %s -> %s", status, page_url, final_url)

        for link in extract_links(html, final_url):
            links.setdefault(link, page_url)
            if (
                is_crawlable(link)
                and link not in queued
                and len(crawled) + len(queue) < max_pages
            ):
                queued.add(link)
                queue.append(link)

    log.info("Crawled %d page(s)", len(crawled))
    return links


def setup_logging(path):
    log.setLevel(logging.INFO)
    formatter = logging.Formatter("%(message)s")
    file_handler = logging.FileHandler(path, mode="w", encoding="utf-8")
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    log.addHandler(file_handler)
    log.addHandler(stream_handler)


def main():
    parser = argparse.ArgumentParser(description="Check links on www.futurebridge.com")
    parser.add_argument("--start-url", default=START_URL)
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES)
    parser.add_argument("--output", default="link_check.log")
    args = parser.parse_args()

    setup_logging(args.output)
    log.info("Link check report")
    log.info("Started: %s", datetime.now().isoformat(timespec="seconds"))
    log.info("Start URL: %s", args.start_url)
    log.info("Max pages: %d", args.max_pages)
    log.info("Timeout: %ds", TIMEOUT)
    log.info("")

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    links = crawl(session, args.start_url, args.max_pages)

    log.info("")
    log.info("Links found: %d", len(links))
    log.info("")

    counts = {
        "OK": 0,
        "HTTP_ERROR": 0,
        "TIMEOUT": 0,
        "REQUEST_ERROR": 0,
        "HOMEPAGE_REDIRECT": 0,
    }
    issues = []

    for url, source_page in sorted(links.items()):
        result = check_link(session, url)
        found = find_issues(result)
        if found:
            issues.append((result, source_page, found))
        else:
            counts["OK"] += 1

        for issue in found:
            counts[issue] += 1

        label = ",".join(found) if found else "OK"
        log.info(
            "%-16s %-4s %-4s %s%s",
            label,
            result.status if result.status is not None else "-",
            result.method,
            url,
            f" -> {result.final_url}" if result.final_url and result.final_url != url else "",
        )

    log.info("")
    log.info("Summary")
    log.info("  Links checked:      %d", len(links))
    log.info("  OK:                 %d", counts["OK"])
    log.info("  HTTP errors:        %d", counts["HTTP_ERROR"])
    log.info("  Timeouts:           %d", counts["TIMEOUT"])
    log.info("  Request errors:     %d", counts["REQUEST_ERROR"])
    log.info("  Homepage redirects: %d", counts["HOMEPAGE_REDIRECT"])
    log.info("  Total issues:       %d", len(issues))

    if issues:
        log.info("")
        log.info("Issues")
        for result, source_page, found in issues:
            log.info(
                "  [%s] %s (found on %s)%s",
                ",".join(found),
                result.url,
                source_page,
                f" -> {result.final_url}" if result.final_url and result.final_url != result.url else "",
            )

    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
