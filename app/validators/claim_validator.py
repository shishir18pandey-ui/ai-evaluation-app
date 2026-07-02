"""
Claim validator: cross-references claims across sources with strict source roles.

OPTIMIZED: instead of judging each evidence piece separately (6 calls per claim),
batch all retrieved evidence for a claim into ONE judging call.

SOURCE ROLES (enforced):
- deck:  CLAIMS ONLY — never validates itself
- video: SUPPORTING evidence
- code:  IMPLEMENTATION evidence
- url:   GROUND TRUTH

CLAIM TYPES:
- verifiable: testable against code/url
- unverifiable: tech-stack claims testable only from code
- subjective: UI quality claims — not DOM-searchable
- planned: future features — exempt from contradiction
"""
import logging
import re

from app.core.models import Evidence, ClaimValidation
from app.storage import EvidenceStore
from app.llm import llm_json

logger = logging.getLogger(__name__)


PLANNED_PATTERNS = [
    r"\bwill\s+(?:be|have|support|include|add)\b",
    r"\bplanned\b", r"\bfuture\b", r"\broadmap\b", r"\bcoming soon\b",
    r"\bnext (?:release|version|phase)\b", r"\bto be (?:added|implemented|built)\b",
]

SUBJECTIVE_KEYWORDS = [
    "clean", "simple", "beautiful", "intuitive", "user-friendly", "user friendly",
    "modern", "elegant", "minimal", "minimalist", "easy to use", "seamless",
    "smooth", "polished", "delightful", "responsive design",
]

TECH_STACK_KEYWORDS = [
    "built with", "built using", "written in", "framework",
    "node.js", "nodejs", "node", "python", "react", "vue", "angular",
    "flask", "fastapi", "django", "express", "next.js", "nextjs",
    "html", "css", "javascript", "typescript", "tailwind",
    "postgresql", "postgres", "mongodb", "mysql", "redis", "sqlite",
    "docker", "kubernetes", "aws", "gcp", "azure",
    "architecture:", "stack:", "tech stack",
]


def _classify_claim(claim: str) -> str:
    """Categorize the claim before validation. Order matters."""
    claim_lower = claim.lower()
    for pattern in PLANNED_PATTERNS:
        if re.search(pattern, claim_lower):
            return "planned"
    if any(kw in claim_lower for kw in SUBJECTIVE_KEYWORDS):
        return "subjective"
    if any(kw in claim_lower for kw in TECH_STACK_KEYWORDS):
        return "unverifiable"
    return "verifiable"


# ── Batched judge: judge ALL evidence for one claim in ONE call ─────
BATCH_JUDGE_SYSTEM = """You evaluate multiple pieces of evidence against a single claim, in one pass.

Each evidence item must first pass a topic-match test: is this evidence actually
ABOUT THE SAME feature/action/endpoint as the claim? If not, the verdict is
"irrelevant" — full stop, regardless of how it reads on its own. Do NOT reason
"the code does X, the claim is about Y, so this contradicts it" — different
topics are irrelevant, not contradicting.

Only for evidence that IS about the same topic as the claim, decide:
- "supports": evidence confirms that same feature/action is implemented/true
- "contradicts": evidence shows that same feature working differently, broken,
  or explicitly absent. INCLUDES:
  * Evidence stating "NOT PRESENT" or "does not function" in prototype
  * Code implementing the SAME claimed feature in a different way (e.g. claim
    says "ML-based sentiment scoring", code for that same feature shows
    rule-based keyword matching instead)
  * Demo narration backing away from the claim
- "irrelevant": different topic than the claim, tangential, or too weak to judge

CRITICAL:
- If evidence says a feature is NOT PRESENT/NOT FOUND, and the claim asserts
  the feature exists, that is "contradicts" — not "irrelevant".
- Evidence describing a DIFFERENT capability than the claim (e.g. claim is
  "delete a task", evidence is "creates a task via POST /tasks") is
  "irrelevant" — not "contradicts" and not "supports". When in doubt whether
  two things are "the same feature", default to "irrelevant".

Return JSON: {"verdicts": [{"index": 0, "verdict": "supports|contradicts|irrelevant", "reasoning": "one line"}, ...]}
Include one verdict per evidence item, in the order given.
"""


