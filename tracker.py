#!/usr/bin/env python3
"""
Daily job tracker for private-company watchlist.
Pulls open roles from Greenhouse, Lever, Ashby, and Paylocity,
diffs against last run, writes new roles to Google Sheets.
"""
import os
import json
import requests
import gspread
from datetime import datetime, timezone
from google.oauth2.service_account import Credentials

# Edit this list with your 5 companies.
# board must be one of: "greenhouse", "lever", "ashby", "paylocity"
COMPANIES = [
    {"name": "Harness", "board": "greenhouse", "slug": "harnessinc"},
    {"name": "CircleCI", "board": "greenhouse", "slug": "circleci"},
    {"name": "CloudBees", "board": "paylocity", "slug": "982bc369-6352-4fa4-942d-7c76bfca29b4"},
    {"name": "Buildkite", "board": "greenhouse", "slug": "buildkite"},
    {"name": "Octopus Deploy", "board": "greenhouse", "slug": "octopusdeploy"},
]

SPREADSHEET_KEY = "1OaNh7Tq7MM8JP6S7qIXBC5toNd3k79WR9DM9tk2x1b0"


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
    r = requests.get(f"https://recruiting.paylocity.com/recruiting/v2/api/feed/jobs/{slug}", timeout=30)
    r.raise_for_status()
    return [
        {
            "title": j.get("title", ""),
            "location": j.get("location", "") or j.get("city", ""),
            "department": j.get("hiringDepartment", ""),
            "url": j.get("applyUrl", ""),
            "external_id": str(j.get("jobId", "")),
        }
        for j in r.json().get("jobs", [])
    ]


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "paylocity": fetch_paylocity,
}


def main():
    creds = Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]),
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive.readonly",
        ],
    )
    gc = gspread.authorize(creds)
    wb = gc.open_by_key(SPREADSHEET_KEY)
    jobs_tab = wb.worksheet("Jobs")
    trend_tab = wb.worksheet("Hiring Trend")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    existing = jobs_tab.get_all_records()
    existing_keys = {f"{r['Company']}|{r['External ID']}" for r in existing}

    new_rows = []
    counts = {}

    for c in COMPANIES:
        try:
            jobs = FETCHERS[c["board"]](c["slug"])
            counts[c["name"]] = len(jobs)
            print(f"{c['name']}: {len(jobs)} open roles")
            for j in jobs:
                key = f"{c['name']}|{j['external_id']}"
                if key not in existing_keys:
                    new_rows.append([
                        today, c["name"], j["title"], j["department"],
                        j["location"], j["url"], j["external_id"], "NEW"
                    ])
        except Exception as e:
            print(f"Error fetching {c['name']}: {e}")
            counts[c["name"]] = ""

    if new_rows:
        jobs_tab.append_rows(new_rows, value_input_option="USER_ENTERED")
        print(f"Added {len(new_rows)} new roles")

    trend_row = [today] + [counts.get(c["name"], "") for c in COMPANIES]
    trend_tab.append_row(trend_row, value_input_option="USER_ENTERED")


if __name__ == "__main__":
    main()
