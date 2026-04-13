
import logging
from typing import Optional

import chromadb
from chromadb.utils import embedding_functions

from app.core.models import Evidence
from app.core import config

logger = logging.getLogger(__name__)


class EvidenceStore:
    """Wraps a Chroma collection scoped to one submission."""

    def __init__(self, submission_id: str):
        self.submission_id = submission_id
        self.client = chromadb.PersistentClient(path=str(config.CHROMA_DIR))
        self.embedder = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name=config.EMBEDDING_MODEL
        )
        collection_name = f"submission_{submission_id}"
       
        try:
            self.collection = self.client.get_collection(
                name=collection_name,
                embedding_function=self.embedder,
            )
        except Exception:
            self.collection = self.client.create_collection(
                name=collection_name,
                embedding_function=self.embedder,
            )

    def reset(self) -> None:
        """Wipe and recreate this submission's collection."""
        collection_name = self.collection.name
        try:
            self.client.delete_collection(collection_name)
        except Exception:
            pass
        self.collection = self.client.create_collection(
            name=collection_name,
            embedding_function=self.embedder,
        )

    def add(self, evidences: list[Evidence]) -> None:
        if not evidences:
            return
        documents = []
        metadatas = []
        ids = []
        for e in evidences:
            # Use both claim and evidence_text so semantic search works on either
            doc = f"{e.claim_or_fact}\n\n{e.evidence_text}".strip()
            documents.append(doc)
            # Chroma requires flat metadata (no nested dicts/lists)
            meta = {
                "source_type": e.source_type,
                "source_id": e.source_id,
                "claim": e.claim_or_fact[:500],
                "confidence": e.confidence,
            }
            # Flatten key metadata fields (Chroma only accepts str/int/float/bool)
            for k, v in e.metadata.items():
                if isinstance(v, (str, int, float, bool)):
                    meta[f"m_{k}"] = v
                elif isinstance(v, list):
                    meta[f"m_{k}"] = ", ".join(str(x) for x in v[:5])[:200]
            metadatas.append(meta)
            ids.append(e.evidence_id)

        self.collection.add(documents=documents, metadatas=metadatas, ids=ids)
        logger.info("Stored %d evidence items", len(evidences))

    def query(
        self,
        query_text: str,
        k: int = None,
        source_types: Optional[list[str]] = None,
    ) -> list[dict]:
        """
        Retrieve top-k evidence items matching query_text.
        Optional filter by source_type(s).
        Returns: [{text, metadata, distance}, ...]
        """
        k = k or config.EVIDENCE_TOP_K
        where = None
        if source_types:
            if len(source_types) == 1:
                where = {"source_type": source_types[0]}
            else:
                where = {"source_type": {"$in": source_types}}

        count = self.collection.count()
        if count == 0:
            return []
        # Don't ask for more than we have
        k = min(k, count)

        results = self.collection.query(
            query_texts=[query_text],
            n_results=k,
            where=where,
        )
        out = []
        for doc, meta, dist in zip(
            results["documents"][0],
            results["metadatas"][0],
            results["distances"][0],
        ):
            out.append({"text": doc, "metadata": meta, "distance": dist})
        return out

    def count_by_source(self) -> dict:
        """Quick sanity check — how much evidence per source type."""
        all_meta = self.collection.get()["metadatas"] or []
        counts = {}
        for m in all_meta:
            st = m.get("source_type", "unknown")
            counts[st] = counts.get(st, 0) + 1
        return counts
