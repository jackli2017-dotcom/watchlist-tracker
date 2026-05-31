#!/usr/bin/env python3
"""
Daily job tracker for private-company watchlist.
Pulls open roles from Greenhouse, Lever, Ashby, and Paylocity boards.
Detects new roles (appends them with Status=NEW) and removed roles (marks
them in place with Removed=TRUE + Removed Date), then writes a daily
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

# Edit this list with your 5 companies.
# board must be one of: "greenhouse", "lever", "ashby", "paylocity"
# For paylocity, slug is the FULL path after /All/ in the careers URL,
# i.e. both the GUID PortalId AND the company slug joined by /, e.g.
#   https://recruiting.paylocity.com/Recruiting/Jobs/All/{PORTAL_ID}/Cloud-Bees-Inc
# → slug = "{PORTAL_ID}/Cloud-Bees-Inc"
COMPANIES = [
    {"name": "Harness",        "board": "greenhouse", "slug": "harnessinc"},
    {"name": "CircleCI",       "board": "greenhouse", "slug": "circleci"},
    {"name": "CloudBees",      "board": "paylocity",  "slug": "a432d829-f701-4cf3-9108-56ef703b2ac5/Cloud-Bees-Inc"},
    {"name": "Buildkite",      "board": "greenhouse", "slug": "buildkite"},
    {"name": "Octopus Deploy", "board": "greenhouse", "slug": "octopusdeploy"},
]

# Use the spreadsheet ID (the long string between /d/ and /edit in the sheet URL)
# rather than the name. open_by_key needs only the Sheets scope; open(name) would
# need the Drive scope too.
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


def fetch_paylocity(slug):
    """
    Scrape Paylocity careers (CloudBees, others).

    `slug` is the FULL path after /All/ — both the PortalId GUID and the
    company slug joined by /, e.g.:
      "a432d829-f701-4cf3-9108-56ef703b2ac5/Cloud-Bees-Inc"

    Paylocity embeds the full job list as a JSON blob inside the page HTML
    (search the page source for `"Jobs":[`). Some portals only embed it
    when the company suffix is in the URL and the request looks like a
    real browser, so we send Chrome-like headers below.

    If the jobs array isn't found, the log prints diagnostics (status code,
    response length, first 200 chars) so you can see whether you got a
    redirect, block page, or a different embedded-data pattern.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/147.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    url = f"https://recruiting.paylocity.com/Recruiting/Jobs/All/{slug}"
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    html = r.text

    idx = html.find('"Jobs":[')
    if idx < 0:
        idx = html.find('"Jobs": [')
    if idx < 0:
        print(f"  Paylocity: 'Jobs' array not found. "
              f"status={r.status_code} length={len(html)} "
              f"first200={html[:200]!r}")
        return []
    start = html.index('[', idx)
    decoder = json.JSONDecoder()
    raw_jobs, _ = decoder.raw_decode(html[start:])
    return [_normalize_paylocity_job(j) for j in raw_jobs]


def _normalize_paylocity_job(j):
    """Normalize a Paylocity JSON job record (field names vary by version)."""
    job_id = str(j.get("JobId") or j.get("Id") or j.get("RequisitionId") or "")
    return {
        "title": j.get("JobTitle") or j.get("Title") or j.get("Name") or "",
        "location": j.get("LocationName") or j.get("Location") or "",
        "department": (j.get("Department") or j.get("DepartmentName")
                       or j.get("Category") or j.get("Categories") or ""),
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
            # are all "not yet marked" (active row, eligible to mark).
            removed_val = str(row.get("Removed", "")).strip().upper()
            if removed_val in ("TRUE", "YES", "1"):
                continue
            cell_updates.append({"range": rowcol_to_a1(row_idx, removed_col),
                                 "values": [["TRUE"]]})
            cell_updates.append({"range": rowcol_to_a1(row_idx, removed_date_col),
                                 "values": [[today]]})
            removed_count += 1

        if cell_updates:
            # USER_ENTERED makes Sheets parse 'TRUE' as a boolean and the date
            # string as a real date, not raw text.
            jobs_tab.batch_update(cell_updates, value_input_option='USER_ENTERED')
            print(f"Marked {removed_count} roles as Removed")

    # Daily headcount snapshot for the Hiring Trend chart
    trend_row = [today] + [counts.get(c["name"], "") for c in COMPANIES]
    trend_tab.append_row(trend_row, value_input_option="USER_ENTERED")


if __name__ == "__main__":
    main()
