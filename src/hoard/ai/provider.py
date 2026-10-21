"""Provider contract implemented by DeepSeek and future AI backends."""
from __future__ import annotations

from abc import ABC, abstractmethod

from .models import AIRequest, AIResponse


class AIProvider(ABC):
    name: str

    @property
    @abstractmethod
    def configured(self) -> bool:
        """Whether the provider has enough local configuration to make a call."""

    @abstractmethod
    async def complete(self, request: AIRequest) -> AIResponse:
        """Execute exactly one paid provider request; implementations do not retry."""
