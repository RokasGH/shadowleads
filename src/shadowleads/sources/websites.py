"""Company / VAT codes self-declared on a business's own website (linking fallback + validation).

Lithuanian law requires online service providers to publish their legal name and company code, and
GDPR privacy notices name the data controller - so the homepage, contacts, requisites and privacy
pages usually carry "Įmonės kodas 304227393" and/or "PVM kodas LT100010155711".

Politeness: robots.txt is honoured, at most MAX_PAGES pages per site, one request at a time per host,
short timeouts and no retries. Social networks and booking platforms are skipped (their codes belong
to the platform, not the business).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from selectolax.parser import HTMLParser

from shadowleads.log import get_logger

log = get_logger(__name__)

MAX_PAGES = 4
SKIP_HOSTS = (
    "facebook.", "instagram.", "treatwell.", "trea.tw", "trw.page.link", "fresha.", "booksy.",
    "linktr.ee", "wolt.", "bolt.", "tiktok.", "youtube.", "google.", "goo.gl", "grozionamai.",
    "salonized.", "rekvizitai.", "nevaziuoja.", "imones.lt", "manoremontas.", "wixsite.",
    "business.site", "g.page",
)  # fmt: skip
LINK_HINTS = (
    "kontakt", "contact", "rekvizit", "apie", "about", "privat", "privacy", "taisykl", "terms",
    "duomen", "slapuk", "cookie", "salyg", "sąlyg", "imone", "įmonė", "company",
)  # fmt: skip

_COMPANY_RE = re.compile(
    r"(?:\bį\s?\.\s?k\s?\.|\bįm\s?\.\s?k(?:odas)?\.?|\bįmonės\s+kodas|\bimones\s+kodas|"
    r"\bjuridinio\s+asmens\s+kodas|\bj\.\s?a\.\s?kodas|\bcompany\s+(?:code|reg(?:istration)?\.?\s*(?:no|number|code))|"
    r"\bregistration\s+(?:no|number|code)|\breg\.\s*(?:no|nr)|\bkodas)"
    r"\s*[:.\-–]?\s*(\d{9})\b",
    re.IGNORECASE,
)
_VAT_RE = re.compile(r"\bLT\s?(\d{9}(?:\d{3})?)\b")


@dataclass(slots=True)
class FoundCode:
    host: str
    page_url: str
    code_type: str  # company | vat
    code: str
    context: str


def extract_codes(text: str) -> list[tuple[str, str, str]]:
    """Return (code_type, code, context) triples found in page text."""
    flat = re.sub(r"\s+", " ", text)
    out: list[tuple[str, str, str]] = []
    for m in _COMPANY_RE.finditer(flat):
        out.append(("company", m.group(1), flat[max(0, m.start() - 60) : m.end() + 20]))
    for m in _VAT_RE.finditer(flat):
        out.append(("vat", m.group(1), flat[max(0, m.start() - 60) : m.end() + 20]))
    return out


def skip_url(url: str | None) -> bool:
    if not url:
        return True
    host = urlsplit(url).netloc.lower()
    return not host or any(s in host for s in SKIP_HOSTS)


class SiteScanner:
    def __init__(self, user_agent: str, raw_dir: Path):
        self.ua = user_agent
        self.raw_dir = raw_dir
        self.client = httpx.Client(
            headers={"User-Agent": user_agent},
            timeout=httpx.Timeout(12.0, connect=6.0),
            follow_redirects=True,
            verify=False,  # many small-business sites have broken TLS chains; we only read text
        )

    def _robots(self, base: str) -> RobotFileParser:
        rp = RobotFileParser()
        try:
            resp = self.client.get(urljoin(base, "/robots.txt"))
            rp.parse(resp.text.splitlines() if resp.status_code == 200 else [])
        except httpx.HTTPError:
            rp.parse([])
        return rp

    def _get(self, url: str) -> str | None:
        try:
            resp = self.client.get(url)
        except httpx.HTTPError:
            return None
        if resp.status_code != 200 or "html" not in resp.headers.get("content-type", ""):
            return None
        return resp.text

    def scan(self, url: str) -> list[FoundCode]:
        parts = urlsplit(url)
        base = f"{parts.scheme or 'https'}://{parts.netloc}"
        host = parts.netloc.lower()
        robots = self._robots(base)
        queue, seen, found = [url], set(), []
        while queue and len(seen) < MAX_PAGES:
            page = queue.pop(0)
            if page in seen or not robots.can_fetch(self.ua, page):
                seen.add(page)
                continue
            seen.add(page)
            html = self._get(page)
            if not html:
                continue
            self._save(host, page, html)
            tree = HTMLParser(html)
            text = tree.body.text(separator=" ") if tree.body else ""
            found += [FoundCode(host, page, t, c, ctx) for t, c, ctx in extract_codes(text)]
            if len(seen) == 1:  # discover requisites/contacts/privacy links from the landing page
                for a in tree.css("a[href]"):
                    href = a.attributes.get("href") or ""
                    label = f"{href} {a.text(strip=True)}".lower()
                    target = urljoin(page, href).split("#")[0]
                    same_site = urlsplit(target).netloc.lower() == host
                    hinted = any(h in label for h in LINK_HINTS)
                    if same_site and hinted and target not in queue and target not in seen:
                        queue.append(target)
        return found

    def _save(self, host: str, page: str, html: str) -> None:
        digest = hashlib.sha256(page.encode()).hexdigest()[:16]
        path = self.raw_dir / host / f"{digest}.html.gz"
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt", encoding="utf-8") as f:
            f.write(html)


def scan_sites(
    places: list[tuple[str, str]], user_agent: str, raw_dir: Path, out_path: Path, workers: int = 12
) -> int:
    """Scan every distinct site once; write one JSON line per (place, code)."""
    by_site: dict[str, list[str]] = {}
    for place_id, url in places:
        if skip_url(url):
            continue
        root = f"{urlsplit(url).scheme}://{urlsplit(url).netloc.lower()}"
        by_site.setdefault(root, []).append(place_id)
    urls = {
        root: next(u for p, u in places if u and u.lower().startswith(root)) for root in by_site
    }
    scanner = SiteScanner(user_agent, raw_dir)
    fetched_at = datetime.now(UTC).isoformat()

    def work(root: str) -> tuple[str, list[FoundCode]]:
        try:
            return root, scanner.scan(urls[root])
        except Exception as exc:  # one broken site must not stop the run
            log.warning("website.failed", site=root, error=str(exc)[:120])
            return root, []

    rows = 0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=workers) as pool, out_path.open("w", encoding="utf-8") as f:
        for i, (root, codes) in enumerate(pool.map(work, by_site)):
            unique = {(c.code_type, c.code): c for c in codes}.values()
            for place_id in by_site[root]:
                f.write(
                    json.dumps(
                        {
                            "place_id": place_id,
                            "site": root,
                            "scanned": True,
                            "fetched_at": fetched_at,
                        }
                    )
                    + "\n"
                )
                for c in unique:
                    f.write(
                        json.dumps(
                            {"place_id": place_id, "fetched_at": fetched_at, **asdict(c)},
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                    rows += 1
            if i % 100 == 0:
                log.info("website.progress", sites_done=i, sites=len(by_site), codes=rows)
    return rows
