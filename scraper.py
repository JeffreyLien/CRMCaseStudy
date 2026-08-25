import csv
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://analyst-assessment-production.up.railway.app"
OUTPUT_PATH = Path(__file__).resolve().parent / "data" / "website_facilities.csv"
EXPECTED_COMMUNITY_COUNT = 35
FIELDS = ["name", "address", "city", "state", "zip", "care_offerings", "source_url"]


def get_session():
    session = requests.Session()
    session.headers["User-Agent"] = "Bellhaven reconciliation scraper/1.0"
    return session


def community_links(soup):
    urls = set()
    for anchor in soup.find_all("a", href=True):
        url = urljoin(BASE_URL, anchor["href"])
        path = urlparse(url).path.rstrip("/")
        if re.fullmatch(r"/communities/[^/]+", path):
            urls.add(f"{BASE_URL}{path}")
    return urls


def get_community_urls(session):
    urls = set()
    response = session.get(BASE_URL, timeout=30)
    response.raise_for_status()
    urls.update(community_links(BeautifulSoup(response.text, "html.parser")))

    for page in range(1, 21):
        response = session.get(
            f"{BASE_URL}/communities", params={"page": page}, timeout=30
        )
        response.raise_for_status()
        page_urls = community_links(BeautifulSoup(response.text, "html.parser"))
        new_urls = page_urls - urls
        urls.update(page_urls)
        print(f"Scanned directory page {page}: {len(page_urls)} links ({len(new_urls)} new)")
        if not new_urls:
            break
    return sorted(urls)


def detail_value(soup, label):
    for term in soup.select("dl.detail dt"):
        if term.get_text(" ", strip=True).casefold() == label.casefold():
            return term.find_next_sibling("dd")
    return None


def parse_address(address_node, source_url):
    parts = [part.strip() for part in address_node.stripped_strings if part.strip()]
    if len(parts) < 2:
        raise ValueError(f"Unexpected address structure at {source_url}: {parts!r}")
    match = re.fullmatch(
        r"(?P<city>.+?),\s*(?P<state>[A-Z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)",
        parts[-1],
    )
    if not match:
        raise ValueError(f"Unexpected city/state/ZIP at {source_url}: {parts[-1]!r}")
    return " ".join(parts[:-1]), match.group("city"), match.group("state"), match.group("zip")


def scrape_community(session, url):
    response = session.get(url, timeout=30)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    heading = soup.find("h1")
    address_node = detail_value(soup, "Address")
    care_node = detail_value(soup, "Care Offerings")
    if not heading or not address_node or not care_node:
        raise ValueError(f"Missing expected page elements at {url}")
    address, city, state, zip_code = parse_address(address_node, url)
    offerings = [text.strip() for text in care_node.stripped_strings if text.strip()]
    return {
        "name": heading.get_text(" ", strip=True),
        "address": address,
        "city": city,
        "state": state,
        "zip": zip_code,
        "care_offerings": " | ".join(offerings),
        "source_url": url,
    }


def validate(rows):
    if len(rows) != EXPECTED_COMMUNITY_COUNT:
        raise ValueError(f"Expected {EXPECTED_COMMUNITY_COUNT} communities, found {len(rows)}")
    missing = {
        field: [index + 2 for index, row in enumerate(rows) if not row[field].strip()]
        for field in FIELDS
    }
    missing = {field: lines for field, lines in missing.items() if lines}
    if missing:
        raise ValueError(f"Missing values (CSV row numbers): {missing}")
    duplicate_urls = len(rows) - len({row["source_url"] for row in rows})
    duplicate_names = len(rows) - len({row["name"] for row in rows})
    if duplicate_urls or duplicate_names:
        raise ValueError(f"Duplicates found: source_url={duplicate_urls}, name={duplicate_names}")


def write_csv(rows):
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w", newline="", encoding="utf-8") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def main():
    session = get_session()
    urls = get_community_urls(session)
    print(f"Total unique community URLs: {len(urls)}")
    rows = []
    for number, url in enumerate(urls, start=1):
        row = scrape_community(session, url)
        rows.append(row)
        print(f"Scraped {number:02d}/{len(urls)}: {row['name']}")
    validate(rows)
    write_csv(rows)
    print(f"Wrote {len(rows)} complete rows to {OUTPUT_PATH}")
    print("Validation passed: 0 missing values, 0 duplicate names, 0 duplicate URLs")


if __name__ == "__main__":
    main()
