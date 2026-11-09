from __future__ import annotations

import re
import json
import requests
from typing import Tuple
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

from jobquest.glassdoor.constant import fallback_token, query_template, headers
from jobquest.glassdoor.util import (
    get_cursor_for_page,
    parse_compensation,
    parse_location,
)
from jobquest.stealth import SCRAPLING_AVAILABLE, stealth_fetch, ResponseAdapter
from urllib.parse import quote

from jobquest.util import (
    extract_emails_from_text,
    extract_experience_range,
    create_logger,
    create_session,
    create_stealth_session,
    markdown_converter,
    plain_converter,
)
from jobquest.exception import GlassdoorException
from jobquest.model import (
    JobPost,
    JobResponse,
    DescriptionFormat,
    Scraper,
    ScraperInput,
    Site,
)

log = create_logger("Glassdoor")


class Glassdoor(Scraper):
    def __init__(
        self, proxies: list[str] | str | None = None, ca_cert: str | None = None, user_agent: str | None = None
    ):
        site = Site(Site.GLASSDOOR)
        super().__init__(site, proxies=proxies, ca_cert=ca_cert, user_agent=user_agent)

        self.base_url = None
        self.country = None
        self.session = None
        self.scraper_input = None
        self.jobs_per_page = 30
        self.max_pages = 30
        self.seen_urls = set()
        self._cf_cookies = {}

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        self.scraper_input = scraper_input
        self.scraper_input.results_wanted = min(900, scraper_input.results_wanted)
        self.base_url = self.scraper_input.country.get_glassdoor_url().rstrip("/")

        if SCRAPLING_AVAILABLE:
            self.session = create_stealth_session(
                proxies=self.proxies, ca_cert=self.ca_cert
            )
        else:
            self.session = create_session(
                proxies=self.proxies, ca_cert=self.ca_cert, has_retry=True
            )

        token = self._get_csrf_token()
        local_headers = headers.copy()
        local_headers["gd-csrf-token"] = token if token else fallback_token
        if self.user_agent:
            local_headers["user-agent"] = self.user_agent
        self.session.headers.update(local_headers)

        location_id, location_type = self._get_location(
            scraper_input.location, scraper_input.is_remote
        )
        if location_type is None:
            log.error("Glassdoor: location not parsed")
            return JobResponse(jobs=[])
        job_list: list[JobPost] = []
        cursor = None

        range_start = 1 + (scraper_input.offset // self.jobs_per_page)
        tot_pages = (scraper_input.results_wanted // self.jobs_per_page) + 2
        range_end = min(tot_pages, self.max_pages + 1)
        for page in range(range_start, range_end):
            log.info(f"search page: {page} / {range_end - 1}")
            try:
                jobs, cursor = self._fetch_jobs_page(
                    scraper_input, location_id, location_type, page, cursor
                )
                job_list.extend(jobs)
                if not jobs or len(job_list) >= scraper_input.results_wanted:
                    job_list = job_list[: scraper_input.results_wanted]
                    break
            except Exception as e:
                log.error(f"Glassdoor: {str(e)}")
                break
        return JobResponse(jobs=job_list)

    def _fetch_jobs_page(
        self,
        scraper_input: ScraperInput,
        location_id: int,
        location_type: str,
        page_num: int,
        cursor: str | None,
    ) -> Tuple[list[JobPost], str | None]:
        jobs = []
        self.scraper_input = scraper_input
        try:
            payload = self._add_payload(location_id, location_type, page_num, cursor)
            response = self.session.post(
                f"{self.base_url}/graph",
                data=payload,
                timeout=15,
                cookies=self._cf_cookies or None,
            )
            # The jobs API 403s behind Cloudflare until a valid cf_clearance cookie
            # is present (common on a cold session). Solve once with the undetected
            # browser (patchright), cache the cookies, and retry — instead of failing.
            if response.status_code == 403 and SCRAPLING_AVAILABLE:
                # Under datacenter-IP scrutiny the stealth browser is intermittent
                # (Cloudflare loops the challenge ~2-in-3 attempts), so retry the
                # solve until a fresh cf_clearance gets us a 200. Each successful
                # solve is ~8s; failures hit the browser timeout, so the bot's outer
                # scrape timeout bounds the total.
                for attempt in range(3):
                    log.info(f"Jobs API 403 — solving Cloudflare (attempt {attempt + 1}/3)")
                    try:
                        cf = stealth_fetch(
                            f"{self.base_url}/Job/computer-science-jobs.htm",
                            solve_cloudflare=True,
                        )
                        if cf.cookies:
                            self._cf_cookies = cf.cookies
                        response = self.session.post(
                            f"{self.base_url}/graph",
                            data=payload,
                            timeout=15,
                            cookies=self._cf_cookies or None,
                        )
                        if response.status_code == 200:
                            log.info(f"Cloudflare solved on attempt {attempt + 1}")
                            break
                    except Exception as e:
                        log.warning(f"Cloudflare solve attempt {attempt + 1} failed: {e}")
            if response.status_code != 200:
                exc_msg = f"bad response status code: {response.status_code}"
                raise GlassdoorException(exc_msg)
            res_json = response.json()[0]
            if "errors" in res_json and "data" not in res_json:
                raise ValueError("Error encountered in API response with no data")
        except (
            requests.exceptions.ReadTimeout,
            GlassdoorException,
            ValueError,
            Exception,
        ) as e:
            log.error(f"Glassdoor: {str(e)}")
            return jobs, None

        jobs_data = res_json["data"]["jobListings"]["jobListings"]

        with ThreadPoolExecutor(max_workers=self.jobs_per_page) as executor:
            future_to_job_data = {
                executor.submit(self._process_job, job): job for job in jobs_data
            }
            for future in as_completed(future_to_job_data):
                try:
                    job_post = future.result()
                    if job_post:
                        jobs.append(job_post)
                except Exception as exc:
                    raise GlassdoorException(f"Glassdoor generated an exception: {exc}")

        return jobs, get_cursor_for_page(
            res_json["data"]["jobListings"]["paginationCursors"], page_num + 1
        )

    def _get_csrf_token(self):
        url = f"{self.base_url}/Job/computer-science-jobs.htm"
        res = self.session.get(url, cookies=self._cf_cookies or None)
        text = res.text

        pattern = r'"token":\s*"([^"]+)"'
        matches = re.findall(pattern, text)
        if matches:
            return matches[0]

        if res.status_code == 403 and SCRAPLING_AVAILABLE:
            log.info("CSRF blocked (403), retrying with StealthyFetcher")
            try:
                proxy = self.proxies[0] if isinstance(self.proxies, list) and self.proxies else self.proxies
                resp = stealth_fetch(url, solve_cloudflare=True, headless=True, locale="en-US", proxy=proxy)
                self._cf_cookies = resp.cookies or {}
                matches = re.findall(pattern, resp.text)
                if matches:
                    return matches[0]
            except Exception as e:
                log.warning(f"StealthyFetcher also failed for CSRF: {e}")

        return None

    def _process_job(self, job_data):
        job_id = job_data["jobview"]["job"]["listingId"]
        job_url = f"{self.base_url}/job-listing/j?jl={job_id}"
        if job_url in self.seen_urls:
            return None
        self.seen_urls.add(job_url)
        job = job_data["jobview"]
        title = job["job"]["jobTitleText"]
        company_name = job["header"]["employerNameFromSearch"]
        company_id = job_data["jobview"]["header"]["employer"]["id"]
        location_name = job["header"].get("locationName", "")
        location_type = job["header"].get("locationType", "")
        age_in_days = job["header"].get("ageInDays")
        is_remote, location = False, None
        date_posted = (datetime.now() - timedelta(days=age_in_days)).date() if age_in_days is not None else None

        # locationType "S" = State-level (e.g. Hamburg, California). Don't blindly
        # treat as remote — only when locationName actually indicates remote work.
        if location_name and any(kw in location_name.lower() for kw in ("remote", "anywhere", "home office")):
            is_remote = True
        elif location_name:
            location = parse_location(location_name)
        elif location_type == "S":
            is_remote = True

        compensation = parse_compensation(job["header"])
        try:
            description = self._fetch_job_description(job_id)
        except Exception:
            description = None

        experience_range = extract_experience_range(description, title)

        company_url = f"{self.base_url}Overview/W-EI_IE{company_id}.htm"
        company_logo = (
            job_data["jobview"].get("overview", {}).get("squareLogoUrl", None)
        )
        listing_type = (
            job_data["jobview"]
            .get("header", {})
            .get("adOrderSponsorshipLevel", "")
            .lower()
        )
        rating = job["header"].get("rating")
        easy_apply = job["header"].get("easyApply", False)
        job_level = job["header"].get("goc", "")
        company_industry = (
            job_data["jobview"].get("overview", {}).get("shortName", None)
        )

        return JobPost(
            id=f"gd-{job_id}",
            title=title,
            company_url=company_url if company_id else None,
            company_name=company_name,
            date_posted=date_posted,
            job_url=job_url,
            job_url_direct=job["header"].get("jobLink"),
            location=location,
            compensation=compensation,
            is_remote=is_remote,
            description=description,
            emails=extract_emails_from_text(description) if description else None,
            company_logo=company_logo,
            listing_type=f"{'easy_apply ' if easy_apply else ''}{listing_type}".strip(),
            job_level=str(rating) if rating else None,
            company_industry=company_industry,
            experience_range=experience_range,
        )

    def _fetch_job_description(self, job_id):
        url = f"{self.base_url}/graph"
        body = [
            {
                "operationName": "JobDetailQuery",
                "variables": {
                    "jl": job_id,
                    "queryString": "q",
                    "pageTypeEnum": "SERP",
                },
                "query": """
                query JobDetailQuery($jl: Long!, $queryString: String, $pageTypeEnum: PageTypeEnum) {
                    jobview: jobView(
                        listingId: $jl
                        contextHolder: {queryString: $queryString, pageTypeEnum: $pageTypeEnum}
                    ) {
                        job {
                            description
                            __typename
                        }
                        __typename
                    }
                }
                """,
            }
        ]
        res = self.session.post(url, json=body, headers=headers)
        if res.status_code != 200:
            return None
        data = res.json()[0]
        desc = data["data"]["jobview"]["job"]["description"]
        if self.scraper_input.description_format == DescriptionFormat.MARKDOWN:
            desc = markdown_converter(desc)
        elif self.scraper_input.description_format == DescriptionFormat.PLAIN:
            desc = plain_converter(desc)
        return desc

    def _get_location(self, location: str, is_remote: bool) -> (int, str):
        if not location or is_remote:
            return "11047", "STATE"
        url = f"{self.base_url}/findPopularLocationAjax.htm?maxLocationsToReturn=10&term={quote(location, safe='')}"

        res = self.session.get(url, cookies=self._cf_cookies or None)

        if res.status_code == 403 and SCRAPLING_AVAILABLE:
            log.info("Location lookup blocked (403), retrying with StealthyFetcher")
            try:
                proxy = self.proxies[0] if isinstance(self.proxies, list) and self.proxies else self.proxies
                resp = stealth_fetch(url, solve_cloudflare=True, headless=True, locale="en-US", proxy=proxy)
                if resp.ok:
                    res = resp
            except Exception as e:
                log.warning(f"StealthyFetcher also failed for location: {e}")

        if res.status_code != 200:
            if res.status_code == 429:
                log.error("429 Response - Blocked by Glassdoor for too many requests")
            else:
                log.error(f"Glassdoor response status code {res.status_code}")
            return None, None

        items = res.json()
        if not items:
            raise ValueError(f"Location '{location}' not found on Glassdoor")
        location_type = items[0]["locationType"]
        if location_type == "C":
            location_type = "CITY"
        elif location_type == "S":
            location_type = "STATE"
        elif location_type == "N":
            location_type = "COUNTRY"
        return int(items[0]["locationId"]), location_type

    def _add_payload(
        self,
        location_id: int,
        location_type: str,
        page_num: int,
        cursor: str | None = None,
    ) -> str:
        fromage = None
        if self.scraper_input.hours_old:
            fromage = max(self.scraper_input.hours_old // 24, 1)
        filter_params = []
        if self.scraper_input.easy_apply:
            filter_params.append({"filterKey": "applicationType", "values": "1"})
        if fromage:
            filter_params.append({"filterKey": "fromAge", "values": str(fromage)})
        payload = {
            "operationName": "JobSearchResultsQuery",
            "variables": {
                "excludeJobListingIds": [],
                "filterParams": filter_params,
                "keyword": self.scraper_input.search_term,
                "numJobsToShow": 30,
                "locationType": location_type,
                "locationId": int(location_id),
                "parameterUrlInput": f"IL.0,12_I{location_type}{location_id}",
                "pageNumber": page_num,
                "pageCursor": cursor,
                "fromage": fromage,
                "sort": "date",
            },
            "query": query_template,
        }
        if self.scraper_input.job_type:
            payload["variables"]["filterParams"].append(
                {"filterKey": "jobType", "values": self.scraper_input.job_type.value[0]}
            )
        return json.dumps([payload])
