"""One-shot: apply to fresh (<=7d) jobs not already in DB.

Set PRODUCT_ONLY=True to only target product-based companies (allowlist).
"""
import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from tools.search import search_jobs
from tools.profile import should_exclude
from tools.apply import bulk_apply
from tools.tracker import is_already_applied

DEFAULT_KEYWORDS = [
    "DevOps Engineer", "Site Reliability Engineer", "SRE Engineer",
    "Cloud Engineer", "Platform Engineer", "MLOps Engineer", "AIOps Engineer",
]
MAX_DAYS = 3
MAX_APPS = 20
PRODUCT_ONLY = False
SKIP_TITLE_KEYWORDS = ("data engineer", "data engineering", "data scientist", "data analyst")
# Only accept jobs whose title clearly matches an allowed role family
ALLOW_TITLE_KEYWORDS = (
    "devops", "dev ops", "sre", "site reliability",
    "cloud engineer", "cloud infrastructure", "cloud platform",
    "platform engineer", "platform engineering",
    "mlops", "ml ops", "aiops", "ai ops",
    "infrastructure engineer", "kubernetes",
)

# Product-based companies (substring match, case-insensitive). Add/remove as needed.
PRODUCT_COMPANIES = {
    # Indian product / unicorn
    "razorpay", "zomato", "swiggy", "cred", "phonepe", "paytm", "freshworks",
    "zoho", "postman", "hasura", "browserstack", "flipkart", "myntra", "meesho",
    "lenskart", "nykaa", "urban company", "practo", "pharmeasy", "mobikwik",
    "zerodha", "upstox", "indmoney", "groww", "dream11", "mpl ", "games24x7",
    "junglee games", "cars24", "oyo", "makemytrip", "yatra", "cleartrip",
    "delhivery", "rivigo", "shadowfax", "ola ", "ola electric", "olacabs",
    "uber", "airbnb", "stripe", "chargebee", "druva", "thoughtspot", "icertis",
    "innovaccer", "highradius", "gupshup", "freecharge", "shaadi", "bharatpe",
    "policybazaar", "easemytrip", "ixigo", "pristyn care", "redbus", "naukri",
    "info edge", "matrimony", "shiprocket", "vedantu", "byju", "unacademy",
    "physics wallah", "upgrad", "scaler", "interviewbit", "great learning",
    "doubtnut", "leetcode", "geeksforgeeks", "khatabook", "okcredit",
    "treebo", "fabhotels", "rebel foods", "blinkit", "zepto", "country delight",
    "licious", "fresh to home", "purple", "mamaearth", "boat", "noise",
    "mfine", "1mg", "tata 1mg", "netmeds", "curefit", "cult.fit",
    "darwinbox", "leadsquared", "capillary", "moengage", "clevertap",
    "sprinklr", "freshdesk", "icertis", "uniphore", "rephrase ai",
    "yellow.ai", "exotel", "knowlarity", "rocketium", "everstage",
    # FAANG + global product
    "google", "microsoft", "meta ", "facebook", "amazon", "apple",
    "adobe", "salesforce", "oracle", "sap ", "ibm india software lab",
    "servicenow", "workday", "snowflake", "databricks", "mongodb",
    "elastic", "confluent", "hashicorp", "atlassian", "github", "gitlab",
    "linkedin", "intuit", "paypal", "ebay", "expedia", "booking.com",
    "walmart global tech", "walmartlabs", "target tech", "lowes india",
    "nvidia", "amd", "intel", "qualcomm", "vmware", "broadcom",
    "twilio", "okta", "splunk", "datadog", "new relic", "dynatrace",
    "pagerduty", "circleci", "jfrog", "elastic.co", "neo4j", "couchbase",
    "redis", "scylladb", "yugabyte", "cockroach", "pinecone", "weaviate",
    "anthropic", "openai", "hugging face", "stability ai", "scale ai",
    "wayfair", "shopify", "square", "block", "doordash", "instacart",
    "lyft", "robinhood", "coinbase", "binance", "crypto", "polygon",
    "tiktok", "bytedance", "discord", "slack technologies", "zoom",
    "salesforce india", "vmware india", "uber india", "google india",
    "microsoft india", "amazon india", "apple india", "adobe india",
    # Enterprise SaaS / dev tools with India presence
    "guidewire", "icertis", "ennoventure", "fractal analytics", "tredence",
    "mu sigma", "latentview", "course5", "manthan", "blueoptima",
    "harness", "lambdatest", "qase", "testsigma", "applitools",
    "browserstack", "sentry", "rollbar", "bugsnag", "raygun", "loggly",
    "sumologic", "wavefront", "circonus", "honeycomb", "lightstep",
    "kong inc", "tyk", "moesif", "smartbear", "postman", "swagger",
    "stoplight", "readme", "redocly", "speakeasy",
    "stryker", "okta", "elastic", "snyk", "veracode", "checkmarx",
    "sonarsource", "jetbrains", "perforce", "trello", "asana", "notion",
    "airtable", "monday.com", "smartsheet", "miro", "figma", "canva",
    "freshchat", "drift", "hubspot", "marketo", "mailchimp", "sendgrid",
    "twilio segment", "amplitude", "mixpanel", "fullstory", "heap",
    "pendo", "intercom", "zendesk", "kustomer", "gladly",
    "branch.io", "appsflyer", "adjust", "kochava", "tune", "singular",
    "rakuten", "expedia india", "tripadvisor", "trivago",
    "epam", "thoughtworks", "globallogic",  # mixed (often product-aligned)
}

