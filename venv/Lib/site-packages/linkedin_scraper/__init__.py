"""LinkedIn Scraper - Async Playwright-based scraper for LinkedIn."""

from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("linkedin_scraper")
except PackageNotFoundError:
    # Package not installed (running directly from source)
    __version__ = "dev"

# Core modules
from .core import (
    BrowserManager,
    login_with_credentials,
    login_with_cookie,
    is_logged_in,
    wait_for_manual_login,
    load_credentials_from_env,
    # Exceptions
    LinkedInScraperException,
    AuthenticationError,
    CheckpointError,
    RateLimitError,
    ElementNotFoundError,
    ProfileNotFoundError,
    NetworkError,
    ScrapingError,
)

# Scrapers
from .scrapers import (
    PersonScraper,
    CompanyScraper,
    JobScraper,
    JobSearchScraper,
    CompanyPostsScraper,
    FeedScraper,
    InvitationScraper,
    MessagingScraper,
)

# Callbacks
from .callbacks import (
    ProgressCallback,
    ConsoleCallback,
    SilentCallback,
    JSONLogCallback,
    MultiCallback,
)

# Models
from .models import (
    Person,
    Experience,
    Education,
    Contact,
    Accomplishment,
    Interest,
    Company,
    CompanySummary,
    Employee,
    Job,
    Post,
    Invitation,
    Conversation,
    Message,
)

__all__ = [
    # Version
    "__version__",
    # Core
    "BrowserManager",
    "login_with_credentials",
    "login_with_cookie",
    "is_logged_in",
    "wait_for_manual_login",
    "load_credentials_from_env",
    # Scrapers
    "PersonScraper",
    "CompanyScraper",
    "JobScraper",
    "JobSearchScraper",
    "CompanyPostsScraper",
    "FeedScraper",
    "InvitationScraper",
    "MessagingScraper",
    # Exceptions
    "LinkedInScraperException",
    "AuthenticationError",
    "CheckpointError",
    "RateLimitError",
    "ElementNotFoundError",
    "ProfileNotFoundError",
    "NetworkError",
    "ScrapingError",
    # Callbacks
    "ProgressCallback",
    "ConsoleCallback",
    "SilentCallback",
    "JSONLogCallback",
    "MultiCallback",
    # Models
    "Person",
    "Experience",
    "Education",
    "Contact",
    "Accomplishment",
    "Interest",
    "Company",
    "CompanySummary",
    "Employee",
    "Job",
    "Post",
    "Invitation",
    "Conversation",
    "Message",
]
