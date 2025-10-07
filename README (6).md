# Website Staleness Audit — User Guide (v1)

_Last updated: Oct 7, 2025_

## 1) What this tool does
A lightweight Streamlit app that crawls client websites, estimates the **freshness date** of each HTML page, and flags pages older than a threshold (e.g., **365 days**). It can run on a **single URL on‑demand** or **batch multiple clients** from a YAML file, and exports **CSV/XLSX** reports (optionally emailing them to staff).

---
## 2) Installation & Launch
**Prereqs:** Python 3.10+ recommended (3.11 OK), internet access from the machine running the app.

1. Clone your repo (contains `site_staleness_audit_app.py`).
2. Install deps (use a venv if you like):
   ```bash
   pip install -r requirements.txt
   # or, individually:
   pip install streamlit httpx beautifulsoup4 dateparser pandas lxml urllib3 xlsxwriter pyyaml
   ```
3. Run the app:
   ```bash
   streamlit run site_staleness_audit_app.py
   ```

_Optional for email_: set Streamlit secrets (`.streamlit/secrets.toml`) or environment variables (see §8.3).

---
## 3) Modes
### A. Single URL (on‑demand)
- Enter a base URL (e.g., `https://www.example.org`).
- Choose controls (see §4) and click **Run Audit**.
- Results appear below and **stay sticky** while you toggle filters.

### B. Batch (clients.yaml)
- Upload or paste a YAML block that lists client sites.
- Optionally enable **Email reports** and provide staff recipients per client.
- Click **Run Batch Now** to process each site.

---
## 4) Crawl Controls (Sidebar)
- **Max pages**: Upper cap of pages to fetch per site.
- **Max depth**: Link depth from the seed/homepage (or from sitemap URLs).
- **Max concurrency**: Number of parallel requests. _Higher is faster but heavier._
- **Polite delay (ms)**: Baseline wait **per request** for niceness.
- **Jitter (± ms)**: Random wiggle around the delay to avoid “robotic” rhythms.
- **Use sitemap for bootstrap**: Pre-seed URLs from `sitemap.xml`/index if present.
- **Respect robots.txt**: Honor `Disallow` and **crawl-delay**/request-rate hints.

**Single‑URL page controls:**
- **Flag older than (days)**: Staleness threshold (default **365**).
- **Include paths**: Comma‑separated path prefixes to focus crawl (e.g., `/news,/blog`).
- **Exclude paths**: Comma‑separated path prefixes to skip (e.g., `/wp-json,/feed`).

> **Rule of thumb presets**
> - _Normal audit:_ Concurrency **4–6**, Delay **250–500 ms**, Jitter **200–400 ms**, robots **ON**.
> - _Cautious/fragile servers:_ Concurrency **2–3**, Delay **500–1000 ms**.

---
## 5) How the app picks a page’s “Best Date”
For each HTML page, we gather candidate dates in this order and then choose the **most recent**:
1) `<time datetime="…">` attribute
2) Common meta tags (e.g., `article:published_time`, `og:updated_time`, `itemprop=datePublished`, etc.)
3) Elements with date‑ish classes/ids (`date`, `published`, `updated`, …)
4) Regex match on visible text (YYYY‑MM‑DD, MM/DD/YYYY, “Oct 4, 2024”, …)
5) HTTP `Last-Modified` header
6) Sitemap `<lastmod>` (if available)

If no date is found, **Stale? = Unknown**.

---
## 6) Report Columns
- **URL** — The page URL.
- **Status** — HTTP result code:

| Code(s) | Meaning |
|---|---|
| **200** | OK. Fetched and parsed. Counts toward staleness. |
| **204** | No content (rare). |
| **301/302/307/308** | Redirects. The app follows redirects; the **final** code is usually shown. If you still see 3xx, it indicates unusual redirect behavior. |
| **401/403** | Unauthorized/Forbidden. Requires auth or blocked. |
| **404** | Not found. Broken or removed. |
| **410** | Gone. Permanently removed. |
| **429** | Rate limited. The app backs off and retries once. |
| **500/502/503/504** | Server error or timeout. Single retry for 503. |
| **0** | Fetch error (DNS/TLS/network/timeout). See **reason**. |

- **Title** — `<title>` text.
- **Best Date** — Chosen date in ISO format.
- **Age (days)** — Now minus Best Date.
- **Date Source** — Which candidate won (e.g., `content:<time datetime>`, `Last‑Modified`, `sitemap:lastmod`).
- **Last‑Modified** — Server header if present.
- **Sitemap lastmod** — From sitemap for that URL.
- **Discovered Via** — `seed` (homepage), `sitemap`, or `crawl` (link traversal).
- **Depth** — Link distance from seed (or sitemap entry, then traversal from it).
- **Word Count** — Rough content size.
- **Bytes** — Response size.
- **Stale?** — `Yes`, `No`, or `Unknown` (no date).

