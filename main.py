import json
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
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

SENT_JOBS_FILE = "sent_jobs.txt"
CV_FILE = "cv.txt"

SEARCH_QUERIES = ["analyst"]


def load_cv_text():
    if not os.path.exists(CV_FILE):
        logger.warning("cv.txt not found")
        return ""
    with open(CV_FILE, "r", encoding="utf-8") as f:
        return f.read()


def load_cv_keywords(cv_text):
    return set(re.findall(r"[a-zA-Z]{4,}", cv_text.lower()))


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
        term_found = 0
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
                term_found += 1
        logger.info(f"Woolworths search '{term}' found {term_found} raw jobs")

    return list(jobs.values())


def is_relevant_location(job):
    """Keep only NSW-based roles (handles both 'NSW' and 'New South Wales' formats)."""
    text = (job.get("location", "") + " " + job.get("title", "")).lower()
    return "nsw" in text or "new south wales" in text


def keyword_score(job, cv_keywords):
    """Fallback scoring, used only if Claude API is unavailable."""
    text = (job.get("title", "") + " " + job.get("description_short", "")).lower()
    job_words = set(re.findall(r"[a-zA-Z]{4,}", text))
    score = len(job_words & cv_keywords)
    location_text = job.get("location", "").lower()
    if "surry hills" in location_text:
        score += 5
    for term in ["cartology", "woolworths group", "ecommerce", "e-commerce"]:
        if term in text or term in location_text:
            score += 3
    if job.get("source") == "Woolworths" and ("insights analyst" in text or "commercial analyst" in text):
        score += 8
    return score


def score_jobs_with_claude(jobs, cv_text):
    """Batch-score all candidate jobs in a single Claude API call, multi-dimensional."""
    if not jobs or not ANTHROPIC_API_KEY:
        return None

    job_block = "\n\n".join(
        f"ID: {j['id']}\n"
        f"Title: {j['title']}\n"
        f"Source: {j['source']}\n"
        f"Location: {j.get('location', '')}\n"
        f"Details: {j.get('description_short', '')[:400]}"
        for j in jobs
    )

    prompt = f"""You are screening job postings for a candidate transitioning from
engineering/operations into business analytics. Here is their profile:

{cv_text}

The candidate's priorities, in order of importance:
1. Role/function fit — is this genuinely analytics/business/operations work matching their background
2. Industry/company type fit — retail, tech, FMCG, logistics preferred
3. Location closeness — Sydney NSW, ideally close to Manly/North Shore
4. Seniority fit — candidate has 7 years experience but is OPEN to junior/entry-level roles
   while establishing themselves in the Australian market (junior = acceptable, not ideal;
   do not penalize junior titles, but mid/senior roles matching their experience are preferred
   when available)
5. Growth/training signals — nice to have, lowest priority

For EACH job below, return a JSON object with these fields:
- id: the job ID
- role_fit: 0-10, how well the actual job function matches
- industry_fit: 0-10
- location_fit: 0-10
- seniority_fit: 0-10 (junior roles score fine here, not zero)
- overall_relevant: true/false — your final call on whether to show this to the candidate
- visa_flag: one of "none", "mentioned", "citizenship_required" —
  flag "citizenship_required" ONLY if the posting explicitly requires Australian
  citizenship or permanent residency with no exceptions. Use "mentioned" for anything
  more ambiguous (e.g. "must have valid work rights", "no sponsorship available").
- reason: one short sentence explaining the overall_relevant call

EXCLUDE (overall_relevant: false) roles that are actually a different function despite
shared vocabulary — e.g. recruiting, HR/workforce planning, warehouse/store floor roles,
sales, unrelated fields.

Return ONLY a JSON array, no other text, no markdown fences:
[{{"id": "...", "role_fit": 0, "industry_fit": 0, "location_fit": 0, "seniority_fit": 0,
"overall_relevant": true, "visa_flag": "none", "reason": "..."}}]

Jobs:
{job_block}
"""

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 4000,
        "messages": [{"role": "user", "content": prompt}],
    }

    try:
        r = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=body, timeout=90)
        r.raise_for_status()
        data = r.json()
        text = "".join(
            block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
        ).strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.lower().startswith("json"):
                text = text[4:]
        results = json.loads(text)
        return {item["id"]: item for item in results}
    except Exception as e:
        logger.error(f"Claude API scoring failed, falling back to keyword scoring: {e}")
        return None


