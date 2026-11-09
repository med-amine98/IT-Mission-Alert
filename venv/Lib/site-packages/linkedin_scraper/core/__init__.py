"""Core modules for LinkedIn scraper."""

from .browser import BrowserManager
from .auth import (
    login_with_credentials,
    login_with_cookie,
    is_logged_in,
    has_login_form,
    wait_for_manual_login,
    load_credentials_from_env,
    warm_up_browser
)
from .exceptions import (
    LinkedInScraperException,
    AuthenticationError,
    CheckpointError,
    RateLimitError,
    ElementNotFoundError,
    ProfileNotFoundError,
    NetworkError,
    ScrapingError
)
from .utils import (
    retry_async,
    detect_rate_limit,
    wait_for_element_smart,
    extract_text_safe,
    scroll_to_bottom,
    scroll_to_half,
    click_see_more_buttons,
    handle_modal_close,
    is_page_loaded
)
from .rate_limit_guard import (
    CooldownActiveError,
    check_cooldown,
    record_rate_limit,
    enforce_write_action_pacing,
    humanize_delay_ms,
)
from .permalink_cache import get_cached_permalink, save_cached_permalink

__all__ = [
    # Browser
    'BrowserManager',
    # Auth
    'login_with_credentials',
    'login_with_cookie',
    'is_logged_in',
    'has_login_form',
    'wait_for_manual_login',
    'load_credentials_from_env',
    'warm_up_browser',
    # Exceptions
    'LinkedInScraperException',
    'AuthenticationError',
    'CheckpointError',
    'RateLimitError',
    'ElementNotFoundError',
    'ProfileNotFoundError',
    'NetworkError',
    'ScrapingError',
    # Utils
    'retry_async',
    'detect_rate_limit',
    'wait_for_element_smart',
    'extract_text_safe',
    'scroll_to_bottom',
    'scroll_to_half',
    'click_see_more_buttons',
    'handle_modal_close',
    'is_page_loaded',
    # Rate limit guard
    'CooldownActiveError',
    'check_cooldown',
    'record_rate_limit',
    'enforce_write_action_pacing',
    'humanize_delay_ms',
    # Permalink cache
    'get_cached_permalink',
    'save_cached_permalink',
]
