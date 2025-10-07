"""
Website Staleness Audit — Streamlit App (Drop‑in)
Author: ChatGPT for Ray (Lutine Management)
Date: 2025-10-06

This drop‑in replaces your current file and adds:
- Host normalization (treats apex and www as the same; locks onto the final redirected host)
- Sticky results (filters don’t clear the table)
- Polite crawling controls: max concurrency, per‑request delay, jitter
- Backoff on 429/503 with a single retry
- Quote cleanups for the "Stale?" column

Tip: ensure `requirements.txt` includes: streamlit, httpx, beautifulsoup4, dateparser, pandas, lxml, urllib3, xlsxwriter, pyyaml
"""
from __future__ import annotations

import asyncio
import argparse
import io
import os
import re
import time
import random
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Optional, List, Dict, Set, Tuple
from urllib.parse import urljoin, urlparse
import xml.etree.ElementTree as ET

import httpx
import pandas as pd
import streamlit as st
from bs4 import BeautifulSoup
import dateparser
import urllib.robotparser as robotparser
import yaml
import random
# =====================
# Config / Constants
# =====================

STALENESS_DEFAULT_DAYS = 365
DEFAULT_CONCURRENCY = 6
HTTP_TIMEOUT = 20
USER_AGENT = "Lutine-StalenessAudit/1.3 (+https://lutinemanagement.com)"
REPORT_ROOT = Path("reports")

# Normalize host (treat apex and www as same)
def _norm_host(host: str) -> str:
    return (host or "").lower().split(":")[0].lstrip("www.")

@dataclass
class PageRecord:
    url: str
    status: int
    title: str
    content_date: Optional[datetime]
    date_source: str
    last_modified: Optional[datetime]
    sitemap_lastmod: Optional[datetime]
    discovered_via: str
    reason: str
    word_count: int
    bytes: int
    crawl_depth: int

    def stale_flag(self, stale_days: int) -> Optional[bool]:
        if self.content_date is None:
            return None
        return self.content_date < datetime.now(timezone.utc) - timedelta(days=stale_days)

# =====================
# Date helpers
# =====================

_MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
DATE_PATTERNS = [
    re.compile(r"\b(20\d{2}|19\d{2})-(0?[1-9]|1[0-2])-(0?[1-9]|[12]\d|3[01])\b"),
    re.compile(rf"\b(0?[1-9]|1[0-2])/(0?[1-9]|[12]\d|3[01])/(20\d{{2}}|19\d{{2}})\b"),
    re.compile(rf"\b(0?[1-9]|[12]\d|3[01])/(0?[1-9]|1[0-2])/(20\d{{2}}|19\d{{2}})\b"),
    re.compile(rf"\b({_MONTHS})\.?\s+(0?[1-9]|[12]\d|3[01]),?\s+(20\d{{2}}|19\d{{2}})\b", re.I),
]
META_DATE_ATTRS = [
    ("meta", {"property": "article:published_time"}),
    ("meta", {"property": "og:updated_time"}),
    ("meta", {"name": "pubdate"}),
    ("meta", {"name": "publish-date"}),
    ("meta", {"name": "date"}),
    ("meta", {"itemprop": "datePublished"}),
]
DATE_CLASS_HINTS = ["date", "published", "updated", "time", "post-date", "entry-date"]


