"""Scraper for LinkedIn pending connection invitations."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Literal, Optional
from urllib.parse import unquote, urlparse

from playwright.async_api import Locator, Page

from ..callbacks import ProgressCallback, SilentCallback
from ..core.exceptions import ScrapingError
from ..models.invitation import Invitation, InvitationKind
from .base import BaseScraper

logger = logging.getLogger(__name__)

INVITATIONS_URL = "https://www.linkedin.com/mynetwork/invitation-manager/received/"

_ACCEPT_RE = re.compile(r"\b(accept|accepter)\b", re.IGNORECASE)
_IGNORE_RE = re.compile(r"\b(ignore|ignorer|delete|supprimer)\b", re.IGNORECASE)
_SHARED_RE = re.compile(
    r"(\d+)\s*(relations?\s+en\s+commun|mutual\s+connections?)",
    re.IGNORECASE,
)
_PROFILE_RE = re.compile(r"/in/([^/?#]+)/?", re.IGNORECASE)
_COMPANY_RE = re.compile(r"/company/([^/?#]+)/?", re.IGNORECASE)
_NEWSLETTER_RE = re.compile(r"/newsletters/([^/?#]+)/?", re.IGNORECASE)
# Showcase pages have their own top-level path (/showcase/{slug}/), distinct
# from /company/{slug}/ — a card linking only to a showcase page was
# previously invisible to entity extraction (fell through to "unknown" or was
# skipped for lack of invitation_id).
_SHOWCASE_RE = re.compile(r"/showcase/([^/?#]+)/?", re.IGNORECASE)
# LinkedIn Event invitations (numeric id, no slug) — same gap as showcase.
_EVENT_RE = re.compile(r"/events/(\d+)/?", re.IGNORECASE)
_FOLLOW_RE = re.compile(
    r"(vous\s+a\s+invité(?:\(e\))?\s+à\s+suivre|"
    r"vous\s+invite\s+à\s+suivre|"
    r"invited\s+you\s+to\s+follow)",
    re.IGNORECASE,
)
_FOLLOW_PAGE_RE = re.compile(
    r"(suivre\s+sa\s+page|follow\s+(?:their|his|her|your)\s+page)",
    re.IGNORECASE,
)
_CONNECT_RE = re.compile(
    r"(rejoindre\s+(?:son|votre|leur)\s+réseau|"
    r"invited\s+you\s+to\s+(?:connect|join\s+(?:their|his|her)\s+network))",
    re.IGNORECASE,
)
_NEWSLETTER_HINT_RE = re.compile(r"\bnewsletter\b", re.IGNORECASE)
_FOLLOW_TARGET_RE = re.compile(
    r"(?:vous\s+a\s+invité(?:\(e\))?\s+à\s+suivre|"
    r"vous\s+invite\s+à\s+suivre|"
    r"invited\s+you\s+to\s+follow)\s+(?P<target>.+)",
    re.IGNORECASE,
)
_INVITER_PREFIX_RE = re.compile(
    r"^(?P<name>.+?)\s+(?:vous\s+a\s+invité|vous\s+invite|invited\s+you)\b",
    re.IGNORECASE,
)
_GENERIC_PAGE_TARGET_RE = re.compile(
    r"^(sa|son|leur|your|their|his|her)\s+page$",
    re.IGNORECASE,
)
# LinkedIn UI chrome on invitation cards (not the person's headline)
_UI_CONTEXT_RE = re.compile(
    r"(vous\s+invite\s+à\s+suivre|"
    r"invited\s+you\s+to\s+follow|"
    r"vous\s+a\s+invité|"
    r"parce\s+que\s+vous\s+avez\s+interagi)",
    re.IGNORECASE,
)


@dataclass
class _EntityLink:
    url: str
    name: Optional[str] = None


@dataclass
class _ClassifiedInvitation:
    invitation_id: Optional[str]
    invitation_kind: InvitationKind
    inviter_name: Optional[str]
    inviter_url: Optional[str]
    target_name: Optional[str]
    target_url: Optional[str]
    display_text: Optional[str]
    profile_name: Optional[str]
    profile_url: Optional[str]


def classify_invitation(
    raw_text: str,
    persons: list[_EntityLink],
    companies: list[_EntityLink],
    newsletters: list[_EntityLink],
    showcases: Optional[list[_EntityLink]] = None,
    events: Optional[list[_EntityLink]] = None,
) -> _ClassifiedInvitation:
    """Derive invitation subtype + inviter/target from card text and entity links."""
    showcases = showcases or []
    events = events or []
    display_text = _extract_display_text(raw_text)
    person = _first_named(persons)
    company = _first_named(companies)
    newsletter = _first_named(newsletters)
    showcase = _first_named(showcases)
    event = _first_named(events)
    text_target = _follow_target_from_text(raw_text)

    is_follow = bool(_FOLLOW_RE.search(raw_text) or _FOLLOW_PAGE_RE.search(raw_text))
    is_connect = bool(_CONNECT_RE.search(raw_text))
    is_newsletter = bool(newsletter) or (
        bool(_NEWSLETTER_HINT_RE.search(raw_text)) and not person
    )

    if is_newsletter or newsletter:
        kind: InvitationKind = "follow_newsletter"
        target = newsletter or company
        inviter = person or target
        invitation_id = _id_from_url(target.url if target else None) or _id_from_url(
            person.url if person else None
        )
        target_name = (target.name if target else None) or text_target
        target_url = target.url if target else None
    elif event:
        kind = "event_invitation"
        target = event
        inviter = person or event
        invitation_id = _id_from_url(event.url) or _id_from_url(
            person.url if person else None
        )
        target_name = event.name or text_target
        target_url = event.url
    elif showcase:
        kind = "follow_showcase_page"
        target = showcase
        inviter = person or showcase
        invitation_id = _id_from_url(showcase.url) or _id_from_url(
            person.url if person else None
        )
        target_name = showcase.name or text_target
        target_url = showcase.url
    elif company and (is_follow or not person):
        kind = "follow_company"
        inviter = person or company
        target = company
        invitation_id = _id_from_url(company.url) or _id_from_url(
            person.url if person else None
        )
        target_name = company.name or text_target
        target_url = company.url
    elif is_follow and person and not company:
        if _FOLLOW_PAGE_RE.search(raw_text):
            kind = "follow_company"
        else:
            kind = "follow_person"
        inviter = person
        target = person if kind == "follow_person" else None
        invitation_id = _id_from_url(person.url)
        target_name = (target.name if target else None) or text_target
        target_url = target.url if target else None
    elif person and (is_connect or not is_follow):
        kind = "connection"
        inviter = person
        target = person
        invitation_id = _id_from_url(person.url)
        target_name = person.name
        target_url = person.url
    elif company:
        kind = "follow_company"
        inviter = company
        target = company
        invitation_id = _id_from_url(company.url)
        target_name = company.name
        target_url = company.url
    else:
        kind = "unknown"
        inviter = person
        target = person or company
        invitation_id = _id_from_url(person.url if person else None) or _id_from_url(
            company.url if company else None
        )
        target_name = target.name if target else text_target
        target_url = target.url if target else None

    inviter_name = inviter.name if inviter else _inviter_from_text(raw_text)
    inviter_url = inviter.url if inviter else None

    # Retrocompat: profile_* stay on the person when present (inviter), else the target.
    if person:
        profile_name, profile_url = person.name, person.url
    elif company:
        profile_name, profile_url = company.name, company.url
    elif newsletter:
        profile_name, profile_url = newsletter.name, newsletter.url
    else:
        profile_name, profile_url = inviter_name, inviter_url

    if not invitation_id:
        invitation_id = _slugify_name(target_name or inviter_name or profile_name)

    return _ClassifiedInvitation(
        invitation_id=invitation_id,
        invitation_kind=kind,
        inviter_name=inviter_name,
        inviter_url=inviter_url,
        target_name=target_name,
        target_url=target_url,
        display_text=display_text,
        profile_name=profile_name,
        profile_url=profile_url,
    )


def _first_named(links: list[_EntityLink]) -> Optional[_EntityLink]:
    for link in links:
        if link.name:
            return link
    return links[0] if links else None


def _extract_display_text(raw_text: str) -> Optional[str]:
    for line in raw_text.splitlines():
        collapsed = " ".join(line.split())
        if not collapsed:
            continue
        if _FOLLOW_RE.search(collapsed) or _CONNECT_RE.search(collapsed):
            collapsed = re.split(
                r"\s+parce\s+que\s+|\s+because\s+",
                collapsed,
                maxsplit=1,
                flags=re.IGNORECASE,
            )[0].strip()
            return collapsed or None
    return None


def _follow_target_from_text(raw_text: str) -> Optional[str]:
    m = _FOLLOW_TARGET_RE.search(" ".join(raw_text.split()))
    if not m:
        return None
    target = m.group("target").strip()
    target = re.split(r"\s+parce\s+que\s+|\s+because\s+", target, maxsplit=1, flags=re.I)[
        0
    ].strip()
    if not target or _GENERIC_PAGE_TARGET_RE.match(target):
        return None
    return target


def _inviter_from_text(raw_text: str) -> Optional[str]:
    for line in raw_text.splitlines():
        collapsed = " ".join(line.split())
        m = _INVITER_PREFIX_RE.match(collapsed)
        if m:
            return m.group("name").strip() or None
    return None


def _slugify_name(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    slug = re.sub(r"\s+", "-", name.strip().lower())
    slug = re.sub(r"[^a-z0-9\-._]", "", slug)
    return slug or None


def _id_from_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    path = urlparse(url).path
    m = (
        _PROFILE_RE.search(path)
        or _COMPANY_RE.search(path)
        or _NEWSLETTER_RE.search(path)
        or _SHOWCASE_RE.search(path)
        or _EVENT_RE.search(path)
    )
    return unquote(m.group(1)) if m else None


def _normalize_href(href: str) -> str:
    if href.startswith("/"):
        href = f"https://www.linkedin.com{href}"
    return href.split("?")[0]


def _clean_link_name(text: str) -> Optional[str]:
    collapsed = " ".join((text or "").split()).strip()
    if not collapsed:
        return None
    if _FOLLOW_RE.search(collapsed) or _CONNECT_RE.search(collapsed):
        m = _INVITER_PREFIX_RE.match(collapsed)
        return m.group("name").strip() if m else None
    return collapsed


class InvitationScraper(BaseScraper):
    """Scrape and act on pending LinkedIn invitations (received)."""

    def __init__(self, page: Page, callback: Optional[ProgressCallback] = None):
        super().__init__(page, callback or SilentCallback())

    async def list_pending(self, limit: int = 20) -> list[Invitation]:
        """List pending received invitations.

        Args:
            limit: Max invitations to return.

        Returns:
            List of Invitation objects.
        """
        logger.info("Listing pending invitations (limit=%s)", limit)
        await self.callback.on_start("Invitations", INVITATIONS_URL)
        await self._open_invitations_page()
        await self.ensure_logged_in()
        await self.check_rate_limit()
        await self._scroll_to_load_invitations(target=limit)
        invitations = await self._extract_invitations(limit=limit)
        await self.callback.on_complete("Invitations", invitations)
        return invitations

    async def accept(self, invitation_id: str) -> bool:
        """Accept a pending invitation by id (profile/company slug or data-id)."""
        return await self._act_on_invitation(invitation_id, "accept")

    async def ignore(self, invitation_id: str) -> bool:
        """Ignore/decline a pending invitation by id."""
        return await self._act_on_invitation(invitation_id, "ignore")

    async def _open_invitations_page(self) -> None:
        if "/mynetwork/invitation-manager" not in (self.page.url or ""):
            await self.navigate_and_wait(INVITATIONS_URL)
        await self.page.wait_for_timeout(1500)
        await self.check_rate_limit()

    async def _scroll_to_load_invitations(self, target: int = 20, max_rounds: int = 25) -> None:
        """Scroll the main content pane so LinkedIn lazy-loads invitation cards.

        Window-level End is not enough: invitations load inside ``main``.
        """
        previous = -1
        stable = 0
        for _ in range(max_rounds):
            count = await self.page.evaluate(
                """() => [...document.querySelectorAll('button')].filter(b => {
                    const label = ((b.getAttribute('aria-label') || '') + ' ' + (b.innerText || '')).toLowerCase();
                    return /\\b(accept|accepter)\\b/.test(label);
                }).length"""
            )
            if count >= target:
                break
            if count == previous:
                stable += 1
                if stable >= 3:
                    break
            else:
                stable = 0
            previous = count
            await self.page.evaluate(
                """() => {
                    const main = document.querySelector('main');
                    if (main) {
                        main.scrollTop = main.scrollHeight;
                    } else {
                        window.scrollTo(0, document.body.scrollHeight);
                    }
                }"""
            )
            await self.page.wait_for_timeout(500)
        await self.check_rate_limit()

    async def _extract_invitations(self, limit: int = 20) -> list[Invitation]:
        cards = self.page.locator('[role="listitem"]')
        count = await cards.count()
        results: list[Invitation] = []

        for i in range(count):
            if len(results) >= limit:
                break
            card = cards.nth(i)
            if not await self._card_has_actions(card):
                continue
            invitation = await self._parse_card(card)
            if invitation is not None:
                results.append(invitation)

        logger.info("Extracted %s invitations from %s listitems", len(results), count)
        return results

    async def _card_has_actions(self, card: Locator) -> bool:
        buttons = card.locator("button")
        n = await buttons.count()
        for i in range(n):
            label = await self._button_label(buttons.nth(i))
            if _ACCEPT_RE.search(label) or _IGNORE_RE.search(label):
                return True
        return False

    async def _parse_card(self, card: Locator) -> Optional[Invitation]:
        raw_text = (await card.inner_text() or "").strip()
        persons, companies, newsletters, showcases, events = await self._extract_entity_links(
            card
        )
        classified = classify_invitation(
            raw_text, persons, companies, newsletters, showcases, events
        )

        data_id = await card.get_attribute("data-invitation-id")
        invitation_id = data_id or classified.invitation_id
        if not invitation_id:
            logger.debug("Skipping card without invitation_id: %r", raw_text[:80])
            return None

        skip_names = {
            n
            for n in (
                classified.profile_name,
                classified.inviter_name,
                classified.target_name,
                classified.display_text,
            )
            if n
        }
        headline = self._extract_headline(raw_text, classified.profile_name, skip_names)
        shared = self._extract_shared_count(raw_text)
        message = await self._extract_message(
            card, raw_text, classified.profile_name, headline
        )

        return Invitation(
            invitation_id=invitation_id,
            profile_name=classified.profile_name,
            profile_url=classified.profile_url,
            headline=headline,
            shared_connection_count=shared,
            message=message,
            invitation_kind=classified.invitation_kind,
            inviter_name=classified.inviter_name,
            inviter_url=classified.inviter_url,
            target_name=classified.target_name,
            target_url=classified.target_url,
            display_text=classified.display_text,
            raw_card_text=raw_text,
        )

    async def _extract_entity_links(
        self, card: Locator
    ) -> tuple[
        list[_EntityLink],
        list[_EntityLink],
        list[_EntityLink],
        list[_EntityLink],
        list[_EntityLink],
    ]:
        links = card.locator(
            'a[href*="/in/"], a[href*="/company/"], a[href*="/newsletters/"], '
            'a[href*="/showcase/"], a[href*="/events/"]'
        )
        persons: list[_EntityLink] = []
        companies: list[_EntityLink] = []
        newsletters: list[_EntityLink] = []
        showcases: list[_EntityLink] = []
        events: list[_EntityLink] = []
        n = await links.count()
        for i in range(n):
            link = links.nth(i)
            href = await link.get_attribute("href")
            if not href:
                continue
            url = _normalize_href(href)
            name = _clean_link_name(await link.inner_text() or "")
            path = urlparse(url).path
            if _NEWSLETTER_RE.search(path):
                self._merge_entity(newsletters, url, name)
            elif _EVENT_RE.search(path):
                self._merge_entity(events, url, name)
            elif _SHOWCASE_RE.search(path):
                self._merge_entity(showcases, url, name)
            elif _COMPANY_RE.search(path):
                self._merge_entity(companies, url, name)
            elif _PROFILE_RE.search(path):
                self._merge_entity(persons, url, name)
        return persons, companies, newsletters, showcases, events

    @staticmethod
    def _merge_entity(bucket: list[_EntityLink], url: str, name: Optional[str]) -> None:
        for existing in bucket:
            if existing.url == url:
                if name and not existing.name:
                    existing.name = name
                elif (
                    name
                    and existing.name
                    and len(name) < len(existing.name)
                    and not _FOLLOW_RE.search(name)
                    and not _CONNECT_RE.search(name)
                ):
                    existing.name = name
                return
        bucket.append(_EntityLink(url=url, name=name))

    @staticmethod
    def _id_from_url(url: Optional[str]) -> Optional[str]:
        return _id_from_url(url)

    @staticmethod
    def _slugify_name(name: Optional[str]) -> Optional[str]:
        return _slugify_name(name)

    @staticmethod
    def _extract_headline(
        raw_text: str,
        profile_name: Optional[str],
        extra_skip: Optional[set[str]] = None,
    ) -> Optional[str]:
        lines = [ln.strip() for ln in raw_text.splitlines() if ln.strip()]
        # Drop duplicated name lines and action labels
        skip = {
            "ignorer",
            "accepter",
            "ignore",
            "accept",
            "… voir plus",
            "... see more",
            "voir plus",
            "see more",
        }
        if extra_skip:
            skip |= {s.strip().lower() for s in extra_skip if s}
        candidates: list[str] = []
        for line in lines:
            low = line.lower()
            if low in skip:
                continue
            if profile_name and (
                line == profile_name or line.startswith(f"{profile_name} ")
            ):
                # Skip name + LinkedIn UI context glued on the same line
                if line == profile_name or _UI_CONTEXT_RE.search(line):
                    continue
            if _SHARED_RE.search(line):
                continue
            if _ACCEPT_RE.search(line) or _IGNORE_RE.search(line):
                continue
            if _UI_CONTEXT_RE.search(line):
                continue
            if _NEWSLETTER_HINT_RE.search(line) and "•" in line:
                continue
            candidates.append(line)
        if not candidates:
            return None
        # First remaining line is typically the headline
        return candidates[0]

    @staticmethod
    def _extract_shared_count(raw_text: str) -> Optional[int]:
        m = _SHARED_RE.search(raw_text)
        return int(m.group(1)) if m else None

    async def _extract_message(
        self,
        card: Locator,
        raw_text: str,
        profile_name: Optional[str],
        headline: Optional[str],
    ) -> Optional[str]:
        box = card.locator('[data-testid="expandable-text-box"]')
        if await box.count() > 0:
            text = (await box.first.inner_text() or "").strip()
            text = re.sub(r"\s*[….]+\s*(voir plus|see more)\s*$", "", text, flags=re.I)
            if text:
                return text
        # Do not guess a message from headline/UI lines — only explicit intro notes.
        return None

    def _find_action_button(
        self, card: Locator, action: Literal["accept", "ignore"]
    ) -> Locator:
        # Match accessible name (aria-label) or visible text — bilingual.
        if action == "accept":
            return card.get_by_role(
                "button", name=re.compile(r"accept|accepter", re.IGNORECASE)
            )
        return card.get_by_role(
            "button",
            name=re.compile(r"ignore|ignorer|delete|supprimer", re.IGNORECASE),
        )

    async def _button_label(self, button: Locator) -> str:
        aria = await button.get_attribute("aria-label")
        text = (await button.inner_text() or "").strip()
        return f"{aria or ''} {text}".strip()

    async def _find_card_by_id(self, invitation_id: str) -> Optional[Locator]:
        cards = self.page.locator('[role="listitem"]')
        count = await cards.count()
        needle = invitation_id.strip().lower()
        for i in range(count):
            card = cards.nth(i)
            if not await self._card_has_actions(card):
                continue
            data_id = (await card.get_attribute("data-invitation-id") or "").lower()
            if data_id == needle:
                return card
            hrefs = card.locator(
                'a[href*="/in/"], a[href*="/company/"], a[href*="/newsletters/"], '
                'a[href*="/showcase/"], a[href*="/events/"]'
            )
            n = await hrefs.count()
            for j in range(n):
                href = (await hrefs.nth(j).get_attribute("href") or "").lower()
                if (
                    f"/in/{needle}" in href
                    or f"/company/{needle}" in href
                    or f"/newsletters/{needle}" in href
                    or f"/showcase/{needle}" in href
                    or f"/events/{needle}" in href
                ):
                    return card
            # Fallback: slugified visible name / classified id
            parsed = await self._parse_card(card)
            if parsed and parsed.invitation_id.lower() == needle:
                return card
        return None

    async def _act_on_invitation(
        self, invitation_id: str, action: Literal["accept", "ignore"]
    ) -> bool:
        if not invitation_id or not invitation_id.strip():
            raise ScrapingError("invitation_id is required")

        logger.info("%s invitation %s", action, invitation_id)
        await self._open_invitations_page()
        await self.ensure_logged_in()
        await self.check_rate_limit()

        card = await self._find_card_by_id(invitation_id)
        if card is None:
            logger.warning("Invitation not found: %s", invitation_id)
            return False

        button = self._find_action_button(card, action)
        if await button.count() == 0:
            # Fallback: aria-label scan
            buttons = card.locator("button")
            n = await buttons.count()
            button = None
            pattern = _ACCEPT_RE if action == "accept" else _IGNORE_RE
            for i in range(n):
                candidate = buttons.nth(i)
                label = await self._button_label(candidate)
                if pattern.search(label):
                    button = candidate
                    break
            if button is None:
                logger.warning("No %s button for invitation %s", action, invitation_id)
                return False

        await button.first.click()
        await self.page.wait_for_timeout(1200)
        await self.check_rate_limit()

        # Success if card no longer found or no longer has actions
        remaining = await self._find_card_by_id(invitation_id)
        if remaining is None:
            return True
        if not await self._card_has_actions(remaining):
            return True
        # Card may still be animating; treat click without error as success
        return True
