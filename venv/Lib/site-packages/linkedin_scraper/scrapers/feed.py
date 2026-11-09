import logging
import re
from urllib.parse import urlparse
from typing import Any, Dict, List, Optional, Set, Tuple
from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from ..models.post import Post
from ..callbacks import ProgressCallback, SilentCallback
from ..core import get_cached_permalink, save_cached_permalink
from ..core.rate_limit_guard import check_cooldown
from .base import BaseScraper

logger = logging.getLogger(__name__)

# Hard cap on how many cards' overflow menus a single _fill_missing_permalinks_from_ui
# call will open. Each miss costs several clicks + clipboard reads against a live
# LinkedIn session; without a cap, a scroll batch with many uncached cards could
# fire dozens of rapid UI interactions in one call — the interaction pattern that
# triggered the 2026-07-22 rate limit. Cards beyond the cap keep whatever
# _finalize_linkedin_url can derive without UI interaction and get resolved on a
# later call once the cache is warm.
_MAX_UI_FALLBACK_PER_CALL = 4

# Plafond de clics d'ouverture de fils de commentaires pour UN scrape entier.
# Il valait auparavant par appel de _expand_visible_comments_for_url_scrape(),
# donc par passe d'extraction — or _scrape_posts() rappelle l'extraction à chaque
# itération de scroll (jusqu'à 3·limit + 10 fois). Le plafond ne plafonnait donc
# pas ce qu'on croyait : mesuré en prod le 2026-09-04, « 4 » puis « 6 » clics pour
# un seul scrape_feed. Même motivation que _MAX_UI_FALLBACK_PER_CALL : limiter le
# signal d'automatisation envoyé à LinkedIn (rate limit du 2026-07-22, checkpoint
# du 2026-09-04).
_MAX_COMMENT_EXPAND_CLICKS_PER_SCRAPE = 8

FEED_URL = "https://www.linkedin.com/feed/"

# Identifiant numérique d'un post dans un permalien LinkedIn.
# Deux formes rencontrées :
#   /posts/{author}_{slug}-share-7500824506219909120-mYgS/
#   /posts/{author}_{slug}-ugcPost-7500865995637538816-ayfc/
#   /feed/update/urn:li:activity:7500989467147542528/
_POST_URL_SLUG_ID_RE = re.compile(r"-(?:share|ugcPost|activity)-(\d{16,})(?:-|/|$)", re.I)
# La forme URN est explicite : pas de garde de longueur (contrairement au slug,
# où un id court serait un faux positif sur un numéro quelconque du slug).
_POST_URL_URN_ID_RE = re.compile(r"urn:li:(?:activity|ugcPost|share):(\d+)", re.I)

# Lignes de la carte de commentaire qui ne font pas partie du texte du commentaire.
_COMMENT_ACTION_LINES = {
    "j'aime", "j’aime", "like", "répondre", "repondre", "reply",
    "voir plus", "see more", "… plus", "...plus", "plus",
    "afficher la traduction", "show translation",
    "1 réponse", "1 reply", "modifié", "edited", "auteur", "author",
}

# Texte du menu overflow « Copier le lien » (FR/EN) — LinkedIn peut rendre un button sans role=menuitem
_COPY_LINK_MENU_TEXT_RE = re.compile(
    r"Copier le lien vers le post|Copier le lien|Copy link to post|Copy link\b",
    re.I,
)

# Wait for at least one Republier/Repost action button (text or aria-label; LinkedIn icon-only UI)
_WAIT_FOR_FEED_JS = (
    "() => Array.from(document.querySelectorAll('button'))"
    ".some(function(b){"
    "var t=(b.innerText||'').trim();"
    "if(t==='Republier'||t==='Repost')return true;"
    "var a=(b.getAttribute('aria-label')||'').trim();"
    "return /^Republier\\b/i.test(a)||/^Repost\\b/i.test(a);"
    "})"
)


# Contrôles à cliquer pour révéler les fils de commentaires. Vérifié en live le
# 2026-09-03 : le compteur « N commentaires » est un div[role="button"] SANS
# aria-label — l'ancien matcher, qui n'acceptait que
# button[aria-label*="commentaire"], ne trouvait plus rien, donc `comments` et
# `top_comment` restaient vides sauf sur les cartes dont LinkedIn pré-affiche un
# commentaire. Un clic JS déclenche bien React (mesuré : 3 → 18 commentaires).
#
# Exclusions volontaires : le composeur (« Commenter », « Écrire un
# commentaire ») ouvre une zone de saisie et non le fil ; les menus d'options
# (« Voir plus d'options pour le commentaire de X ») contiennent aussi le mot
# « commentaire » mais ouvrent un dropdown.
_EXPAND_COMMENTS_JS = r"""
(maxClicks) => {
  var countRe = /(\d[\d\s.,]*k?)\s*(commentaires?|comments?)/i;
  var moreRe = /^(voir|afficher|load|show)\s.*(commentaires?|comments?)/i;
  var composerRe = /^(commenter|comment|écrire un commentaire|write a comment|add a comment)$/i;
  var excludeRe = /option|menu|réagir|react|répondre|reply/i;
  function isRepostBtn(b) {
    var t = (b.innerText || "").trim();
    if (t === "Republier" || t === "Repost") return true;
    var a = (b.getAttribute("aria-label") || "").trim();
    return /^Republier\b/i.test(a) || /^Repost\b/i.test(a);
  }
  // Carte du post contenant ce contrôle : le premier ancêtre qui porte un bouton
  // Republier (même repère que l'extraction des posts).
  function cardOf(el) {
    var n = el;
    for (var d = 0; d < 15 && n && n !== document.body; d++) {
      if (n.querySelectorAll && Array.from(n.querySelectorAll("button")).some(isRepostBtn)) {
        return n;
      }
      n = n.parentElement;
    }
    return null;
  }
  function alreadyExpanded(el) {
    var card = cardOf(el);
    return !!(card && card.querySelector('[componentkey^="replaceableComment_"], .comments-comment-item'));
  }

  var ctrls = Array.from(document.querySelectorAll('button, [role="button"]'));
  var clicked = 0;
  for (var i = 0; i < ctrls.length && clicked < maxClicks; i++) {
    var el = ctrls[i];
    if (!(el.offsetParent || el.getClientRects().length)) continue;
    var aria = (el.getAttribute("aria-label") || "").trim();
    var lines = (el.innerText || "").split("\n").map(function (l) {
      return l.trim();
    }).filter(Boolean);
    var label = lines.length ? lines[0] : "";
    if (composerRe.test(label) || composerRe.test(aria)) continue;
    // Le compteur reste affiché une fois le fil ouvert : sans cette garde, chaque
    // passe d'extraction recliquait les mêmes fils.
    if (alreadyExpanded(el)) continue;
    if (excludeRe.test(aria) || excludeRe.test(label)) continue;
    if (!(countRe.test(label) || moreRe.test(label) || countRe.test(aria) || moreRe.test(aria))) continue;
    try {
      el.click();
      clicked++;
    } catch (e) {}
  }
  return clicked;
}
"""

def _post_keys(post: Post) -> Set[str]:
    """Identifiants sous lesquels une carte a pu être rendue.

    Une même carte peut sortir avec son compkey tant que son permalien n'est
    pas résolu, puis avec son URN d'activité au défilement suivant : la
    mémoire « déjà rendu » d'une session de fil doit couvrir les trois."""
    return {k for k in (post.urn, post.feed_compkey, post.linkedin_url) if k}


