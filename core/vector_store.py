from datetime import datetime
from pathlib import Path
from typing import Optional

import chromadb


class VectorStore:
    def __init__(self, persist_dir: str | Path, lmstudio_url: str = "",
                 embedding_model: str = "all-MiniLM-L6-v2"):
        self.persist_dir = Path(persist_dir)
        self.persist_dir.mkdir(parents=True, exist_ok=True)

        self.client = chromadb.PersistentClient(
            path=str(self.persist_dir)
        )

        self.decisions = self.client.get_or_create_collection(
            name="decisions",
            metadata={"hnsw:space": "cosine"},
        )

        self.vision = self.client.get_or_create_collection(
            name="vision",
            metadata={"hnsw:space": "cosine"},
        )

    def add_decision(self, session_id: int, issue_title: str, issue_description: str,
                     final_reasoning: str, vote_choice: str, all_interventions_text: str,
                     topic_tags: str = ""):
        doc = f"{issue_title}\n{issue_description}\n\n{all_interventions_text}"
        self.decisions.upsert(
            ids=[str(session_id)],
            documents=[doc],
            metadatas=[{
                "session_id": session_id,
                "issue_title": issue_title,
                "vote": vote_choice,
                "reasoning_preview": final_reasoning[:500],
                "tags": topic_tags,
                "date": datetime.utcnow().isoformat(),
            }]
        )

    def search_similar(self, query: str, n: int = 5) -> list[dict]:
        try:
            results = self.decisions.query(
                query_texts=[query],
                n_results=min(n, max(1, self.decisions.count()))
            )
        except Exception:
            return []
        output = []
        if results["ids"] and results["ids"][0]:
            for i, doc_id in enumerate(results["ids"][0]):
                output.append({
                    "id": doc_id,
                    "document": results["documents"][0][i],
                    "metadata": results["metadatas"][0][i],
                    "distance": results["distances"][0][i] if results["distances"] else None,
                })
        return output

    def search_by_tags(self, tag_filter: str, n: int = 10) -> list[dict]:
        results = self.decisions.get(
            where={"tags": tag_filter},
            limit=n
        )
        output = []
        if results["ids"]:
            for i, doc_id in enumerate(results["ids"]):
                output.append({
                    "id": doc_id,
                    "document": results["documents"][i],
                    "metadata": results["metadatas"][i],
                })
        return output

    def get_decision_count(self) -> int:
        return self.decisions.count()

    def store_vision(self, vision_text: str, session_id: Optional[int] = None):
        existing = self.vision.get(ids=["current_vision"])
        if existing["ids"]:
            self.vision.update(
                ids=["current_vision"],
                documents=[vision_text],
                metadatas=[{
                    "session_id": str(session_id or ""),
                    "updated_at": datetime.utcnow().isoformat()
                }]
            )
        else:
            self.vision.add(
                ids=["current_vision"],
                documents=[vision_text],
                metadatas=[{
                    "session_id": str(session_id or ""),
                    "updated_at": datetime.utcnow().isoformat()
                }]
            )

    def get_vision(self) -> Optional[str]:
        results = self.vision.get(ids=["current_vision"])
        if results["ids"] and results["documents"]:
            return results["documents"][0]
        return None

    def delete_decision(self, session_id: int):
        self.decisions.delete(ids=[str(session_id)])