def parse_http_date(value: str) -> Optional[datetime]:
    try:
        dt = dateparser.parse(value)
        if dt is None:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def extract_date_from_html(html: str, soup: BeautifulSoup) -> Tuple[Optional[datetime], str, str]:
    # 1) <time datetime>
    for t in soup.find_all("time"):
        dt_attr = t.get("datetime") or t.get("content")
        if dt_attr:
            dt = parse_http_date(dt_attr)
            if dt:
                return dt, "<time datetime>", dt_attr
    # 2) meta tags
    for tag, attrs in META_DATE_ATTRS:
        el = soup.find(tag, attrs=attrs)
        if el:
            val = el.get("content") or el.get("value")
            dt = parse_http_date(val) if val else None
            if dt:
                k = next(iter(attrs.items()))
                return dt, f"meta[{k[0]}={k[1]}]", val or ""
    # 3) elements with date-ish classes
    for cls_hint in DATE_CLASS_HINTS:
        el = soup.find(attrs={"class": re.compile(cls_hint, re.I)})
        if el and el.text:
            dt = parse_http_date(el.text.strip())
            if dt:
                return dt, f"class~={cls_hint}", el.text.strip()[:80]
    # 4) regex scan on visible text
    text = soup.get_text(" ", strip=True)[:50000]
    for pat in DATE_PATTERNS:
        m = pat.search(text)
        if m:
            dt = parse_http_date(m.group(0))
            if dt:
                return dt, "regex", m.group(0)
    return None, "", ""

# =====================
# Robots & Sitemaps
# =====================

def load_robots(base: str) -> robotparser.RobotFileParser:
    rp = robotparser.RobotFileParser()
    try:
        rp.set_url(urljoin(base, "/robots.txt"))
        rp.read()
    except Exception:
        pass
    return rp


def find_sitemaps(base: str) -> List[str]:
    candidates = [urljoin(base, "/sitemap.xml"), urljoin(base, "/sitemap_index.xml")]
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT}, follow_redirects=True) as client:
            r = client.get(urljoin(base, "/robots.txt"))
            if r.status_code == 200:
                for line in r.text.splitlines():
                    if line.lower().startswith("sitemap:"):
                        candidates.append(line.split(":", 1)[1].strip())
    except Exception:
        pass
    # unique
    out, seen = [], set()
    for c in candidates:
        if c not in seen:
            out.append(c); seen.add(c)
    return out


def parse_robots_delay_ms(base: str) -> int:
    """Parse Crawl-delay or Request-rate from robots.txt if present (best‑effort)."""
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT}, follow_redirects=True) as client:
            r = client.get(urljoin(base, "/robots.txt"))
            if r.status_code != 200:
                return 0
            delay_ms = 0
            for raw in r.text.splitlines():
                line = raw.strip().lower()
                if line.startswith("crawl-delay"):
                    # crawl-delay: seconds
                    try:
                        sec = float(re.split(r"[:\s]+", line, maxsplit=1)[1])
                        delay_ms = max(delay_ms, int(sec * 1000))
                    except Exception:
                        pass
                elif line.startswith("request-rate"):
                    # request-rate: N/seconds
                    try:
                        part = re.split(r"[:\s]+", line, maxsplit=1)[1]
                        nums = re.findall(r"(\d+)/(\d+)", part)
                        if nums:
                            n, s = map(int, nums[0])
                            if n > 0:
                                per_req_ms = int((s / n) * 1000)
                                delay_ms = max(delay_ms, per_req_ms)
                    except Exception:
                        pass
            return delay_ms
    except Exception:
        return 0


def parse_sitemap(url: str) -> List[Tuple[str, Optional[datetime]]]:
    pages: List[Tuple[str, Optional[datetime]]] = []
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT}, follow_redirects=True) as client:
            r = client.get(url)
            if r.status_code != 200:
                return pages
            root = ET.fromstring(r.content)
            ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
            if root.tag.endswith("sitemapindex"):
                for sm_el in root.findall("sm:sitemap", ns):
                    loc_el = sm_el.find("sm:loc", ns)
                    if loc_el is not None and loc_el.text:
                        pages.extend(parse_sitemap(loc_el.text.strip())[:500])
            else:
                for url_el in root.findall("sm:url", ns):
                    loc_el = url_el.find("sm:loc", ns)
                    lastmod_el = url_el.find("sm:lastmod", ns)
                    if loc_el is not None and loc_el.text:
                        loc = loc_el.text.strip()
                        lm = parse_http_date(lastmod_el.text.strip()) if lastmod_el is not None and lastmod_el.text else None
                        pages.append((loc, lm))
    except Exception:
        pass
    return pages