**Triage tips**
- Sort or filter **Status ≠ 200** to spot broken/blocked pages.
- Filter **Unknown** to find evergreen pages where a date isn’t present (candidates for exclusions later).
- Many **401/403** → content behind login; consider authenticated mode (future feature).
- Many **429/503** → lower **Concurrency** or raise **Delay/Jitter** and rerun.

---
## 7) Using Results
1. **Summary tiles** show counts and average age.
2. **Show filter**: toggle between All / Stale / Unknown / Fresh.<br>
3. **Export**: download **CSV** or **XLSX** (includes a Summary sheet).
4. **Batch**: per-client files saved into `reports/YYYY-MM/DD/` and optionally emailed.

---
## 8) Batch Mode & Email
### 8.1 Example `clients.yaml`
```yaml
clients:
  - name: SCAAP
    url: https://www.scaap.org/
    stale_days: 365
    include_paths: ["/"]
    exclude_paths: ["/wp-json", "/feed"]
    staff_emails: ["editor@scaap.org"]

  - name: CT-AAP
    url: https://ct-aap.org/
    stale_days: 365
    include_paths: ["/"]
    exclude_paths: ["/wp-json", "/feed"]
    staff_emails: []
```

### 8.2 Running batch from the UI
- Upload the YAML or paste into the text area.
- Toggle **Email reports to staff** if you want emails sent.
- Click **Run Batch Now**.

### 8.3 SMTP configuration (two options)
**Streamlit secrets** (`.streamlit/secrets.toml`):
```toml
[smtp]
host = "smtp.office365.com"
port = 587
user = "YOUR_USER"
password = "YOUR_APP_PASSWORD"
from_addr = "reports@yourdomain.org"
from_name = "Lutine Website Auditor"
```
**Environment variables:**
```bash
export SMTP_HOST=smtp.office365.com
export SMTP_PORT=587
export SMTP_USER=YOUR_USER
export SMTP_PASSWORD=YOUR_APP_PASSWORD
export SMTP_FROM_ADDR=reports@yourdomain.org
export SMTP_FROM_NAME="Lutine Website Auditor"
```

### 8.4 CLI batch (for cron, containers)
```bash
python site_staleness_audit_app.py --batch --clients clients.yaml --max_pages 300 --max_depth 6
```
Reports land in `reports/YYYY-MM/DD/` and emails are sent if `staff_emails` are set.

### 8.5 GitHub Actions (monthly)
Create `.github/workflows/staleness_monthly.yml` and use the snippet in the source file comments (preconfigured for Python and SMTP secrets).

---
## 9) Polite Crawling & Robots
- **Respect robots.txt** keeps you within site owner guidance and honors `Crawl-delay`/`Request-rate` when present.
- **Polite delay + jitter** reduces load and the chance of throttling.
- If you manage the site and need deeper coverage, prefer **allow‑listing your crawler** in robots for specific paths rather than turning robots off globally.
- Turning robots off **does not** grant access to login‑only content.

**Suggested excludes** for noise on WordPress:
- `/search`, `?s=`, `/tag/`, `/category/`, `/wp-admin/`

---
## 10) Troubleshooting
- **Stuck after seeding many URLs**: Fixed in this build. If it reappears, increase **Max pages** or disable **Use sitemap** to seed fewer.
- **Many 429/503**: Lower **Concurrency** or raise **Polite delay/Jitter**; rerun.
- **Status 0 / `fetch_error`**: Network/SSL/timeouts—try again with more delay or exclude the path.
- **401/403**: Private area; authenticated mode required (future feature).
- **ModuleNotFoundError: yaml**: `pip install pyyaml` (included in requirements).
- **Excel export missing**: `pip install xlsxwriter` (also in requirements).

---
## 11) FAQ
**Q: Does turning off robots increase coverage?**  
Sometimes, but it often pulls in noisy/duplicate indexes. Safer: keep robots **ON**, add allow‑rules for your bot, or expand the sitemap.

**Q: Can it crawl login‑only pages?**  
Not yet. Plan: session cookies / headers, or CMS/API export for authenticated content.

**Q: How should I handle evergreen pages (About, Contact)?**  
Use the report’s **Unknown** filter to identify them, and (coming soon) an uploadable **exclusion list** per site. For now, keep them in mind during staff review.

**Q: What is a good initial config?**  
Max pages **300–800**, Max depth **5–6**, Concurrency **4–6**, Delay **250–500 ms**, Jitter **200–400 ms**, robots **ON**, sitemap **ON**.

---
## 12) Data & Ethics
The auditor fetches HTML only, skips heavy binaries, and aims to be a good citizen: modest concurrency, delay/jitter, robots adherence, and backoff on 429/503.

---
## 13) Release Notes (current build)
- Queue drain fix when **Max pages < seeded** (no hangs)
- Triple‑quoted email bodies (no stray‑quote SyntaxError)
- Host normalization (`www` ↔ apex) with final‑host lock
- Sticky results across filter toggles
- Polite crawling controls + 429/503 backoff/one retry
