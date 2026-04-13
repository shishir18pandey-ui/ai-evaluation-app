"""
Transcript extractor: ONE LLM call for the whole transcript.

Optimization: was 3-7 calls (one per chunk). Now one call labelled by chunk index.
"""
import logging
import re
from pathlib import Path

from app.core.models import Evidence
from app.llm import llm_json

logger = logging.getLogger(__name__)


TRANSCRIPT_EXTRACTION_SYSTEM = """You analyze a demo video transcript split into segments.

For each segment, extract demonstrated and claimed features:
- "demonstrated": user visibly performs an action OR feature is shown working
  e.g. "clicks login, dashboard loads"
- "claimed": narrator says the system does X, but we don't see it happen
  e.g. "our AI analyzes sentiment"
- "hedged": narrator backs away from a claim ("coming soon", "in next release", "roadmap")

Return JSON: {"items": [{"segment": N, "type": "demonstrated|claimed|hedged",
                         "statement": "...", "timestamp": "..."}]}

Use timestamp from the segment header if visible (e.g. "[00:42]"), else "".
Return {"items": []} if nothing notable.
Process ALL segments.
"""


def _chunk_transcript(text: str, chunk_size: int = 1500) -> list[tuple[str, str]]:
    """Break transcript into chunks. Returns [(timestamp_hint, chunk), ...]."""
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []

    chunks = []
    sentences = re.split(r"(?<=[.!?])\s+", text)
    current = ""
    for s in sentences:
        if len(current) + len(s) < chunk_size:
            current += " " + s
        else:
            if current.strip():
                chunks.append(current.strip())
            current = s
    if current.strip():
        chunks.append(current.strip())

    result = []
    ts_pattern = re.compile(r"\[?(\d{1,2}:\d{2}(?::\d{2})?)\]?")
    for c in chunks:
        m = ts_pattern.search(c)
        result.append((m.group(1) if m else "", c))
    return result


def extract_from_transcript(transcript: str | Path) -> list[Evidence]:
    """Main entry: takes raw text or path. ONE LLM call for whole transcript."""
    text = None

    if isinstance(transcript, Path):
        if transcript.exists():
            text = transcript.read_text(encoding="utf-8", errors="ignore")
        else:
            text = str(transcript)
    elif isinstance(transcript, str):
        looks_like_path = (
            len(transcript) < 260
            and "\n" not in transcript
            and ("/" in transcript or "\\" in transcript or transcript.endswith(".txt"))
        )
        if looks_like_path:
            try:
                p = Path(transcript)
                if p.exists() and p.is_file():
                    text = p.read_text(encoding="utf-8", errors="ignore")
            except (OSError, ValueError):
                pass
        if text is None:
            text = transcript
    else:
        text = str(transcript)

    if not text or not text.strip():
        return []

    chunks = _chunk_transcript(text)
    if not chunks:
        return []

    # Build single labelled blob — ONE LLM call
    labelled_parts = []
    for idx, (ts_hint, chunk) in enumerate(chunks):
        header = f"[{ts_hint}]" if ts_hint else ""
        labelled_parts.append(f"=== SEGMENT {idx + 1} {header} ===\n{chunk}")
    blob = "\n\n".join(labelled_parts)
    if len(blob) > 15000:
        blob = blob[:15000] + "\n\n[... transcript truncated ...]"

    logger.info("Extracting from %d transcript segments in ONE call", len(chunks))

    try:
        result = llm_json(
            TRANSCRIPT_EXTRACTION_SYSTEM,
            f"TRANSCRIPT:\n\n{blob}\n\nExtract.",
        )
    except Exception as e:
        logger.error("Transcript extraction failed: %s", e)
        return []

    items = result.get("items", []) if isinstance(result, dict) else []
    chunk_lookup = {idx + 1: (ts_hint, chunk) for idx, (ts_hint, chunk) in enumerate(chunks)}

    evidences = []
    for item in items:
        if not isinstance(item, dict) or "statement" not in item:
            continue
        seg_num = item.get("segment", 0)
        try:
            seg_num = int(seg_num)
        except (ValueError, TypeError):
            seg_num = 0

        ts = item.get("timestamp", "")
        if not ts and seg_num in chunk_lookup:
            ts = chunk_lookup[seg_num][0]

        chunk_text_excerpt = ""
        if seg_num in chunk_lookup:
            chunk_text_excerpt = chunk_lookup[seg_num][1][:400]

        source_id = f"video_{ts}" if ts else f"video_chunk_{seg_num or '?'}"

        item_type = item.get("type", "claimed")
        if item_type == "demonstrated":
            confidence = 0.85
        elif item_type == "hedged":
            confidence = 0.5
        else:
            confidence = 0.6

        evidences.append(Evidence(
            source_type="video",
            source_id=source_id,
            claim_or_fact=item["statement"],
            evidence_text=chunk_text_excerpt or item["statement"],
            confidence=confidence,
            metadata={
                "type": item_type,
                "timestamp": ts,
                "segment": seg_num,
            },
        ))

    logger.info("Extracted %d statements from transcript", len(evidences))
    return evidences