# =====================
# Crawler
# =====================

class Crawler:
    def __init__(self, base_url: str, max_pages: int = 200, max_depth: int = 4, include_paths: List[str] | None = None, exclude_paths: List[str] | None = None, respect_robots: bool = True, concurrency: int = DEFAULT_CONCURRENCY, polite_delay_ms: int = 250, jitter_ms: int = 200):
        self.base = self._normalize_base(base_url)
        self.domain = urlparse(self.base).netloc
        self.host_norm = _norm_host(self.domain)
        self.max_pages = max_pages
        self.max_depth = max_depth
        self.include_paths = include_paths or []
        self.exclude_paths = exclude_paths or []
        self.respect_robots = respect_robots
        self.concurrency = max(1, int(concurrency))
        self.polite_delay_ms = max(0, int(polite_delay_ms))
        self.jitter_ms = max(0, int(jitter_ms))

        self.robots = load_robots(self.base)
        self.robots_delay_ms = parse_robots_delay_ms(self.base) if respect_robots else 0

        self.seen: Set[str] = set()
        self.retry_count: Dict[str, int] = {}
        self.queue: asyncio.Queue[Tuple[str,int,str,Optional[datetime]]] = asyncio.Queue()
        self.sitemap_dates: Dict[str, datetime] = {}

    def _normalize_base(self, url: str) -> str:
        u = url.strip()
        if not u.startswith("http"):
            u = "https://" + u
        p = urlparse(u)
        return f"{p.scheme}://{p.netloc}"

    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        try:
            return self.robots.can_fetch(USER_AGENT, url)
        except Exception:
            return True

    def in_scope(self, url: str) -> bool:
        p = urlparse(url)
        if _norm_host(p.netloc) != self.host_norm:
            return False
        path = p.path or "/"
        if self.include_paths and not any(path.startswith(ip) for ip in self.include_paths):
            return False
        if self.exclude_paths and any(path.startswith(ep) for ep in self.exclude_paths):
            return False
        if re.search(r"\.(pdf|jpg|jpeg|png|gif|webp|svg|zip|rar|7z|mp[34]|wav|docx?|xlsx?|pptx?)$", path, re.I):
            return False
        return True

    async def bootstrap(self, use_sitemap: bool = True):
        if use_sitemap:
            for sm in find_sitemaps(self.base):
                for loc, lm in parse_sitemap(sm)[: self.max_pages * 2]:
                    if self.in_scope(loc) and self.allowed(loc):
                        await self.queue.put((loc, 0, "sitemap", lm))
                        if lm:
                            self.sitemap_dates[loc] = lm
        await self.queue.put((self.base, 0, "seed", None))