def build_email_body(matches, used_claude):
    method = "Claude AI (multi-dimensional)" if used_claude else "keyword matching"
    lines = [f"Here are today's job matches (scored via {method}):\n"]
    for job, info in matches:
        lines.append(f"- [{job['source']}] {job['title']} | {job.get('location', '')}")
        if used_claude:
            lines.append(
                f"  Role fit: {info.get('role_fit')}/10 | "
                f"Industry fit: {info.get('industry_fit')}/10 | "
                f"Location fit: {info.get('location_fit')}/10 | "
                f"Seniority fit: {info.get('seniority_fit')}/10"
            )
            if info.get("visa_flag") == "mentioned":
                lines.append("  ⚠ Visa/work-rights language mentioned — check posting")
            lines.append(f"  Why: {info.get('reason', '')}")
        else:
            lines.append(f"  Score: {info.get('score')}")
        lines.append(f"  {job['url']}\n")
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
    cv_text = load_cv_text()
    cv_keywords = load_cv_keywords(cv_text)
    sent_jobs = load_sent_jobs()

    all_jobs = {}
    amazon_raw = []
    for query in SEARCH_QUERIES:
        jobs = fetch_amazon_jobs(query)
        logger.info(f"Amazon query '{query}' returned {len(jobs)} raw jobs")
        amazon_raw.extend(jobs)

    woolworths_raw = fetch_woolworths_jobs()
    logger.info(f"Woolworths returned {len(woolworths_raw)} raw jobs total (deduplicated)")

    for job in amazon_raw:
        all_jobs[job["id"]] = job
    for job in woolworths_raw:
        all_jobs[job["id"]] = job

    logger.info(f"Total unique jobs collected: {len(all_jobs)}")

    all_jobs = {jid: j for jid, j in all_jobs.items() if is_relevant_location(j)}
    logger.info(f"Jobs after location filter: {len(all_jobs)}")

    candidates = [j for jid, j in all_jobs.items() if jid not in sent_jobs]
    logger.info(f"New candidates (not previously sent): {len(candidates)}")

    claude_results = score_jobs_with_claude(candidates, cv_text)
    used_claude = claude_results is not None

    new_matches = []
    if used_claude:
        for job in candidates:
            info = claude_results.get(job["id"])
            if not info:
                continue
            if info.get("visa_flag") == "citizenship_required":
                logger.info(f"  [Excluded - citizenship required] {job['title']}")
                continue
            if info.get("overall_relevant"):
                new_matches.append((job, info))
                logger.info(f"  [Claude] {job['source']} | {job['title']} | {info}")
    else:
        for job in candidates:
            score = keyword_score(job, cv_keywords)
            logger.info(f"  [Keyword] {job['source']} | {job['title']} | score={score}")
            if score >= 3:
                new_matches.append((job, {"score": score}))

    if used_claude:
        new_matches.sort(key=lambda x: x[1].get("role_fit", 0), reverse=True)
    else:
        new_matches.sort(key=lambda x: x[1].get("score", 0), reverse=True)

    if new_matches:
        body = build_email_body(new_matches, used_claude)
        send_email(f"Job matches for today ({len(new_matches)} found)", body)
        save_sent_jobs([job["id"] for job, _ in new_matches])
        logger.info(f"Sent {len(new_matches)} new matches (method: {'Claude' if used_claude else 'keyword'})")
    else:
        logger.info("No new matching jobs today")
