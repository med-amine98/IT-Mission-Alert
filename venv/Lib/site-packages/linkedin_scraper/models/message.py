"""Pydantic model for a LinkedIn messaging message."""

from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel

MessageDirection = Literal["inbound", "outbound", "unknown"]


class Message(BaseModel):
    conversation_id: str
    message_id: Optional[str] = None
    sender_name: Optional[str] = None
    sender_url: Optional[str] = None
    direction: MessageDirection = "unknown"
    text: Optional[str] = None
    sent_at: Optional[str] = None
    raw_event_text: Optional[str] = None

    _DEFAULT_PUBLIC_EXCLUDE = {"raw_event_text"}

    def to_dict(self) -> Dict[str, Any]:
        return self.model_dump()

    def to_public_dict(self) -> Dict[str, Any]:
        return self.model_dump(exclude=self._DEFAULT_PUBLIC_EXCLUDE)

    def to_json(self, **kwargs) -> str:
        return self.model_dump_json(**kwargs)

    def __repr__(self) -> str:
        preview = (self.text or "")[:60]
        return (
            f"<Message id={self.message_id!r} direction={self.direction!r} "
            f"text={preview!r}>"
        )
