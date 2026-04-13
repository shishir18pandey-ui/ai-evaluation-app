
import logging
from app.core.models import UnifiedSubmission
from app.storage import EvidenceStore
from app.llm import llm_json

logger = logging.getLogger(__name__)


UNIFY_SYSTEM = """You merge evidence from multiple artefacts (deck, video, code, prototype)
into a single, factual summary of a project submission.

Rules:
- Stick to what the evidence shows. No speculation.
- If deck claims something the code doesn't back, still include it but note the source.
- Keep it concise.

Return JSON:
{
  "problem": "1-2 sentences describing the problem solved",
  "solution": "1-2 sentences describing the approach",
  "claimed_features": ["feature 1", "feature 2", ...],
  "tech_stack": ["tech 1", "tech 2"],
  "implementation_depth": "one sentence: how deep does the implementation go?"
}
"""


def build_unified_submission(store: EvidenceStore) -> UnifiedSubmission:
    """
    Retrieve diverse evidence across all sources and have the LLM synthesize.
    """
    
    queries = [
        ("problem statement and motivation", 6),
        ("system architecture and tech stack", 6),
        ("main features and capabilities", 8),
        ("implementation details and core logic", 6),
    ]
    collected = []
    seen_ids = set()
    for q, k in queries:
        for r in store.query(q, k=k):
            key = r["metadata"].get("source_id", "") + r["text"][:60]
            if key in seen_ids:
                continue
            seen_ids.add(key)
            collected.append(r)

    if not collected:
        return UnifiedSubmission(
            problem="[No evidence available]",
            solution="[No evidence available]",
            claimed_features=[],
            implementation_depth="Unknown — no artefacts processed.",
        )

   
    by_source = {}
    for r in collected:
        st = r["metadata"].get("source_type", "other")
        by_source.setdefault(st, []).append(
            f"[{r['metadata'].get('source_id', '?')}] {r['metadata'].get('claim', '')}"
        )

    evidence_blob = ""
    for st, items in by_source.items():
        evidence_blob += f"\n## From {st}:\n"
        evidence_blob += "\n".join(f"- {i}" for i in items[:15])
        evidence_blob += "\n"

    result = llm_json(UNIFY_SYSTEM, f"EVIDENCE:\n{evidence_blob}\n\nSynthesize.")

    if not isinstance(result, dict):
        result = {}

    sources_map = {st: len(items) for st, items in by_source.items()}

    return UnifiedSubmission(
        problem=result.get("problem", "Not determined."),
        solution=result.get("solution", "Not determined."),
        claimed_features=result.get("claimed_features", []),
        implementation_depth=result.get("implementation_depth", "Not assessed."),
        tech_stack=result.get("tech_stack", []),
        sources=sources_map,
    )