def _batch_judge_evidence(claim: str, evidences: list[dict]) -> list[dict]:
    """ONE LLM call to judge all evidence pieces for a claim."""
    if not evidences:
        return []

    evidence_blob = "\n\n".join(
        f"[{i}] (source: {e['metadata'].get('source_type', '?')})\n{e['text'][:300]}"
        for i, e in enumerate(evidences)
    )
    user_msg = f"""CLAIM: {claim}

EVIDENCE ITEMS TO JUDGE:
{evidence_blob}

Judge each item."""

    try:
        result = llm_json(BATCH_JUDGE_SYSTEM, user_msg)
    except Exception as e:
        logger.warning("Batch judge failed for claim '%s': %s", claim[:50], e)
        return [{"index": i, "verdict": "irrelevant", "reasoning": "judge failed"}
                for i in range(len(evidences))]

    if not isinstance(result, dict):
        return [{"index": i, "verdict": "irrelevant", "reasoning": "parse failed"}
                for i in range(len(evidences))]

    verdicts = result.get("verdicts", [])
    if not isinstance(verdicts, list):
        return [{"index": i, "verdict": "irrelevant", "reasoning": "bad format"}
                for i in range(len(evidences))]

    # Normalize: ensure one verdict per evidence item, in order
    by_index = {}
    for v in verdicts:
        if isinstance(v, dict) and "index" in v:
            try:
                by_index[int(v["index"])] = v
            except (ValueError, TypeError):
                continue

    normalized = []
    for i in range(len(evidences)):
        if i in by_index:
            normalized.append(by_index[i])
        else:
            normalized.append({"index": i, "verdict": "irrelevant", "reasoning": "missing"})
    return normalized


def _decide_status(judgments: list[dict], claim_type: str) -> tuple[str, str]:
    """Decide claim status from judgments + claim_type."""
    supporting_sources = set()
    contradicting_sources = set()

    for j in judgments:
        source_type = j.get("source_type", "")
        verdict = j.get("verdict", "")
        # ENFORCE: deck never contradicts
        if source_type == "deck" and verdict == "contradicts":
            continue
        if verdict == "supports":
            supporting_sources.add(source_type)
        elif verdict == "contradicts":
            contradicting_sources.add(source_type)

    if claim_type == "planned":
        return "planned", "Marked as planned/future — not evaluated against current implementation."

    if claim_type == "subjective":
        return "unverifiable", "Subjective claim (UI quality) — not objectively testable."

    if claim_type == "unverifiable":
        if "code" in contradicting_sources:
            return "contradicted", "Code does not match the claimed tech stack."
        if "code" in supporting_sources:
            return "verified", "Tech stack confirmed by code evidence."
        return "unverifiable", "Tech-stack claim not verifiable from prototype or video alone."

    # Verifiable claims
    # Verifiable claims
    if "url" in contradicting_sources:
        sources_list = ", ".join(sorted(contradicting_sources))
        return "contradicted", f"Contradicted by ground truth ({sources_list})."

    if "code" in contradicting_sources:
        return "contradicted", "Code does not implement the claimed feature."

    # Strongest: prototype confirms it works
    if "url" in supporting_sources:
        return "verified", "Confirmed by working prototype."

    # Strong: implemented AND demonstrated
    if "code" in supporting_sources and "video" in supporting_sources:
        return "verified", "Implemented in code and demonstrated in video."

    # Code-only: verified at implementation level
    if "code" in supporting_sources:
        return "verified", "Implementation found in code."

    # Video-only (no code, no URL) is weak — narrator says it but we never saw code or working app.
    # For a functional claim, this is effectively unsupported.
    if supporting_sources == {"video"} or supporting_sources == {"video", "deck"}:
        return "unsupported", (
            "Only narrated in demo video. No code implementation or working prototype found — "
            "treating as unsupported for a functional claim."
        )

    # Partial is now reserved for: claim has SOME concrete evidence but it's incomplete
    # (e.g. code partially implements, or prototype has element but it's broken — already
    # caught by 'contradicted' above). In practice this branch is rarely hit now.
    if supporting_sources - {"deck"}:
        return "partial", f"Partially supported by: {', '.join(sorted(supporting_sources))}."

    if "deck" in supporting_sources:
        return "unsupported", "Claimed only in deck — no implementation or demo evidence."

    return "unsupported", "No supporting evidence found."

def _check_url_ground_truth(claim: str, results: list[dict]) -> tuple | None:
    """
    If the URL validator explicitly tested this claim's feature, return the
    resulting status directly. This prevents LLM noise from overriding
    observed DOM behavior.

    Returns (status, reasoning, supporting_evidence, contradicting_evidence)
    or None if no direct URL evidence for this claim exists.
    """
    claim_lower = claim.lower()
    # Strip common wrapping phrases to get to the feature itself
    for prefix in ["the application ", "the app ", "users can ", "user can ",
                   "the system ", "ability to "]:
        if claim_lower.startswith(prefix):
            claim_lower = claim_lower[len(prefix):]

    claim_words = set(re.findall(r"\w+", claim_lower))
    claim_words = {w for w in claim_words if len(w) > 3}  # drop stopwords-ish

    for r in results:
        meta = r["metadata"]
        if meta.get("source_type") != "url":
            continue
        sid = meta.get("source_id", "")
        if not sid.startswith("url_test_"):
            continue

        # Compare claim to the feature that was tested
        feature = meta.get("m_feature", "") or meta.get("feature", "")
        if not feature:
            # Fallback: pull feature out of source_id
            feature = sid.replace("url_test_", "").replace("_", " ")
        feature_words = set(re.findall(r"\w+", feature.lower()))
        feature_words = {w for w in feature_words if len(w) > 3}

        # Require meaningful overlap (at least 1 significant word)
        overlap = claim_words & feature_words
        if not overlap:
            continue

        status_field = meta.get("m_status", "") or meta.get("status", "")
        ev = Evidence(
            source_type="url",
            source_id=sid,
            claim_or_fact=meta.get("claim", ""),
            evidence_text=r["text"][:300],
            confidence=meta.get("confidence", 0.85),
            metadata={},
        )

        if status_field == "working":
            return (
                "verified",
                f"Ground truth: prototype shows '{feature}' working.",
                [ev], [],
            )
        if status_field == "not_found":
            return (
                "contradicted",
                f"Ground truth: prototype does NOT implement '{feature}'.",
                [], [ev],
            )
        if status_field == "broken":
            return (
                "contradicted",
                f"Ground truth: '{feature}' element exists in prototype but does not function.",
                [], [ev],
            )

    return None

