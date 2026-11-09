from typing import TYPE_CHECKING, List, Literal, Optional
from patchright._impl._api_structures import NameValue

if TYPE_CHECKING:
    from patchright._impl._page import Page
WebStorageKind = Literal["local", "session"]


class WebStorage:
    def __init__(self, page: "Page", kind: WebStorageKind) -> None:
        self._page = page
        self._kind = kind
        self._loop = page._loop
        self._dispatcher_fiber = page._dispatcher_fiber

    async def items(self) -> List[NameValue]:
        result = await self._page._channel.send_return_as_dict(
            "webStorageItems", None, {"kind": self._kind}
        )
        return (result or {}).get("items", [])

    async def get_item(self, name: str) -> Optional[str]:
        result = await self._page._channel.send_return_as_dict(
            "webStorageGetItem", None, {"kind": self._kind, "name": name}
        )
        return (result or {}).get("value")

    async def set_item(self, name: str, value: str) -> None:
        await self._page._channel.send(
            "webStorageSetItem",
            None,
            {"kind": self._kind, "name": name, "value": value},
        )

    async def remove_item(self, name: str) -> None:
        await self._page._channel.send(
            "webStorageRemoveItem", None, {"kind": self._kind, "name": name}
        )

    async def clear(self) -> None:
        await self._page._channel.send("webStorageClear", None, {"kind": self._kind})
