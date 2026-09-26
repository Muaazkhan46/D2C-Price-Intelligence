"""
STEP 1 — Build the vector index.

Run this once. Run it again any time you edit knowledge_base.json.

What it does:
  1. Loads knowledge_base.json (the columns, glossary, and example queries)
  2. Breaks it into small individual chunks
  3. Hands those chunks to Chroma, which embeds them and saves them to disk
     in a folder called chroma_db/

After this finishes, chroma_db/ is your searchable index. You don't touch
this script again unless the knowledge base itself changes.
"""
import json
import chromadb

with open("knowledge_base.json") as f:
    kb = json.load(f)

# A persistent client saves to disk in ./chroma_db so the index survives
# between runs -- you don't have to rebuild it every time you ask a question.
client = chromadb.PersistentClient(path="./chroma_db")

# Delete + recreate so re-running this script is always safe (no duplicates).
try:
    client.delete_collection("rto_knowledge")
except Exception:
    pass
collection = client.create_collection("rto_knowledge")

documents = []
metadatas = []
ids = []

# --- Chunk 1: one entry per column ---
for i, col in enumerate(kb["columns"]):
    text = f"Column '{col['name']}' ({col['type']}): {col['description']}"
    documents.append(text)
    metadatas.append({"type": "column", "name": col["name"]})
    ids.append(f"col_{i}")

# --- Chunk 2: one entry per glossary term ---
for i, g in enumerate(kb["glossary"]):
    text = f"Term '{g['term']}': {g['definition']}"
    documents.append(text)
    metadatas.append({"type": "glossary", "term": g["term"]})
    ids.append(f"glossary_{i}")

# --- Chunk 3: one entry per example question -> SQL pair ---
for i, ex in enumerate(kb["example_queries"]):
    text = f"Example question: {ex['question']}\nSQL: {ex['sql']}"
    documents.append(text)
    metadatas.append({"type": "example", "question": ex["question"], "sql": ex["sql"]})
    ids.append(f"example_{i}")

# Chroma embeds every document automatically here -- no separate embedding
# step to manage yourself.
collection.add(documents=documents, metadatas=metadatas, ids=ids)

print(f"Indexed {len(documents)} chunks into ./chroma_db")
print(f"  - {len(kb['columns'])} column descriptions")
print(f"  - {len(kb['glossary'])} glossary terms")
print(f"  - {len(kb['example_queries'])} example queries")