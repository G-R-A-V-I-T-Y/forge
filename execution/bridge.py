from abc import ABC, abstractmethod


class TradingBridge(ABC):
    @abstractmethod
    def enter(self, order: dict) -> dict: ...

    @abstractmethod
    def get_positions(self) -> list[dict]: ...

    @abstractmethod
    def close(self, position_id: str, reason: str) -> dict: ...

    @abstractmethod
    def get_account(self) -> dict: ...
