"""Pydantic model for a LinkedIn messaging conversation."""

from typing import Any, Dict, Optional

from pydantic import BaseModel


class Conversation(BaseModel):
    conversation_id: str
    participant_name: Optional[str] = None
    participant_url: Optional[str] = None
    last_message_preview: Optional[str] = None
    last_activity_at: Optional[str] = None
    unread_count: Optional[int] = None
    raw_item_text: Optional[str] = None

    _DEFAULT_PUBLIC_EXCLUDE = {"raw_item_text"}

    def to_dict(self) -> Dict[str, Any]:
        return self.model_dump()

    def to_public_dict(self) -> Dict[str, Any]:
        return self.model_dump(exclude=self._DEFAULT_PUBLIC_EXCLUDE)

    def to_json(self, **kwargs) -> str:
        return self.model_dump_json(**kwargs)

    def __repr__(self) -> str:
        return (
            f"<Conversation id={self.conversation_id!r} "
            f"participant={self.participant_name!r}>"
        )
