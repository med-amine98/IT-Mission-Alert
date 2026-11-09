from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field


class Post(BaseModel):
    linkedin_url: Optional[str] = None
    urn: Optional[str] = None
    # Clé de carte de feed (urn:li:compkey:…) quand LinkedIn n'expose que
    # celle-ci dans le DOM. Éphémère et dépendante du rendu : conservée pour
    # le debug uniquement, `urn` porte l'identifiant canonique.
    feed_compkey: Optional[str] = None
    author_name: Optional[str] = None
    author_url: Optional[str] = None
    # For activity posts ("Y a liké/commenté/republié ce contenu de X"):
    # actor_name/actor_url = Y (the network contact who triggered the feed appearance)
    actor_name: Optional[str] = None
    actor_url: Optional[str] = None
    text: Optional[str] = None
    posted_date: Optional[str] = None
    reactions_count: Optional[int] = None
    comments_count: Optional[int] = None
    reposts_count: Optional[int] = None
    image_urls: List[str] = Field(default_factory=list)
    video_url: Optional[str] = None
    article_url: Optional[str] = None
    # `comments` ne contient que les commentaires porteurs d'un lien externe
    # (alimente le pipeline d'extraction de liens). `top_comment` est le texte
    # du premier commentaire visible, sans filtre.
    comments: List[Dict[str, Any]] = Field(default_factory=list)
    top_comment: Optional[str] = None
    top_comment_author: Optional[str] = None
    # Extra debug/trace fields for difficult feed cards (compkey, repost wrappers, A/B layouts)
    identifier_candidates: List[str] = Field(default_factory=list)
    permalink_candidates: List[str] = Field(default_factory=list)
    component_keys: List[str] = Field(default_factory=list)
    ui_permalink_fallback_status: Optional[str] = None
    ui_permalink_fallback_error: Optional[str] = None

    _DEFAULT_PUBLIC_EXCLUDE = {
        "identifier_candidates",
        "permalink_candidates",
        "component_keys",
    }
    
    def to_dict(self) -> Dict[str, Any]:
        return self.model_dump()

    def to_public_dict(self) -> Dict[str, Any]:
        """Compact export used by scripts/MCP output (without heavy debug arrays)."""
        return self.model_dump(exclude=self._DEFAULT_PUBLIC_EXCLUDE)
    
    def to_json(self, **kwargs) -> str:
        return self.model_dump_json(**kwargs)
    
    def __repr__(self) -> str:
        text_preview = self.text[:80] + "..." if self.text and len(self.text) > 80 else self.text
        return (
            f"<Post\n"
            f"  Text: {text_preview}\n"
            f"  Posted: {self.posted_date}\n"
            f"  Reactions: {self.reactions_count}\n"
            f"  Comments: {self.comments_count}>"
        )
