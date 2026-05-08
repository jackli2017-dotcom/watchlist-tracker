### 3.3 Add the job tracker script (30 minutes with Codex)

#!/usr/bin/env python3
"""
Daily job tracker for private-company watchlist.
Pulls open roles from Greenhouse, Lever, Ashby, and Paylocity boards.
Detects new roles (appends them with Status=NEW) and removed roles (marks
them in place with Removed=YES + Removed Date), then writes a daily
headcount snapshot to the Hiring Trend tab.
"""
import os
import re
import json
import requests
import gspread
from datetime import datetime, timezone
from gspread.utils import rowcol_to_a1
from google.oauth2.service_account import Credentials
pip install beautifulsoup4

# Edit this list with your 5 companies.
# board must be one of: "greenhouse", "lever", "ashby", "paylocity"
# For paylocity, slug = the GUID-like PortalId in the careers URL, e.g.
#   https://recruiting.paylocity.com/Recruiting/Jobs/All/{PORTAL_ID}/Cloud-Bees-Inc
# Find it by visiting the company's careers page and copying from the URL.
COMPANIES = [
    {"name": "Harness",        "board": "greenhouse", "slug": "harnessinc"},
    {"name": "CircleCI",       "board": "greenhouse", "slug": "circleci"},
    {"name": "CloudBees",      "board": "paylocity",  "slug": "a432d829-f701-4cf3-9108-56ef703b2ac5"},
    {"name": "Buildkite",      "board": "greenhouse", "slug": "buildkite"},
    {"name": "Octopus Deploy", "board": "greenhouse", "slug": "octopusdeploy"},
]

SHEET_ID = "1OaNh7Tq7MM8JP6S7qIXBC5toNd3k79WR9DM9tk2x1b0"


def fetch_greenhouse(slug):
    r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", timeout=30)
    r.raise_for_status()
    return [
        {
            "title": j.get("title", ""),
            "location": (j.get("location") or {}).get("name", ""),
            "department": ", ".join(d.get("name", "") for d in j.get("departments", []) or []),
            "url": j.get("absolute_url", ""),
            "external_id": str(j.get("id", "")),
        }
        for j in r.json().get("jobs", [])
    ]


def fetch_lever(slug):
    r = requests.get(f"https://api.lever.co/v0/postings/{slug}?mode=json", timeout=30)
    r.raise_for_status()
    return [
        {
            "title": j.get("text", ""),
            "location": (j.get("categories") or {}).get("location", ""),
            "department": (j.get("categories") or {}).get("team", ""),
            "url": j.get("hostedUrl", ""),
            "external_id": j.get("id", ""),
        }
        for j in r.json()
    ]


def fetch_ashby(slug):
    r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", timeout=30)
    r.raise_for_status()
    return [
        {
            "title": j.get("title", ""),
            "location": j.get("locationName", ""),
            "department": j.get("departmentName", ""),
            "url": j.get("jobUrl", ""),
            "external_id": j.get("id", ""),
        }
        for j in r.json().get("jobs", [])
    ]


def fetch_paylocity(portal_id):
    """
    Scrape Paylocity careers (used by CloudBees and others).

    portal_id is the GUID-like value from the Paylocity URL after /All/, e.g.
      https://recruiting.paylocity.com/Recruiting/Jobs/All/{PORTAL_ID}/Cloud-Bees-Inc

    Tries Paylocity's JSON search endpoint first; falls back to HTML scraping.
    If neither path returns jobs, the specific Paylocity portal version may
    differ. Open the careers page in Chrome DevTools (Network tab), reload,
    and look for an XHR returning JSON — that's the real endpoint to use.
    Hand the actual URL to Codex and ask it to adapt this function.
    """
    headers = {"User-Agent": "Mozilla/5.0 (compatible; watchlist-tracker)"}

    # Path 1 — JSON search endpoint
    try:
        json_url = (
            "https://recruiting.paylocity.com/Recruiting/Jobs/GetSearchData"
            f"?CategoryId=&LocationId=&Term=&Skip=0&Take=200&PortalId={portal_id}"
        )
        r = requests.get(json_url, headers=headers, timeout=30)
        if r.ok and "json" in r.headers.get("content-type", ""):
            data = r.json()
            raw_jobs = data.get("Jobs") or data.get("Results") or data.get("Data") or []
            if raw_jobs:
                return [_normalize_paylocity_job(j) for j in raw_jobs]
    except Exception as e:
        print(f"  Paylocity JSON endpoint failed: {e}; falling back to HTML")

    # Path 2 — HTML fallback
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        raise RuntimeError(
            "Paylocity HTML fallback requires beautifulsoup4. "
            "Add it to your GitHub Actions pip install line."
        )

    html_url = f"https://recruiting.paylocity.com/Recruiting/Jobs/All/{portal_id}"
    r = requests.get(html_url, headers=headers, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    jobs, seen = [], set()
    for link in soup.find_all("a", href=re.compile(r"/Apply/\d+/")):
        href = link.get("href", "")
        m = re.search(r"/Apply/(\d+)/", href)
        if not m:
            continue
        job_id = m.group(1)
        if job_id in seen:
            continue
        seen.add(job_id)
        full_url = href if href.startswith("http") else f"https://recruiting.paylocity.com{href}"
        title = link.get_text(strip=True)
        if not title:
            continue
        dept = ""
        parent = link.find_parent()
        if parent:
            for sib in parent.find_all(["span", "div"]):
                txt = sib.get_text(strip=True)
                if txt and txt != title and len(txt) < 60:
                    dept = txt
                    break
        jobs.append({
            "title": title,
            "location": "",
            "department": dept,
            "url": full_url,
            "external_id": job_id,
        })
    return jobs


def _normalize_paylocity_job(j):
    """Normalize a Paylocity JSON job record (field names vary by version)."""
    job_id = str(j.get("JobId") or j.get("Id") or j.get("RequisitionId") or "")
    return {
        "title": j.get("JobTitle") or j.get("Title") or j.get("Name") or "",
        "location": j.get("Location") or j.get("LocationName") or "",
        "department": j.get("Department") or j.get("Category") or "",
        "url": (j.get("Url") or j.get("ApplyUrl")
                or f"https://recruiting.paylocity.com/Recruiting/Jobs/Details/{job_id}"),
        "external_id": job_id,
    }


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever":      fetch_lever,
    "ashby":      fetch_ashby,
    "paylocity":  fetch_paylocity,
}


