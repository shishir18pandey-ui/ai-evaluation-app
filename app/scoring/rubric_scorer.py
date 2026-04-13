"""
Rubric scorer: for each criterion, retrieves relevant evidence and scores 1-5.

Hard rule: NO scoring without evidence. Every score carries citations.
Each criterion has its OWN guidance — a missing feature affects Implementation Quality,
not Problem Understanding.
"""
import logging

from app.core.models import RubricScore, ClaimValidation, UnifiedSubmission
from app.core import config
from app.storage import EvidenceStore
from app.llm import llm_json

logger = logging.getLogger(__name__)



CRITERION_SOURCE_WEIGHTS = {
    "Problem Understanding": ["deck", "video"],
    "Technical Approach": ["deck", "code"],
    "Implementation Quality": ["code", "url"],
    "Innovation / Originality": ["deck", "code"],
    "Communication & Demo Clarity": ["deck", "video"],
}

CRITERION_QUERIES = {
    "Problem Understanding": "problem statement user need pain point motivation",
    "Technical Approach": "architecture design decisions system approach tech stack",
    "Implementation Quality": "code structure error handling tests implementation details",
    "Innovation / Originality": "novel approach unique idea differentiator creative solution",
    "Communication & Demo Clarity": "explanation demo narrative walkthrough clarity presentation",
}


CRITERION_GUIDANCE = {
    "Problem Understanding": (
        "Score whether the problem is clearly defined, real, and well-motivated. "
        "DO NOT penalize for missing implementation — that belongs in Implementation Quality. "
        "If the deck clearly articulates a real user problem, score 4+. "
        "Score 1-2 ONLY if the problem is unclear, trivial, or absent."
    ),
    "Technical Approach": (
        "Score the soundness and appropriateness of the architectural approach. "
        "Consider whether the design makes sense for the problem. "
        "Penalize if the claimed architecture is fundamentally flawed or absent in code. "
        "Tech stack claims marked 'unverifiable' should NOT lower this score — they are neutral."
    ),
    "Implementation Quality": (
        "Score what actually exists in code and the prototype. "
        "Contradicted feature claims (claimed but not implemented) ARE a real signal here. "
        "Working prototype features = positive evidence. "
        "Missing core features = score 1-2. Working basic features in an MVP = score 3+."
    ),
    "Innovation / Originality": (
        "Score whether the approach is novel or creative. "
        "Don't conflate execution quality with innovation. "
        "A simple but novel idea can score well; a complex but generic implementation should score modestly. "
        "Reuse of standard tools (RAG, sentiment analysis) without a twist = score 2-3."
    ),
    "Communication & Demo Clarity": (
        "Score how clearly the deck and demo explain the project. "
        "DO NOT penalize for missing features — that's Implementation Quality's job. "
        "A clear demo of a small feature set is better than a vague demo of many. "
        "Hedging in the demo ('coming soon', 'roadmap') for many features = score 2-3."
    ),
}


SCORING_SYSTEM = """You score a submission on ONE rubric criterion using retrieved evidence.

Scoring scale (1-5):
1 = Absent or deeply flawed — work is essentially missing
2 = Below expectations — significant gaps, multiple contradicted core claims
3 = Adequate / meets basics — most claims supported; some gaps acceptable for an MVP
4 = Strong / above average — claims well-backed with minor gaps
5 = Exceptional — comprehensive evidence across multiple sources

CRITICAL RULES:
- Base your score ONLY on the provided evidence. No outside assumptions.
- Cite specific source_ids (e.g. "slide_3", "src/auth.py", "video_02:10", "url_test_note_creation").
- Each criterion is INDEPENDENT — read the criterion-specific guidance carefully.
- Do NOT conflate criteria. A broken prototype affects Implementation Quality, NOT Problem Understanding.
- Reserve score=1 for genuinely absent work, not "could be better".
- NEVER treat UI quality claims ("clean", "simple", "intuitive") as positive evidence. They are subjective and unverifiable.

Claim status meanings:
- "verified": confirmed across multiple sources, including ground truth (URL/code) — POSITIVE signal
- "partial": one supporting source only — MILD positive
- "unverifiable": tech stack / subjective — NEUTRAL, ignore
- "planned": explicitly future feature — NEUTRAL, do not penalize as missing
- "contradicted": claim does not match reality — REAL negative signal
- "unsupported": claimed only in deck, no backing — mild negative

Return JSON:
{
  "score": 1-5,
  "reasoning": "2-3 sentences grounded in the evidence, referencing claim statuses where relevant",
  "citations": ["source_id1", "source_id2", ...],
  "confidence": 0.0-1.0
}
"""