class FeedScraper(BaseScraper):

    def __init__(self, page: Page, callback: Optional[ProgressCallback] = None):
        super().__init__(page, callback or SilentCallback())
        self._comment_expand_budget = _MAX_COMMENT_EXPAND_CLICKS_PER_SCRAPE
        # Session de fil (R56) : clés des posts déjà rendus par `scrape_next`.
        self._returned_keys: Set[str] = set()
        # Cartes déjà passées par le repli UI des permaliens, réussi ou non.
        self._ui_fallback_attempted: Set[str] = set()

    def _reset_comment_expand_budget(self) -> None:
        """Rend son budget de clics d'ouverture de commentaires à un nouveau scrape."""
        self._comment_expand_budget = _MAX_COMMENT_EXPAND_CLICKS_PER_SCRAPE

    async def open_feed(self) -> bool:
        """Charge le fil et attend qu'au moins un post soit rendu.

        Partagé par `scrape()` et par l'ouverture d'une session de fil
        (`linkedin-mcp`, R55). Rend `False` si le fil ne s'est pas chargé."""
        await self.navigate_and_wait(FEED_URL)
        await self.callback.on_progress("Navigated to feed", 10)
        await self.ensure_logged_in()

        await self.check_rate_limit()

        # Bring browser to front and scroll to trigger lazy-loading
        try:
            await self.page.bring_to_front()
        except Exception:
            pass
        await self.page.wait_for_timeout(3000)
        await self.page.evaluate("window.scrollBy(0, 1200)")
        await self.page.wait_for_timeout(2000)

        # Wait for at least one post's action button to appear
        try:
            await self.page.wait_for_function(_WAIT_FOR_FEED_JS, timeout=40000)
        except PlaywrightTimeoutError:
            diag = await self.page.evaluate(r"""() => ({
                url: location.href,
                title: document.title,
                buttons: Array.from(document.querySelectorAll("button"))
                    .map(function(b){ return (b.innerText||"").trim(); })
                    .filter(function(t){ return t.length > 0; })
                    .slice(0, 10),
            })""")
            logger.warning(
                "Feed posts not loaded after 40s. url=%s title=%s buttons=%s",
                diag.get("url"),
                diag.get("title"),
                diag.get("buttons"),
            )
            return False

        await self.page.wait_for_timeout(2000)
        await self.callback.on_progress("Feed loaded", 20)
        return True

    async def scrape(self, limit: int = 10) -> List[Post]:
        logger.info(f"Starting feed scraping (limit={limit})")
        self._reset_comment_expand_budget()
        await self.callback.on_start("feed", FEED_URL)

        if not await self.open_feed():
            return []

        posts = await self._scrape_posts(limit)
        posts = await self._enrich_missing_comments_from_post_page(posts)
        await self.callback.on_progress(f"Scraped {len(posts)} posts", 100)
        await self.callback.on_complete("feed", posts)

        logger.info(f"Successfully scraped {len(posts)} posts from feed")
        return posts

    async def scrape_next(self, limit: int = 10) -> Tuple[List[Post], bool]:
        """Suite d'une session de fil (R56) : défile depuis la position
        courante, sans jamais naviguer, et rend au plus `limit` posts jamais
        rendus par cette instance.

        Ni `goto`, ni enrichissement des commentaires par page de post : ce
        dernier recharge le fil au retour et ferait perdre la position.
        Rend `(posts, exhausted)` ; `exhausted` vaut vrai quand le plafond de
        défilements est atteint avant `limit` posts."""
        # Un onglet d'arrière-plan est gelé par Chrome (af51454) : sans premier
        # plan, ni le défilement ni le lazy-loading ne répondent.
        try:
            await self.page.bring_to_front()
        except Exception:
            pass
        check_cooldown()
        await self.check_rate_limit()
        if "linkedin.com/feed" not in (getattr(self.page, "url", "") or ""):
            # LinkedIn a pu rediriger l'onglet de session (login, challenge).
            await self.ensure_logged_in()
        self._reset_comment_expand_budget()
        posts, exhausted = await self._collect_posts(limit, self._returned_keys)
        if exhausted:
            # Une page de challenge apparue en cours de défilement ressemble à
            # un fil vide : la reconnaître plutôt que conclure à l'épuisement.
            await self.check_rate_limit()
        for post in posts:
            self._returned_keys |= _post_keys(post)
        logger.info(f"Session de fil : {len(posts)} post(s) rendu(s), épuisé={exhausted}")
        return posts, exhausted

    async def scrape_post_by_url(self, post_url: str) -> List[Post]:
        """Scrape un post LinkedIn précis depuis son URL /feed/update/ ou /posts/."""
        logger.info("Starting single post scraping: %s", post_url)
        self._reset_comment_expand_budget()
        await self.callback.on_start("feed_single_post", post_url)
        await self.navigate_and_wait(post_url)
        await self.ensure_logged_in()
        await self.check_rate_limit()
        await self.page.wait_for_timeout(1200)
        posts = await self._extract_posts_from_feed()
        posts = await self._enrich_missing_comments_from_post_page(posts)
        if not posts:
            logger.warning("No post extracted from detail URL: %s", post_url)
            return []

        post = posts[0]
        await self._correct_author_on_detail_page(post, post_url)
        return [post]

    async def _correct_author_on_detail_page(self, post: Post, post_url: str) -> None:
        """Sur page détail, l'auteur peut être confondu avec le compte de session (navbar)."""
        if not self._is_post_detail_url(post_url):
            return

        card_author = await self._extract_author_from_post_card()
        session_user = await self._get_session_user_profile()

        if card_author.get("name") and card_author.get("url"):
            if (
                post.author_name != card_author["name"]
                or post.author_url != card_author["url"]
            ):
                logger.warning(
                    "Author corrected on detail page (%s): %r -> %r",
                    card_author.get("source", "post_card"),
                    post.author_name,
                    card_author["name"],
                )
            post.author_name = card_author["name"]
            post.author_url = card_author["url"]
            return

        if session_user.get("name") and post.author_name == session_user["name"]:
            logger.warning(
                "Author on detail page matches logged-in session user %r; "
                "could not resolve post author from actor block (url=%s)",
                post.author_name,
                post_url,
            )
            post.author_name = None
            post.author_url = None
        elif not post.author_name:
            logger.warning(
                "Could not extract author on detail page (url=%s)",
                post_url,
            )

    @staticmethod
    def _is_post_detail_url(url: str) -> bool:
        path = urlparse(url).path.lower()
        return "/posts/" in path or "/feed/update/" in path

    async def _get_session_user_profile(self) -> Dict[str, Optional[str]]:
        """Profil affiché dans la barre de navigation (utilisateur connecté)."""
        data = await self.page.evaluate("""() => {
            function profileFromNavLink(a) {
                if (!a) return null;
                var href = a.getAttribute("href") || "";
                var m = href.match(/[/]in[/][^/?#]+/);
                if (!m) return null;
                var nameSpan = a.querySelector("span[aria-hidden='true']");
                var nameSpanText = nameSpan ? (nameSpan.innerText || "").trim() : "";
                var name = nameSpanText || (a.innerText || "").trim().split("\\n")[0].trim();
                if (!name || name.length < 2) return null;
                return { name: name, url: "https://www.linkedin.com" + m[0] };
            }
            var nav = document.querySelector("#global-nav, header.global-nav");
            if (!nav) return { name: null, url: null };
            var meLink = nav.querySelector(
                'a[href*="/in/"][data-view-name="identity-profile-photo"], ' +
                'a[href*="/in/"].global-nav__me-photo, ' +
                'a[href*="/in/"]'
            );
            var prof = profileFromNavLink(meLink);
            return prof || { name: null, url: null };
        }""")
        if not isinstance(data, dict):
            return {"name": None, "url": None}
        return {
            "name": data.get("name") or None,
            "url": data.get("url") or None,
        }

    async def _extract_author_from_post_card(self) -> Dict[str, Optional[str]]:
        """Extrait auteur depuis le bloc acteur du post (hors chrome global)."""
        data = await self.page.evaluate("""() => {
            function isGlobalChrome(node) {
                if (!node) return false;
                return !!node.closest(
                    '#global-nav, header.global-nav, nav, ' +
                    '[data-view-name="navigation"], .scaffold-layout-toolbar, ' +
                    '.msg-overlay-list-bubble, .msg-overlay-bubble-header, ' +
                    '.profile-rail-card, .scaffold-layout__aside--right, ' +
                    '.comments-comment-item, .comments-comments-list'
                );
            }
            function profileFromAnchor(a) {
                if (!a || isGlobalChrome(a)) return null;
                var href = a.getAttribute("href") || "";
                var m = href.match(/[/]in[/][^/?#]+/) || href.match(/[/]company[/][^/?#]+/);
                if (!m) return null;
                var nameSpan = a.querySelector("span[aria-hidden='true']");
                var nameSpanText = nameSpan ? (nameSpan.innerText || "").trim() : "";
                var rawText = nameSpanText || (a.innerText || "").trim();
                var candidate = rawText.split("\\n")[0].trim()
                    .replace(/\\s*[^\\w\\s]\\s*(\\d+e(\\s+et\\s+\\+)?|Suivi|Following)[\\s\\S]*$/, "")
                    .trim();
                if (candidate.length < 2) return null;
                return {
                    name: candidate,
                    url: "https://www.linkedin.com" + m[0],
                };
            }
            function extractFromActor(actor, source) {
                if (!actor || isGlobalChrome(actor)) return null;
                var links = actor.querySelectorAll("a[href*='/in/'], a[href*='/company/']");
                for (var i = 0; i < links.length; i++) {
                    var prof = profileFromAnchor(links[i]);
                    if (prof) return { name: prof.name, url: prof.url, source: source };
                }
                var nameEl = actor.querySelector(
                    '.feed-shared-actor__name, .update-components-actor__name, ' +
                    '.feed-shared-actor__title, .update-components-actor__title, ' +
                    '.feed-shared-actor__meta-link, .update-components-actor__meta-link'
                );
                if (nameEl) {
                    var nm = (nameEl.innerText || "").trim().split("\\n")[0].trim();
                    if (nm.length >= 2) {
                        var linkInActor = actor.querySelector("a[href*='/in/'], a[href*='/company/']");
                        var url = null;
                        if (linkInActor) {
                            var hm = (linkInActor.getAttribute("href") || "")
                                .match(/[/](in|company)[/][^/?#]+/);
                            if (hm) url = "https://www.linkedin.com" + hm[0];
                        }
                        return { name: nm, url: url, source: source + "_text" };
                    }
                }
                return null;
            }

            var postRoots = document.querySelectorAll(
                '.feed-shared-update-v2, .feed-shared-update, ' +
                'div[data-urn*="urn:li:activity"], div[data-urn*="urn:li:ugcPost"]'
            );
            for (var pr = 0; pr < postRoots.length; pr++) {
                var root = postRoots[pr];
                if (isGlobalChrome(root)) continue;
                var actors = root.querySelectorAll(
                    '.feed-shared-actor, .update-components-actor, ' +
                    '[class*="feed-shared-actor__container"], [class*="update-components-actor__container"]'
                );
                for (var ai = 0; ai < actors.length; ai++) {
                    var hit = extractFromActor(actors[ai], "post_card_actor");
                    if (hit) return hit;
                }
            }

            var pageActors = document.querySelectorAll(
                '.feed-shared-actor, .update-components-actor, ' +
                '[class*="feed-shared-actor__container"], [class*="update-components-actor__container"]'
            );
            for (var pa = 0; pa < pageActors.length; pa++) {
                var hit2 = extractFromActor(pageActors[pa], "page_actor_fallback");
                if (hit2) return hit2;
            }

            // LinkedIn a migré certaines pages /posts/... vers un CSS atomisé
            // (classes hashées, plus de .feed-shared-actor / data-view-name).
            // Repli class-agnostic : l'ancre de l'acteur du post affiche le
            // degré de connexion ("Nom • Suivi", "Nom • 1er", "Nom • 3e et +"),
            // un motif stable indépendant des classes CSS.
            var degreeRe = /\\u2022\\s*(Suivi|Following|\\d+e(\\s+et\\s+\\+)?|1er|1st|2nd|3rd)/i;
            var candidateLinks = document.querySelectorAll("a[href*='/in/'], a[href*='/company/']");
            for (var ci = 0; ci < candidateLinks.length; ci++) {
                var candidate = candidateLinks[ci];
                if (isGlobalChrome(candidate)) continue;
                var nameSpan = candidate.querySelector("span[aria-hidden='true']");
                var nameSpanText = nameSpan ? (nameSpan.innerText || "").trim() : "";
                var rawCandidateText = nameSpanText || (candidate.innerText || "").trim();
                if (!degreeRe.test(rawCandidateText)) continue;
                var degreeProf = profileFromAnchor(candidate);
                if (degreeProf) {
                    return { name: degreeProf.name, url: degreeProf.url, source: "degree_suffix_anchor" };
                }
            }

            return { name: null, url: null, source: "none" };
        }""")
        if not isinstance(data, dict):
            return {"name": None, "url": None, "source": "none"}
        return {
            "name": data.get("name") or None,
            "url": data.get("url") or None,
            "source": data.get("source") or "none",
        }

    async def _scrape_posts(self, limit: int) -> List[Post]:
        posts, _ = await self._collect_posts(limit, set())
        return posts

    async def _collect_posts(self, limit: int, exclude: Set[str]) -> Tuple[List[Post], bool]:
        posts: List[Post] = []
        seen: Set[str] = set()
        scroll_attempts = 0
        max_scrolls = limit * 3 + 10

        while len(posts) < limit and scroll_attempts < max_scrolls:
            new_posts = await self._extract_posts_from_feed()

            for post in new_posts:
                if not post.urn:
                    continue
                keys = _post_keys(post)
                # R56 — une carte compkey non résolue à une passe peut sortir
                # sous son URN d'activité à la suivante, dans le même lot.
                if keys & (exclude | seen):
                    continue
                posts.append(post)
                seen |= keys
                if len(posts) >= limit:
                    break

            if len(posts) < limit:
                await self._scroll_for_more_posts()
                scroll_attempts += 1

        return posts[:limit], len(posts) < limit

    async def _extract_posts_from_feed(self) -> List[Post]:
        await self._expand_visible_comments_for_url_scrape()
        posts_data = await self.page.evaluate("""() => {
            function isRepostButton(b) {
                if (!b) return false;
                var t = (b.innerText || "").trim();
                if (t === "Republier" || t === "Repost") return true;
                var aria = (b.getAttribute("aria-label") || "").trim();
                return /^Republier\\b/i.test(aria) || /^Repost\\b/i.test(aria);
            }
            var repostBtns = Array.from(document.querySelectorAll("button")).filter(isRepostButton);

            var results = [];
            var seenUrns = {};
            var seenContainers = [];

            // isTimeLine: line starts with a relative time expression and is short enough
            var timeRe = /^(\\d+\\s*(j|h|d|w|sem\\.?|an|min|mois?|y)|maintenant|just now|hier|yesterday)/i;
            function isTimeLine(line) { return line.length < 55 && timeRe.test(line); }
            // isDegree: short line like "• 2e" or "• 3e et +" (degree indicator in reshared headers)
            function isDegree(line) { return line.length < 16 && /\\d+e(\\s+et\\s+\\+)?$/.test(line); }

            function isGlobalChrome(node) {
                if (!node) return false;
                return !!node.closest(
                    '#global-nav, header.global-nav, nav, ' +
                    '[data-view-name="navigation"], .scaffold-layout-toolbar, ' +
                    '.msg-overlay-list-bubble, .msg-overlay-bubble-header, ' +
                    '.profile-rail-card, .scaffold-layout__aside--right, ' +
                    '.comments-comment-item, .comments-comments-list'
                );
            }
            function profileLinkFromAnchor(a, actionTakerName) {
                if (!a || isGlobalChrome(a)) return null;
                var href = a.getAttribute("href") || "";
                var m = href.match(/[/]in[/][^/?#]+/) || href.match(/[/]company[/][^/?#]+/);
                if (!m) return null;
                var nameSpan = a.querySelector("span[aria-hidden='true']");
                var nameSpanText = nameSpan ? (nameSpan.innerText || "").trim() : "";
                var rawText = nameSpanText || (a.innerText || "").trim();
                var candidate = rawText.split("\\n")[0].trim()
                    .replace(/\\s*[^\\w\\s]\\s*(\\d+e(\\s+et\\s+\\+)?|Suivi|Following)[\\s\\S]*$/, "")
                    .trim();
                if (candidate.length < 2) return null;
                if (actionTakerName && candidate.toLowerCase() === actionTakerName) return null;
                return {
                    name: candidate,
                    url: "https://www.linkedin.com" + m[0],
                };
            }
            function extractAuthorFromContainer(el, actionTakerName) {
                var authorName = "";
                var authorUrl = "";
                var actorRoots = el.querySelectorAll(
                    '.feed-shared-actor, .update-components-actor, ' +
                    '[class*="feed-shared-actor__container"], [class*="update-components-actor__container"]'
                );
                for (var ar = 0; ar < actorRoots.length && !authorName; ar++) {
                    var actor = actorRoots[ar];
                    if (isGlobalChrome(actor)) continue;
                    var actorLinks = actor.querySelectorAll("a[href*='/in/'], a[href*='/company/']");
                    for (var li = 0; li < actorLinks.length && !authorName; li++) {
                        var prof = profileLinkFromAnchor(actorLinks[li], actionTakerName);
                        if (!prof) continue;
                        authorName = prof.name;
                        authorUrl = prof.url;
                    }
                    if (!authorName) {
                        var nameEl = actor.querySelector(
                            '.feed-shared-actor__name, .update-components-actor__name, ' +
                            '.feed-shared-actor__title, .update-components-actor__title, ' +
                            '.feed-shared-actor__meta-link, .update-components-actor__meta-link'
                        );
                        if (nameEl) {
                            var nm = (nameEl.innerText || "").trim().split("\\n")[0].trim();
                            if (nm.length >= 2 && !(actionTakerName && nm.toLowerCase() === actionTakerName)) {
                                authorName = nm;
                                var linkInActor = actor.querySelector("a[href*='/in/'], a[href*='/company/']");
                                if (linkInActor) {
                                    var hm = (linkInActor.getAttribute("href") || "")
                                        .match(/[/](in|company)[/][^/?#]+/);
                                    if (hm) authorUrl = "https://www.linkedin.com" + hm[0];
                                }
                            }
                        }
                    }
                }
                if (!authorName) {
                    var inLinks = el.querySelectorAll("a[href*='/in/'], a[href*='/company/']");
                    for (var i = 0; i < inLinks.length && !authorName; i++) {
                        var prof2 = profileLinkFromAnchor(inLinks[i], actionTakerName);
                        if (!prof2) continue;
                        authorName = prof2.name;
                        authorUrl = prof2.url;
                    }
                }
                if (!authorName) {
                    var allLines = (el.innerText || "").split("\\n").map(function(l) {
                        return l.trim();
                    }).filter(Boolean);
                    var fallback = actionTakerName ? (allLines[2] || "") : (allLines[1] || "");
                    fallback = fallback.replace(/\\s*(a\\s+|aime|comment|publi|r\u00e9agi).*$/i, "").trim();
                    if (fallback.length > 1 && !isTimeLine(fallback) && !isDegree(fallback)) {
                        authorName = fallback;
                    }
                }
                return { name: authorName, url: authorUrl };
            }

            function normalizeLinkedInHref(href) {
                if (!href) return "";
                var s = String(href).trim();
                if (!s) return "";
                if (s.indexOf("//") === 0) return "https:" + s;
                if (s.indexOf("http") === 0) return s;
                if (s.charAt(0) === "/") return "https://www.linkedin.com" + s;
                return "";
            }
            function ensureTrailingSlashOnFeedUpdate(url) {
                if (!url || url.indexOf("/feed/update/") === -1) return url;
                return url.charAt(url.length - 1) === "/" ? url : url + "/";
            }
            /** Activity numeric id from any string (href with query, encoded URN, /posts/ slug). */
            function extractActivityIdFromText(text) {
                if (!text) return "";
                var t = String(text);
                var dec = t;
                try { dec = decodeURIComponent(t); } catch (e0) { dec = t; }
                var m = dec.match(/urn:li:activity:(\\d+)/) || t.match(/urn:li:activity:(\\d+)/);
                if (m) return m[1];
                m = dec.match(/activity-(\\d{10,})-/) || t.match(/activity-(\\d{10,})-/);
                if (m) return m[1];
                return "";
            }
            function feedUpdatePermalinkFromActivityId(id) {
                if (!id) return "";
                return ensureTrailingSlashOnFeedUpdate(
                    "https://www.linkedin.com/feed/update/urn:li:activity:" + id
                );
            }
            /**
             * Prefer direct post URL from DOM (href). Handles compkey cards when activity sits in
             * query params, reposts (several ids — prefer /feed/update/ or /posts/), data-urn, innerHTML.
             */
            function extractPermalinkFromContainer(root) {
                if (!root) return "";
                var links = root.querySelectorAll("a[href]");
                var i, raw, k, variants, v, pathOnly, full;
                // 1) Canonical /feed/update/… path with activity in path
                for (i = 0; i < links.length; i++) {
                    raw = links[i].getAttribute("href") || "";
                    if (!raw) continue;
                    variants = [raw];
                    try { variants.push(decodeURIComponent(raw)); } catch (eDec) {}
                    for (k = 0; k < variants.length; k++) {
                        v = variants[k];
                        if (v.indexOf("/feed/update/") === -1) continue;
                        pathOnly = v.split("#")[0].split("?")[0];
                        full = normalizeLinkedInHref(pathOnly);
                        if (full && /urn:li:activity:\\d+/.test(full)) {
                            return ensureTrailingSlashOnFeedUpdate(full);
                        }
                    }
                }
                for (i = 0; i < links.length; i++) {
                    raw = links[i].getAttribute("href") || "";
                    if (!raw) continue;
                    try { v = decodeURIComponent(raw.split("#")[0]); } catch (e2) { v = raw.split("#")[0]; }
                    if (v.indexOf("/feed/update/") === -1) continue;
                    if (/urn:li:activity:\\d+/.test(v)) {
                        full = normalizeLinkedInHref(v.split("?")[0]);
                        if (full) return ensureTrailingSlashOnFeedUpdate(full);
                    }
                }
                // 2) /posts/… slugs
                for (i = 0; i < links.length; i++) {
                    raw = links[i].getAttribute("href") || "";
                    if (!raw) continue;
                    pathOnly = raw.split("#")[0].split("?")[0];
                    full = normalizeLinkedInHref(pathOnly);
                    if (full && full.indexOf("/posts/") !== -1) return full;
                }
                // 3) Activity id anywhere in href (query string, encoded) — typical for newer cards / reposts
                var candidates = [];
                for (i = 0; i < links.length; i++) {
                    raw = links[i].getAttribute("href") || "";
                    var aid = extractActivityIdFromText(raw);
                    if (aid) candidates.push({ id: aid, href: raw });
                }
                if (candidates.length === 1) {
                    return feedUpdatePermalinkFromActivityId(candidates[0].id);
                }
                if (candidates.length > 1) {
                    for (k = 0; k < candidates.length; k++) {
                        if (candidates[k].href.indexOf("/feed/update/") !== -1) {
                            return feedUpdatePermalinkFromActivityId(candidates[k].id);
                        }
                    }
                    try {
                        var decH = "";
                        for (k = 0; k < candidates.length; k++) {
                            try { decH = decodeURIComponent(candidates[k].href); } catch (eH) { decH = candidates[k].href; }
                            if (decH.indexOf("/feed/update/") !== -1) {
                                return feedUpdatePermalinkFromActivityId(candidates[k].id);
                            }
                        }
                    } catch (eK) {}
                    for (k = 0; k < candidates.length; k++) {
                        if (candidates[k].href.indexOf("/posts/") !== -1) {
                            return feedUpdatePermalinkFromActivityId(candidates[k].id);
                        }
                    }
                    return feedUpdatePermalinkFromActivityId(candidates[candidates.length - 1].id);
                }
                // 4) data-urn descendants (may expose activity while card URN stays compkey)
                var duNodes = root.querySelectorAll("[data-urn]");
                for (i = 0; i < duNodes.length; i++) {
                    var du = duNodes[i].getAttribute("data-urn") || "";
                    var aidDu = extractActivityIdFromText(du);
                    if (aidDu) return feedUpdatePermalinkFromActivityId(aidDu);
                }
                // 5) Last activity URN in subtree HTML (repost: nested original often appears after header chrome)
                var html = root.innerHTML || "";
                var reGlob = /urn:li:activity:(\\d+)/g;
                var mm;
                var lastId = "";
                while ((mm = reGlob.exec(html)) !== null) {
                    lastId = mm[1];
                }
                if (lastId) return feedUpdatePermalinkFromActivityId(lastId);
                return "";
            }
            function pushUnique(arr, value) {
                if (!value) return;
                if (arr.indexOf(value) === -1) arr.push(value);
            }
            function collectIdentifiersAndPermalinkCandidates(root, baseUrn, basePermalink, compEls) {
                var identifierCandidates = [];
                var permalinkCandidates = [];
                var componentKeys = [];

                if (baseUrn) pushUnique(identifierCandidates, baseUrn);
                if (basePermalink) pushUnique(permalinkCandidates, ensureTrailingSlashOnFeedUpdate(basePermalink));

                // component keys (raw + normalized compkey urn)
                for (var i = 0; i < compEls.length; i++) {
                    var ck = compEls[i].getAttribute("componentkey") || "";
                    if (!ck) continue;
                    pushUnique(componentKeys, ck);
                    var base = ck.replace(/^expanded/, "").replace(/FeedType_.*$/, "");
                    if (base.length > 10) {
                        pushUnique(identifierCandidates, "urn:li:compkey:" + base);
                    }
                    var aidCk = extractActivityIdFromText(ck);
                    if (aidCk) pushUnique(identifierCandidates, "urn:li:activity:" + aidCk);
                }

                // data-urn and other attrs
                var duNodes = root.querySelectorAll("[data-urn]");
                for (var j = 0; j < duNodes.length; j++) {
                    var du = duNodes[j].getAttribute("data-urn") || "";
                    if (!du) continue;
                    var aidDu = extractActivityIdFromText(du);
                    if (aidDu) {
                        pushUnique(identifierCandidates, "urn:li:activity:" + aidDu);
                        pushUnique(permalinkCandidates, feedUpdatePermalinkFromActivityId(aidDu));
                    }
                }

                // href-based candidates
                var links = root.querySelectorAll("a[href]");
                for (var k = 0; k < links.length; k++) {
                    var href = links[k].getAttribute("href") || "";
                    if (!href) continue;
                    var fullHref = normalizeLinkedInHref(href) || normalizeLinkedInHref(href.split("?")[0]);
                    if (fullHref && (fullHref.indexOf("/feed/update/") !== -1 || fullHref.indexOf("/posts/") !== -1)) {
                        pushUnique(permalinkCandidates, ensureTrailingSlashOnFeedUpdate(fullHref.split("#")[0]));
                    }
                    var aidHref = extractActivityIdFromText(href);
                    if (aidHref) {
                        pushUnique(identifierCandidates, "urn:li:activity:" + aidHref);
                        pushUnique(permalinkCandidates, feedUpdatePermalinkFromActivityId(aidHref));
                    }
                }

                // last resort: activity IDs in HTML
                var html = root.innerHTML || "";
                var reGlob = /urn:li:activity:(\\d+)/g;
                var mm;
                while ((mm = reGlob.exec(html)) !== null) {
                    var urnA = "urn:li:activity:" + mm[1];
                    pushUnique(identifierCandidates, urnA);
                    pushUnique(permalinkCandidates, feedUpdatePermalinkFromActivityId(mm[1]));
                }

                return {
                    identifierCandidates: identifierCandidates,
                    permalinkCandidates: permalinkCandidates,
                    componentKeys: componentKeys,
                };
            }

            // LinkedIn n'affiche pas « Republier » sur les posts du compte connecté : une
            // carte sans ce bouton est ancrée sur son bouton « Commenter » (B-003).
            function isCommentButton(b) {
                if (!b) return false;
                var t = (b.innerText || "").trim();
                if (t === "Commenter" || t === "Comment") return true;
                var aria = (b.getAttribute("aria-label") || "").trim();
                return aria === "Commenter" || aria === "Comment";
            }
            function cardOf(btn, isAnchor) {
                var node = btn.parentElement;
                while (node && node !== document.body) {
                    var up = node.parentElement;
                    if (!up || up === document.body) break;
                    var anchorCount = Array.from(up.querySelectorAll("button")).filter(isAnchor).length;
                    if (anchorCount > 1) break;
                    node = node.parentElement;
                }
                return node;
            }
            var repostCards = repostBtns.map(function(b) { return cardOf(b, isRepostButton); });
            var anchors = repostBtns.map(function(b) { return { btn: b, isAnchor: isRepostButton }; });
            Array.from(document.querySelectorAll("button")).filter(isCommentButton).forEach(function(b) {
                var inRepostCard = repostCards.some(function(c) { return c && c !== document.body && c.contains(b); });
                if (!inRepostCard) anchors.push({ btn: b, isAnchor: isCommentButton });
            });

            for (var bi = 0; bi < anchors.length; bi++) {
                var el = cardOf(anchors[bi].btn, anchors[bi].isAnchor);

                if (!el || el === document.body) continue;
                if (seenContainers.indexOf(el) >= 0) continue;
                seenContainers.push(el);

                // ---- URN + permalink URL extraction ----
                // DOM permalink first (handles urn:li:compkey when href still points to activity URL)
                var urn = "";
                var permalinkUrl = extractPermalinkFromContainer(el);

                // Strategy 0: data-urn attribute — LinkedIn's stable anchor per CLAUDE.md
                var urnEls = el.querySelectorAll("[data-urn]");
                for (var i = 0; i < urnEls.length && !urn; i++) {
                    var du = urnEls[i].getAttribute("data-urn") || "";
                    var m = du.match(/urn:li:activity:(\\d+)/);
                    if (m) urn = m[0];
                }
                if (!urn) {
                    var du = el.getAttribute("data-urn") || "";
                    var m = du.match(/urn:li:activity:(\\d+)/);
                    if (m) urn = m[0];
                }

                var compEls = el.querySelectorAll("[componentkey]");

                if (!urn) {
                for (var i = 0; i < compEls.length && !urn; i++) {
                    var ck = compEls[i].getAttribute("componentkey") || "";
                    var m = ck.match(/urn:li:activity:(\\d+)/);
                    if (m) urn = m[0];
                }
                }

                // Strategy 2: /posts/ permalink — captures the canonical URL directly
                if (!urn || !permalinkUrl) {
                    var postLinks = el.querySelectorAll("a[href*='/posts/']");
                    for (var i = 0; i < postLinks.length; i++) {
                        var href = postLinks[i].getAttribute("href") || "";
                        // Strip query string for clean URL
                        var cleanHref = href.split("?")[0];
                        if (!permalinkUrl && cleanHref.includes("/posts/")) {
                            permalinkUrl = cleanHref.startsWith("http")
                                ? cleanHref
                                : "https://www.linkedin.com" + cleanHref;
                        }
                        if (!urn) {
                            var m = href.match(/activity-(\\d{15,})-/);
                            if (m) urn = "urn:li:activity:" + m[1];
                        }
                        if (urn && permalinkUrl) break;
                    }
                }

                // Strategy 3: /feed/update/ permalink — LinkedIn's standard share URL format,
                // also used as the timestamp anchor link
                if (!urn || !permalinkUrl) {
                    var feedLinks = el.querySelectorAll("a[href*='/feed/update/']");
                    for (var i = 0; i < feedLinks.length; i++) {
                        var href = feedLinks[i].getAttribute("href") || "";
                        var cleanHref = href.split("?")[0];
                        var m = cleanHref.match(/urn:li:activity:(\\d+)/);
                        if (m) {
                            if (!urn) urn = "urn:li:activity:" + m[1];
                            if (!permalinkUrl) {
                                permalinkUrl = cleanHref.startsWith("http")
                                    ? cleanHref
                                    : "https://www.linkedin.com" + cleanHref;
                            }
                        }
                        if (urn && permalinkUrl) break;
                    }
                }

                if (!urn) {
                    var allEls = el.querySelectorAll("*");
                    for (var i = 0; i < allEls.length && !urn; i++) {
                        var attrs = allEls[i].attributes;
                        for (var j = 0; j < attrs.length && !urn; j++) {
                            var m = attrs[j].value.match(/urn:li:activity:(\\d+)/);
                            if (m) urn = m[0];
                        }
                    }
                }

                if (!urn) {
                    var m = (el.innerHTML || "").match(/urn:li:activity:(\\d+)/);
                    if (m) urn = m[0];
                }

                if (!urn) {
                    for (var i = 0; i < compEls.length && !urn; i++) {
                        var ck = compEls[i].getAttribute("componentkey") || "";
                        var base = ck.replace(/^expanded/, "").replace(/FeedType_.*$/, "");
                        if (base.length > 10) { urn = "urn:li:compkey:" + base; break; }
                    }
                }

                if (!urn || seenUrns[urn]) continue;
                seenUrns[urn] = true;

                var elText = el.innerText || "";
                var allLines = elText.split("\\n").map(function(l){ return l.trim(); }).filter(Boolean);

                if (
                    elText.includes("Sponsorisé") || elText.includes("Sponsored") ||
                    elText.includes("Promoted") || el.querySelector("[data-control-name='promoted']")
                ) continue;

                // ---- Detect activity post (liked/commented/reshared) ----
                // Y = actor (network contact who triggered the feed entry)
                // X = original author (the person who wrote the post)
                //
                // Deux formulations coexistent (vérifié en live le 2026-09-03) :
                //   suffixe : « Y a republié ce contenu », « Y aime ce contenu »
                //   préfixe : « Suivi par Y », « Recommandé par Y »
                // La forme préfixe n'était pas reconnue : aucun acteur détecté, donc
                // le premier bloc auteur de la carte — celui du contact réseau —
                // sortait en author_name (BUG #3).
                // On ne se fie plus à un index fixe non plus : LinkedIn ouvre
                // désormais chaque carte par « Post du fil d'actualité », ce qui
                // décalait allLines[1].
                var actorName = "";
                var actorUrl = "";
                var actionTakerName = "";
                var actionTakerRaw = "";
                var actKwPat = /\\s(a\\s+comment|a\\s+r\u00e9pondu|a\\s+republi|aime\\s|aiment\\s|a\\s+aim|ont\\s+aim|ont\\s+republi|ont\\s+comment|a\\s+r\u00e9agi|a\\s+partag|commented|liked|reshared|reposted|reacted)/i;
                var actPrefixPat = /^(?:Suivi par|Followed by|Recommand\u00e9 par|Recommended by|Aim\u00e9 par|Liked by|Republi\u00e9 par|Reposted by|Sugg\u00e9r\u00e9 par|Suggested by)\\s+(.+)$/i;
                for (var ai2 = 0; ai2 < Math.min(4, allLines.length) && !actionTakerRaw; ai2++) {
                    var candLine = allLines[ai2] || "";
                    if (candLine.length > 120) continue;
                    var prefMatch = candLine.match(actPrefixPat);
                    if (prefMatch) {
                        actionTakerRaw = prefMatch[1]
                            .replace(/\\s+(et|and)\\s+\\d[\\s\\S]*$/i, "")
                            .trim();
                        break;
                    }
                    var actMatch = candLine.match(actKwPat);
                    // index borné : au-delà, on est dans du contenu, pas dans un nom
                    if (actMatch && actMatch.index > 0 && actMatch.index <= 60) {
                        actionTakerRaw = candLine.substring(0, actMatch.index).trim();
                    }
                }
                if (actionTakerRaw) {
                    actionTakerName = actionTakerRaw.toLowerCase();
                    // Resolve actor's profile URL from the first /in/ link that matches their name
                    var inLinksAll = el.querySelectorAll("a[href*='/in/']");
                    for (var i = 0; i < inLinksAll.length && !actorUrl; i++) {
                        var href = inLinksAll[i].getAttribute("href") || "";
                        var m = href.match(/[/]in[/][^/?#]+/);
                        if (!m) continue;
                        var nameSpan = inLinksAll[i].querySelector("span[aria-hidden='true']");
                        var nameSpanText = nameSpan ? (nameSpan.innerText || "").trim() : "";
                        var rawText = nameSpanText || (inLinksAll[i].innerText || "").trim();
                        var candidate = rawText.split("\\n")[0].trim()
                            .replace(/\\s*[^\\w\\s]\\s*(\\d+e(\\s+et\\s+\\+)?|Suivi|Following)[\\s\\S]*$/, "").trim();
                        if (candidate.toLowerCase() === actionTakerName) {
                            actorName = candidate;
                            actorUrl = "https://www.linkedin.com" + m[0];
                        }
                    }
                    if (!actorName) actorName = actionTakerRaw;
                }

                // ---- Author (original content creator) ----
                var authorInfo = extractAuthorFromContainer(el, actionTakerName);
                var authorName = authorInfo.name;
                var authorUrl = authorInfo.url;

                // ---- Published date ----
                var publishedAt = "";
                var timeEl = el.querySelector("time");
                if (timeEl) {
                    publishedAt = (timeEl.getAttribute("datetime") || timeEl.innerText || "").trim();
                }
                if (!publishedAt) {
                    for (var i = 0; i < allLines.length && !publishedAt; i++) {
                        if (isTimeLine(allLines[i])) {
                            publishedAt = allLines[i].replace(/\\s*(\\W+\\s*)+$/, "").trim();
                        }
                    }
                }

                // ---- Post text ----
                // Only the bottom action bar terminates the content.
                // Profile-level buttons ("Suivre", "Se connecter") appear BEFORE content
                // in activity posts (liked/commented/reshared) — do NOT treat them as terminators.
                var actionWords = {
                    "J'aime": 1, "Like": 1, "Commenter": 1, "Comment": 1,
                    "Republier": 1, "Repost": 1, "Envoyer": 1, "Send": 1,
                    "Voir plus": 1, "See more": 1,
                    "… plus": 1, "Afficher la traduction": 1, "Show translation": 1,
                };

                // Sur un post du compte connecté, les boutons d'action sont des icônes sans
                // libellé visible : leur texte n'est qu'un espace invisible (B-003).
                function isInvisibleLine(l) { return /^[\\u200b-\\u200d\\u2060\\ufeff]+$/.test(l); }
                var startIdx = -1;
                var endIdx = allLines.length;
                for (var i = 0; i < allLines.length; i++) {
                    if (startIdx < 0 && isTimeLine(allLines[i])) startIdx = i + 1;
                    if (actionWords[allLines[i]] || isInvisibleLine(allLines[i])) { endIdx = i; break; }
                }

                // Barre d'action (J'aime / Commenter / Republier / Envoyer) : frontière
                // entre le post et ses commentaires affichés. Distincte de endIdx, qui
                // peut tomber bien avant sur « … plus » ou « Afficher la traduction ».
                // LinkedIn rend l'apostrophe typographique (J’aime), d'où les deux formes.
                var actionBarWords = {
                    "J'aime": 1, "J\u2019aime": 1, "Like": 1,
                    "Commenter": 1, "Comment": 1,
                    "Republier": 1, "Repost": 1, "Envoyer": 1, "Send": 1,
                };
                var actionBarIdx = allLines.length;
                for (var i = 0; i < allLines.length; i++) {
                    if (actionBarWords[allLines[i]] || isInvisibleLine(allLines[i])) { actionBarIdx = i; break; }
                }

                // Skip profile-level follow/connect buttons that appear right after the date
                // in activity posts (structure: ... DATE, Suivre, CONTENT...)
                while (startIdx >= 0 && startIdx < endIdx &&
                       (allLines[startIdx] === "Suivre" || allLines[startIdx] === "Follow" ||
                        allLines[startIdx] === "Se connecter" || allLines[startIdx] === "Connect" ||
                        allLines[startIdx] === "Suivi" || allLines[startIdx] === "Following")) {
                    startIdx++;
                }

                // For reshared posts where the reposter added their own commentary:
                // lines after the reposter's date → [commentary], [reshared author block]
                // Detect via degree indicator within 6 lines.
                if (startIdx >= 0) {
                    var degreeAt = -1;
                    for (var k = startIdx; k < Math.min(startIdx + 6, endIdx); k++) {
                        if (isDegree(allLines[k])) { degreeAt = k; break; }
                    }
                    if (degreeAt >= 0) {
                        var commentary = allLines.slice(startIdx, degreeAt).join(" ").trim();
                        if (commentary.length > 15) {
                            endIdx = degreeAt;
                        } else {
                            var nextDateIdx = -1;
                            for (var k = degreeAt + 1; k < endIdx; k++) {
                                if (isTimeLine(allLines[k])) { nextDateIdx = k + 1; break; }
                            }
                            if (nextDateIdx >= 0 && nextDateIdx < endIdx) {
                                startIdx = nextDateIdx;
                                // Also skip follow/connect after the reshared date
                                while (startIdx < endIdx &&
                                       (allLines[startIdx] === "Suivre" || allLines[startIdx] === "Follow" ||
                                        allLines[startIdx] === "Se connecter" || allLines[startIdx] === "Connect")) {
                                    startIdx++;
                                }
                            }
                        }
                    }
                }

                var content = "";
                if (startIdx >= 0 && startIdx < endIdx) {
                    content = allLines.slice(startIdx, endIdx).join("\\n").trim();
                }
                content = content.slice(0, 3000);

                // ---- Images ----
                // Collect LinkedIn CDN image URLs (feedshare images, not avatars/logos).
                // LinkedIn lazy-loads images via data-delayed-url or data-src before setting src.
                var images = [];
                var seenImgUrls = {};
                var imgEls = el.querySelectorAll("img");
                for (var i = 0; i < imgEls.length; i++) {
                    var src = imgEls[i].getAttribute("data-delayed-url") ||
                              imgEls[i].getAttribute("data-src") ||
                              imgEls[i].getAttribute("src") || "";
                    if (!src) continue;
                    // Only keep LinkedIn media CDN images; skip avatars and logos
                    if (!src.includes("media.licdn.com")) continue;
                    if (src.includes("/profile-") || src.includes("ghost-") ||
                        src.includes("/company-logo") || src.includes("logo")) continue;
                    var cleanSrc = src.split("?")[0];
                    if (!seenImgUrls[cleanSrc]) {
                        seenImgUrls[cleanSrc] = true;
                        images.push(src);
                    }
                }

                // ---- Reactions / comments ----
                var reactionsText = "";
                var commentsText = "";
                var repostsText = "";
                // Scan button aria-labels for counts — extract the leading number only
                var btns = el.querySelectorAll("button[aria-label]");
                for (var i = 0; i < btns.length; i++) {
                    var label = (btns[i].getAttribute("aria-label") || "").toLowerCase();
                    var btext = (btns[i].innerText || "").trim();
                    // Prefer button text if it's numeric; else extract leading number from label
                    var numMatch = btext.match(/^\\d[\\d.,k\\s]*/) || label.match(/^\\d[\\d.,k\\s]*/);
                    if (label.includes("reaction") || label.includes("réaction") ||
                            label.includes("réagi") || label.includes("personnes")) {
                        if (!reactionsText && numMatch) reactionsText = numMatch[0].trim();
                    } else if (
                        (label.includes("comment") || label.includes("commentaire")) &&
                        label !== "commenter" && label !== "comment"
                    ) {
                        if (!commentsText && numMatch) commentsText = numMatch[0].trim();
                    } else if (
                        (label.includes("republication") || label.includes("repost") ||
                         label.includes("partage")) &&
                        label !== "republier" && label !== "repost" &&
                        label !== "partager" && label !== "share"
                    ) {
                        if (!repostsText && numMatch) repostsText = numMatch[0].trim();
                    }
                }
                // Fallback: scan text lines for "NNN réactions" / "NNN commentaires"
                // Le rendu de septembre 2026 n'expose plus aucun compteur dans les
                // aria-labels de boutons : tout passe par les lignes de texte. Deux
                // pièges vérifiés en live : les réactions basculent en preuve sociale
                // (« Réaction de X et N autres personnes ») dès qu'une relation a
                // réagi, et les lignes « N réactions » situées après la barre d'action
                // appartiennent aux commentaires affichés — les compter donnait un
                // `reactions_count` faux et silencieux (BUG #5a).
                if (!reactionsText || !commentsText || !repostsText) {
                    var reactLineRe = /^(\\d[\\d\\s.,]*k?)\\s*(r\\u00e9actions?|reactions?)/i;
                    var commentLineRe = /^(\\d[\\d\\s.,]*k?)\\s*(commentaires?|comments?)/i;
                    // LinkedIn groupe souvent « X commentaires \u00b7 Y republications »
                    // dans une m\u00eame ligne : on cherche donc le motif partout dans la ligne.
                    var repostLineRe = /(\\d[\\d\\s.,]*k?)\\s*(republications?|reposts?|partages?)/i;
                    var socialProofRe = /(?:et|and)\\s+(\\d[\\d\\s.,]*k?)\\s+(?:autres?\\s+personnes?|autres?|others?)/i;
                    for (var i = 0; i < actionBarIdx; i++) {
                        if (!reactionsText) {
                            var m = allLines[i].match(reactLineRe);
                            if (m) reactionsText = m[1].trim();
                        }
                        if (!reactionsText) {
                            // « Réaction de X et 154 autres personnes » = 155 réactions
                            var sp = allLines[i].match(socialProofRe);
                            if (sp) {
                                var rawN = sp[1].trim();
                                if (/k/i.test(rawN)) {
                                    reactionsText = rawN;
                                } else {
                                    var n = parseInt(rawN.replace(/[^0-9]/g, ""), 10);
                                    if (!isNaN(n)) reactionsText = String(n + 1);
                                }
                            }
                        }
                        if (!commentsText) {
                            var m = allLines[i].match(commentLineRe);
                            if (m) commentsText = m[1].trim();
                        }
                        if (!repostsText) {
                            var m = allLines[i].match(repostLineRe);
                            if (m) repostsText = m[1].trim();
                        }
                    }
                }

                // ---- Video ----
                // 1. Native LinkedIn video: <video> element with data-sources JSON or src
                // 2. External embed: <iframe> with youtube.com/embed or similar
                // Note: native LinkedIn video URLs require auth cookies to stream outside LinkedIn.
                var videoUrl = "";
                var videoEl = el.querySelector("video");
                if (videoEl) {
                    // data-sources is a JSON array of {src, type} objects
                    var dataSources = videoEl.getAttribute("data-sources") || "";
                    if (dataSources) {
                        try {
                            var sources = JSON.parse(dataSources);
                            // Prefer MP4, then DASH (.mpd), then HLS (.m3u8)
                            var mp4 = "", dash = "", hls = "";
                            for (var si = 0; si < sources.length; si++) {
                                var s = sources[si].src || "";
                                if (!mp4 && s.includes(".mp4")) mp4 = s;
                                if (!dash && s.includes(".mpd")) dash = s;
                                if (!hls && s.includes(".m3u8")) hls = s;
                            }
                            videoUrl = mp4 || dash || hls || (sources[0] && sources[0].src) || "";
                        } catch(e) {}
                    }
                    if (!videoUrl) {
                        videoUrl = videoEl.getAttribute("src") ||
                                   videoEl.querySelector("source") && videoEl.querySelector("source").getAttribute("src") || "";
                    }
                }
                // External embed (YouTube, Vimeo, etc.)
                if (!videoUrl) {
                    var iframeEl = el.querySelector("iframe[src]");
                    if (iframeEl) videoUrl = iframeEl.getAttribute("src") || "";
                }
                // Reject blob: URLs — they are browser-internal and not portable
                if (videoUrl.startsWith("blob:")) videoUrl = "";

                // ---- External link (lnkd.in shortlinks and other non-LinkedIn URLs) ----
                // Collect first external link for article_url; lnkd.in ones are resolved in Python.
                var externalUrl = "";
                var allLinks = el.querySelectorAll("a[href]");
                for (var i = 0; i < allLinks.length && !externalUrl; i++) {
                    var href = allLinks[i].getAttribute("href") || "";
                    if (!href || href.startsWith("#") || href.startsWith("javascript")) continue;
                    var full = href.startsWith("http") ? href : "https://www.linkedin.com" + href;
                    // Skip LinkedIn-internal URLs; keep external ones and lnkd.in shortlinks
                    if (full.includes("linkedin.com") && !full.includes("lnkd.in")) continue;
                    externalUrl = full;
                }

                // ---- Comments containing URLs ----
                // Best effort: LinkedIn does not always render comment DOM if collapsed.
                var comments = [];
                var seenCommentLinks = {};
                function addCommentUrl(url, author, text) {
                    if (!url) return;
                    var dedupKey = (author || "") + "|" + url;
                    if (seenCommentLinks[dedupKey]) return;
                    seenCommentLinks[dedupKey] = true;
                    comments.push({
                        authorName: author || null,
                        text: text || null,
                        url: url
                    });
                }
                // Le nouveau rendu (CSS atomisé) n'expose plus les classes
                // .comments-comment-item ni les [data-id] sur le feed : chaque
                // commentaire y porte un componentkey `replaceableComment_…`. Les
                // pages de détail d'un post, elles, utilisent encore les classes.
                //
                // Deux familles distinctes, et c'est ce qui compte : PRIMAIRES désigne
                // un commentaire entier, REPLI désigne un fragment (corps seul) ou un
                // conteneur. Mélanger les deux fait hériter les liens d'un commentaire
                // de l'auteur d'un autre — relevé en live le 2026-09-08 : trois liens
                // de profils distincts, tous attribués à la même personne.
                var commentSelectorsPrimary = [
                    '[componentkey^="replaceableComment_"]',
                    '.comments-comment-item',
                    '[data-id^="urn:li:comment"]'
                ];
                var commentSelectorsFallback = [
                    '[componentkey^="commentsSectionContainer"]',
                    '.comments-comment-item-content-body',
                    '.comments-comment-item__main-content'
                ];
                function collectCommentNodes(selectors) {
                    var acc = [];
                    for (var cs = 0; cs < selectors.length; cs++) {
                        var found = el.querySelectorAll(selectors[cs]);
                        for (var fi = 0; fi < found.length; fi++) acc.push(found[fi]);
                    }
                    // Un nœud qui en contient un autre du même rang est un conteneur,
                    // pas un commentaire : on ne garde que les plus internes.
                    return acc.filter(function (node, idx) {
                        if (acc.indexOf(node) !== idx) return false;
                        for (var k = 0; k < acc.length; k++) {
                            if (acc[k] !== node && node.contains(acc[k])) return false;
                        }
                        return true;
                    });
                }
                var commentNodes = collectCommentNodes(commentSelectorsPrimary);
                if (!commentNodes.length) {
                    commentNodes = collectCommentNodes(commentSelectorsFallback);
                }

                for (var ci = 0; ci < commentNodes.length; ci++) {
                    var cnode = commentNodes[ci];
                    var ctext = (cnode.innerText || "").trim().slice(0, 2000);
                    var cauthor = commentAuthorFromNode(cnode);

                    var cLinks = cnode.querySelectorAll("a[href]");
                    for (var cli = 0; cli < cLinks.length; cli++) {
                        var chref = cLinks[cli].getAttribute("href") || "";
                        if (!chref || chref.startsWith("#") || chref.startsWith("javascript")) continue;
                        var cfull = chref.startsWith("http") ? chref : "https://www.linkedin.com" + chref;
                        if (!cfull.includes("http")) continue;
                        addCommentUrl(cfull, cauthor, ctext);
                    }
                    // Fallback texte brut si LinkedIn n'expose pas d'ancre <a>.
                    var urlMatches = ctext.match(/(?:https?:\\/\\/|www\\.)[^\\s<>"')]+/gi) || [];
                    for (var um = 0; um < urlMatches.length; um++) {
                        var rawUrl = urlMatches[um] || "";
                        if (!rawUrl) continue;
                        if (rawUrl.indexOf("http") !== 0) rawUrl = "https://" + rawUrl;
                        addCommentUrl(rawUrl, cauthor, ctext);
                    }
                }

                // Premier commentaire visible, quel qu'il soit (contrairement \u00e0
                // `comments` qui ne retient que ceux porteurs d'un lien externe).
                //
                // Le corps du commentaire n'a plus de conteneur d\u00e9di\u00e9 dans le rendu
                // atomis\u00e9 : on le d\u00e9limite comme pour le post lui-m\u00eame, en partant de
                // l'horodatage (l'en-t\u00eate — nom, badge, degr\u00e9, accroche — le pr\u00e9c\u00e8de)
                // et en s'arr\u00eatant \u00e0 la premi\u00e8re ligne d'action ou de compteur.
                // Nom de l'auteur d'un commentaire.
                //
                // LinkedIn a renomm\u00e9 `comments-post-meta__*` en
                // `comments-comment-meta__*` : le code cherchait les anciennes
                // classes et retombait sur la premi\u00e8re ancre `/in/` du bloc, qui
                // est celle de l'avatar et n'a pas de texte. D'o\u00f9 un auteur
                // toujours vide, et un consommateur oblig\u00e9 de reconstruire
                // \u00ab Auteur \u2014 texte \u00bb \u00e0 la main.
                function cleanCommentAuthor(raw) {
                    return (raw || "").split("\\n")[0]
                        .replace(/\\s*[\u2022\u00b7]\\s*(\\d+e(\\s+et\\s+\\+)?|1er|Suivi|Following)[\\s\\S]*$/i, "")
                        .replace(/\\s*(V\u00e9rifi\u00e9|Verified|Premium|Auteur|Author)\\s*$/i, "")
                        .trim();
                }
                function commentAuthorFromNode(node) {
                    var named = node.querySelectorAll(
                        '.comments-comment-meta__description-title, ' +
                        '.comments-comment-meta__description-container, ' +
                        '.comments-post-meta__name-text, .comments-post-meta__name'
                    );
                    for (var i = 0; i < named.length; i++) {
                        var n1 = cleanCommentAuthor(named[i].innerText || "");
                        if (n1.length > 1) return n1.slice(0, 120);
                    }
                    // Repli : la premi\u00e8re ancre de profil PORTANT du texte — l'ancre
                    // de l'avatar, vide, doit \u00eatre ignor\u00e9e.
                    var links = node.querySelectorAll("a[href*='/in/']");
                    for (var j = 0; j < links.length; j++) {
                        var sp = links[j].querySelector("span[aria-hidden='true']");
                        var n2 = cleanCommentAuthor((sp ? sp.innerText : links[j].innerText) || "");
                        if (n2.length > 1) return n2.slice(0, 120);
                    }
                    return "";
                }

                function commentBodyFromNode(node) {
                    var bodyEl = node.querySelector(
                        '.comments-comment-item-content-body, .update-components-text, ' +
                        '.comments-comment-item__main-content'
                    );
                    if (bodyEl) {
                        var direct = (bodyEl.innerText || "").trim();
                        if (direct) return direct;
                    }
                    var cl = (node.innerText || "").split("\\n").map(function (l) {
                        return l.trim();
                    }).filter(Boolean);
                    var afterTime = -1;
                    for (var i = 0; i < cl.length && afterTime < 0; i++) {
                        if (isTimeLine(cl[i])) afterTime = i + 1;
                    }
                    if (afterTime < 0) return "";
                    var skipLines = {
                        "Suivre": 1, "Follow": 1, "Suivi": 1, "Following": 1,
                        "Auteur": 1, "Author": 1, "Modifi\u00e9": 1, "Edited": 1,
                        "Se connecter": 1, "Connect": 1,
                    };
                    var stopRe = /^(?:\u2026\\s*plus|\\.{3}\\s*plus|Voir plus|See more|J.aime|Like|R\u00e9pondre|Reply|Commenter|Comment)$/i;
                    var countRe = /^\\d[\\d\\s.,]*k?\\s*(?:r\u00e9actions?|reactions?|commentaires?|comments?|r\u00e9ponses?|replies?)/i;
                    // Puces de compteur rendues en chiffre nu (« 0 », « 1 », « 13 ») :
                    // elles suivent le corps du commentaire et le polluaient.
                    var bareNumRe = /^\\d[\\d\\s.,]*k?$/;
                    var parts = [];
                    for (var i = afterTime; i < cl.length; i++) {
                        if (skipLines[cl[i]]) continue;
                        if (stopRe.test(cl[i]) || countRe.test(cl[i]) || bareNumRe.test(cl[i])) break;
                        parts.push(cl[i]);
                    }
                    return parts.join("\\n").trim();
                }

                var topComment = "";
                var topCommentAuthor = "";
                for (var tc = 0; tc < commentNodes.length && !topComment; tc++) {
                    var rawTop = commentBodyFromNode(commentNodes[tc]);
                    if (rawTop) {
                        topComment = rawTop.slice(0, 2000);
                        topCommentAuthor = commentAuthorFromNode(commentNodes[tc]);
                    }
                }

                if (!permalinkUrl) {
                    permalinkUrl = extractPermalinkFromContainer(el);
                }
                var debugCandidates = collectIdentifiersAndPermalinkCandidates(el, urn, permalinkUrl, compEls);

                results.push({
                    urn: urn,
                    permalinkUrl: permalinkUrl,
                    identifierCandidates: debugCandidates.identifierCandidates,
                    permalinkCandidates: debugCandidates.permalinkCandidates,
                    componentKeys: debugCandidates.componentKeys,
                    authorName: authorName,
                    authorUrl: authorUrl,
                    actorName: actorName,
                    actorUrl: actorUrl,
                    publishedAt: publishedAt,
                    content: content,
                    reactionsText: reactionsText,
                    commentsText: commentsText,
                    repostsText: repostsText,
                    comments: comments,
                    topComment: topComment,
                    topCommentAuthor: topCommentAuthor,
                    images: images,
                    videoUrl: videoUrl,
                    externalUrl: externalUrl,
                });
            }

            return results;
        }""")
        # Anti-détection : en session, les cartes déjà rendues restent dans le DOM ;
        # les reconstruire re-résoudrait leurs liens (requêtes avec cookies) et le
        # volume par appel croîtrait avec l'index du lot. On les écarte d'emblée.
        if self._returned_keys:
            posts_data = [
                d for d in posts_data
                if not ({d.get("urn"), d.get("permalinkUrl")} & self._returned_keys)
            ]
        posts_data = await self._fill_missing_permalinks_from_ui(posts_data)

        result: List[Post] = []
        for data in posts_data:
            urn = data["urn"]
            permalink = data.get("permalinkUrl") or None
            permalink_candidates = data.get("permalinkCandidates", []) or []
            linkedin_url = self._finalize_linkedin_url(permalink, urn, permalink_candidates)

            # Ne jamais exposer un compkey comme `urn` : dès qu'un permalien a
            # été résolu (DOM, menu overflow ou copy-link), il porte l'identifiant
            # canonique du post. Sans cette normalisation, la branche copy-link
            # renvoyait une URL correcte et un urn éphémère — doublons en base à
            # chaque scrape et actions like/repost impossibles en aval.
            feed_compkey = urn if str(urn or "").startswith("urn:li:compkey:") else None
            if not str(urn or "").startswith("urn:li:activity:"):
                canonical_urn = self._canonical_activity_urn_from_url(linkedin_url)
                if canonical_urn:
                    urn = canonical_urn

            external_url = data.get("externalUrl") or None
            if external_url:
                external_url = await self._resolve_url(external_url)

            comments = data.get("comments", []) or []
            normalized_comments: List[Dict[str, Any]] = []
            for c in comments:
                if not isinstance(c, dict):
                    continue
                raw_url = (c.get("url") or "").strip()
                if not raw_url:
                    continue
                resolved_url = raw_url
                if (
                    "lnkd.in" in raw_url.lower()
                    or "/slink" in raw_url.lower()
                    or "linkedin.com/redir/" in raw_url.lower()
                ):
                    resolved_url = await self._resolve_url(raw_url)
                kept_url = resolved_url if self._is_external_comment_url(resolved_url) else None
                if not kept_url and "lnkd.in" in raw_url.lower() and self._is_external_comment_url(raw_url):
                    kept_url = raw_url
                if not kept_url:
                    continue
                normalized_comments.append(
                    {
                        "author_name": c.get("authorName") or None,
                        "text": c.get("text") or None,
                        "url": kept_url,
                    }
                )

            post = Post(
                linkedin_url=linkedin_url,
                urn=urn,
                feed_compkey=feed_compkey,
                top_comment=self._clean_comment_text(data.get("topComment")),
                top_comment_author=(data.get("topCommentAuthor") or None),
                identifier_candidates=data.get("identifierCandidates", []),
                permalink_candidates=permalink_candidates,
                component_keys=data.get("componentKeys", []),
                ui_permalink_fallback_status=data.get("uiPermalinkFallbackStatus") or None,
                ui_permalink_fallback_error=data.get("uiPermalinkFallbackError") or None,
                author_name=data.get("authorName") or None,
                author_url=data.get("authorUrl") or None,
                actor_name=data.get("actorName") or None,
                actor_url=data.get("actorUrl") or None,
                text=data.get("content") or None,
                posted_date=self._clean_date(data.get("publishedAt", "")),
                reactions_count=self._parse_count(data.get("reactionsText", "")),
                comments_count=self._parse_count(data.get("commentsText", "")),
                reposts_count=self._parse_count(data.get("repostsText", "")),
                image_urls=data.get("images", []),
                video_url=data.get("videoUrl") or None,
                article_url=external_url,
                comments=normalized_comments,
            )
            result.append(post)

        return result

    async def _enrich_missing_comments_from_post_page(self, posts: List[Post]) -> List[Post]:
        """For posts with comments_count but empty comments, open detail page and extract URLs."""
        targets = [
            p for p in posts
            if (p.comments_count or 0) > 0 and not p.comments and p.linkedin_url
        ]
        if not targets:
            return posts

        logger.info("Comments URL fallback on detail pages: %s target(s)", len(targets))
        original_url = self.page.url
        for post in targets:
            try:
                await self.page.goto(post.linkedin_url, wait_until="domcontentloaded", timeout=25000)
                await self.page.wait_for_timeout(900)
                detail_comments = await self._extract_comment_urls_from_current_page()
                if detail_comments:
                    post.comments = detail_comments
                    logger.info(
                        "Comments URL fallback resolved for %s -> %s urls",
                        post.urn,
                        len(detail_comments),
                    )
            except Exception as e:
                logger.debug("Comments URL fallback failed for %s: %s", post.urn, e)
                continue
        try:
            if original_url:
                await self.page.goto(original_url, wait_until="domcontentloaded", timeout=25000)
        except Exception:
            pass
        return posts

    async def _extract_comment_urls_from_current_page(self) -> List[Dict[str, Any]]:
        """Extract comment URLs from currently opened post page."""
        raw_comments = await self.page.evaluate(
            """() => {
                const out = [];
                const seen = new Set();
                const nodes = Array.from(document.querySelectorAll(
                    '.comments-comment-item, [data-id^="urn:li:comment"], .comments-comment-item__main-content'
                ));
                function add(url, author, text) {
                    if (!url) return;
                    const key = `${author||""}|${url}`;
                    if (seen.has(key)) return;
                    seen.add(key);
                    out.push({ authorName: author || null, text: text || null, url });
                }
                for (const n of nodes) {
                    const text = (n.innerText || "").trim().slice(0, 2500);
                    const authorEl = n.querySelector('.comments-post-meta__name-text, .comments-post-meta__name, a[href*="/in/"]');
                    const author = authorEl ? (authorEl.innerText || "").trim().slice(0, 120) : "";
                    n.querySelectorAll("a[href]").forEach(a => {
                        let href = a.getAttribute("href") || "";
                        if (!href || href.startsWith("#") || href.startsWith("javascript")) return;
                        if (!href.startsWith("http")) href = "https://www.linkedin.com" + href;
                        add(href, author, text);
                    });
                    const textUrls = text.match(/(?:https?:\\/\\/|www\\.)[^\\s<>"')]+/gi) || [];
                    textUrls.forEach(u => {
                        let v = u;
                        if (!v.startsWith("http")) v = "https://" + v;
                        add(v, author, text);
                    });
                }
                return out.slice(0, 120);
            }"""
        )

        normalized: List[Dict[str, Any]] = []
        for c in raw_comments or []:
            if not isinstance(c, dict):
                continue
            url = (c.get("url") or "").strip()
            if not url:
                continue
            resolved_url = url
            if (
                "lnkd.in" in url.lower()
                or "/slink" in url.lower()
                or "linkedin.com/redir/" in url.lower()
            ):
                resolved_url = await self._resolve_url(url)
            kept_url = resolved_url if self._is_external_comment_url(resolved_url) else None
            if not kept_url and "lnkd.in" in url.lower() and self._is_external_comment_url(url):
                kept_url = url
            if not kept_url:
                continue
            normalized.append(
                {
                    "author_name": c.get("authorName") or None,
                    "text": c.get("text") or None,
                    "url": kept_url,
                }
            )
        return normalized

    async def _expand_visible_comments_for_url_scrape(
        self, max_clicks: Optional[int] = None
    ) -> int:
        """Ouvre les fils de commentaires visibles pour que leur DOM existe.

        Best effort, ne lève jamais. Le plafond de clics est conservé tel quel :
        chaque clic est une interaction UI sur une session live, et c'est ce type
        de rafale qui avait déclenché le rate limit du 2026-07-22.

        Returns:
            Nombre de contrôles cliqués (0 s'il n'y avait rien à ouvrir).
        """
        budget = self._comment_expand_budget if max_clicks is None else max_clicks
        if budget <= 0:
            logger.debug("Budget d'ouverture de commentaires épuisé pour ce scrape")
            return 0

        try:
            clicked = await self.page.evaluate(_EXPAND_COMMENTS_JS, budget)
        except Exception as exc:
            logger.debug("Expansion des commentaires ignorée : %s", exc)
            return 0

        try:
            clicked = int(clicked or 0)
        except (TypeError, ValueError):
            return 0

        if max_clicks is None:
            self._comment_expand_budget = max(0, self._comment_expand_budget - clicked)

        if clicked:
            logger.info(
                "Fils de commentaires ouverts : %s (budget restant : %s)",
                clicked, self._comment_expand_budget,
            )
            await self.page.wait_for_timeout(1200)
        return clicked

    async def _fill_missing_permalinks_from_ui(
        self,
        posts_data: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Try to recover missing permalinks via resilient UI interactions.

        This is a best-effort fallback for feed cards exposing only `compkey` in DOM.
        It must never fail scraping: errors are returned in per-post attributes.

        Two rate-limit safeguards on top of the fallback itself:
        - a disk cache (urn -> permalink) skips the UI entirely for cards
          already resolved in a previous call/process;
        - a hard cap on how many cards can go through the UI fallback in a
          single call, so a cold cache with many missing permalinks can't
          fire dozens of overflow-menu clicks back to back.
        """
        ui_fallback_attempts = 0
        for data in posts_data:
            if data.get("permalinkUrl"):
                data["uiPermalinkFallbackStatus"] = "not_needed"
                continue

            urn = data.get("urn") or ""
            # R61 — une carte déjà rendue par la session n'a plus besoin de
            # permalien ici ; sans ce garde, chaque passe rouvrirait son menu.
            if urn and urn in self._returned_keys:
                data["uiPermalinkFallbackStatus"] = "skipped_already_returned"
                continue
            cached = get_cached_permalink(urn)
            if cached:
                data["permalinkUrl"] = cached
                data["permalinkCandidates"] = list(
                    dict.fromkeys((data.get("permalinkCandidates", []) or []) + [cached])
                )
                data["uiPermalinkFallbackStatus"] = "resolved_via_cache"
                continue

            # Une seule tentative par carte et par instance : chaque passe de
            # défilement rappelle ce repli, et le clic sur le menu ramène la page
            # sur la carte — retenter une carte rebelle bloque le défilement et
            # répète un geste d'automatisation (essai réel du 2026-10-05).
            if urn and urn in self._ui_fallback_attempted:
                data["uiPermalinkFallbackStatus"] = "skipped_already_attempted"
                continue
            if ui_fallback_attempts >= _MAX_UI_FALLBACK_PER_CALL:
                data["uiPermalinkFallbackStatus"] = "skipped_cap_reached"
                continue
            ui_fallback_attempts += 1
            if urn:
                self._ui_fallback_attempted.add(urn)

            data["uiPermalinkFallbackStatus"] = "no_permalink_found"
            errors: List[str] = []
            component_keys = data.get("componentKeys", []) or []

            # Keep only likely card-level keys (skip random UUID-like component keys).
            card_keys: List[str] = []
            for key in component_keys:
                if not isinstance(key, str):
                    continue
                base = key.replace("expanded", "")
                if "FeedType_" in base:
                    base = base.split("FeedType_", 1)[0]
                if len(base) < 16:
                    continue
                if base.count("-") >= 4:
                    continue
                if base not in card_keys:
                    card_keys.append(base)

            card_locator = None
            for key in card_keys:
                try:
                    locator = self.page.locator(f'[componentkey*="{key}"]').first
                    count = await locator.count()
                    if count > 0:
                        card_locator = locator
                        break
                except Exception as e:
                    errors.append(f"card_lookup_failed:{e}")

            if card_locator is None:
                data["uiPermalinkFallbackError"] = "; ".join(errors + ["card_not_found"])
                continue

            # 1) Liens uniquement dans la carte (sans menu) — évite les faux positifs globaux
            card_dom_candidates: List[str] = []
            try:
                card_dom_candidates = await card_locator.evaluate(
                    """(root) => {
                        function abs(href) {
                            if (!href) return "";
                            if (href.startsWith("//")) return "https:" + href;
                            if (href.startsWith("http")) return href;
                            if (href.startsWith("/")) return "https://www.linkedin.com" + href;
                            return "";
                        }
                        function junk(u) {
                            if (!u) return true;
                            try {
                                var p = new URL(u).pathname.replace(/\\/+$/, "");
                                if (/^\\/company\\/[^\\/]+\\/posts$/.test(p)) return true;
                            } catch (e) {}
                            return false;
                        }
                        function addIf(list, url) {
                            if (!url || junk(url)) return;
                            if (url.indexOf("/feed/update/") !== -1 && url.charAt(url.length - 1) !== "/") url = url + "/";
                            if (list.indexOf(url) === -1) list.push(url);
                        }
                        const out = [];
                        const links = root.querySelectorAll("a[href]");
                        for (var i = 0; i < links.length; i++) {
                            var href = links[i].getAttribute("href") || "";
                            var dec = href;
                            try { dec = decodeURIComponent(href); } catch (e2) {}
                            if (href.indexOf("/feed/update/") !== -1 || dec.indexOf("/feed/update/") !== -1) {
                                var pick = dec.indexOf("/feed/update/") !== -1 ? dec : href;
                                addIf(out, abs(pick.split("#")[0].split("?")[0]));
                            }
                            if (href.indexOf("/posts/") !== -1) addIf(out, abs(href.split("#")[0].split("?")[0]));
                            var m = dec.match(/urn:li:activity:(\\d+)/) || href.match(/urn:li:activity:(\\d+)/);
                            if (m) addIf(out, "https://www.linkedin.com/feed/update/urn:li:activity:" + m[1] + "/");
                        }
                        return out.slice(0, 12);
                    }"""
                )
            except Exception as e:
                errors.append(f"card_dom_scan_failed:{e}")

            if card_dom_candidates:
                data["permalinkCandidates"] = list(
                    dict.fromkeys((data.get("permalinkCandidates", []) or []) + card_dom_candidates)
                )
                data["permalinkUrl"] = data["permalinkUrl"] or card_dom_candidates[0]
                data["uiPermalinkFallbackStatus"] = "resolved_via_card_dom"
                save_cached_permalink(urn, data["permalinkUrl"])
                try:
                    await self.page.keyboard.press("Escape")
                except Exception:
                    pass
                continue

            menu_button_selectors = [
                'button[aria-label*="Plus"]',
                'button[aria-label*="More"]',
                'button[aria-label*="menu"]',
                'button[aria-label*="Menu"]',
                'button[data-control-name*="overflow"]',
                '[data-control-name="overflow_menu"]',
            ]
            menu_btn = None
            for selector in menu_button_selectors:
                try:
                    candidate = card_locator.locator(selector).first
                    if await candidate.count() and await candidate.is_visible():
                        menu_btn = candidate
                        break
                except Exception:
                    continue

            if menu_btn is None:
                ah = (data.get("authorName") or data.get("actorName") or "?")[:48]
                logger.warning(
                    "permalink_fallback [%s]: menu_button_not_found (no overflow control in card)",
                    ah,
                )
                data["uiPermalinkFallbackError"] = "; ".join(errors + ["menu_button_not_found"])
                continue

            try:
                await menu_btn.click(timeout=3000)
                await self.page.wait_for_timeout(350)
            except Exception as e:
                data["uiPermalinkFallbackError"] = "; ".join(errors + [f"menu_click_failed:{e}"])
                continue

            try:
                menu_candidate = await self.page.evaluate(
                    """() => {
                        function abs(href) {
                            if (!href) return "";
                            if (href.startsWith("//")) return "https:" + href;
                            if (href.startsWith("http")) return href;
                            if (href.startsWith("/")) return "https://www.linkedin.com" + href;
                            return "";
                        }
                        function junk(u) {
                            if (!u) return true;
                            try {
                                var p = new URL(u).pathname.replace(/\\/+$/, "");
                                if (/^\\/company\\/[^\\/]+\\/posts$/.test(p)) return true;
                            } catch (e) {}
                            return false;
                        }
                        function addIf(list, url) {
                            if (!url || junk(url)) return;
                            if (url.includes("/feed/update/") && !url.endsWith("/")) url = url + "/";
                            if (!list.includes(url)) list.push(url);
                        }
                        const out = [];
                        const menuRoots = Array.from(document.querySelectorAll(
                            '[role="menu"], [data-test-artdeco-dropdown-content], .artdeco-dropdown__content--is-open, [data-floating-ui-portal] [role="menu"]'
                        ));
                        if (!menuRoots.length) return [];
                        for (const root of menuRoots) {
                            root.querySelectorAll("a[href]").forEach(function(a) {
                                const href = a.getAttribute("href") || "";
                                const decoded = (() => { try { return decodeURIComponent(href); } catch (_) { return href; } })();
                                if (href.includes("/feed/update/") || decoded.includes("/feed/update/")) {
                                    const pick = decoded.includes("/feed/update/") ? decoded : href;
                                    addIf(out, abs(pick.split("#")[0].split("?")[0]));
                                }
                                if (href.includes("/posts/")) addIf(out, abs(href.split("#")[0].split("?")[0]));
                                const m = decoded.match(/urn:li:activity:(\\d+)/) || href.match(/urn:li:activity:(\\d+)/);
                                if (m) addIf(out, "https://www.linkedin.com/feed/update/urn:li:activity:" + m[1] + "/");
                            });
                        }
                        return out.slice(0, 10);
                    }"""
                )
                if menu_candidate:
                    data["permalinkCandidates"] = list(
                        dict.fromkeys((data.get("permalinkCandidates", []) or []) + menu_candidate)
                    )
                    data["permalinkUrl"] = data.get("permalinkUrl") or menu_candidate[0]
                    data["uiPermalinkFallbackStatus"] = "resolved_via_ui_menu"
                    save_cached_permalink(urn, data["permalinkUrl"])
                else:
                    log_label = (data.get("authorName") or data.get("actorName") or "?")[:48]
                    clip_url, clip_diag = await self._try_read_permalink_via_copy_link_menu(
                        log_label=log_label
                    )
                    if clip_url:
                        merged = (data.get("permalinkCandidates", []) or []) + [clip_url]
                        data["permalinkCandidates"] = list(dict.fromkeys(merged))
                        data["permalinkUrl"] = data.get("permalinkUrl") or clip_url
                        data["uiPermalinkFallbackStatus"] = "resolved_via_copy_link_clipboard"
                        save_cached_permalink(urn, data["permalinkUrl"])
                    else:
                        err_parts = errors + ["menu_opened_but_no_permalink"]
                        if clip_diag:
                            err_parts.append(clip_diag)
                        data["uiPermalinkFallbackError"] = "; ".join(err_parts)
            except Exception as e:
                data["uiPermalinkFallbackError"] = "; ".join(errors + [f"menu_extract_failed:{e}"])
            finally:
                try:
                    await self.page.keyboard.press("Escape")
                except Exception:
                    pass
        return posts_data

    @staticmethod
    def _looks_like_linkedin_post_url(url: str) -> bool:
        u = url.strip().lower()
        if "linkedin.com" not in u:
            return False
        if "/feed/update/" in u or "/posts/" in u:
            return True
        return False

    @staticmethod
    def _is_external_comment_url(url: str) -> bool:
        """Keep only external URLs for comments, but allow lnkd.in short-links."""
        try:
            parsed = urlparse(url)
            host = (parsed.netloc or "").lower()
            if not host:
                return False
            if host == "lnkd.in" or host.endswith(".lnkd.in"):
                return True
            if host == "linkedin.com" or host.endswith(".linkedin.com"):
                return False
            # Common false positive from post text ("CLAUDE.md") auto-cast as URL.
            if host in {"claude.md", "www.claude.md"}:
                return False
            # Basic hostname sanity (must contain a dot and a plausible TLD).
            if "." not in host:
                return False
            tld = host.rsplit(".", 1)[-1]
            if len(tld) < 2:
                return False
            return True
        except Exception:
            return False

    @staticmethod
    def _normalize_clipboard_post_url(url: str) -> str:
        u = url.strip().splitlines()[0].strip()
        if "/feed/update/" in u and not u.endswith("/"):
            u = f"{u}/"
        return u

    async def _try_read_permalink_via_copy_link_menu(self, log_label: str = "") -> tuple[Optional[str], str]:
        """Overflow déjà ouvert : clique « Copier le lien vers le post » et lit le presse-papiers.

        Returns:
            (url normalisée ou None, code diagnostic vide si succès — utile logs + ui_permalink_fallback_error)
        """
        label = (log_label or "?").strip() or "?"

        try:
            for origin in ("https://www.linkedin.com", "https://linkedin.com"):
                try:
                    await self.page.context.grant_permissions(
                        ["clipboard-read", "clipboard-write"],
                        origin=origin,
                    )
                except Exception:
                    pass
        except Exception as e:
            logger.debug("permalink_fallback [%s]: grant_permissions skipped: %s", label, e)

        async def _try_click(loc: Any, strategy: str) -> bool:
            try:
                n = await loc.count()
                if n == 0:
                    logger.debug(
                        "permalink copy_link [%s] strategy=%s count=0",
                        label,
                        strategy,
                    )
                    return False
                await loc.last.click(timeout=5000)
                logger.info(
                    "permalink copy_link [%s] clicked strategy=%s (n=%s)",
                    label,
                    strategy,
                    n,
                )
                return True
            except Exception as e:
                logger.warning(
                    "permalink copy_link [%s] strategy=%s click failed: %s",
                    label,
                    strategy,
                    e,
                )
                return False

        clicked = False
        rx = _COPY_LINK_MENU_TEXT_RE

        # The overflow menu can render its items asynchronously right after
        # the "..." click, so a single immediate attempt can race the DOM and
        # miss "Copier le lien" even though it appears a few hundred ms later.
        # Retry the whole strategy cascade a few times before giving up.
        for retry in range(3):
            if retry:
                await self.page.wait_for_timeout(400)

            mv = self.page.locator('[role="menu"]:visible')
            if await mv.count() > 0:
                menu_last = mv.last
                for loc, strategy in (
                    (menu_last.locator('[role="menuitem"]').filter(has_text=rx), "menuitem"),
                    (menu_last.locator("button").filter(has_text=rx), "button_in_menu"),
                    (
                        menu_last.locator(
                            "div.artdeco-dropdown__item, .artdeco-dropdown__item"
                        ).filter(has_text=rx),
                        "artdeco_item_in_menu",
                    ),
                ):
                    if await _try_click(loc, strategy):
                        clicked = True
                        break

            if not clicked:
                dd = self.page.locator(".artdeco-dropdown__content--is-open")
                if await dd.count() > 0:
                    loc = dd.last.locator(
                        "button, [role='menuitem'], .artdeco-dropdown__item, div[role='button']"
                    ).filter(has_text=rx)
                    clicked = await _try_click(loc, "artdeco_dropdown_open")

            if not clicked:
                loc = self.page.locator('[role="menuitem"]').filter(has_text=rx)
                clicked = await _try_click(loc, "menuitem_page_fallback")

            if clicked:
                break

        if not clicked:
            msg = "copy_link_no_matching_control"
            logger.warning(
                "permalink_fallback [%s]: %s (menu ouvert mais aucun bouton « Copier le lien » cliquable)",
                label,
                msg,
            )
            return None, msg

        await self.page.wait_for_timeout(450)

        try:
            text = await self.page.evaluate(
                """async () => {
                    try {
                        return await navigator.clipboard.readText();
                    } catch (e) {
                        return "";
                    }
                }"""
            )
        except Exception as e:
            msg = f"clipboard_read_failed:{e}"
            logger.warning("permalink_fallback [%s]: %s", label, msg)
            return None, msg

        if not text or not isinstance(text, str) or not text.strip():
            msg = "clipboard_empty_after_copy"
            logger.warning("permalink_fallback [%s]: %s", label, msg)
            return None, msg

        url = text.strip().splitlines()[0].strip()

        if "lnkd.in" in url.lower() or "/slink" in url.lower():
            try:
                resolved = await self._resolve_url(url)
                if resolved and resolved != url:
                    logger.info(
                        "permalink_fallback [%s]: resolved short link -> %s",
                        label,
                        resolved[:100],
                    )
                    url = resolved
            except Exception as e:
                logger.debug("permalink_fallback [%s]: short link resolve skip: %s", label, e)

        if not FeedScraper._looks_like_linkedin_post_url(url):
            try:
                url2 = await self._resolve_url(url)
                if url2 and url2 != url and FeedScraper._looks_like_linkedin_post_url(url2):
                    url = url2
            except Exception:
                pass

        if not FeedScraper._looks_like_linkedin_post_url(url):
            msg = f"clipboard_not_a_post_url:{url[:160]}"
            logger.warning("permalink_fallback [%s]: %s", label, msg)
            return None, msg

        if FeedScraper._is_company_posts_feed_listing(url):
            msg = f"clipboard_company_posts_listing:{url[:120]}"
            logger.warning("permalink_fallback [%s]: %s", label, msg)
            return None, msg

        out = FeedScraper._normalize_clipboard_post_url(url)
        logger.info("permalink_fallback [%s]: copy_link_clipboard ok url=%s", label, out[:90])
        return out, ""

    @staticmethod
    def _canonical_activity_urn_from_url(url: Optional[str]) -> Optional[str]:
        """`urn:li:activity:<id>` déduit d'un permalien de post.

        Sert à ne jamais exposer un `urn:li:compkey:` comme identifiant de post :
        le compkey est une clé de carte de feed, éphémère et dépendante de la
        session, donc inutilisable comme clé d'unicité en base ou en entrée de
        `like_post` / `repost_post`.

        Le id numérique porté par un slug `-share-` / `-ugcPost-` est celui de la
        share/ugcPost sous-jacente. On l'expose sous forme `activity` (c'est la
        forme attendue en aval et l'identifiant de déduplication), mais il ne
        faut pas pour autant reconstruire `/feed/update/urn:li:activity:<id>/` à
        partir de là : cette URL peut tomber sur « Post introuvable » alors que le
        permalien d'origine se charge très bien. D'où `linkedin_url` inchangé.
        """
        if not url:
            return None
        match = _POST_URL_URN_ID_RE.search(url) or _POST_URL_SLUG_ID_RE.search(url)
        if not match:
            return None
        return f"urn:li:activity:{match.group(1)}"

    @staticmethod
    def _clean_comment_text(raw: Optional[str]) -> Optional[str]:
        """Texte d'un commentaire sans les lignes d'action (J'aime, Répondre, date)."""
        if not raw:
            return None
        kept: List[str] = []
        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.lower() in _COMMENT_ACTION_LINES:
                continue
            if re.fullmatch(r"\d+\s*(j|h|d|w|sem\.?|min|mois|an[s]?|y)", stripped, re.I):
                continue
            kept.append(stripped)
        return "\n".join(kept)[:1000] or None

    @staticmethod
    def _is_company_posts_feed_listing(url: str) -> bool:
        """True for /company/X/posts (fil d'entreprise), not a permalink de post."""
        try:
            path = urlparse(url).path.rstrip("/")
            return bool(re.match(r"^/company/[^/]+/posts$", path))
        except Exception:
            return False

    @staticmethod
    def _finalize_linkedin_url(
        permalink: Optional[str],
        urn: str,
        permalink_candidates: List[str],
    ) -> Optional[str]:
        """Prefer URL from DOM; else build from activity URN; normalize /feed/update/ trailing slash."""
        options = []
        raw = (permalink or "").strip()
        if raw and not FeedScraper._is_company_posts_feed_listing(raw):
            options.append(raw)
        for candidate in permalink_candidates:
            if (
                candidate
                and candidate not in options
                and not FeedScraper._is_company_posts_feed_listing(candidate)
            ):
                options.append(candidate)

        def score(candidate_url: str) -> int:
            s = 0
            if "/feed/update/" in candidate_url:
                s += 100
            if "/posts/" in candidate_url:
                s += 70
            if "urn:li:activity:" in candidate_url:
                s += 50
            if "/company/" in candidate_url and "/posts/" in candidate_url:
                s -= 80
            if urn.startswith("urn:li:activity:") and urn in candidate_url:
                s += 40
            return s

        url = None
        if options:
            url = sorted(options, key=score, reverse=True)[0]
        if not url and urn.startswith("urn:li:activity:"):
            url = f"https://www.linkedin.com/feed/update/{urn}/"
        if url and "/feed/update/" in url and not url.endswith("/"):
            url = f"{url}/"
        return url

    async def _resolve_url(self, url: str) -> str:
        """Follow redirects (e.g. lnkd.in shortlinks) and return the final destination URL."""
        try:
            response = await self.page.request.get(
                url,
                max_redirects=10,
                timeout=8000,
            )
            final_url = response.url
            await response.dispose()
            return final_url or url
        except Exception as e:
            logger.debug(f"Could not resolve URL {url}: {e}")
            return url

    def _clean_date(self, text: str) -> Optional[str]:
        if not text:
            return None
        return text.split("•")[0].strip() or None

    def _parse_count(self, text: str) -> Optional[int]:
        if not text:
            return None
        try:
            cleaned = re.sub(r"\s", "", text)
            cleaned = re.sub(r"[^\d.,k]", "", cleaned, flags=re.IGNORECASE)
            if "k" in cleaned.lower():
                return int(float(cleaned.lower().replace("k", "").replace(",", ".")) * 1000)
            numbers = re.findall(r"\d+", cleaned)
            if numbers:
                return int("".join(numbers))
        except Exception:
            pass
        return None

    async def _scroll_for_more_posts(self) -> None:
        try:
            # Move mouse to page center, then scroll with wheel (triggers LinkedIn's listeners)
            vp = self.page.viewport_size or {"width": 1280, "height": 720}
            cx = vp["width"] // 2
            cy = vp["height"] // 2
            await self.page.mouse.move(cx, cy)
            await self.page.mouse.wheel(0, 800)
            await self.page.wait_for_timeout(1000)
            await self.page.mouse.wheel(0, 800)
            await self.page.wait_for_timeout(1500)
        except Exception as e:
            logger.debug(f"Error scrolling feed: {e}")
