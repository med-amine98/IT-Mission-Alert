"""Pydantic model for a LinkedIn connection invitation."""

from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel

InvitationKind = Literal[
    "connection",
    "follow_person",
    "follow_company",
    "follow_newsletter",
    "follow_showcase_page",
    "event_invitation",
    "unknown",
]


class Invitation(BaseModel):
    invitation_id: str
    profile_name: Optional[str] = None
    profile_url: Optional[str] = None
    headline: Optional[str] = None
    shared_connection_count: Optional[int] = None
    message: Optional[str] = None
    received_at: Optional[str] = None
    invitation_kind: InvitationKind = "unknown"
    inviter_name: Optional[str] = None
    inviter_url: Optional[str] = None
    target_name: Optional[str] = None
    target_url: Optional[str] = None
    display_text: Optional[str] = None
    raw_card_text: Optional[str] = None

    _DEFAULT_PUBLIC_EXCLUDE = {"raw_card_text"}

    def to_dict(self) -> Dict[str, Any]:
        return self.model_dump()

    def to_public_dict(self) -> Dict[str, Any]:
        """Compact export for scripts/MCP (without debug fields)."""
        return self.model_dump(exclude=self._DEFAULT_PUBLIC_EXCLUDE)

    def to_json(self, **kwargs) -> str:
        return self.model_dump_json(**kwargs)

    def __repr__(self) -> str:
        return (
            f"<Invitation id={self.invitation_id!r} "
            f"name={self.profile_name!r} headline={self.headline!r}>"
        )
