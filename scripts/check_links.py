#!/usr/bin/env python3
"""Check links found on a website.

Crawls at most 10 pages of the site at --start-url, then checks every
discovered link with a HEAD request, falling back to GET. Flags HTTP 4xx/5xx
responses, timeouts, request failures, and internal links that redirect to the
homepage. The start URL's domain and all of its subdomains count as internal.

Social media links (linkedin.com, facebook.com, twitter.com, x.com,
instagram.com, youtube.com) are reported as SKIPPED_SOCIAL and never as errors.
WordPress login/admin/logout links are ignored.

Results are written to link_check.log. Exits with status 1 when issues are found.

Usage:
    python scripts/check_links.py [--start-url URL] [--max-pages N] [--output FILE]

Requires: requests, beautifulsoup4
"""

import argparse
import logging
import os
import sys
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import parse_qs, urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

START_URL = "https://www.futurebridge.com/"
SOCIAL_DOMAINS = {
    "linkedin.com",
    "facebook.com",
    "twitter.com",
    "x.com",
    "instagram.com",
    "youtube.com",
}
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


def base_domain(url):
    host = host_of(url)
    return host[4:] if host.startswith("www.") else host


def is_internal(url, domain):
    host = host_of(url)
    return bool(domain) and (host == domain or host.endswith("." + domain))


def is_crawlable(url, crawl_hosts):
    return host_of(url) in crawl_hosts


def is_homepage(url):
    return urlparse(url).path in ("", "/")


def is_social(url):
    host = host_of(url)
    return any(host == domain or host.endswith("." + domain) for domain in SOCIAL_DOMAINS)


def is_ignored(url):
    parsed = urlparse(url)
    path = parsed.path.lower()
    if path.startswith("/wp-login.php") or path == "/wp-admin" or path.startswith("/wp-admin/"):
        return True
    return "logout" in parse_qs(parsed.query).get("action", [])


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
        text = " ".join(anchor.get_text(" ", strip=True).split())
        if link:
            links.append((link, text))
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


def find_issues(result, domain):
    issues = []
    if result.error == "timeout":
        issues.append("TIMEOUT")
    elif result.error:
        issues.append("REQUEST_ERROR")
    elif result.status is not None and result.status >= 400:
        issues.append("HTTP_ERROR")

    if (
        is_internal(result.url, domain)
        and not is_homepage(result.url)
        and result.final_url
        and is_internal(result.final_url, domain)
        and is_homepage(result.final_url)
    ):
        issues.append("HOMEPAGE_REDIRECT")
    return issues


def severity_of(url, domain):
    return "High" if is_internal(url, domain) else "Low"


def crawl(session, start_url, max_pages, domain):
    """Return ({link: [(page, text), ...]}, ignored_count) for crawled pages."""
    queue = deque([normalize(start_url)])
    queued = set(queue)
    crawl_hosts = {host_of(start_url), domain, f"www.{domain}"}
    crawled = []
    links = {}
    ignored = set()

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
        if is_internal(final_url, domain):
            crawl_hosts.add(host_of(final_url))

        for link, text in extract_links(html, final_url):
            if is_ignored(link):
                ignored.add(link)
                continue
            occurrences = links.setdefault(link, [])
            if (page_url, text) not in occurrences:
                occurrences.append((page_url, text))
            if (
                is_crawlable(link, crawl_hosts)
                and link not in queued
                and len(crawled) + len(queue) < max_pages
            ):
                queued.add(link)
                queue.append(link)

    log.info("Crawled %d page(s)", len(crawled))
    return links, len(ignored)


def stdout_is(path):
    """True when stdout is already redirected to the log file."""
    try:
        out = os.fstat(sys.stdout.fileno())
        target = os.stat(path)
    except (OSError, ValueError):
        return False
    return (out.st_dev, out.st_ino) == (target.st_dev, target.st_ino)


def setup_logging(path):
    log.setLevel(logging.INFO)
    formatter = logging.Formatter("%(message)s")
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    log.addHandler(stream_handler)

    if not stdout_is(path):
        file_handler = logging.FileHandler(path, mode="w", encoding="utf-8")
        file_handler.setFormatter(formatter)
        log.addHandler(file_handler)


def md_cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def status_text(result, found):
    if result.error:
        base = result.error
    elif result.final_url and result.final_url != result.url:
        base = f"{result.status} -> {result.final_url}"
    else:
        base = str(result.status)
    return f"{base} ({','.join(found)})"


def main():
    parser = argparse.ArgumentParser(description="Check links on a website")
    parser.add_argument("--start-url", default=START_URL)
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES)
    parser.add_argument("--output", default="link_check.log")
    args = parser.parse_args()
    domain = base_domain(args.start_url)

    setup_logging(args.output)
    log.info("Link check report")
    log.info("Started: %s", datetime.now().isoformat(timespec="seconds"))
    log.info("Start URL: %s", args.start_url)
    log.info("Internal domain: %s", domain)
    log.info("Max pages: %d", args.max_pages)
    log.info("Timeout: %ds", TIMEOUT)
    log.info("")

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    links, ignored = crawl(session, args.start_url, args.max_pages, domain)

    log.info("")
    log.info("Links found: %d (ignored wp-login/wp-admin/logout: %d)", len(links), ignored)
    log.info("")

    counts = {
        "OK": 0,
        "HTTP_ERROR": 0,
        "TIMEOUT": 0,
        "REQUEST_ERROR": 0,
        "HOMEPAGE_REDIRECT": 0,
        "SKIPPED_SOCIAL": 0,
    }
    issues = []

    for url in sorted(links):
        page, text = links[url][0]

        if is_social(url):
            counts["SKIPPED_SOCIAL"] += 1
            log.info("%-16s %-4s %-4s %s", "SKIPPED_SOCIAL", "-", "-", url)
            continue

        result = check_link(session, url)
        found = find_issues(result, domain)
        if found:
            issues.append((severity_of(url, domain), result, text, page, found))
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
    log.info("  Links checked:      %d", len(links) - counts["SKIPPED_SOCIAL"])
    log.info("  OK:                 %d", counts["OK"])
    log.info("  HTTP errors:        %d", counts["HTTP_ERROR"])
    log.info("  Timeouts:           %d", counts["TIMEOUT"])
    log.info("  Request errors:     %d", counts["REQUEST_ERROR"])
    log.info("  Homepage redirects: %d", counts["HOMEPAGE_REDIRECT"])
    log.info("  Skipped social:     %d", counts["SKIPPED_SOCIAL"])
    log.info("  Ignored wp links:   %d", ignored)
    log.info("  Total issues:       %d", len(issues))

    if issues:
        log.info("")
        log.info("Issues")
        log.info("| Severity | Link text | Broken URL | Status / final URL | Found on page |")
        log.info("| --- | --- | --- | --- | --- |")
        for severity, result, text, page, found in sorted(
            issues, key=lambda item: (item[0] != "High", item[1].url)
        ):
            log.info(
                "| %s | %s | %s | %s | %s |",
                severity,
                md_cell(text or "(no text)"),
                md_cell(result.url),
                md_cell(status_text(result, found)),
                md_cell(page),
            )

    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
