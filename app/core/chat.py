

import logging
from typing import Optional

from app.storage import EvidenceStore
from app.llm import llm_complete

logger = logging.getLogger(__name__)


CHAT_SYSTEM = """You are a helpful assistant answering questions about a project submission
that has already been evaluated. You have access to evidence retrieved from:
- The pitch deck (source_type: deck)
- The demo video transcript (source_type: video)
- The code repository (source_type: code)
- The live prototype (source_type: url)

RULES:
- Answer ONLY based on the provided evidence below. Don't speculate.
- Cite specific sources inline using [source_id] notation.
  Examples: [slide_5], [src/auth.py], [video_02:10], [url_home]
- If the evidence doesn't answer the question, say so clearly.
- Keep responses concise and grounded.
"""


def answer_question(
    question: str,
    store: EvidenceStore,
    conversation_history: Optional[list[dict]] = None,
    k: int = 8,
) -> dict:
    """
    Retrieve evidence for the question and generate a grounded answer.

    Returns: {"answer": str, "sources_used": list[str], "retrieved_count": int}
    """
    # Retrieve relevant evidence from all source types
    results = store.query(question, k=k)

    if not results:
        return {
            "answer": "I don't have any evidence loaded yet. Please run an evaluation first.",
            "sources_used": [],
            "retrieved_count": 0,
        }

    # Format evidence for the LLM
    evidence_blob = "\n\n".join(
        f"[{r['metadata'].get('source_id', '?')}] ({r['metadata'].get('source_type', '')})\n"
        f"{r['text'][:400]}"
        for r in results
    )

    # Include recent conversation context if provided
    history_text = ""
    if conversation_history:
        recent = conversation_history[-4:]  # last 2 exchanges
        history_text = "\n\nRECENT CONVERSATION:\n" + "\n".join(
            f"{m['role']}: {m['content'][:200]}" for m in recent
        )

    user_msg = f"""QUESTION: {question}
{history_text}

RETRIEVED EVIDENCE:
{evidence_blob}

Answer the question using the evidence. Cite sources inline."""

    try:
        answer = llm_complete(CHAT_SYSTEM, user_msg, temperature=0.2)
    except Exception as e:
        logger.exception("Chat LLM failed")
        return {
            "answer": f"Sorry, something went wrong: {e}",
            "sources_used": [],
            "retrieved_count": len(results),
        }

    sources_used = [r["metadata"].get("source_id", "") for r in results]

    return {
        "answer": answer,
        "sources_used": [s for s in sources_used if s],
        "retrieved_count": len(results),
    }