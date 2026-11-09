"""Custom exceptions for LinkedIn scraper."""


class LinkedInScraperException(Exception):
    """Base exception for LinkedIn scraper."""
    pass


class AuthenticationError(LinkedInScraperException):
    """Raised when authentication fails."""
    pass


class CheckpointError(AuthenticationError):
    """Raised when LinkedIn shows a security checkpoint / challenge.

    Distincte d'une session expirée : les cookies restent valides, mais LinkedIn
    exige une vérification humaine (code e-mail, puzzle, confirmation d'appareil).
    Réessayer ne fait qu'aggraver le signal côté LinkedIn — il faut résoudre le
    challenge dans un navigateur, puis regénérer la session de scraping.

    Sous-classe d'``AuthenticationError`` pour ne rien casser chez les appelants
    qui l'attrapent déjà.
    """
    pass


class RateLimitError(LinkedInScraperException):
    """Raised when rate limiting is detected."""
    
    def __init__(self, message: str, suggested_wait_time: int = 300):
        super().__init__(message)
        self.suggested_wait_time = suggested_wait_time


class ElementNotFoundError(LinkedInScraperException):
    """Raised when an expected element is not found."""
    pass


class ProfileNotFoundError(LinkedInScraperException):
    """Raised when a profile/page returns 404."""
    pass


class NetworkError(LinkedInScraperException):
    """Raised when network-related issues occur."""
    pass


class ScrapingError(LinkedInScraperException):
    """Raised when scraping fails for various reasons."""
    pass
