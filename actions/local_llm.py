"""
Local LLM + RAG action for JARVIS.
Provides offline/private inference via Ollama and retrieval-augmented generation over personal documents.
"""

import json
import sys
import os
from pathlib import Path
from typing import Optional

def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = _get_base_dir()
MEMORY_DIR = BASE_DIR / "memory"
RAG_DIR = BASE_DIR / "rag_store"
RAG_DIR.mkdir(exist_ok=True)


# ─── Ollama Client ────────────────────────────────────────────────────────
class OllamaClient:
    def __init__(self, base_url: str = "http://localhost:11434"):
        self.base_url = base_url.rstrip("/")
        self._session = None

    def _get_session(self):
        import requests
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update({"Content-Type": "application/json"})
        return self._session

    def is_available(self) -> bool:
        try:
            r = self._get_session().get(f"{self.base_url}/api/tags", timeout=3)
            return r.status_code == 200
        except Exception:
            return False

    def list_models(self) -> list[dict]:
        try:
            r = self._get_session().get(f"{self.base_url}/api/tags", timeout=5)
            r.raise_for_status()
            return r.json().get("models", [])
        except Exception:
            return []

    def pull_model(self, model: str) -> str:
        try:
            r = self._get_session().post(f"{self.base_url}/api/pull", json={"name": model}, stream=True, timeout=300)
            r.raise_for_status()
            for line in r.iter_lines():
                if line:
                    data = json.loads(line)
                    if "status" in data:
                        print(f"[Ollama] {data['status']}")
            return f"Model {model} pulled successfully."
        except Exception as e:
            return f"Failed to pull {model}: {e}"

    def generate(self, model: str, prompt: str, system: str = "", temperature: float = 0.7, stream: bool = False) -> str:
        try:
            payload = {
                "model": model,
                "prompt": prompt,
                "system": system,
                "temperature": temperature,
                "stream": stream,
            }
            r = self._get_session().post(f"{self.base_url}/api/generate", json=payload, timeout=120)
            r.raise_for_status()
            if stream:
                # Not handling streaming here for simplicity
                return "Streaming not implemented in this wrapper."
            return r.json().get("response", "").strip()
        except Exception as e:
            return f"Generation failed: {e}"

    def chat(self, model: str, messages: list[dict], temperature: float = 0.7) -> str:
        try:
            payload = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "stream": False,
            }
            r = self._get_session().post(f"{self.base_url}/api/chat", json=payload, timeout=120)
            r.raise_for_status()
            return r.json().get("message", {}).get("content", "").strip()
        except Exception as e:
            return f"Chat failed: {e}"


