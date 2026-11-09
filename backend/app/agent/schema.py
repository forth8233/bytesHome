from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

RiskHint = Literal["low", "medium", "high"]


class ProposedAction(BaseModel):
    domain: str = Field(min_length=1, max_length=32)
    service: str = Field(min_length=1, max_length=48)
    entity_id: str | None = Field(default=None, max_length=128)
    data: dict[str, Any] = Field(default_factory=dict)

    @field_validator("domain", "service")
    @classmethod
    def _plain_token(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned or any(ch.isspace() for ch in cleaned) or "." in cleaned:
            raise ValueError("domain and service must be bare tokens")
        return cleaned

    @property
    def full_service(self) -> str:
        return f"{self.domain}.{self.service}"


class AgentProposal(BaseModel):
    should_propose: bool = False
    thought: str = Field(default="", max_length=2000)
    suggestion: str = Field(default="", max_length=500)
    risk_hint: RiskHint = "low"
    actions: list[ProposedAction] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def _proposal_needs_action(self) -> "AgentProposal":
        if self.should_propose and not self.actions:
            raise ValueError("should_propose=true requires at least one action")
        return self