# Service / consultancy / staffing companies — explicit deny (overrides allow)
SERVICE_BLOCKLIST_SUBSTRINGS = (
    "tcs", "tata consultancy services", "infosys", "wipro", "cognizant",
    "accenture", "capgemini", "hcl ", "hcltech", "tech mahindra",
    "mphasis", "ltimindtree", "persistent", "ibm india pvt", "dxc",
    "atos", "hexaware", "cybage", "coforge", "genpact", "wns",
    "iris software", "mindtree", "birlasoft", "ltts", "quess",
    "randstad", "adecco", "manpower", "kelly services", "teamlease",
    "infotech", "it services", "consulting services", "tech services",
    "tech consultancy", "solutions pvt", "technologies pvt",
    "staffing", "consultancy", "consultants", "infosolutions",
    "infosys bpm", "global services", "outsourc",
)


def is_product_company(company: str) -> bool:
    c = company.lower()
    # Blocklist wins
    for bad in SERVICE_BLOCKLIST_SUBSTRINGS:
        if bad in c:
            return False
    # Allowlist match
    for good in PRODUCT_COMPANIES:
        if good in c:
            return True
    return False


async def main():
    print(f"Searching with {len(DEFAULT_KEYWORDS)} keyword sets...\n")
    all_jobs, seen = [], set()
    for kw in DEFAULT_KEYWORDS:
        jobs = await search_jobs(
            keywords=[kw], location="India",
            experience_years=3, platforms=["naukri"],
        )
        for j in jobs:
            if j["apply_url"] not in seen:
                seen.add(j["apply_url"])
                all_jobs.append(j)
        print(f"  {kw:30s} +{len(jobs):2d}  total={len(all_jobs)}")

    print(f"\nTotal unique jobs: {len(all_jobs)}")

    filtered = [
        j for j in all_jobs
        if j.get("match_score", 0) >= 0.25
        and not should_exclude(j.get("title", ""), j.get("description", ""))
        and not any(k in j.get("title", "").lower() for k in SKIP_TITLE_KEYWORDS)
        and any(k in j.get("title", "").lower() for k in ALLOW_TITLE_KEYWORDS)
    ]
    filtered.sort(key=lambda j: j.get("match_score", 0), reverse=True)
    fresh = [j for j in filtered if 0 <= j.get("posted_days_ago", -1) <= MAX_DAYS]
    print(f"After match filter: {len(filtered)}; fresh (<= {MAX_DAYS}d): {len(fresh)}")

    if PRODUCT_ONLY:
        product = [j for j in fresh if is_product_company(j.get("company", ""))]
        print(f"Product-based companies only: {len(product)}")
        fresh = product

    not_applied = [j for j in fresh if not is_already_applied(j["apply_url"])]
    print(f"Not already applied: {len(not_applied)}\n")

    if not not_applied:
        print("Nothing fresh to apply to.")
        return

    print("Top 10 to apply:")
    for i, j in enumerate(not_applied[:10], 1):
        d = j.get("posted_days_ago", -1)
        print(f"  {i:2d}. [{j['match_score']:.2f}] {j['title']} @ {j['company']} ({d}d)")

    target = min(MAX_APPS, len(not_applied))
    print(f"\nApplying to {target} jobs (REAL)...\n")

    result = await bulk_apply(
        jobs=not_applied, max_applications=MAX_APPS, dry_run=False,
    )
    print("\nSummary:", json.dumps(result["summary"], indent=2))
    if result["applied"]:
        print(f"\nApplied ({len(result['applied'])}):")
        for a in result["applied"]:
            print(f"  [OK] {a.get('title','?')} @ {a.get('company','?')}")
    if result["failed"]:
        print(f"\nFailed ({len(result['failed'])}):")
        for f_ in result["failed"]:
            print(f"  [X]  {f_.get('title','?')} - {str(f_.get('error',''))[:80]}")


if __name__ == "__main__":
    asyncio.run(main())