def _score_one_criterion(
    criterion: str,
    store: EvidenceStore,
    unified: UnifiedSubmission,
    validations: list[ClaimValidation],
) -> RubricScore:
    query = CRITERION_QUERIES.get(criterion, criterion)
    source_filter = CRITERION_SOURCE_WEIGHTS.get(criterion)

    # Retrieve focused + broader evidence
    focused = store.query(query, k=6, source_types=source_filter)
    broad = store.query(query, k=4)

    # Dedup
    seen = set()
    evidence_items = []
    for r in focused + broad:
        sid = r["metadata"].get("source_id", "")
        if sid in seen:
            continue
        seen.add(sid)
        evidence_items.append(r)

    if not evidence_items:
        return RubricScore(
            criterion=criterion,
            score=1,
            reasoning="No evidence retrieved for this criterion.",
            citations=[],
            confidence=0.2,
        )


    SUBJECTIVE_NOISE = [
        "clean", "simple ui", "beautiful", "intuitive", "user-friendly",
        "modern", "elegant", "minimal", "seamless", "smooth", "polished",
    ]

    def _is_subjective_noise(text: str, source_type: str) -> bool:
        if source_type != "deck":
            return False
        t = text.lower()
        noise_hits = sum(1 for kw in SUBJECTIVE_NOISE if kw in t)
        return noise_hits >= 2 and len(t) < 200

    filtered_evidence = [
        r for r in evidence_items
        if not _is_subjective_noise(r["text"], r["metadata"].get("source_type", ""))
    ]
    if not filtered_evidence:
        filtered_evidence = evidence_items  # fallback if everything got filtered

    evidence_text = "\n\n".join(
        f"[{r['metadata'].get('source_id', '?')}] ({r['metadata'].get('source_type', '')})\n"
        f"{r['text'][:400]}"
        for r in filtered_evidence
    )

    # Count all claim statuses including the new categories
    verified_count = sum(1 for v in validations if v.status == "verified")
    partial_count = sum(1 for v in validations if v.status == "partial")
    contradicted_count = sum(1 for v in validations if v.status == "contradicted")
    unsupported_count = sum(1 for v in validations if v.status == "unsupported")
    unverifiable_count = sum(1 for v in validations if v.status == "unverifiable")
    planned_count = sum(1 for v in validations if v.status == "planned")

    # Get the criterion-specific guidance — this is the key fix
    guidance = CRITERION_GUIDANCE.get(
        criterion,
        "Score using your best judgment based on the evidence.",
    )

    context = f"""CRITERION: {criterion}

CRITERION-SPECIFIC GUIDANCE (READ CAREFULLY):
{guidance}

SUBMISSION CONTEXT:
Problem: {unified.problem}
Solution: {unified.solution}
Tech stack: {', '.join(unified.tech_stack) if unified.tech_stack else 'unclear'}

CLAIM VALIDATION SUMMARY:
- Verified (multi-source confirmation, including URL/code): {verified_count}     [POSITIVE]
- Partial (one supporting source only): {partial_count}                          [mild positive]
- Unverifiable (tech stack / subjective claims): {unverifiable_count}            [NEUTRAL — ignore]
- Planned (explicitly future features): {planned_count}                          [NEUTRAL — do not penalize]
- Contradicted (claim does not match prototype/code): {contradicted_count}       [REAL NEGATIVE]
- Unsupported (claimed only in deck, no backing): {unsupported_count}            [mild negative]

REMINDERS:
- This is the {criterion} criterion specifically. Apply the guidance above.
- An MVP with clear problem framing + 1-2 working features = score 3+, not 1.
- "Unverifiable" and "Planned" are NEUTRAL signals — do not let them pull the score down.
- Only "Contradicted" claims represent a true promise-vs-reality gap.

RETRIEVED EVIDENCE:
{evidence_text}

Score this criterion fairly per the guidance above."""

    result = llm_json(SCORING_SYSTEM, context)
    if not isinstance(result, dict):
        result = {}

    score = result.get("score", 3)
    try:
        score = max(1, min(5, int(score)))
    except (ValueError, TypeError):
        score = 3

    # Fallback citations — if LLM didn't cite, use the retrieved source_ids
    citations = result.get("citations", [])
    if not citations:
        citations = [r["metadata"].get("source_id", "") for r in evidence_items[:4]]

    return RubricScore(
        criterion=criterion,
        score=score,
        reasoning=result.get("reasoning", "No reasoning provided."),
        citations=[c for c in citations if c],
        confidence=float(result.get("confidence", 0.7)),
    )


def score_rubric(
    store: EvidenceStore,
    unified: UnifiedSubmission,
    validations: list[ClaimValidation],
) -> list[RubricScore]:
    """Score all rubric criteria. Returns list of RubricScore."""
    scores = []
    for criterion in config.RUBRIC_CRITERIA:
        try:
            scores.append(_score_one_criterion(criterion, store, unified, validations))
        except Exception as e:
            logger.error("Scoring failed for %s: %s", criterion, e)
            scores.append(RubricScore(
                criterion=criterion, score=1,
                reasoning=f"Scoring error: {e}",
                citations=[], confidence=0.0,
            ))
    return scores