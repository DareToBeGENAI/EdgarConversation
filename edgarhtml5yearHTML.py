"""
EDGAR 10-K/20-F HTML & Exhibits Downloader (Official EDGAR APIs compliant)

Features:
- Submissions API to list recent filings per CIK
- Finds the actual 10-K/20-F HTML (avoids SGML master "complete submission")
- SGML-aware fallback: if server returns SGML, auto-extract real HTML and retry
- Downloads exhibits too (configurable) using the filing folder's index.json
- Builds a local filing_index.html with both LOCAL and SEC links to all items
- Saves the master submission .txt for reference

Notes:
- Provide a real, descriptive User-Agent with contact info per SEC guidance.
- Be gentle with rate limits (sleep + simple retries).
"""

import requests
import json
import time
import os
import re
from pathlib import Path
from typing import Dict, List, Tuple, Optional

# ----------------------------
# USER CONFIG
# ----------------------------
USER_NAME = "DB"
USER_EMAIL = "db234529@gmail.com"
USER_AGENT = f"{USER_NAME} ({USER_EMAIL})"

REQUEST_TIMEOUT = 30
SLEEP_BETWEEN_CALLS = 0.30
SLEEP_BETWEEN_COMPANIES = 1.0

# Include amended 10-Ks? (10-K/A)
INCLUDE_AMENDS = True
# Also consider foreign-issuer annuals? (20-F, 20-F/A)
INCLUDE_20F = True
# Download exhibits (all files from index.json) into the filing folder?
DOWNLOAD_EXHIBITS = True
# File extensions to download when grabbing exhibits
EXHIBIT_EXTS = (".htm", ".html", ".txt", ".pdf")  # expand if you want (e.g., .xls, .xlsx, .jpg, .gif, .xml)

# ----------------------------
# Utilities
# ----------------------------
def sec_headers_for(url: str) -> dict:
    """Build SEC-compliant headers. Do NOT set Host explicitly; requests sets it."""
    return {
        "User-Agent": USER_AGENT,
        "Accept-Encoding": "gzip, deflate",
    }