# ─── Vector Store (Simple file-based, no external deps) ───────────────────
class SimpleVectorStore:
    def __init__(self, store_dir: Path):
        self.store_dir = store_dir
        self.store_dir.mkdir(exist_ok=True)
        self.index_file = store_dir / "index.json"
        self.data_file = store_dir / "vectors.jsonl"
        self._load_index()

    def _load_index(self):
        try:
            with open(self.index_file, "r") as f:
                self.index = json.load(f)
        except Exception:
            self.index = {"documents": [], "embeddings": {}}

    def _save_index(self):
        with open(self.index_file, "w") as f:
            json.dump(self.index, f)

    def _get_embedding(self, text: str) -> list[float]:
        """Get embedding via Ollama (nomic-embed-text) or fallback to hash-based."""
        client = OllamaClient()
        if client.is_available():
            try:
                import requests
                r = client._get_session().post(
                    f"{client.base_url}/api/embeddings",
                    json={"model": "nomic-embed-text", "prompt": text},
                    timeout=30
                )
                if r.status_code == 200:
                    return r.json().get("embedding", [])
            except Exception:
                pass
        # Fallback: simple hash-based pseudo-embedding (not semantic, but deterministic)
        import hashlib
        h = hashlib.md5(text.encode()).hexdigest()
        return [int(h[i:i+2], 16) / 255.0 for i in range(0, 32, 2)]

    def add(self, doc_id: str, text: str, metadata: dict = None) -> str:
        embedding = self._get_embedding(text)
        if not embedding:
            return "Failed to generate embedding"

        entry = {
            "id": doc_id,
            "text": text,
            "metadata": metadata or {},
            "embedding": embedding,
        }

        # Append to JSONL
        with open(self.data_file, "a") as f:
            f.write(json.dumps(entry) + "\n")

        # Update index
        self.index["documents"].append(doc_id)
        self.index["embeddings"][doc_id] = embedding
        self._save_index()
        return f"Added document {doc_id}"

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        query_emb = self._get_embedding(query)
        if not query_emb:
            return []

        # Load all embeddings
        results = []
        try:
            with open(self.data_file, "r") as f:
                for line in f:
                    entry = json.loads(line)
                    score = self._cosine_sim(query_emb, entry["embedding"])
                    results.append((score, entry))
        except Exception:
            return []

        results.sort(key=lambda x: x[0], reverse=True)
        return [{"score": s, "text": e["text"], "metadata": e.get("metadata", {}), "id": e["id"]} for s, e in results[:top_k]]

    @staticmethod
    def _cosine_sim(a: list[float], b: list[float]) -> float:
        if len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(y * y for y in b) ** 0.5
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    def list_documents(self) -> list[dict]:
        docs = []
        try:
            with open(self.data_file, "r") as f:
                for line in f:
                    entry = json.loads(line)
                    docs.append({"id": entry["id"], "metadata": entry.get("metadata", {}), "preview": entry["text"][:100]})
        except Exception:
            pass
        return docs

    def delete(self, doc_id: str) -> str:
        # Rebuild without the doc (simple approach)
        entries = []
        try:
            with open(self.data_file, "r") as f:
                for line in f:
                    entry = json.loads(line)
                    if entry["id"] != doc_id:
                        entries.append(entry)
        except Exception:
            return f"Document {doc_id} not found"

        with open(self.data_file, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

        if doc_id in self.index["embeddings"]:
            del self.index["embeddings"][doc_id]
        if doc_id in self.index["documents"]:
            self.index["documents"].remove(doc_id)
        self._save_index()
        return f"Deleted {doc_id}"


# ─── RAG Pipeline ─────────────────────────────────────────────────────────
def _build_rag_context(query: str, top_k: int = 5) -> str:
    store = SimpleVectorStore(RAG_DIR)
    results = store.search(query, top_k)
    if not results:
        return ""
    ctx = "RELEVANT CONTEXT FROM YOUR DOCUMENTS:\n"
    for i, r in enumerate(results, 1):
        ctx += f"\n[{i}] (score: {r['score']:.3f}) {r['text'][:500]}"
        if r.get("metadata"):
            ctx += f" — {r['metadata']}"
    return ctx


def _index_memory_files():
    """Index long_term.json and session summaries into RAG store."""
    store = SimpleVectorStore(RAG_DIR)
    import time

    # Index long-term memory
    mem_file = MEMORY_DIR / "long_term.json"
    if mem_file.exists():
        try:
            with open(mem_file, "r") as f:
                mem = json.load(f)
            for category, items in mem.items():
                if isinstance(items, dict):
                    for key, val in items.items():
                        if isinstance(val, dict) and "value" in val:
                            text = f"{category}.{key}: {val['value']}"
                            doc_id = f"memory_{category}_{key}"
                            store.add(doc_id, text, {"type": "memory", "category": category, "key": key})
        except Exception:
            pass

    # Index session summaries
    sessions_dir = MEMORY_DIR / "sessions"
    if sessions_dir.exists():
        for f in sessions_dir.glob("*.json"):
            try:
                with open(f, "r") as sf:
                    sess = json.load(f)
                text = f"Session {sess.get('date', f.stem)}: {sess.get('summary', '')}"
                store.add(f"session_{f.stem}", text, {"type": "session", "date": sess.get("date")})
            except Exception:
                pass


# ─── Main Run Function ────────────────────────────────────────────────────
def run(parameters: dict, player=None, session_memory=None) -> str:
    action = parameters.get("action", "").lower().strip()
    model = parameters.get("model", "llama3.1:8b")
    prompt = parameters.get("prompt", "").strip()
    system = parameters.get("system", "").strip()
    query = parameters.get("query", "").strip()
    doc_id = parameters.get("doc_id", "").strip()
    text = parameters.get("text", "").strip()
    metadata = parameters.get("metadata") or {}
    top_k = int(parameters.get("top_k", 5))
    temperature = float(parameters.get("temperature", 0.7))

    client = OllamaClient()
    store = SimpleVectorStore(RAG_DIR)

    if action == "status":
        available = client.is_available()
        models = client.list_models() if available else []
        status = "Running" if available else "Not running"
        return f"Ollama: {status}\nModels: {', '.join(m['name'] for m in models) or 'none'}"

    if action == "pull":
        if not model:
            return "Specify model to pull (e.g., 'llama3.1:8b', 'nomic-embed-text', 'phi3:mini')"
        return client.pull_model(model)

    if action == "chat":
        if not prompt:
            return "Provide a prompt for chat."
        if not client.is_available():
            return "Ollama not running. Start it with 'ollama serve' or install from ollama.com"
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return client.chat(model, messages, temperature)

    if action == "generate":
        if not prompt:
            return "Provide a prompt for generation."
        if not client.is_available():
            return "Ollama not running."
        full_prompt = f"{system}\n\n{prompt}" if system else prompt
        return client.generate(model, full_prompt, temperature=temperature)

    if action == "rag_query":
        if not query:
            return "Provide a query for RAG search."
        _index_memory_files()  # Ensure memory is indexed
        context = _build_rag_context(query, top_k)
        if not context:
            return "No relevant documents found in your knowledge base."
        # Use local LLM to answer with context
        if not client.is_available():
            return context + "\n\n(Ollama not available — showing raw context only)"
        sys_prompt = system or "You are JARVIS. Answer using ONLY the provided context. If context doesn't contain the answer, say so."
        full_prompt = f"{context}\n\nQUESTION: {query}\n\nANSWER:"
        return client.generate(model, full_prompt, system=sys_prompt, temperature=temperature)

    if action == "index":
        _index_memory_files()
        if doc_id and text:
            return store.add(doc_id, text, metadata)
        return "Memory indexed. Provide doc_id and text to add custom documents."

    if action == "add_doc":
        if not doc_id or not text:
            return "Provide doc_id and text."
        return store.add(doc_id, text, metadata)

    if action == "search":
        if not query:
            return "Provide a query."
        results = store.search(query, top_k)
        if not results:
            return "No results."
        out = f"Top {len(results)} results for: {query}\n\n"
        for i, r in enumerate(results, 1):
            out += f"{i}. [{r['score']:.3f}] {r['id']}: {r['text'][:200]}\n"
        return out

    if action == "list":
        docs = store.list_documents()
        if not docs:
            return "No documents indexed."
        return f"Indexed documents ({len(docs)}):\n" + "\n".join(f"- {d['id']}: {d['preview']}" for d in docs)

    if action == "delete":
        if not doc_id:
            return "Provide doc_id."
        return store.delete(doc_id)

    return f"Unknown action: {action}. Use: status, pull, chat, generate, rag_query, index, add_doc, search, list, delete"


# ── Tool declaration ──────────────────────────────────────────────────────
TOOL = {
    "name": "local_llm",
    "description": "Local LLM inference and RAG via Ollama. Actions: 'status' (check Ollama), 'pull' (download model), 'chat' (conversation), 'generate' (completion), 'rag_query' (ask your documents), 'index' (re-index memory), 'add_doc' (add custom doc), 'search' (vector search), 'list' (list docs), 'delete' (remove doc). Use for private/offline tasks, personal knowledge queries, or when you want to avoid cloud API calls.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "status | pull | chat | generate | rag_query | index | add_doc | search | list | delete"},
            "model": {"type": "STRING", "description": "Ollama model name (default: llama3.1:8b)"},
            "prompt": {"type": "STRING", "description": "User prompt for chat/generate"},
            "system": {"type": "STRING", "description": "System prompt"},
            "query": {"type": "STRING", "description": "Query for RAG/search"},
            "doc_id": {"type": "STRING", "description": "Document ID for add_doc/delete"},
            "text": {"type": "STRING", "description": "Document text for add_doc"},
            "metadata": {"type": "OBJECT", "description": "Optional metadata for documents"},
            "top_k": {"type": "INTEGER", "description": "Number of results for RAG/search (default 5)"},
            "temperature": {"type": "NUMBER", "description": "Sampling temperature (default 0.7)"},
        },
        "required": ["action"]
    },
    "handler": run,
}