async def crawl(self) -> List[PageRecord]:
    results: List[PageRecord] = []
    sem = asyncio.Semaphore(self.concurrency)
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}

    async def worker():
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers=headers, follow_redirects=True) as client:
            while True:
                try:
                    url, depth, source, lm_hint = await asyncio.wait_for(self.queue.get(), timeout=2.0)
                except asyncio.TimeoutError:
                    # Nothing ready; if we've hit the cap or the queue is empty, exit.
                    if len(results) >= self.max_pages or self.queue.empty():
                        break
                    continue

                # If we've hit the cap, mark done and keep draining the queue.
                if len(results) >= self.max_pages:
                    self.queue.task_done()
                    continue

                # Skip duplicates early
                if url in self.seen:
                    self.queue.task_done()
                    continue

                # Scope/robots checks
                if depth > self.max_depth or not self.in_scope(url) or not self.allowed(url):
                    self.queue.task_done()
                    continue

                async with sem:
                    # Polite delay + jitter (respect robots delay if larger)
                    base_delay = max(self.polite_delay_ms, self.robots_delay_ms)
                    if base_delay or self.jitter_ms:
                        j = random.uniform(-self.jitter_ms, self.jitter_ms)
                        await asyncio.sleep(max(0, (base_delay + j)) / 1000.0)

                    try:
                        r = await client.get(url)
                    except Exception:
                        results.append(PageRecord(
                            url, 0, "", None, "", None, lm_hint, source, "fetch_error", 0, 0, depth
                        ))
                        self.queue.task_done()
                        continue

                    # If the seed redirected (e.g., apex -> www), lock onto final host
                    if depth == 0 and source == "seed":
                        try:
                            canon = _norm_host(urlparse(str(r.url)).netloc)
                            if canon and canon != self.host_norm:
                                self.host_norm = canon
                        except Exception:
                            pass

                    # Backoff and retry once on 429/503
                    if r.status_code in (429, 503) and self.retry_count.get(url, 0) < 1:
                        self.retry_count[url] = self.retry_count.get(url, 0) + 1
                        await asyncio.sleep(random.uniform(2.0, 5.0))
                        await self.queue.put((url, depth, source, lm_hint))
                        self.queue.task_done()
                        continue

                    # Mark as seen after a non-429/503 attempt
                    self.seen.add(url)

                    # Extract dates/metrics
                    last_mod = parse_http_date(r.headers.get("Last-Modified")) if r.headers.get("Last-Modified") else None
                    html = r.text if r.headers.get("Content-Type", "").lower().startswith("text/html") else ""
                    title, wc, best_date, date_src = "", 0, None, ""
                    if html:
                        soup = BeautifulSoup(html, "lxml")
                        ttag = soup.find("title")
                        title = (ttag.text.strip() if ttag and ttag.text else "")
                        best_date, date_src, _ = extract_date_from_html(html, soup)
                        wc = len(soup.get_text(" ").split())

                    sm_date = self.sitemap_dates.get(url, lm_hint)
                    candidates = [
                        (best_date, "content:" + (date_src or "")),
                        (last_mod, "Last-Modified"),
                        (sm_date, "sitemap:lastmod"),
                    ]
                    dated = [c for c in candidates if c[0]]
                    chosen_date, chosen_src = (max(dated, key=lambda x: x[0]) if dated else (None, ""))

                    results.append(PageRecord(
                        url, r.status_code, title, chosen_date, chosen_src,
                        last_mod, sm_date, source, "", wc, len(r.content), depth
                    ))

                    # Enqueue links
                    if html and r.status_code == 200 and depth < self.max_depth:
                        soup = BeautifulSoup(html, "lxml")
                        base_for_join = str(r.url) if hasattr(r, "url") else url
                        for a in soup.find_all("a", href=True):
                            nxt = urljoin(base_for_join, a.get("href").strip())
                            if nxt not in self.seen and self.in_scope(nxt) and self.allowed(nxt):
                                await self.queue.put((nxt, depth + 1, "crawl", None))

                    self.queue.task_done()

    workers = [asyncio.create_task(worker()) for _ in range(self.concurrency)]
    await self.queue.join()
    for w in workers:
        w.cancel()
    return results



# =====================
# Email utils (Office365 SMTP)
# =====================

def load_smtp_from_secrets() -> dict:
    try:
        s = st.secrets["smtp"]
        return {
            "host": s.get("host"),
            "port": int(s.get("port", 587)),
            "user": s.get("user"),
            "password": s.get("password"),
            "from_addr": s.get("from_addr"),
            "from_name": s.get("from_name", "Lutine Website Auditor"),
        }
    except Exception:
        # Fallback to env vars if not running in Streamlit context
        return {
            "host": os.getenv("SMTP_HOST"),
            "port": int(os.getenv("SMTP_PORT", "587")),
            "user": os.getenv("SMTP_USER"),
            "password": os.getenv("SMTP_PASSWORD"),
            "from_addr": os.getenv("SMTP_FROM_ADDR"),
            "from_name": os.getenv("SMTP_FROM_NAME", "Lutine Website Auditor"),
        }


