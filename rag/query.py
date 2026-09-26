"""
STEP 2 -- Ask a question in plain English.

Uses GLM 4.5 (via OpenRouter) to answer in one of two modes:
  - DATA questions ("which brand has the highest X") get answered by
    generating SQL, running it read-only, and showing the result table.
  - METHODOLOGY / conceptual questions ("how do you calculate X", "why
    does this exclude Y") get answered with a plain-English explanation
    pulled from the knowledge base, not forced into a SQL query.

Run this after build_index.py has completed successfully at least once.
Run it as many times as you like after that -- it doesn't rebuild the index.

Usage:
    python3 query.py "which state has the highest RTO leakage?"
    python3 query.py "how is revenue lost to stockouts calculated?"
"""
import os
import re
import sys
import sqlite3
import chromadb
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()  # reads OPENROUTER_API_KEY from a .env file in this folder, if present

DB_PATH = "../data/db/price_intel.db"

# --- Load the index built in step 1 ---
chroma_client = chromadb.PersistentClient(path="./chroma_db")
collection = chroma_client.get_collection("rto_knowledge")


def retrieve_context(question, n_results=5):
    """Find the chunks (column docs, glossary, examples) closest to the question."""
    results = collection.query(query_texts=[question], n_results=n_results)
    return "\n\n".join(results["documents"][0])


# OPENROUTER_API_KEY must be set (via .env or the environment) for this to work.
or_client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.environ["OPENROUTER_API_KEY"],
)


def generate_response(question, context):
    prompt = f"""You are an assistant for a D2C price intelligence project, backed by a
SQLite database with tables dim_product and fact_price_daily (join on variant_id).
Use ONLY the column names and facts given in the context below. Never invent a column
or table that isn't mentioned there.

You have TWO response modes -- pick exactly one:

MODE 1 -- DATA QUESTION (e.g. "which brand has the highest X", "how many Y"):
Respond with a raw SQL SELECT query (a WITH ... clause is fine too), prefixed with
"SQL:" on the first line, e.g.:
SQL: SELECT brand, COUNT(*) FROM dim_product GROUP BY brand;

MODE 2 -- METHODOLOGY / CONCEPTUAL QUESTION (e.g. "how do you calculate X", "why
does the pipeline exclude Y", "what's the assumption behind Z"): respond with a
plain-English explanation drawn from the context, prefixed with "EXPLANATION:" on
the first line. Do not write SQL for these -- there is no table that stores an
explanation, only data.

DEFAULT RULE for MODE 1 queries only: Unless the question explicitly asks about test
SKUs, freebies, combos, campaigns, or flash sales, always exclude dim_product rows
where is_test = 1, is_freebie = 1, is_combo = 1, is_campaign = 1, or is_flash_sale = 1.
EXCEPTION: if the question specifically asks about one of those, filter TO that flag
instead of excluding it, while still excluding the other four.

GAPS AND ISLANDS (for any streak/duration question): compute TWO ROW_NUMBERs over the
SAME unfiltered set -- one partitioned by variant_id only, one partitioned by
variant_id AND available -- their difference is constant within a consecutive run.
Do NOT compute a single ROW_NUMBER over all rows and filter afterwards -- with daily
snapshots that makes the row-number difference constant across a variant's ENTIRE
history, silently merging all of that product's separate stockouts into one.

Keep queries reasonably concise -- prefer clear CTEs over deeply nested subqueries,
since a response that runs too long may get cut off.

Context:
{context}

Question: {question}

Reply with ONLY the prefixed response (SQL: ... or EXPLANATION: ...). No other text."""

    response = or_client.chat.completions.create(
        model="z-ai/glm-4.5",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1200,  # raised from 400 -- CTE-heavy queries were getting cut off mid-SQL
    )
    return response.choices[0].message.content.strip()


def parse_response(raw):
    """Split the model's prefixed response into (mode, content)."""
    if raw.upper().startswith("SQL:"):
        return "sql", raw[4:].strip()
    if raw.upper().startswith("EXPLANATION:"):
        return "explanation", raw[len("EXPLANATION:"):].strip()
    if raw.strip().upper().startswith("SELECT") or raw.strip().upper().startswith("WITH"):
        return "sql", raw.strip()
    return "explanation", raw.strip()


def is_safe_select(sql):
    """
    Allow SELECT queries, including ones that start with a WITH clause (CTEs),
    which the window-function queries rely on. Reject anything containing a
    write or DDL statement.

    The read-only SQLite connection is the real enforcement -- this is a second,
    independent layer that rejects a bad query before it reaches the database.
    """
    cleaned = re.sub(r"--[^\n]*", " ", sql)
    cleaned = re.sub(r"/\*.*?\*/", " ", cleaned, flags=re.S)
    normalized = " ".join(cleaned.split()).upper()

    if not (normalized.startswith("SELECT") or normalized.startswith("WITH")):
        return False

    forbidden = ["INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE",
                 "REPLACE", "TRUNCATE", "ATTACH", "DETACH", "PRAGMA", "VACUUM"]
    for kw in forbidden:
        if re.search(rf"\b{kw}\b", normalized):
            return False
    return True


# --- Run the SQL against a READ-ONLY connection only ---
def run_readonly_query(sql):
    if not is_safe_select(sql):
        raise ValueError(f"Refusing to run a non-SELECT query: {sql}")
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    cur = conn.cursor()
    cur.execute(sql)
    columns = [d[0] for d in cur.description]
    rows = cur.fetchall()
    conn.close()
    return columns, rows


def ask(question):
    context = retrieve_context(question)
    raw = generate_response(question, context)
    mode, content = parse_response(raw)

    if mode == "explanation":
        print(f"\n{content}\n")
        return

    print(f"\nGenerated SQL:\n  {content}\n")
    try:
        columns, rows = run_readonly_query(content)
    except sqlite3.OperationalError as e:
        print(f"SQL error: {e}")
        print("(The generated query may have been cut off or malformed -- try rephrasing the question.)")
        return
    print("Result:")
    print(" | ".join(columns))
    for row in rows[:15]:
        print(row)
    if len(rows) > 15:
        print(f"... ({len(rows) - 15} more rows)")


if __name__ == "__main__":
    question = " ".join(sys.argv[1:]) or "Which state has the highest RTO leakage?"
    ask(question)