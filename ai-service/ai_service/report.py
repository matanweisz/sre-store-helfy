"""Structured incident report — the machine-readable counterpart to the agent's
prose note. Produced by a post-hoc structuring pass over the investigation, so
callers get both a human narrative (``insight``) and a typed object (``report``).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Confidence = Literal["low", "medium", "high"]


class IncidentReport(BaseModel):
    """A triage conclusion in structured form. Mirrors the prose note's sections."""

    anomaly: str = Field(..., description="What is anomalous, in one sentence.")
    window: str = Field(..., description="Time window analyzed, e.g. 'last 15 minutes'.")
    supporting_evidence: list[str] = Field(
        default_factory=list, description="Observations that confirm the anomaly."
    )
    negative_evidence: list[str] = Field(
        default_factory=list, description="Observations that rule out alternative causes."
    )
    root_cause: str = Field(..., description="The most likely root cause.")
    next_action: str = Field(..., description="One concrete next action for the on-call.")
    confidence: Confidence = Field("medium", description="Confidence in the conclusion.")

    def to_markdown(self) -> str:
        def bullets(items: list[str]) -> str:
            return "\n".join(f"- {i}" for i in items) if items else "- (none recorded)"

        return (
            f"🚨 **Incident Note — {self.anomaly}**\n"
            f"*Window analyzed: {self.window} · confidence: {self.confidence}*\n\n"
            f"**Supporting evidence**\n{bullets(self.supporting_evidence)}\n\n"
            f"**Negative evidence (rules out alternative causes)**\n"
            f"{bullets(self.negative_evidence)}\n\n"
            f"**Root cause.** {self.root_cause}\n\n"
            f"**Next action.** {self.next_action}\n"
        )


def incident_report_schema() -> dict[str, Any]:
    """JSON schema for IncidentReport, tightened for provider strict modes.

    Strict structured-output modes (OpenAI/OpenRouter ``json_schema`` with
    ``strict: true``) require ``additionalProperties: false`` and every property
    listed in ``required``.
    """
    schema = IncidentReport.model_json_schema()
    schema["additionalProperties"] = False
    schema["required"] = list(schema.get("properties", {}).keys())
    return schema