def send_email_with_attachments(to_addrs: List[str], subject: str, body: str, attachments: List[Tuple[str, bytes, str]]):
    cfg = load_smtp_from_secrets()
    msg = EmailMessage()
    msg["From"] = f"{cfg['from_name']} <{cfg['from_addr']}>"
    msg["To"] = ", ".join(to_addrs)
    msg["Subject"] = subject
    msg.set_content(body)
    for filename, data, mime in attachments:
        maintype, subtype = mime.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)

    context = ssl.create_default_context()
    with smtplib.SMTP(cfg["host"], cfg["port"]) as server:
        server.starttls(context=context)
        server.login(cfg["user"], cfg["password"])
        server.send_message(msg)

# =====================
# Reporting helpers
# =====================

def build_dataframe(records: List[PageRecord], stale_days: int) -> pd.DataFrame:
    rows = []
    now = datetime.now(timezone.utc)
    for r in records:
        age_days = int((now - r.content_date).days) if r.content_date else None
        stale_str = "Yes" if r.stale_flag(stale_days) is True else ("No" if r.stale_flag(stale_days) is False else "Unknown")
        rows.append({
            "URL": r.url,
            "Status": r.status,
            "Title": r.title,
            "Best Date": r.content_date.isoformat() if r.content_date else "",
            "Age (days)": age_days,
            "Date Source": r.date_source,
            "Last-Modified": r.last_modified.isoformat() if r.last_modified else "",
            "Sitemap lastmod": r.sitemap_lastmod.isoformat() if r.sitemap_lastmod else "",
            "Discovered Via": r.discovered_via,
            "Depth": r.crawl_depth,
            "Word Count": r.word_count,
            "Bytes": r.bytes,
            "Stale?": stale_str,
        })
    return pd.DataFrame(rows)


def to_excel_bytes(df: pd.DataFrame, summary: Dict[str, int | str]) -> bytes:
    bio = io.BytesIO()
    with pd.ExcelWriter(bio, engine="xlsxwriter") as writer:
        df.to_excel(writer, index=False, sheet_name="Audit")
        pd.DataFrame({"Metric": list(summary.keys()), "Value": list(summary.values())}).to_excel(writer, index=False, sheet_name="Summary")
    return bio.getvalue()

# =====================
# Batch runner (for cron/GitHub Actions)
# =====================