def main():
    creds = Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    wb = gc.open_by_key(SHEET_ID)
    jobs_tab = wb.worksheet("Jobs")
    trend_tab = wb.worksheet("Hiring Trend")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Read existing Jobs rows: needed both for de-dup AND removal detection.
    existing = jobs_tab.get_all_records()
    headers = jobs_tab.row_values(1)

    # Map "Company|External ID" -> (1-based sheet row index, row dict)
    by_key = {}
    for i, r in enumerate(existing, start=2):  # row 1 = header, data starts at row 2
        key = f"{r.get('Company', '')}|{r.get('External ID', '')}"
        by_key[key] = (i, r)

    new_rows = []
    counts = {}
    successfully_fetched = set()
    current_keys = set()

    for c in COMPANIES:
        try:
            jobs = FETCHERS[c["board"]](c["slug"])
            successfully_fetched.add(c["name"])
            counts[c["name"]] = len(jobs)
            print(f"{c['name']}: {len(jobs)} open roles")
            for j in jobs:
                key = f"{c['name']}|{j['external_id']}"
                current_keys.add(key)
                if key not in by_key:
                    # New role; Removed defaults to FALSE so it renders as a
                    # boolean/checkbox in Sheets and matches the convention used
                    # by the Apps Script's Distill careers fallback.
                    new_rows.append([
                        today, c["name"], j["title"], j["department"],
                        j["location"], j["url"], j["external_id"], "NEW",
                        "FALSE", ""
                    ])
        except Exception as e:
            print(f"Error fetching {c['name']}: {e}")
            counts[c["name"]] = ""

    if new_rows:
        jobs_tab.append_rows(new_rows, value_input_option="USER_ENTERED")
        print(f"Added {len(new_rows)} new roles")

    # Mark roles previously listed but absent in this run.
    # Only for companies we successfully fetched — never false-mark on fetch errors.
    try:
        removed_col = headers.index("Removed") + 1            # 1-based for sheets
        removed_date_col = headers.index("Removed Date") + 1
    except ValueError:
        print("Note: 'Removed' / 'Removed Date' columns missing — skipping removal tracking.")
        removed_col = None

    if removed_col is not None:
        cell_updates = []
        removed_count = 0
        for key, (row_idx, row) in by_key.items():
            company = key.split("|", 1)[0]
            if company not in successfully_fetched:
                continue
            if key in current_keys:
                continue                                      # still listed
            # Treat any of TRUE / YES / 1 as already-marked; FALSE / NO / blank
            # are all treated as "not yet marked" (active row, eligible to mark).
            removed_val = str(row.get("Removed", "")).strip().upper()
            if removed_val in ("TRUE", "YES", "1"):
                continue
            cell_updates.append({"range": rowcol_to_a1(row_idx, removed_col),
                                 "values": [["TRUE"]]})
            cell_updates.append({"range": rowcol_to_a1(row_idx, removed_date_col),
                                 "values": [[today]]})
            removed_count += 1

        if cell_updates:
            # value_input_option='USER_ENTERED' makes Sheets parse 'TRUE' as a
            # boolean and the date string as a real date, not raw text.
            jobs_tab.batch_update(cell_updates, value_input_option='USER_ENTERED')
            print(f"Marked {removed_count} roles as Removed")

    # Daily headcount snapshot for the Hiring Trend chart
    trend_row = [today] + [counts.get(c["name"], "") for c in COMPANIES]
    trend_tab.append_row(trend_row, value_input_option="USER_ENTERED")


if __name__ == "__main__":
    main()

