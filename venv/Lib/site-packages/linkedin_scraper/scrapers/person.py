"""Person/Profile scraper for LinkedIn."""

import logging
from typing import Optional
from urllib.parse import urljoin
from playwright.async_api import Page

from .base import BaseScraper
from ..models import Person, Experience, Education, Accomplishment, Interest, Contact
from ..callbacks import ProgressCallback, SilentCallback
from ..core.exceptions import ScrapingError

logger = logging.getLogger(__name__)


class PersonScraper(BaseScraper):
    """Async scraper for LinkedIn person profiles."""

    def __init__(self, page: Page, callback: Optional[ProgressCallback] = None):
        """
        Initialize person scraper.

        Args:
            page: Playwright page object
            callback: Progress callback
        """
        super().__init__(page, callback)

    async def scrape(self, linkedin_url: str) -> Person:
        """
        Scrape a LinkedIn person profile.

        Args:
            linkedin_url: LinkedIn profile URL

        Returns:
            Person object with all scraped data

        Raises:
            AuthenticationError: If not logged in
            ScrapingError: If scraping fails
        """
        await self.callback.on_start("person", linkedin_url)

        try:
            # Navigate to profile first (this loads the page with our session)
            await self.navigate_and_wait(linkedin_url)
            await self.callback.on_progress("Navigated to profile", 10)

            # Now check if logged in
            await self.ensure_logged_in()

            # Wait for main content
            await self.page.wait_for_selector("main", timeout=10000)
            await self.wait_and_focus(1)

            # Get name and location
            name, location = await self._get_name_and_location()
            await self.callback.on_progress(f"Got name: {name}", 20)

            # Check open to work
            open_to_work = await self._check_open_to_work()

            # Get about
            about = await self._get_about()
            await self.callback.on_progress("Got about section", 30)

            # Scroll to load content
            await self.scroll_page_to_half()
            await self.scroll_page_to_bottom(pause_time=0.5, max_scrolls=3)

            # Get experiences
            experiences = await self._get_experiences(linkedin_url)
            await self.callback.on_progress(f"Got {len(experiences)} experiences", 60)

            educations = await self._get_educations(linkedin_url)
            await self.callback.on_progress(f"Got {len(educations)} educations", 50)

            interests = await self._get_interests(linkedin_url)
            await self.callback.on_progress(f"Got {len(interests)} interests", 65)

            accomplishments = await self._get_accomplishments(linkedin_url)
            await self.callback.on_progress(
                f"Got {len(accomplishments)} accomplishments", 85
            )

            contacts = await self._get_contacts(linkedin_url)
            await self.callback.on_progress(f"Got {len(contacts)} contacts", 95)

            person = Person(
                linkedin_url=linkedin_url,
                name=name,
                location=location,
                about=about,
                open_to_work=open_to_work,
                experiences=experiences,
                educations=educations,
                interests=interests,
                accomplishments=accomplishments,
                contacts=contacts,
            )

            await self.callback.on_progress("Scraping complete", 100)
            await self.callback.on_complete("person", person)

            return person

        except Exception as e:
            await self.callback.on_error(e)
            raise ScrapingError(f"Failed to scrape person profile: {e}")

    async def _get_name_and_location(self) -> tuple[str, Optional[str]]:
        """Extract name and location from profile."""
        try:
            # Page title is the most stable source: "Name | LinkedIn"
            title = await self.page.title()
            name = title.split(" | ")[0].strip() if " | " in title else "Unknown"

            # Location: parse main text — structure is "Name \n Headline \n Location · Coordonnées..."
            location = await self.page.evaluate('''() => {
                const main = document.querySelector("main");
                if (!main) return null;
                // Take text before the "·" separator (contact info marker)
                const text = main.innerText.split("·")[0];
                const lines = text.split("\\n").map(l => l.trim()).filter(Boolean);
                // lines[0]=name, lines[1]=headline, lines[2]=location
                if (lines.length >= 3) return lines[2];
                return null;
            }''')

            return name, location
        except Exception as e:
            logger.warning(f"Error getting name/location: {e}")
            return "Unknown", None

    async def _check_open_to_work(self) -> bool:
        """Check if profile has open to work badge."""
        try:
            # Look for open to work indicator
            img_title = await self.get_attribute_safe(
                ".pv-top-card-profile-picture img", "title", default=""
            )
            return "#OPEN_TO_WORK" in img_title.upper()
        except:
            return False

    async def _get_about(self) -> Optional[str]:
        """Extract about section via JS — robust to obfuscated class names."""
        try:
            about = await self.page.evaluate('''() => {
                const main = document.querySelector("main");
                if (!main) return null;
                const aboutKeywords = ["about", "\xe0 propos", "infos", "info", "sobre", "uber mich", "acerca"];
                for (const h2 of main.querySelectorAll("h2")) {
                    const heading = (h2.innerText || "").trim().toLowerCase();
                    if (!aboutKeywords.some(k => heading.includes(k))) continue;
                    // Walk up to find the section container, then grab content spans
                    let container = h2.parentElement;
                    for (let i = 0; i < 6 && container && container !== main; i++) {
                        const spans = container.querySelectorAll("span, p");
                        for (const span of spans) {
                            const t = (span.innerText || "").trim();
                            if (t.length > 30 && !aboutKeywords.some(k => t.toLowerCase() === k)) {
                                return t;
                            }
                        }
                        container = container.parentElement;
                    }
                }
                return null;
            }''')
            return about
        except Exception as e:
            logger.debug(f"Error getting about section: {e}")
            return None

    async def _get_experiences(self, base_url: str) -> list[Experience]:
        """Extract experiences from /details/experience/ using JS text parsing."""
        try:
            exp_url = base_url.rstrip('/') + '/details/experience/'
            await self.navigate_and_wait(exp_url)
            await self.page.wait_for_timeout(2000)
            for _ in range(3):
                await self.page.keyboard.press('End')
                await self.page.wait_for_timeout(700)

            items_data = await self.page.evaluate('''() => {
                const main = document.querySelector("main");
                if (!main) return [];
                const datePattern = /\\b(20\\d{2}|19\\d{2}|janv|f\\xe9vr|mars|avr|mai|juin|juil|ao\\xfbt|sept|oct|nov|d\\xe9c|jan|feb|mar|apr|jun|jul|aug|sep|dec)\\b/i;
                const results = [];
                for (const el of main.querySelectorAll("div, section")) {
                    const text = el.innerText?.trim() || "";
                    if (text.length < 15 || text.length > 1000) continue;
                    if (!datePattern.test(text)) continue;
                    if (el.querySelectorAll("div").length > 5) continue;
                    const lines = text.split("\\n").map(l => l.trim()).filter(Boolean);
                    if (lines.length >= 2) results.push(lines);
                }
                return results;
            }''')

            experiences = []
            for lines in items_data:
                try:
                    title = lines[0]
                    company_raw = lines[1] if len(lines) > 1 else ''
                    company = company_raw.split(' · ')[0].strip()
                    dates_str = lines[2] if len(lines) > 2 else ''
                    location_raw = lines[3] if len(lines) > 3 else ''
                    location = location_raw.split(' · ')[0].strip() or None
                    description = '\n'.join(lines[4:]) if len(lines) > 4 else None

                    from_date, to_date, duration = self._parse_work_times(dates_str)

                    experiences.append(Experience(
                        position_title=title,
                        institution_name=company,
                        from_date=from_date,
                        to_date=to_date,
                        duration=duration,
                        location=location,
                        description=description,
                    ))
                except Exception as e:
                    logger.debug(f"Error parsing experience item: {e}")
            return experiences

        except Exception as e:
            logger.warning(f"Error getting experiences: {e}")
            return []

    def _parse_work_times(
        self, work_times: str
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """
        Parse work times string into from_date, to_date, duration.

        Examples:
        - "2000 - Present · 26 yrs 1 mo" -> ("2000", "Present", "26 yrs 1 mo")
        - "Jan 2020 - Dec 2022 · 2 yrs" -> ("Jan 2020", "Dec 2022", "2 yrs")
        - "2015 - Present" -> ("2015", "Present", None)
        """
        if not work_times:
            return None, None, None

        try:
            # Split by · to separate date range from duration
            parts = work_times.split("·")
            times = parts[0].strip() if len(parts) > 0 else ""
            duration = parts[1].strip() if len(parts) > 1 else None

            # Parse dates - split by " - " to get from and to
            if " - " in times:
                date_parts = times.split(" - ")
                from_date = date_parts[0].strip()
                to_date = date_parts[1].strip() if len(date_parts) > 1 else ""
            else:
                from_date = times
                to_date = ""

            return from_date, to_date, duration
        except Exception as e:
            logger.debug(f"Error parsing work times '{work_times}': {e}")
            return None, None, None

    async def _get_educations(self, base_url: str) -> list[Education]:
        """Extract educations from /details/education/ using JS text parsing."""
        try:
            edu_url = base_url.rstrip('/') + '/details/education/'
            await self.navigate_and_wait(edu_url)
            await self.page.wait_for_timeout(2000)
            for _ in range(3):
                await self.page.keyboard.press('End')
                await self.page.wait_for_timeout(700)

            items_data = await self.page.evaluate('''() => {
                const main = document.querySelector("main");
                if (!main) return [];
                // Education items: divs containing a 4-digit year
                const yearPattern = /\\b(19|20)\\d{2}\\b/;
                const results = [];
                for (const el of main.querySelectorAll("div, section")) {
                    const text = el.innerText?.trim() || "";
                    if (text.length < 5 || text.length > 800) continue;
                    if (!yearPattern.test(text)) continue;
                    if (el.querySelectorAll("div").length > 5) continue;
                    const lines = text.split("\\n").map(l => l.trim()).filter(Boolean);
                    if (lines.length >= 1) results.push(lines);
                }
                return results;
            }''')

            educations = []
            for lines in items_data:
                try:
                    institution = lines[0]
                    degree = None
                    times = ''

                    if len(lines) == 2:
                        # Could be degree or dates
                        if any(c.isdigit() for c in lines[1]):
                            times = lines[1]
                        else:
                            degree = lines[1]
                    elif len(lines) >= 3:
                        degree = lines[1]
                        times = lines[2]

                    from_date, to_date = self._parse_education_times(times)

                    educations.append(Education(
                        institution_name=institution,
                        degree=degree,
                        from_date=from_date,
                        to_date=to_date,
                        description='\n'.join(lines[3:]) if len(lines) > 3 else None,
                    ))
                except Exception as e:
                    logger.debug(f"Error parsing education item: {e}")
            return educations

        except Exception as e:
            logger.warning(f"Error getting educations: {e}")
            return []

    def _parse_education_times(self, times: str) -> tuple[Optional[str], Optional[str]]:
        """
        Parse education times string into from_date, to_date.

        Examples:
        - "1973 - 1977" -> ("1973", "1977")
        - "2015" -> ("2015", "2015")
        - "" -> (None, None)
        """
        if not times:
            return None, None

        try:
            # Split by " - " to get from and to dates
            if " - " in times:
                parts = times.split(" - ")
                from_date = parts[0].strip()
                to_date = parts[1].strip() if len(parts) > 1 else ""
            else:
                # Single year
                from_date = times.strip()
                to_date = times.strip()

            return from_date, to_date
        except Exception as e:
            logger.debug(f"Error parsing education times '{times}': {e}")
            return None, None

    async def _get_interests(self, base_url: str) -> list[Interest]:
        """Extract interests from the main profile page Interests section with tablist."""
        interests = []

        try:
            interests_heading = self.page.locator('h2:has-text("Interests")').first
            
            if await interests_heading.count() > 0:
                interests_section = interests_heading.locator('xpath=ancestor::*[.//tablist or .//*[@role="tablist"]][1]')
                if await interests_section.count() == 0:
                    interests_section = interests_heading.locator('xpath=ancestor::*[4]')
                
                tabs = await interests_section.locator('[role="tab"], tab').all() if await interests_section.count() > 0 else []
                
                if tabs:
                    for tab in tabs:
                        try:
                            tab_name = await tab.text_content()
                            if not tab_name:
                                continue
                            tab_name = tab_name.strip()
                            category = self._map_interest_tab_to_category(tab_name)

                            await tab.click()
                            await self.wait_and_focus(0.5)

                            tabpanel = interests_section.locator('[role="tabpanel"]').first
                            if await tabpanel.count() > 0:
                                list_items = await tabpanel.locator('li, listitem').all()
                                
                                for item in list_items:
                                    try:
                                        interest = await self._parse_interest_item(item, category)
                                        if interest:
                                            interests.append(interest)
                                    except Exception as e:
                                        logger.debug(f"Error parsing interest item: {e}")
                                        continue
                        except Exception as e:
                            logger.debug(f"Error processing interest tab: {e}")
                            continue
            
            if not interests:
                interests_url = urljoin(base_url, "details/interests/")
                await self.navigate_and_wait(interests_url)
                await self.page.wait_for_selector("main", timeout=10000)
                await self.wait_and_focus(1.5)

                tabs = await self.page.locator('[role="tab"], tab').all()

                if not tabs:
                    logger.debug("No interests tabs found on profile")
                    return interests

                for tab in tabs:
                    try:
                        tab_name = await tab.text_content()
                        if not tab_name:
                            continue
                        tab_name = tab_name.strip()
                        category = self._map_interest_tab_to_category(tab_name)

                        await tab.click()
                        await self.wait_and_focus(0.8)

                        tabpanel = self.page.locator('[role="tabpanel"], tabpanel').first
                        list_items = await tabpanel.locator("listitem, li, .pvs-list__paged-list-item").all()

                        for item in list_items:
                            try:
                                interest = await self._parse_interest_item(item, category)
                                if interest:
                                    interests.append(interest)
                            except Exception as e:
                                logger.debug(f"Error parsing interest item: {e}")
                                continue

                    except Exception as e:
                        logger.debug(f"Error processing interest tab: {e}")
                        continue

        except Exception as e:
            logger.warning(f"Error getting interests: {e}")

        return interests
    
    async def _parse_interest_item(self, item, category: str) -> Optional[Interest]:
        """Parse a single interest item from profile or details page."""
        try:
            link = item.locator("a, link").first
            if await link.count() == 0:
                return None
            href = await link.get_attribute("href")

            unique_texts = await self._extract_unique_texts_from_element(item)
            name = unique_texts[0] if unique_texts else None

            if name and href:
                return Interest(
                    name=name,
                    category=category,
                    linkedin_url=href,
                )
            return None
        except Exception as e:
            logger.debug(f"Error parsing interest: {e}")
            return None

    def _map_interest_tab_to_category(self, tab_name: str) -> str:
        tab_lower = tab_name.lower()
        if "compan" in tab_lower:
            return "company"
        elif "group" in tab_lower:
            return "group"
        elif "school" in tab_lower:
            return "school"
        elif "newsletter" in tab_lower:
            return "newsletter"
        elif "voice" in tab_lower or "influencer" in tab_lower:
            return "influencer"
        else:
            return tab_lower

    async def _get_accomplishments(self, base_url: str) -> list[Accomplishment]:
        accomplishments = []

        accomplishment_sections = [
            ("certifications", "certification"),
            ("honors", "honor"),
            ("publications", "publication"),
            ("patents", "patent"),
            ("courses", "course"),
            ("projects", "project"),
            ("languages", "language"),
            ("organizations", "organization"),
        ]

        for url_path, category in accomplishment_sections:
            try:
                section_url = urljoin(base_url, f"details/{url_path}/")
                await self.navigate_and_wait(section_url)
                await self.page.wait_for_selector("main", timeout=10000)
                await self.wait_and_focus(1)

                nothing_to_see = await self.page.locator(
                    'text="Nothing to see for now"'
                ).count()
                if nothing_to_see > 0:
                    continue

                main_list = self.page.locator(
                    ".pvs-list__container, main ul, main ol"
                ).first
                if await main_list.count() == 0:
                    continue

                items = await main_list.locator(".pvs-list__paged-list-item").all()
                if not items:
                    items = await main_list.locator("> li").all()

                seen_titles = set()
                for item in items:
                    try:
                        accomplishment = await self._parse_accomplishment_item(
                            item, category
                        )
                        if accomplishment and accomplishment.title not in seen_titles:
                            seen_titles.add(accomplishment.title)
                            accomplishments.append(accomplishment)
                    except Exception as e:
                        logger.debug(f"Error parsing {category} item: {e}")
                        continue

            except Exception as e:
                logger.debug(f"Error getting {category}s: {e}")
                continue

        return accomplishments

    async def _parse_accomplishment_item(
        self, item, category: str
    ) -> Optional[Accomplishment]:
        try:
            entity = item.locator(
                'div[data-view-name="profile-component-entity"]'
            ).first
            if await entity.count() > 0:
                spans = await entity.locator('span[aria-hidden="true"]').all()
            else:
                spans = await item.locator('span[aria-hidden="true"]').all()

            title = ""
            issuer = ""
            issued_date = ""
            credential_id = ""

            for i, span in enumerate(spans[:5]):
                text = await span.text_content()
                if not text:
                    continue
                text = text.strip()

                if len(text) > 500:
                    continue

                if i == 0:
                    title = text
                elif "Issued by" in text:
                    parts = text.split("·")
                    issuer = parts[0].replace("Issued by", "").strip()
                    if len(parts) > 1:
                        issued_date = parts[1].strip()
                elif "Issued " in text and not issued_date:
                    issued_date = text.replace("Issued ", "")
                elif "Credential ID" in text:
                    credential_id = text.replace("Credential ID ", "")
                elif i == 1 and not issuer:
                    issuer = text
                elif (
                    any(
                        month in text
                        for month in [
                            "Jan",
                            "Feb",
                            "Mar",
                            "Apr",
                            "May",
                            "Jun",
                            "Jul",
                            "Aug",
                            "Sep",
                            "Oct",
                            "Nov",
                            "Dec",
                        ]
                    )
                    and not issued_date
                ):
                    if "·" in text:
                        parts = text.split("·")
                        issued_date = parts[0].strip()
                    else:
                        issued_date = text

            link = item.locator('a[href*="credential"], a[href*="verify"]').first
            credential_url = (
                await link.get_attribute("href") if await link.count() > 0 else None
            )

            if not title or len(title) > 200:
                return None

            return Accomplishment(
                category=category,
                title=title,
                issuer=issuer if issuer else None,
                issued_date=issued_date if issued_date else None,
                credential_id=credential_id if credential_id else None,
                credential_url=credential_url,
            )

        except Exception as e:
            logger.debug(f"Error parsing accomplishment: {e}")
            return None

    async def _get_contacts(self, base_url: str) -> list[Contact]:
        """Extract contact info from the contact-info overlay dialog."""
        contacts = []

        try:
            contact_url = urljoin(base_url, "overlay/contact-info/")
            await self.navigate_and_wait(contact_url)
            await self.wait_and_focus(1)

            dialog = self.page.locator('dialog, [role="dialog"]').first
            if await dialog.count() == 0:
                logger.warning("Contact info dialog not found")
                return contacts

            contact_sections = await dialog.locator('h3').all()
            
            for section_heading in contact_sections:
                try:
                    heading_text = await section_heading.text_content()
                    if not heading_text:
                        continue
                    heading_text = heading_text.strip().lower()
                    
                    section_container = section_heading.locator('xpath=ancestor::*[1]')
                    if await section_container.count() == 0:
                        continue
                    
                    contact_type = self._map_contact_heading_to_type(heading_text)
                    if not contact_type:
                        continue
                    
                    links = await section_container.locator('a').all()
                    for link in links:
                        href = await link.get_attribute('href')
                        text = await link.text_content()
                        if href and text:
                            text = text.strip()
                            label = None
                            sibling_text = await section_container.locator('span, generic').all()
                            for sib in sibling_text:
                                sib_text = await sib.text_content()
                                if sib_text and sib_text.strip().startswith('(') and sib_text.strip().endswith(')'):
                                    label = sib_text.strip()[1:-1]
                                    break
                            
                            if contact_type == "linkedin":
                                contacts.append(Contact(type=contact_type, value=href, label=label))
                            elif contact_type == "email" and "mailto:" in href:
                                contacts.append(Contact(type=contact_type, value=href.replace("mailto:", ""), label=label))
                            else:
                                contacts.append(Contact(type=contact_type, value=text, label=label))
                    
                    if contact_type == "birthday" and not links:
                        birthday_text = await section_container.text_content()
                        if birthday_text:
                            birthday_value = birthday_text.replace(heading_text, "").replace("Birthday", "").strip()
                            if birthday_value:
                                contacts.append(Contact(type="birthday", value=birthday_value))
                    
                    if contact_type == "phone" and not links:
                        phone_text = await section_container.text_content()
                        if phone_text:
                            phone_value = phone_text.replace(heading_text, "").replace("Phone", "").strip()
                            if phone_value:
                                contacts.append(Contact(type="phone", value=phone_value))
                    
                    if contact_type == "address" and not links:
                        address_text = await section_container.text_content()
                        if address_text:
                            address_value = address_text.replace(heading_text, "").replace("Address", "").strip()
                            if address_value:
                                contacts.append(Contact(type="address", value=address_value))
                                
                except Exception as e:
                    logger.debug(f"Error parsing contact section: {e}")
                    continue

        except Exception as e:
            logger.warning(f"Error getting contacts: {e}")

        return contacts
    
    def _map_contact_heading_to_type(self, heading: str) -> Optional[str]:
        """Map contact section heading to contact type."""
        heading = heading.lower()
        if "profile" in heading:
            return "linkedin"
        elif "website" in heading:
            return "website"
        elif "email" in heading:
            return "email"
        elif "phone" in heading:
            return "phone"
        elif "twitter" in heading or "x.com" in heading:
            return "twitter"
        elif "birthday" in heading:
            return "birthday"
        elif "address" in heading:
            return "address"
        return None
