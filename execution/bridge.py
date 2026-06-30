from abc import ABC, abstractmethod


class TradingBridge(ABC):
    @abstractmethod
    async def enter(self, order: dict) -> dict: ...

    @abstractmethod
    def get_positions(self) -> list[dict]: ...

    @abstractmethod
    async def close(self, position_id: str, reason: str) -> dict: ...

    @abstractmethod
    async def get_account(self) -> dict: ...