def get_json(url: str, max_retries: int = 3, backoff: float = 1.0) -> Optional[dict]:
    """GET JSON with basic retry/backoff for transient errors / rate limiting."""
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.get(url, headers=sec_headers_for(url), timeout=REQUEST_TIMEOUT)
            if r.status_code == 429 or 500 <= r.status_code < 600:
                wait = backoff * attempt
                print(f"    … retry {attempt}/{max_retries} after HTTP {r.status_code}, sleeping {wait:.1f}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException as e:
            if attempt == max_retries:
                print(f"    ✗ GET {url} failed after {max_retries} attempts: {e}")
                return None
            wait = backoff * attempt
            print(f"    … network error (attempt {attempt}/{max_retries}): {e}; sleeping {wait:.1f}s")
            time.sleep(wait)
    return None

def get_text(url: str) -> Optional[str]:
    """GET text (no retries here; caller handles)."""
    try:
        r = requests.get(url, headers=sec_headers_for(url), timeout=REQUEST_TIMEOUT)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.text
    except requests.RequestException:
        return None

# ----------------------------
# Ticker -> CIK mapping
# ----------------------------
def get_cik_from_ticker(ticker: str) -> Tuple[Optional[str], Optional[str]]:
    """Convert ticker to CIK using SEC's company_tickers.json."""
    try:
        url = "https://www.sec.gov/files/company_tickers.json"
        data = get_json(url)
        if not data:
            return None, None

        ticker_upper = ticker.upper().replace('.', '')
        for company in data.values():
            company_ticker = company["ticker"].upper().replace('.', '')
            if company_ticker == ticker_upper:
                cik = str(company["cik_str"]).zfill(10)
                company_name = company["title"]
                return cik, company_name
        return None, None
    except Exception as e:
        print(f"Error fetching CIK for {ticker}: {e}")
        return None, None

def convert_tickers_to_ciks(ticker_list: List[str], delay: float = 0.1) -> Tuple[Dict[str, dict], List[str]]:
    """Convert list of tickers to CIKs."""
    print(f"Converting {len(ticker_list)} tickers to CIK numbers...")

    url = "https://www.sec.gov/files/company_tickers.json"
    companies = get_json(url) or {}

    ticker_info = {}
    failed_tickers = []

    for ticker in ticker_list:
        ticker_search = ticker.upper().replace('.', '')
        found = False
        for company_data in companies.values():
            company_ticker = company_data['ticker'].upper().replace('.', '')
            if company_ticker == ticker_search:
                cik = str(company_data['cik_str']).zfill(10)
                company_name = company_data['title']
                ticker_info[ticker] = {'cik': cik, 'name': company_name}
                print(f"✓ {ticker} -> CIK: {cik} ({company_name})")
                found = True
                break

        if not found:
            failed_tickers.append(ticker)
            print(f"✗ {ticker}: Not found in SEC database")

        time.sleep(delay)

    print(f"\n{'=' * 60}")
    print(f"Converted: {len(ticker_info)}/{len(ticker_list)}")
    print(f"Failed: {len(failed_tickers)}")
    print(f"{'=' * 60}")

    if failed_tickers:
        print(f"\nFailed tickers: {', '.join(failed_tickers)}")

    return ticker_info, failed_tickers

# ----------------------------
# Filing selection helpers
# ----------------------------
def _index_json_items(cik_no_zeros: str, accession_nodashes: str) -> List[dict]:
    """Return list of dicts from filing folder index.json (may be empty on failure)."""
    idx_url = f"https://www.sec.gov/Archives/edgar/data/{cik_no_zeros}/{accession_nodashes}/index.json"
    data = get_json(idx_url)
    if not data:
        return []
    return data.get("directory", {}).get("item", [])

def pick_html_from_index_json(cik_no_zeros: str, accession_nodashes: str) -> Optional[str]:
    """Prefer real 10-K/20-F html from index.json; avoid exhibits and index.html."""
    items = _index_json_items(cik_no_zeros, accession_nodashes)
    htmls = [it["name"] for it in items if it.get("name","").lower().endswith((".htm",".html"))]
    if not htmls:
        return None

    def score(name: str) -> int:
        n = name.lower()
        s = 0
        if "10-k" in n or "10k" in n: s += 10
        if "20-f" in n or "20f" in n: s += 9
        if "form" in n: s += 2
        if n == "index.html": s -= 10
        if re.search(r"\bex[-_ ]?\d", n) or "exhibit" in n: s -= 5
        s += min(len(n)//10, 3)
        return s

    best = max(htmls, key=score)
    return f"https://www.sec.gov/Archives/edgar/data/{cik_no_zeros}/{accession_nodashes}/{best}"

def parse_submission_txt_for_annual_html(cik_no_zeros: str, accession_nodashes: str) -> Optional[str]:
    """Parse master submission .txt for <TYPE>10-K/20-F -> <FILENAME> real HTML."""
    txt_url = f"https://www.sec.gov/Archives/edgar/data/{cik_no_zeros}/{accession_nodashes}/{accession_nodashes}.txt"
    txt = get_text(txt_url)
    if not txt:
        return None

    doc_blocks = re.findall(r"<DOCUMENT>(.*?)</DOCUMENT>", txt, flags=re.DOTALL | re.IGNORECASE)
    for block in doc_blocks:
        mtype = re.search(r"<TYPE>\s*([^\s<]+)", block, flags=re.IGNORECASE)
        mfile = re.search(r"<FILENAME>\s*([^\s<]+)", block, flags=re.IGNORECASE)
        if not mtype or not mfile:
            continue
        ftype = mtype.group(1).upper()
        fname = mfile.group(1)
        if ftype in {"10-K", "10-K/A", "20-F", "20-F/A"} and fname.lower().endswith((".htm", ".html")):
            return f"https://www.sec.gov/Archives/edgar/data/{cik_no_zeros}/{accession_nodashes}/{fname}"
    return None

def pick_best_annual_html(cik_no_zeros: str, accession_nodashes: str, primary_doc: Optional[str]) -> Optional[str]:
    """
    1) Use primaryDocument if it looks like the annual HTML
    2) else pick from index.json (avoids index.html/exhibits)
    3) else parse master .txt for <TYPE>10-K/20-F <FILENAME>
    """
    if primary_doc:
        p = primary_doc.lower()
        if p.endswith((".htm", ".html")) and any(k in p for k in ("10-k","10k","20-f","20f")):
            return f"https://www.sec.gov/Archives/edgar/data/{cik_no_zeros}/{accession_nodashes}/{primary_doc}"

    url = pick_html_from_index_json(cik_no_zeros, accession_nodashes)
    if url and not url.endswith("/index.html"):
        return url

    return parse_submission_txt_for_annual_html(cik_no_zeros, accession_nodashes)

# ----------------------------
# Submissions API -> annual filings
# ----------------------------
def get_annual_filings_html(cik: str, count: int = 5, include_amends: bool = True, include_20f: bool = True) -> List[dict]:
    """
    Use the official Submissions API to get up to N most recent annual filings for a CIK.
    (10-K, optionally 10-K/A; optionally 20-F/20-F/A for FPIs)
    Returns list of dicts: [{'date','url','accession','form','is_amend'}]
    """
    try:
        url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        data = get_json(url)
        if not data:
            return []

        recent = data.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        accession_numbers = recent.get("accessionNumber", [])
        filing_dates = recent.get("filingDate", [])
        primary_documents = recent.get("primaryDocument", [])

        wanted_forms = {"10-K"}
        if include_amends:
            wanted_forms.add("10-K/A")
        if include_20f:
            wanted_forms.update({"20-F", "20-F/A"})

        candidates = []
        for i, form in enumerate(forms):
            if form in wanted_forms:
                candidates.append({
                    "form": form,
                    "accession": accession_numbers[i],
                    "filing_date": filing_dates[i],
                    "primary_doc": primary_documents[i],
                })

        if not candidates:
            return []

        candidates.sort(key=lambda d: d["filing_date"], reverse=True)
        candidates = candidates[:count]

        out = []
        cik_no_zeros = str(int(cik))  # strip leading zeros for URL path

        for c in candidates:
            accession_nodashes = c["accession"].replace("-", "")
            use_url = pick_best_annual_html(cik_no_zeros, accession_nodashes, c["primary_doc"])
            if use_url:
                out.append({
                    "date": c["filing_date"],
                    "url": use_url,
                    "accession": c["accession"],
                    "form": c["form"],
                    "is_amend": (c["form"].endswith("/A")),
                })

        return out

    except requests.exceptions.HTTPError as e:
        print(f"  ✗ HTTP Error for CIK {cik}: {e}")
        return []
    except Exception as e:
        print(f"  ✗ Error getting filings for CIK {cik}: {e}")
        return []

# ----------------------------
# SGML-aware HTML & Exhibit downloading
# ----------------------------
def _extract_annual_html_from_sgml(sgml_text: str) -> Optional[str]:
    """Return the annual HTML filename from SGML master submission."""
    doc_blocks = re.findall(r"<DOCUMENT>(.*?)</DOCUMENT>", sgml_text, flags=re.DOTALL | re.IGNORECASE)
    for block in doc_blocks:
        mtype = re.search(r"<TYPE>\s*([^\s<]+)", block, flags=re.IGNORECASE)
        mfile = re.search(r"<FILENAME>\s*([^\s<]+)", block, flags=re.IGNORECASE)
        if not mtype or not mfile:
            continue
        ftype = mtype.group(1).upper()
        fname = mfile.group(1)
        if ftype in {"10-K", "10-K/A", "20-F", "20-F/A"} and fname.lower().endswith((".htm", ".html")):
            return fname
    return None

def _write_clickable_index_from_items(
    cik_no_zeros: str,
    accession_nodashes: str,
    items: List[dict],
    dest_dir: Path,
    filename: str = "filing_index.html",
) -> None:
    """
    Create a local HTML index with LOCAL links (if file exists) and absolute SEC links for *all* items.
    """
    try:
        rows = []
        for it in items:
            name = it.get("name", "")
            href = f"https://www.sec.gov/Archives/edgar/data/{cik_no_zeros}/{accession_nodashes}/{name}"
            local_exists = (dest_dir / name).exists()
            rows.append((name, href, local_exists))

        html = [
            "<!doctype html><html><head><meta charset='utf-8'><title>Filing Index</title>",
            "<style>body{font-family:system-ui,Arial,sans-serif;margin:24px} table{border-collapse:collapse;width:100%} th,td{border:1px solid #ddd;padding:8px} th{background:#f7f7f7;text-align:left} .local{color:green}</style>",
            "</head><body>",
            f"<h2>EDGAR Filing Index — CIK {cik_no_zeros}, Accession {accession_nodashes}</h2>",
            "<p>Local links point to files downloaded into this folder. SEC links point to the live archive.</p>",
            "<table><thead><tr><th>File</th><th>Local</th><th>SEC Link</th></tr></thead><tbody>"
        ]
        for name, href, local_exists in rows:
            local_col = f"<a class='local' href='{name}' target='_blank'>open</a>" if local_exists else ""
            html.append(f"<tr><td>{name}</td><td>{local_col}</td><td><a href='{href}' target='_blank'>{href}</a></td></tr>")
        html += ["</tbody></table>", "</body></html>"]

        dest_dir.mkdir(parents=True, exist_ok=True)
        (dest_dir / filename).write_text("\n".join(html), encoding="utf-8")
    except Exception:
        # Non-fatal
        pass

def _save_master_submission_txt(
    cik_no_zeros: str,
    accession_nodashes: str,
    sgml_text: str,
    dest_dir: Path,
    filename: str = None,
) -> None:
    try:
        if filename is None:
            filename = f"{accession_nodashes}.txt"
        (dest_dir / filename).write_text(sgml_text, encoding="utf-8")
    except Exception:
        pass

def _download_binary(url: str, dest_path: Path) -> bool:
    """Download any file (binary-safe)."""
    try:
        r = requests.get(url, headers=sec_headers_for(url), timeout=REQUEST_TIMEOUT)
        if r.status_code == 404:
            print("    ⚠️  404 on", url)
            return False
        r.raise_for_status()
        dest_path.write_bytes(r.content)
        return True
    except requests.RequestException as e:
        print(f"    ✗ Download error for {url}: {e}")
        return False

def _download_exhibits(
    cik_no_zeros: str,
    accession_nodashes: str,
    dest_dir: Path,
    include_exts: Tuple[str, ...] = EXHIBIT_EXTS
) -> int:
    """
    Download all items from index.json that match include_exts.
    Returns count of files downloaded.
    """
    items = _index_json_items(cik_no_zeros, accession_nodashes)
    downloaded = 0
    for it in items:
        name = it.get("name", "")
        if not name:
            continue
        if not name.lower().endswith(include_exts):
            continue
        url = f"https://www.sec.gov/Archives/edgar/data/{cik_no_zeros}/{accession_nodashes}/{name}"
        dest_path = dest_dir / name
        if dest_path.exists():
            continue
        if _download_binary(url, dest_path):
            downloaded += 1
            time.sleep(SLEEP_BETWEEN_CALLS)
    # Always write/refresh a clickable index for all items
    _write_clickable_index_from_items(cik_no_zeros, accession_nodashes, items, dest_dir)
    return downloaded

def download_html(url: str, output_path: Path) -> bool:
    """
    Download HTML content from URL to output_path.
    If the response is the master SGML instead of HTML, auto-extract the real HTML,
    re-download, and write a local clickable filing_index.html with absolute links.
    """
    try:
        r = requests.get(url, headers=sec_headers_for(url), timeout=REQUEST_TIMEOUT)
        if r.status_code == 404:
            print("    ⚠️  File not found (404)")
            return False
        r.raise_for_status()

        text = r.text

        # Detect SGML master submission (relative links won't click locally)
        if ("<SEC-HEADER>" in text or "<DOCUMENT>" in text) and "<HTML" not in text[:2000].upper():
            print("    ↪ Detected SGML master submission; extracting real 10-K/20-F HTML…")

            # Parse CIK/accession from URL
            m = re.search(r"/edgar/data/(\d+)/(\d{18})/", url, flags=re.IGNORECASE)
            if not m:
                print("    ✗ Could not parse CIK/accession from URL; aborting fallback.")
                return False
            cik_no_zeros, accession_nodashes = m.group(1), m.group(2)

            # Save the original master submission text
            _save_master_submission_txt(cik_no_zeros, accession_nodashes, text, output_path.parent)

            # Build local index + (optionally) download exhibits
            if DOWNLOAD_EXHIBITS:
                print("    → Downloading exhibits from index.json…")
                count = _download_exhibits(cik_no_zeros, accession_nodashes, output_path.parent)
                print(f"    → Exhibits downloaded: {count}")
            else:
                # Even if we don't download exhibits, create a clickable index for convenience
                items = _index_json_items(cik_no_zeros, accession_nodashes)
                _write_clickable_index_from_items(cik_no_zeros, accession_nodashes, items, output_path.parent)

            # Try to fetch the real annual HTML referenced in SGML
            fname = _extract_annual_html_from_sgml(text)
            if not fname:
                print("    ✗ Could not find 10-K/20-F HTML in SGML.")
                return False

            real_html_url = f"https://www.sec.gov/Archives/edgar/data/{cik_no_zeros}/{accession_nodashes}/{fname}"
            print(f"    → Retrying with HTML: {fname}")
            r2 = requests.get(real_html_url, headers=sec_headers_for(real_html_url), timeout=REQUEST_TIMEOUT)
            if r2.status_code == 404:
                print("    ✗ HTML file 404 after SGML parse.")
                return False
            r2.raise_for_status()
            output_path.write_text(r2.text, encoding="utf-8")
            return True

        # Normal HTML path (already the real document)
        output_path.write_text(text, encoding="utf-8")
        return True

    except requests.exceptions.HTTPError as e:
        print(f"    ✗ HTTP Error: {e}")
        return False
    except Exception as e:
        print(f"    ✗ Error: {e}")
        return False

# ----------------------------
# Download orchestrator
# ----------------------------
def download_annual_html(
    ticker_info: Dict[str, dict],
    base_output_dir: str = "./fortune500_10k_html",
    count: int = 5,
    include_amends: bool = True,
    include_20f: bool = True,
):
    """Download up to N most recent annual-report HTML filings, one folder per ticker."""
    print(f"\n{'=' * 60}")
    print(f"Downloading last {count} annual-report HTML files for {len(ticker_info)} companies")
    print(f"{'=' * 60}\n")
    print(f"Base directory: {base_output_dir}\n")

    os.makedirs(base_output_dir, exist_ok=True)

    success_count = 0
    error_count = 0
    total_files = 0

    for i, (ticker, info) in enumerate(ticker_info.items(), 1):
        cik = info["cik"]
        company_name = info["name"]

        ticker_folder = Path(base_output_dir) / ticker
        ticker_folder.mkdir(parents=True, exist_ok=True)

        print(f"[{i}/{len(ticker_info)}] {ticker} ({company_name[:50]}...)")
        print(f"  CIK: {cik}")

        try:
            filings = get_annual_filings_html(
                cik,
                count=count,
                include_amends=include_amends,
                include_20f=include_20f,
            )

            if not filings:
                print(f"  ✗ No annual report filings found\n")
                error_count += 1
                time.sleep(SLEEP_BETWEEN_COMPANIES)
                continue

            print(f"  Found {len(filings)} filing(s)")

            downloaded = 0
            for filing in filings:
                date = filing["date"]
                url = filing["url"]
                accession = filing["accession"]
                form = filing.get("form", "10-K")

                # Each filing gets its own subfolder (helps separate exhibits for multiple years)
                filing_dir = ticker_folder / f"{form.replace('/', '')}_{date}_{accession}"
                filing_dir.mkdir(parents=True, exist_ok=True)

                # Annual report HTML path
                html_filename = f"{form.replace('/', '')}_{date}_{accession}.html"
                html_path = filing_dir / html_filename

                # Download annual HTML (SGML-aware)
                if download_html(url, html_path):
                    downloaded += 1

                # Optionally download exhibits (or any other files) for this filing
                if DOWNLOAD_EXHIBITS:
                    print("  • Grabbing exhibits/assets listed in index.json…")
                    cik_no_zeros = str(int(cik))
                    accession_nodashes = accession.replace("-", "")
                    count_dl = _download_exhibits(cik_no_zeros, accession_nodashes, filing_dir)
                    print(f"    … downloaded {count_dl} exhibit file(s)")

                time.sleep(SLEEP_BETWEEN_CALLS)

            if downloaded > 0:
                print(f"  ✓ Downloaded {downloaded} annual HTML file(s) for {ticker}\n")
                success_count += 1
                total_files += downloaded
            else:
                print(f"  ⚠️  No annual HTML files downloaded for {ticker}\n")
                error_count += 1

        except Exception as e:
            print(f"  ✗ Error: {e}\n")
            error_count += 1

        time.sleep(SLEEP_BETWEEN_COMPANIES)

    print(f"\n{'=' * 60}")
    print("DOWNLOAD COMPLETE")
    print(f"{'=' * 60}")
    print(f"Successful companies: {success_count}")
    print(f"Failed companies: {error_count}")
    print(f"Total annual HTML files downloaded: {total_files}")
    print(f"All files saved to: {base_output_dir}")

# ----------------------------
# Orchestrator
# ----------------------------
def process_tickers_html(
    ticker_list: List[str],
    output_dir: str = "./fortune500_10k_html",
    count: int = 5,
    include_amends: bool = True,
    include_20f: bool = True,
):
    """Complete workflow: convert tickers and download HTML annual reports via Submissions API."""
    print("=" * 60)
    print("EDGAR 10-K/20-F HTML & EXHIBITS DOWNLOADER (LAST 5 YEARS)")
    print("=" * 60)

    # Step 1: Convert tickers to CIKs
    ticker_info, failed = convert_tickers_to_ciks(ticker_list)

    # Step 2: Save mapping
    with open('ticker_cik_mapping_html.json', 'w', encoding="utf-8") as f:
        json.dump({'successful': ticker_info, 'failed': failed}, f, indent=2)
    print(f"\nMapping saved to: ticker_cik_mapping_html.json")

    # Step 3: Download HTML filings (+ exhibits if enabled)
    if ticker_info:
        print(f"\n{'=' * 60}")
        response = input(f"Download filings for {len(ticker_info)} companies? (y/n): ")
        if response.lower() == 'y':
            download_annual_html(
                ticker_info,
                base_output_dir=output_dir,
                count=count,
                include_amends=include_amends,
                include_20f=include_20f,
            )
    else:
        print("\nNo valid CIKs found. Cannot proceed with download.")

    return ticker_info, failed

# ----------------------------
# Example list (short; replace with your full list)
# remove WMT for this next batch. Re-add after initial outputs
# ----------------------------
fortune_500_tickers = [
    "AMZN", "UNH", "AAPL", "CVS", "BRK.B", "GOOGL", "XOM", "MCK", "ABC",
    "CVX", "CAH", "COST", "CI", "F", "JPM", "GM", "ELV", "BAC", "MSFT",
    "HD", "KR", "WFC", "WBA", "VZ", "META", "C", "CAT", "COR", "CNC",
    "CMCSA", "T", "DUK", "RTX", "HUM", "PEP", "TGT", "LLY", "JNJ", "LOW",
    "UNP", "UPS", "MS", "GS", "HON", "AIG", "BA", "LMT", "DIS", "MRK",
    "DE", "TJX", "IBM", "ADP", "SCHW", "USB", "MET", "PFE", "TMO", "INTC",
    "NEE", "DELL", "GE", "ABBV", "SYF", "COF", "AMD", "ABT", "SO", "DTE",
    "PNC", "BMY", "PM", "ORCL", "MDT", "UHS", "FDX", "MO", "PLD", "TRV",
    "ALL", "SPG", "AEP", "XEL", "D", "ED", "EXC", "SRE", "PCG", "WM",
    "DHR", "CHTR", "NKE", "COP", "PSX", "VLO", "MPC", "EOG", "SLB", "HAL",
    "OXY", "DVN", "HES", "APA", "MRO", "AMGN", "GILD", "BIIB", "REGN", "VRTX",
    "HCA", "THC", "CYH", "SYK", "BSX", "BDX", "BAX", "ZBH", "ISRG", "EW",
    "IDXX", "A", "MTD", "WAT", "PKI", "TFX", "HOLX", "GEV", "EMR", "ETN",
    "PH", "ROK", "AME", "FTV", "ITW", "DOV", "IR", "XYL", "IEX", "ROP",
    "VMC", "MLM", "NUE", "FERG", "SWK", "FAST", "WCC", "GWW", "AOS", "BLDR",
    "FND", "WSM", "BBY", "DG", "DLTR", "ROST", "GPS", "ANF", "AEO", "URBN",
    "M", "KSS", "JWN", "DDS", "CRI", "RL", "PVH", "LEVI", "SKX", "FL",
    "DKS", "ULTA", "EL", "CLX", "CHD", "SPB", "PG", "KMB", "CL", "GIS",
    "K", "CPB", "CAG", "HRL", "TSN", "SJM", "MKC", "HSY", "MDLZ", "KHC",
    "KO", "MNST", "KDP", "TAP", "STZ", "BF.B", "DEO", "SAM", "FIZZ", "CELH",
    "SYY", "USFD", "PFGC", "UNFI", "SFM", "WMK", "IMKTA", "GO", "BJ", "EBAY",
    "ETSY", "W", "CHWY", "DASH", "ABNB", "BKNG", "EXPE", "TRIP", "LYV", "MAR",
    "HLT", "H", "WH", "IHG", "CHH", "VAC", "TNL", "PLYA", "RHP", "PK",
    "SHO", "RLJ", "APLE", "AAL", "DAL", "UAL", "LUV", "ALK", "JBLU", "SAVE",
    "HA", "SKYW", "ALGT", "MESA", "R", "CHRW", "EXPD", "XPO", "JBHT", "KNX",
    "ODFL", "SAIA", "ARCB", "WERN", "HTLD", "TFII", "LSTR", "MATX", "KEX", "NSC",
    "CSX", "CP", "TRN", "KVUE", "UL", "NWL", "HAS", "MAT", "JAKK", "FUN",
    "SIX", "TKO", "PARA", "WBD", "NFLX", "SPOT", "RBLX", "EA", "TTWO", "DKNG",
    "PENN", "MGM", "WYNN", "LVS", "CZR", "BYD", "RRR", "CHDN", "BALY", "EVRI",
    "IGT", "MLCO", "RSI", "CRM", "ADBE", "NOW", "INTU", "WDAY", "PANW", "CRWD",
    "ZS", "FTNT", "CYBR", "OKTA", "NET", "DDOG", "MDB", "SNOW", "PLTR", "COIN",
    "SQ", "PYPL", "V", "MA", "AXP", "DFS", "ALLY", "CACC", "WRLD", "LPRO",
    "ENVA", "UPST", "LC", "AFRM", "SOFI", "NU", "BLK", "TROW", "IVZ", "BEN",
    "APAM", "AMG", "VIRT", "MCO", "SPGI", "MSCI", "ICE", "CME", "NDAQ", "CBOE",
    "MKTX", "TW", "EVRG", "AEE", "CMS", "ATO", "WEC", "ES", "FE", "ETR",
    "PPL", "PEG", "AWK", "SJW", "CWT", "WTRG", "MSEX", "YORW", "ARTNA", "GWRS",
    "WTS", "VMI", "UFPI", "LPX", "BCC", "WY", "PCH", "RYN", "CTT", "TGNA",
    "NXST", "SIRI", "GTN", "SSP", "SBGI", "LEE", "GCI", "NYT", "MNI", "NWS",
    "DJT", "SATS", "DISH", "LBRDA", "LBTYA", "AMC", "CABO", "ATUS", "LILAK", "TMUS",
    "GRMN", "USM", "VOD", "TEF", "AMX", "VIV", "TU", "BCE", "LUMN", "FYBR",
    "CNSL", "SHEN", "OTTR", "AWR", "SJI", "NWE", "AVA", "MGEE", "POR", "HE",
    "NWN", "SR", "BKH", "NFG", "NJR", "OGS", "CPK", "UTL", "GNE", "ACIW",
    "JKHY", "FIS", "FISV", "GPN", "WU", "FOUR", "PAYO", "NCNO", "TOST", "AYX",
    "ESTC", "CFLT", "GTLB", "S", "RBRK", "CRDO", "CVLT", "VRNS", "QLYS", "TENB",
    "RPD", "RELY", "GDDY", "EQIX", "DLR", "AMT", "CCI", "SBAC", "UNIT", "CIEN",
    "ANET", "JNPR", "FFIV", "CSCO", "HPE", "NTAP", "PSTG", "WDC", "STX", "NVDA",
    "AVGO", "QCOM", "TXN"
]

# ----------------------------
# Main
# ----------------------------
if __name__ == "__main__":
    print("\n" + "#" * 60)
    print("10-K/20-F HTML & EXHIBITS DOWNLOADER - LAST 5 YEARS PER COMPANY")
    print("#" * 60 + "\n")

    # Toggle exhibits globally here if you like:
    # DOWNLOAD_EXHIBITS = True

    ticker_info, failed = process_tickers_html(
        fortune_500_tickers,
        output_dir='./fortune500_10k_html',
        count=5,
        include_amends=INCLUDE_AMENDS,
        include_20f=INCLUDE_20F,
    )

    print(f"\n{'=' * 60}")
    print("FINAL SUMMARY")
    print(f"{'=' * 60}")
    print(f"Successfully processed: {len(ticker_info)}")
    print(f"Failed (likely not in database/foreign symbol variants): {len(failed)}")
    if failed:
        print("\nFailed tickers:")
        for t in failed:
            print(f"  - {t}")
