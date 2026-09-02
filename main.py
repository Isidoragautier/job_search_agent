import logging
import logging.handlers
import os
import re
import smtplib
import requests
from bs4 import BeautifulSoup
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ---------- Logging ----------
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
logger_file_handler = logging.handlers.RotatingFileHandler(
    "status.log", maxBytes=1024 * 1024, backupCount=1, encoding="utf8"
)
formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger_file_handler.setFormatter(formatter)
logger.addHandler(logger_file_handler)
logger.addHandler(logging.StreamHandler())

# ---------- Secrets ----------
EMAIL_ADDRESS = os.environ.get("EMAIL_ADDRESS")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")
TO_EMAIL = os.environ.get("EMAIL_ADDRESS")

SENT_JOBS_FILE = "sent_jobs.txt"
CV_FILE = "cv.txt"

SEARCH_QUERIES = [
    "business analyst",
    "data analyst",
    "operations analyst",
    "supply chain analyst",
    "commercial analyst",
]


def load_cv_keywords():
    if not os.path.exists(CV_FILE):
        logger.warning("cv.txt not found, using empty keyword set")
        return set()
    with open(CV_FILE, "r", encoding="utf-8") as f:
        text = f.read().lower()
    return set(re.findall(r"[a-zA-Z]{4,}", text))


def load_sent_jobs():
    if not os.path.exists(SENT_JOBS_FILE):
        return set()
    with open(SENT_JOBS_FILE, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())


def save_sent_jobs(job_ids):
    with open(SENT_JOBS_FILE, "a", encoding="utf-8") as f:
        for jid in job_ids:
            f.write(jid + "\n")


def fetch_amazon_jobs(query, limit=20):
    """Query Amazon's job search endpoint directly for JSON results."""
    url = "https://www.amazon.jobs/en/search"
    params = {
        "base_query": query,
        "loc_query": "Australia",
        "country": "AUS",
        "sort": "recent",
        "offset": 0,
        "result_limit": limit,
    }
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
    }
    jobs = []
    try:
        r = requests.get(url, params=params, headers=headers, timeout=20)
        r.raise_for_status()
        for job in r.json().get("jobs", []):
            job_id = job.get("id") or job.get("id_icims")
            if not job_id:
                continue
            jobs.append({
                "id": f"amazon_{job_id}",
                "title": job.get("title", ""),
                "description_short": job.get("description_short", ""),
                "location": job.get("normalized_location", ""),
                "url": f"https://www.amazon.jobs{job.get('job_path', '')}",
                "source": "Amazon",
            })
    except Exception as e:
        logger.error(f"Amazon fetch failed for '{query}': {e}")
    return jobs


def fetch_woolworths_jobs(max_pages_per_query=3):
    """Search Woolworths careers by keyword (site returns 6 results per page)."""
    base_url = "https://careers.woolworthsgroup.com.au/en_GB/apply/search-jobs"
    headers = {"User-Agent": "Mozilla/5.0"}
    search_terms = ["analyst", "cartology", "ecommerce", "commercial", "data"]

    jobs = {}
    for term in search_terms:
        for page in range(max_pages_per_query):
            offset = page * 6
            params = {"search": term, "jobOffset": offset}
            try:
                r = requests.get(base_url, params=params, headers=headers, timeout=20)
                r.raise_for_status()
            except Exception as e:
                logger.error(f"Woolworths fetch failed for '{term}' page {page}: {e}")
                break

            soup = BeautifulSoup(r.text, "html.parser")
            containers = soup.find_all("div", class_="article__header__text")
            if not containers:
                break

            for c in containers:
                title_tag = c.find("a", href=True)
                if not title_tag:
                    continue
                href = title_tag["href"]
                title = title_tag.get_text(strip=True)
                job_id = href.rstrip("/").split("/")[-1]
                full_id = f"woolworths_{job_id}"
                if full_id in jobs:
                    continue

                def get_text(cls):
                    tag = c.find("span", class_=cls)
                    return tag.get_text(strip=True) if tag else ""

                location_source = get_text("list-item-locationSource")
                location = get_text("list-item-location")
                career_group = get_text("list-item-careerGroup")
                brand = get_text("list-item-brand")

                jobs[full_id] = {
                    "id": full_id,
                    "title": title,
                    "description_short": f"{title} {career_group} {brand}",
                    "location": f"{location_source} — {location}".strip(" —"),
                    "url": href if href.startswith("http") else f"https://careers.woolworthsgroup.com.au{href}",
                    "source": "Woolworths",
                }

    return list(jobs.values())


def score_job(job, cv_keywords):
    text = (job.get("title", "") + " " + job.get("description_short", "")).lower()
    job_words = set(re.findall(r"[a-zA-Z]{4,}", text))
    score = len(job_words & cv_keywords)

    location_text = job.get("location", "").lower()
    if "surry hills" in location_text:
        score += 5  # strong bonus: Woolworths Group HQ / corporate hub

    bonus_terms = ["cartology", "woolworths group", "ecommerce", "e-commerce"]
    for term in bonus_terms:
        if term in text or term in location_text:
            score += 3

    return score


def is_relevant_location(job):
    """Keep only NSW-based roles (handles both 'NSW' and 'New South Wales' formats)."""
    text = (job.get("location", "") + " " + job.get("title", "")).lower()
    return "nsw" in text or "new south wales" in text


def build_email_body(matches):
    lines = ["Here are today's job matches:\n"]
    for job, score in matches:
        lines.append(
            f"- [{job['source']}] {job['title']} | {job.get('location', '')} | fit score: {score}\n  {job['url']}\n"
        )
    return "\n".join(lines)


def send_email(subject, body):
    if not EMAIL_ADDRESS or not EMAIL_PASSWORD:
        logger.error("Email credentials not set, skipping send")
        return
    msg = MIMEMultipart()
    msg["From"] = EMAIL_ADDRESS
    msg["To"] = TO_EMAIL
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
        server.send_message(msg)
    logger.info("Email sent successfully")


if __name__ == "__main__":
    cv_keywords = load_cv_keywords()
    sent_jobs = load_sent_jobs()

    all_jobs = {}

    for query in SEARCH_QUERIES:
        for job in fetch_amazon_jobs(query):
            all_jobs[job["id"]] = job

    for job in fetch_woolworths_jobs():
        all_jobs[job["id"]] = job

    logger.info(f"Total unique jobs collected: {len(all_jobs)}")

    all_jobs = {job_id: job for job_id, job in all_jobs.items() if is_relevant_location(job)}
    logger.info(f"Jobs after location filter: {len(all_jobs)}")

    amazon_count = sum(1 for j in all_jobs.values() if j["source"] == "Amazon")
    woolworths_count = sum(1 for j in all_jobs.values() if j["source"] == "Woolworths")
    logger.info(f"  -> Amazon: {amazon_count}, Woolworths: {woolworths_count}")

    new_matches = []
    for job_id, job in all_jobs.items():
        if job_id in sent_jobs:
            continue
        score = score_job(job, cv_keywords)
        logger.info(f"  {job['source']} | {job['title']} | score={score}")
        if score >= 3:
            new_matches.append((job, score))

    new_matches.sort(key=lambda x: x[1], reverse=True)

    if new_matches:
        body = build_email_body(new_matches)
        send_email(f"Job matches for today ({len(new_matches)} found)", body)
        save_sent_jobs([job["id"] for job, _ in new_matches])
        logger.info(f"Sent {len(new_matches)} new matches")
    else:
        logger.info("No new matching jobs today")
