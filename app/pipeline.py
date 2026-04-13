
import logging
from typing import Callable, Optional
from dataclasses import dataclass

from app.core.models import (
    Evidence, SubmissionReport, PrototypeValidation,
)
from app.core.unifier import build_unified_submission
from app.storage import EvidenceStore
from app.extractors import (
    extract_from_deck, extract_from_transcript,
    extract_from_repo, validate_prototype, prototype_to_evidence,
)
from app.validators import validate_claims
from app.scoring import score_rubric

logger = logging.getLogger(__name__)


@dataclass
class SubmissionInput:
    """Bundled inputs for one evaluation."""
    submission_id: str
    deck_path: Optional[str] = None
    transcript_text: Optional[str] = None
    transcript_path: Optional[str] = None
    repo_url_or_path: Optional[str] = None
    prototype_url: Optional[str] = None


# Progress callback signature: fn(step_name, status, detail)
# status in {"start", "ok", "warn", "fail"}
ProgressFn = Callable[[str, str, str], None]


def _noop(*args, **kwargs):
    pass


def run_pipeline(
    inputs: SubmissionInput,
    on_progress: ProgressFn = _noop,
) -> SubmissionReport:
    """Execute the full evaluation pipeline. Returns a SubmissionReport."""

    all_evidence: list[Evidence] = []
    prototype_result: Optional[PrototypeValidation] = None

    if inputs.deck_path:
        on_progress("Parsing deck", "start", inputs.deck_path)
        try:
            deck_ev = extract_from_deck(inputs.deck_path)
            all_evidence.extend(deck_ev)
            on_progress("Parsing deck", "ok", f"{len(deck_ev)} claims extracted")
        except Exception as e:
            logger.exception("Deck extraction failed")
            on_progress("Parsing deck", "fail", str(e))
    else:
        on_progress("Parsing deck", "warn", "no deck provided")

    transcript = inputs.transcript_text
    if not transcript and inputs.transcript_path:
        try:
            from pathlib import Path
            transcript = Path(inputs.transcript_path).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            transcript = None

    if transcript:
        on_progress("Processing transcript", "start", "")
        try:
            video_ev = extract_from_transcript(transcript)
            all_evidence.extend(video_ev)
            on_progress("Processing transcript", "ok", f"{len(video_ev)} segments")
        except Exception as e:
            logger.exception("Transcript extraction failed")
            on_progress("Processing transcript", "fail", str(e))
    else:
        on_progress("Processing transcript", "warn", "no transcript provided")

    if inputs.repo_url_or_path:
        on_progress("Analyzing code", "start", inputs.repo_url_or_path)
        try:
            code_ev = extract_from_repo(inputs.repo_url_or_path)
            all_evidence.extend(code_ev)
            on_progress("Analyzing code", "ok", f"{len(code_ev)} capabilities identified")
        except Exception as e:
            logger.exception("Code extraction failed")
            on_progress("Analyzing code", "fail", str(e))
    else:
        on_progress("Analyzing code", "warn", "no repo provided")

    if not all_evidence:
        on_progress("Pipeline", "fail",
                    "No evidence extracted from any source. Cannot proceed.")
        return SubmissionReport(
            submission_id=inputs.submission_id,
            summary="Insufficient input — no evidence extracted.",
            unified_submission=None,  # type: ignore
            prototype_validation=None,
            claim_validations=[],
            scores=[],
        )

    on_progress("Storing evidence", "start", "")
    store = EvidenceStore(inputs.submission_id)
    store.reset()  # fresh store for new pipeline run
    store.add(all_evidence)
    counts = store.count_by_source()
    on_progress("Storing evidence", "ok", f"by source: {counts}")

    on_progress("Building unified understanding", "start", "")
    unified = build_unified_submission(store)
    on_progress("Building unified understanding", "ok",
                f"{len(unified.claimed_features)} features identified")

    if inputs.prototype_url:
        on_progress("Validating prototype", "start", inputs.prototype_url)
        try:
            # Test the top few claimed features
            features_to_test = unified.claimed_features[:5] or ["main functionality"]
            prototype_result = validate_prototype(inputs.prototype_url, features_to_test)
            # Add prototype evidence into the store so it informs scoring
            proto_ev = prototype_to_evidence(prototype_result)
            store.add(proto_ev)
            status = "ok" if prototype_result.accessible else "warn"
            detail = (f"{len(prototype_result.features_tested)} features tested"
                      if prototype_result.accessible else "URL inaccessible")
            on_progress("Validating prototype", status, detail)
        except Exception as e:
            logger.exception("Prototype validation failed")
            on_progress("Validating prototype", "fail", str(e))
    else:
        on_progress("Validating prototype", "warn", "no URL provided")

    on_progress("Cross-referencing claims", "start", "")
    deck_claims = [e.claim_or_fact for e in all_evidence if e.source_type == "deck"]
    claims_to_validate = list(dict.fromkeys(
        unified.claimed_features + deck_claims
    ))[:15]  # dedup + cap
    validations = validate_claims(claims_to_validate, store)
    verified = sum(1 for v in validations if v.status == "verified")
    contradicted = sum(1 for v in validations if v.status == "contradicted")
    on_progress("Cross-referencing claims", "ok",
                f"{verified} verified, {contradicted} contradicted of {len(validations)}")

    on_progress("Scoring against rubric", "start", "")
    scores = score_rubric(store, unified, validations)
    avg = sum(s.score for s in scores) / len(scores) if scores else 0
    on_progress("Scoring against rubric", "ok", f"average {avg:.1f}/5")

    summary = (f"{unified.problem} {unified.solution} "
               f"Overall score: {avg:.1f}/5 across {len(scores)} criteria.")

    return SubmissionReport(
        submission_id=inputs.submission_id,
        summary=summary,
        unified_submission=unified,
        prototype_validation=prototype_result,
        claim_validations=validations,
        scores=scores,
    )
