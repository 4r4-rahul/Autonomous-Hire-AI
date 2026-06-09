import json
from typing import Any
import chromadb
from chromadb.config import Settings as ChromaSettings
from core.config import get_settings
from core.logger import get_logger

logger = get_logger("memory")

COLLECTIONS = [
    "companies",
    "job_listings",
    "applications",
    "recruiter_profiles",
    "learnings",
    "user_context",
]


class Memory:
    """Shared vector store. All agents read/write through this singleton."""

    _instance: "Memory | None" = None

    def __init__(self) -> None:
        cfg = get_settings()
        self._client = chromadb.PersistentClient(
            path=cfg.chroma_persist_dir,
            settings=ChromaSettings(anonymized_telemetry=False),
        )
        self._cols: dict[str, chromadb.Collection] = {}
        for name in COLLECTIONS:
            self._cols[name] = self._client.get_or_create_collection(
                name=name,
                metadata={"hnsw:space": "cosine"},
            )
        logger.info("memory_initialized", collections=COLLECTIONS)

    @classmethod
    def get(cls) -> "Memory":
        if cls._instance is None:
            cls._instance = Memory()
        return cls._instance

    # ------------------------------------------------------------------ #
    # Generic CRUD                                                         #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _sanitize_metadata(metadata: dict) -> dict:
        """
        ChromaDB only accepts str/int/float/bool as metadata values.
        Convert lists → JSON strings, drop None values, drop private keys (_*).
        """
        clean: dict = {}
        for k, v in metadata.items():
            if k.startswith("_"):
                continue
            if v is None:
                continue
            if isinstance(v, (list, dict)):
                clean[k] = json.dumps(v)
            elif isinstance(v, (str, int, float, bool)):
                clean[k] = v
            else:
                clean[k] = str(v)
        return clean

    def upsert(self, collection: str, doc_id: str, text: str, metadata: dict | None = None) -> None:
        """Store or update a document. Text is embedded automatically by ChromaDB."""
        self._cols[collection].upsert(
            ids=[doc_id],
            documents=[text],
            metadatas=[self._sanitize_metadata(metadata or {})],
        )

    def get_by_id(self, collection: str, doc_id: str) -> dict | None:
        result = self._cols[collection].get(ids=[doc_id], include=["documents", "metadatas"])
        if not result["ids"]:
            return None
        return {"id": doc_id, "text": result["documents"][0], "metadata": result["metadatas"][0]}

    def query(self, collection: str, query_text: str, n_results: int = 5) -> list[dict]:
        """Semantic search. Returns top-n most similar docs."""
        result = self._cols[collection].query(
            query_texts=[query_text],
            n_results=min(n_results, self._cols[collection].count() or 1),
            include=["documents", "metadatas", "distances"],
        )
        docs = []
        for i, doc_id in enumerate(result["ids"][0]):
            docs.append({
                "id": doc_id,
                "text": result["documents"][0][i],
                "metadata": result["metadatas"][0][i],
                "distance": result["distances"][0][i],
            })
        return docs

    def delete(self, collection: str, doc_id: str) -> None:
        self._cols[collection].delete(ids=[doc_id])

    def count(self, collection: str) -> int:
        return self._cols[collection].count()

    # ------------------------------------------------------------------ #
    # Typed helpers                                                        #
    # ------------------------------------------------------------------ #

    def add_learning(self, learning_id: str, text: str, tags: list[str] | None = None) -> None:
        """Write a rejection/success insight to the learnings store."""
        self.upsert("learnings", learning_id, text, metadata={"tags": json.dumps(tags or [])})
        logger.log_decision("add_learning", "stored", {"learning_id": learning_id})

    def get_relevant_learnings(self, context: str, n: int = 5) -> list[str]:
        docs = self.query("learnings", context, n_results=n)
        return [d["text"] for d in docs]

    def add_company(self, company_id: str, company_data: dict) -> None:
        text = f"{company_data.get('name', '')} {company_data.get('domain', '')} {' '.join(company_data.get('tech_stack', []))}"
        self.upsert("companies", company_id, text, metadata=company_data)

    def add_job_listing(self, job_id: str, description: str, metadata: dict) -> None:
        self.upsert("job_listings", job_id, description, metadata=metadata)

    def add_recruiter(self, recruiter_id: str, profile_text: str, metadata: dict) -> None:
        self.upsert("recruiter_profiles", recruiter_id, profile_text, metadata=metadata)

    def get_similar_jobs(self, query: str, n: int = 10) -> list[dict]:
        return self.query("job_listings", query, n_results=n)