def validate_claims(
    claims: list[str],
    store: EvidenceStore,
    top_k: int = 6,
) -> list[ClaimValidation]:
    """For each claim, retrieve evidence and judge in ONE batched call."""
    validations = []

    for claim in claims:
        if not claim or len(claim) < 5:
            continue

        claim_type = _classify_claim(claim)

        # Planned features short-circuit — no LLM call needed
        if claim_type == "planned":
            validations.append(ClaimValidation(
                claim=claim,
                status="planned",
                reasoning="Explicitly marked as future/planned — not penalized.",
            ))
            continue

        # Subjective claims also short-circuit — no LLM call
        if claim_type == "subjective":
            validations.append(ClaimValidation(
                claim=claim,
                status="unverifiable",
                reasoning="Subjective claim (UI quality) — not objectively testable.",
            ))
            continue

        # Retrieve evidence. For verifiable (functional) claims, query the
        # implementation sources (code/url) and the claim/context sources
        # (deck/video) as separate pools, then combine. A single shared top-k
        # search lets many near-duplicate deck/video matches (deck items are
        # near-identical to the claim text, since claims come FROM the deck)
        # crowd out the handful of code items that actually matter for verifying
        # a functional claim — code is the authoritative "implementation" source
        # per the source-role design above, so it must never be starved out.
        # Tech-stack ("unverifiable") claims skip this: forcing in unrelated code
        # items there just feeds the judge noise it tends to over-match on.
        if claim_type == "verifiable":
            impl_results = store.query(claim, k=top_k, source_types=["code", "url"])
            context_results = store.query(claim, k=top_k, source_types=["deck", "video"])
            seen = set()
            results = []
            for r in impl_results + context_results:
                key = (r["metadata"].get("source_id", ""), r["text"][:80])
                if key in seen:
                    continue
                seen.add(key)
                results.append(r)
        else:
            results = store.query(claim, k=top_k)

        if not results:
            validations.append(ClaimValidation(
                claim=claim,
                status="unsupported",
                reasoning="No related evidence found in any artefact.",
            ))
            continue

        # ─── URL GROUND-TRUTH SHORT-CIRCUIT ───
        # If the prototype explicitly tested this feature, trust that verdict.
        # Don't let the LLM override observed DOM behavior.
        url_verdict = _check_url_ground_truth(claim, results)
        if url_verdict is not None:
            status, reasoning, supp, contra = url_verdict
            validations.append(ClaimValidation(
                claim=claim,
                status=status,
                supporting_evidence=supp,
                contradicting_evidence=contra,
                reasoning=f"[{claim_type}] {reasoning}",
            ))
            continue

        # Otherwise: LLM batch judge
        verdicts = _batch_judge_evidence(claim, results)

        # Build judgment list with source_type
        judgments = []
        supporting = []
        contradicting = []
        for i, r in enumerate(results):
            if i >= len(verdicts):
                break
            v = verdicts[i]
            source_type = r["metadata"].get("source_type", "")
            verdict_str = v.get("verdict", "irrelevant")

            judgments.append({
                "source_type": source_type,
                "verdict": verdict_str,
                "reasoning": v.get("reasoning", ""),
            })

            ev = Evidence(
                source_type=source_type,
                source_id=r["metadata"].get("source_id", "unknown"),
                claim_or_fact=r["metadata"].get("claim", ""),
                evidence_text=r["text"][:300],
                confidence=r["metadata"].get("confidence", 0.7),
                metadata={k[2:]: v for k, v in r["metadata"].items() if k.startswith("m_")},
            )
            if verdict_str == "supports":
                supporting.append(ev)
            elif verdict_str == "contradicts" and source_type != "deck":
                contradicting.append(ev)

        status, reasoning = _decide_status(judgments, claim_type)

        validations.append(ClaimValidation(
            claim=claim,
            status=status,
            supporting_evidence=supporting,
            contradicting_evidence=contradicting,
            reasoning=f"[{claim_type}] {reasoning}",
        ))

    return validations