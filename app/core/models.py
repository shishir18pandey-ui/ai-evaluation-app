
from dataclasses import dataclass, field, asdict
from typing import Literal, Optional
from datetime import datetime
import uuid


SourceType = Literal["deck", "video", "code", "url"]
ClaimStatus = Literal["verified", "partial", "unsupported", "contradicted", "not_testable", "unverifiable", "planned"]
@dataclass
class Evidence:
    """
    Single piece of evidence extracted from an artefact.
    This is the atomic unit that gets stored in the vector DB.
    """
    source_type: SourceType
    source_id: str               
    claim_or_fact: str          
    evidence_text: str        
    confidence: float = 0.8      
    metadata: dict = field(default_factory=dict)
    evidence_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ClaimValidation:
    """Result of cross-referencing a single claim against all evidence."""
    claim: str
    status: ClaimStatus
    supporting_evidence: list[Evidence] = field(default_factory=list)
    contradicting_evidence: list[Evidence] = field(default_factory=list)
    reasoning: str = ""


@dataclass
class PrototypeValidation:
    """Result of probing the live prototype URL."""
    url: str
    accessible: bool
    page_title: Optional[str] = None
    features_tested: list[dict] = field(default_factory=list)  # [{feature, status, evidence}]
    errors: list[str] = field(default_factory=list)
    screenshots: list[str] = field(default_factory=list)       # file paths


@dataclass
class UnifiedSubmission:
    """The 'single source of truth' merged from all artefacts."""
    problem: str
    solution: str
    claimed_features: list[str]
    implementation_depth: str
    tech_stack: list[str] = field(default_factory=list)
    sources: dict = field(default_factory=dict)  # which artefact contributed what


@dataclass
class RubricScore:
    """Single rubric criterion score with citations."""
    criterion: str
    score: int                      # 1-5
    reasoning: str
    citations: list[str]            # ["slide_5", "video_02:10", "src/auth.py"]
    confidence: float               # 0-1


@dataclass
class SubmissionReport:
    """Final output — what gets returned as JSON."""
    submission_id: str
    summary: str
    unified_submission: UnifiedSubmission
    prototype_validation: Optional[PrototypeValidation]
    claim_validations: list[ClaimValidation]
    scores: list[RubricScore]
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    def to_json_dict(self) -> dict:
        """Convert to a JSON-serializable dict matching the assignment's output spec."""
        return {
            "submission_id": self.submission_id,
            "summary": self.summary,
            "unified_submission": asdict(self.unified_submission),
            "prototype_validation": asdict(self.prototype_validation) if self.prototype_validation else None,
            "claim_validation": {
                "total_claims": len(self.claim_validations),
                "verified": sum(1 for c in self.claim_validations if c.status == "verified"),
                "partial": sum(1 for c in self.claim_validations if c.status == "partial"),
                "unsupported": sum(1 for c in self.claim_validations if c.status == "unsupported"),
                "contradicted": sum(1 for c in self.claim_validations if c.status == "contradicted"),
                "unverifiable": sum(1 for c in self.claim_validations if c.status == "unverifiable"),
                "planned": sum(1 for c in self.claim_validations if c.status == "planned"),
                "details": [
                    {
                        "claim": c.claim,
                        "status": c.status,
                        "reasoning": c.reasoning,
                        "supporting_sources": [e.source_id for e in c.supporting_evidence],
                        "contradicting_sources": [e.source_id for e in c.contradicting_evidence],
                    }
                    for c in self.claim_validations
                ],
            },
            "scores": [asdict(s) for s in self.scores],
            "created_at": self.created_at,
        }