def run_batch(clients_cfg: dict, max_pages=250, max_depth=4, use_sitemap=True, respect_robots=True):
    date_folder = REPORT_ROOT / datetime.now().strftime("%Y-%m") / datetime.now().strftime("%d")
    date_folder.mkdir(parents=True, exist_ok=True)

    for c in clients_cfg.get("clients", []):
        name = c["name"]
        url = c["url"]
        stale_days = int(c.get("stale_days", STALENESS_DEFAULT_DAYS))
        include_paths = c.get("include_paths")
        exclude_paths = c.get("exclude_paths")
        staff_emails = c.get("staff_emails", [])

        crawler = Crawler(url, max_pages=max_pages, max_depth=max_depth, include_paths=include_paths, exclude_paths=exclude_paths, respect_robots=respect_robots)
        asyncio.run(crawler.bootstrap(use_sitemap=use_sitemap))
        records = asyncio.run(crawler.crawl())
        df = build_dataframe(records, stale_days)

        total = len(df)
        stale_count = int(df[df["Stale?"] == "Yes"].shape[0])
        undated = int(df[df["Stale?"] == "Unknown"].shape[0])
        avg_age = int(df["Age (days)"].dropna().mean()) if not df["Age (days)"].dropna().empty else 0

        csv_bytes = df.to_csv(index=False).encode("utf-8")
        xlsx_bytes = to_excel_bytes(df, {
            "Client": name,
            "Pages scanned": total,
            "Stale pages": stale_count,
            "Undated pages": undated,
            "Avg age (days)": avg_age,
            "Threshold (days)": stale_days,
        })

        # save to disk
        base = f"{name.replace(' ', '_')}_staleness_audit"
        (date_folder / f"{base}.csv").write_bytes(csv_bytes)
        (date_folder / f"{base}.xlsx").write_bytes(xlsx_bytes)

        # email
        if staff_emails:
            subject = f"{name}: Website Staleness Audit"
            body = (
                f"Automated audit for {name} ({url})\n\n"
                f"Pages scanned: {total}\nStale pages: {stale_count}\nUndated pages: {undated}\n"
                f"Avg age (days): {avg_age}\nThreshold: {stale_days} days\n\n"
                f"CSV and Excel reports attached."
            )
            send_email_with_attachments(
                to_addrs=staff_emails,
                subject=subject,
                body=body,
                attachments=[
                    (f"{base}.csv", csv_bytes, "text/csv"),
                    (f"{base}.xlsx", xlsx_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
                ],
            )

# =====================
# Streamlit UI
# =====================

st.set_page_config(page_title="Website Staleness Audit", layout="wide")
st.title("🔎 Website Staleness Audit")
st.caption("Scan one site on‑demand or run a batch across clients and email staff.")

mode = st.radio("Mode", ["Single URL (on‑demand)", "Batch (clients.yaml)"], horizontal=True)

with st.sidebar:
    st.header("Crawl Settings")
    max_pages = st.number_input("Max pages", 10, 5000, 300, step=10)
    max_depth = st.slider("Max depth", 1, 10, 6)
    max_conc = st.slider("Max concurrency", 1, 16, DEFAULT_CONCURRENCY)
    polite_delay_ms = st.slider("Polite delay (ms)", 0, 2000, 250)
    jitter_ms = st.slider("Jitter (± ms)", 0, 1000, 200)
    use_sitemap = st.checkbox("Use sitemap for bootstrap", True)
    respect_robots = st.checkbox("Respect robots.txt", True)

if mode == "Single URL (on‑demand)":
    base_url = st.text_input("Site base URL (https://…)")
    stale_days = st.number_input("Flag older than (days)", 30, 2000, STALENESS_DEFAULT_DAYS, step=30)
    include_paths = st.text_input("Include paths (comma‑sep, optional)", "")
    exclude_paths = st.text_input("Exclude paths (comma‑sep, optional)", "/wp-json,/feed")
    start_btn = st.button("🚀 Run Audit")

    if start_btn:
        if not base_url:
            st.error("Enter a base URL."); st.stop()
        inc = [p.strip() for p in include_paths.split(",") if p.strip()] or None
        exc = [p.strip() for p in exclude_paths.split(",") if p.strip()] or None

        crawler = Crawler(base_url, max_pages=int(max_pages), max_depth=int(max_depth), include_paths=inc, exclude_paths=exc, respect_robots=respect_robots, concurrency=int(max_conc), polite_delay_ms=int(polite_delay_ms), jitter_ms=int(jitter_ms))
        with st.status("Crawling…", expanded=True) as status:
            asyncio.run(crawler.bootstrap(use_sitemap=use_sitemap))
            status.update(label=f"Seeded {crawler.queue.qsize()} URLs")
            t0 = time.time(); records = asyncio.run(crawler.crawl()); dur = time.time() - t0
            status.update(label=f"Crawl complete in {dur:.1f}s. {len(records)} pages fetched.", state="complete")

        df = build_dataframe(records, int(stale_days))
        st.session_state["last_df"] = df  # persist across reruns
        st.session_state["last_summary"] = {
            "total": len(df),
            "stale": int(df[df["Stale?"] == "Yes"].shape[0]),
            "undated": int(df[df["Stale?"] == "Unknown"].shape[0]),
            "avg_age": int(df["Age (days)"].dropna().mean()) if not df["Age (days)"].dropna().empty else 0,
            "stale_days": int(stale_days),
        }

# Show results if we have them (sticky UI)
if mode == "Single URL (on‑demand)" and "last_df" in st.session_state:
    df = st.session_state["last_df"]
    summary = st.session_state.get("last_summary", {})

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Pages scanned", summary.get("total", len(df)))
    c2.metric("Stale pages", summary.get("stale", 0))
    c3.metric("Undated pages", summary.get("undated", 0))
    c4.metric("Avg age (days)", summary.get("avg_age", 0))

    st.subheader("Results")
    show_only = st.radio("Show", ["All", "Stale", "Unknown (no date found)", "Fresh (< threshold)"], index=0, horizontal=True, key="show_filter")
    view = df
    if show_only == "Stale":
        view = df[df["Stale?"] == "Yes"]
    elif show_only.startswith("Unknown"):
        view = df[df["Stale?"] == "Unknown"]
    elif show_only.startswith("Fresh"):
        view = df[df["Stale?"] == "No"]
    st.dataframe(view, use_container_width=True, hide_index=True)

    st.subheader("Download report")
    csv_bytes = df.to_csv(index=False).encode("utf-8")
    st.download_button("Download CSV", data=csv_bytes, file_name="staleness_audit.csv", mime="text/csv")
    try:
        xlsx_bytes = to_excel_bytes(df, {
            "Pages scanned": summary.get("total", len(df)),
            "Stale pages": summary.get("stale", 0),
            "Undated pages": summary.get("undated", 0),
            "Avg age (days)": summary.get("avg_age", 0),
            "Threshold (days)": summary.get("stale_days", STALENESS_DEFAULT_DAYS),
        })
        st.download_button("Download Excel (XLSX)", data=xlsx_bytes, file_name="staleness_audit.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    except Exception:
        st.info("Excel export unavailable (xlsxwriter not installed).")

elif mode == "Single URL (on‑demand)":
    st.info("Run an audit to see results.")

# ===== Batch UI (inherits polite crawling under the hood) =====
if mode == "Batch (clients.yaml)":
    st.write("Upload or paste your **clients.yaml**. You can also store it in the repo and run the monthly batch via cron/GitHub Actions.")
    up = st.file_uploader("clients.yaml", type=["yaml", "yml"])
    default_yaml = """
clients:
  - name: Example Org
    url: https://www.example.org
    staff_emails: ["webmaster@example.org"]
    stale_days: 365
    include_paths: ["/"]
    exclude_paths: ["/wp-json", "/feed"]
"""
    yaml_text = st.text_area("Or paste YAML here", value=default_yaml, height=220)
    do_email = st.checkbox("Email reports to staff (uses smtp secrets)", True)
    run_btn = st.button("📦 Run Batch Now")

    if run_btn:
        try:
            cfg = yaml.safe_load(up.getvalue().decode("utf-8")) if up else yaml.safe_load(yaml_text)
            if not cfg or "clients" not in cfg:
                st.error("Invalid YAML: must contain a top-level 'clients' list."); st.stop()
        except Exception as e:
            st.error(f"Error parsing YAML: {e}"); st.stop()

        # Run batch
        date_folder = REPORT_ROOT / datetime.now().strftime("%Y-%m") / datetime.now().strftime("%d")
        date_folder.mkdir(parents=True, exist_ok=True)

        for c in cfg["clients"]:
            name = c["name"]; url = c["url"]; stale_days = int(c.get("stale_days", STALENESS_DEFAULT_DAYS))
            include_paths = c.get("include_paths"); exclude_paths = c.get("exclude_paths")
            staff_emails = c.get("staff_emails", [])

            with st.status(f"Crawling {name}…", expanded=False):
                crawler = Crawler(url, max_pages=int(max_pages), max_depth=int(max_depth), include_paths=include_paths, exclude_paths=exclude_paths, respect_robots=respect_robots, concurrency=int(max_conc), polite_delay_ms=int(polite_delay_ms), jitter_ms=int(jitter_ms))
                asyncio.run(crawler.bootstrap(use_sitemap=use_sitemap))
                records = asyncio.run(crawler.crawl())
                df = build_dataframe(records, stale_days)

            total = len(df)
            stale_count = int(df[df["Stale?"] == "Yes"].shape[0])
            undated = int(df[df["Stale?"] == "Unknown"].shape[0])
            avg_age = int(df["Age (days)"].dropna().mean()) if not df["Age (days)"].dropna().empty else 0

            csv_bytes = df.to_csv(index=False).encode("utf-8")
            xlsx_bytes = to_excel_bytes(df, {
                "Client": name,
                "Pages scanned": total,
                "Stale pages": stale_count,
                "Undated pages": undated,
                "Avg age (days)": avg_age,
                "Threshold (days)": stale_days,
            })
            base = f"{name.replace(' ', '_')}_staleness_audit"
            (date_folder / f"{base}.csv").write_bytes(csv_bytes)
            (date_folder / f"{base}.xlsx").write_bytes(xlsx_bytes)

            if do_email and staff_emails:
                try:
                    send_email_with_attachments(
                        to_addrs=staff_emails,
                        subject=f"{name}: Website Staleness Audit",
                        body=(
                            f"Automated audit for {name} ({url})\n\n"
                            f"Pages scanned: {total}\nStale pages: {stale_count}\nUndated pages: {undated}\n"
                            f"Avg age (days): {avg_age}\nThreshold: {stale_days} days\n\n"
                            f"CSV and Excel reports attached."
                        ),
                        attachments=[
                            (f"{base}.csv", csv_bytes, "text/csv"),
                            (f"{base}.xlsx", xlsx_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
                        ],
                    )
                    st.success(f"Emailed report to: {', '.join(staff_emails)}")
                except Exception as e:
                    st.warning(f"Email failed for {name}: {e}")

# =====================
# CLI entrypoint for schedulers (python file.py --batch --clients clients.yaml)
# =====================

def _cli():
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", action="store_true", help="Run batch across clients and email reports")
    parser.add_argument("--clients", type=str, default="clients.yaml", help="Path to clients YAML")
    parser.add_argument("--max_pages", type=int, default=250)
    parser.add_argument("--max_depth", type=int, default=4)
    parser.add_argument("--no_sitemap", action="store_true")
    parser.add_argument("--ignore_robots", action="store_true")
    args = parser.parse_args()

    if args.batch:
        cfg = yaml.safe_load(Path(args.clients).read_text())
        run_batch(cfg, max_pages=args.max_pages, max_depth=args.max_depth, use_sitemap=not args.no_sitemap, respect_robots=not args.ignore_robots)

if __name__ == "__main__":
    _cli()

# =====================
# GitHub Actions workflow (optional)
# Save as .github/workflows/staleness_monthly.yml
# name: Website Staleness — Monthly
# on:
#   schedule:
#     - cron: '0 11 1 * *'
#   workflow_dispatch: {}
# jobs:
#   run:
#     runs-on: ubuntu-latest
#     steps:
#       - uses: actions/checkout@v4
#       - uses: actions/setup-python@v5
#         with: { python-version: '3.11' }
#       - run: pip install httpx beautifulsoup4 dateparser pandas lxml urllib3 xlsxwriter pyyaml
#       - name: Run batch
#         env:
#           SMTP_HOST: smtp.office365.com
#           SMTP_PORT: 587
#           SMTP_USER: ${{ secrets.SMTP_USER }}
#           SMTP_PASSWORD: ${{ secrets.SMTP_PASSWORD }}
#           SMTP_FROM_ADDR: ${{ secrets.SMTP_FROM_ADDR }}
#           SMTP_FROM_NAME: "Lutine Website Auditor"
#         run: |
#           python site_staleness_audit_app.py --batch --clients clients.yaml --max_pages 300 --max_depth 6
#       - name: Upload reports artifact
#         uses: actions/upload-artifact@v4
#         with:
#           name: website-staleness-reports
#           path: reports/

