"""IncidentReport rendering + strict-schema shape."""

from __future__ import annotations

from ai_service.report import IncidentReport, incident_report_schema


def _sample() -> IncidentReport:
    return IncidentReport(
        anomaly="Payment failure rate elevated",
        window="last 15 minutes",
        supporting_evidence=["failure ratio 0.40 vs 0.08 baseline"],
        negative_evidence=["db payment_record p95 flat at 24ms"],
        root_cause="PAYMENT_FAILURE_RATE was bumped",
        next_action="check the env var on the backend container",
        confidence="high",
    )


def test_to_markdown_has_all_sections() -> None:
    md = _sample().to_markdown()
    assert "Incident Note — Payment failure rate elevated" in md
    assert "last 15 minutes" in md and "confidence: high" in md
    assert "Supporting evidence" in md and "failure ratio 0.40 vs 0.08 baseline" in md
    assert "Negative evidence" in md and "p95 flat at 24ms" in md
    assert "**Root cause.**" in md and "**Next action.**" in md


def test_to_markdown_handles_empty_evidence_and_default_confidence() -> None:
    r = IncidentReport(anomaly="a", window="w", root_cause="rc", next_action="na")
    md = r.to_markdown()
    assert "(none recorded)" in md
    assert "confidence: medium" in md  # the default


def test_schema_is_strict_compatible() -> None:
    schema = incident_report_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"].keys())
    assert "confidence" in schema["properties"]
