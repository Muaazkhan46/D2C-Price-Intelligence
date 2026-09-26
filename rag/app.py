"""
app.py — Streamlit demo for the D2C Price Intelligence RAG assistant.

Two response modes, same as query.py:
  - DATA questions get answered with generated SQL + a results table.
  - METHODOLOGY / conceptual questions get answered with a plain-English
    explanation pulled from the knowledge base, not forced into SQL.

Run: streamlit run app.py
(from inside rag/, same folder as build_index.py, query.py, knowledge_base.json)
"""
import os
import re
import sqlite3
import pandas as pd
import streamlit as st
import chromadb
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()  # reads OPENROUTER_API_KEY from a .env file in this folder, if present

DB_PATH = "../data/db/price_intel.db"
CHROMA_PATH = "./chroma_db"
COLLECTION_NAME = "rto_knowledge"  # leftover name from the shared build_index.py script -- same collection used for D2C's index, harmless

st.set_page_config(page_title="D2C Price Intelligence Assistant", page_icon="📊")
st.title("📊 D2C Price Intelligence Assistant")
st.caption(
    "Ask a question in plain English about pricing, stockouts, and discounts "
    "across Pilgrim, Foxtale, DermaCo, DotAndKey, and Minimalist -- or ask how "
    "an analysis was calculated. Powered by a RAG pipeline over a real SQLite "
    "database -- read-only, nothing here can write to the underlying data."
)


@st.cache_resource
def get_collection():
    client = chromadb.PersistentClient(path=CHROMA_PATH)
    return client.get_collection(COLLECTION_NAME)


@st.cache_resource
def get_llm_client():
    return OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=os.environ["OPENROUTER_API_KEY"],
    )


def retrieve_context(collection, question, n_results=5):
    results = collection.query(query_texts=[question], n_results=n_results)
    return "\n\n".join(results["documents"][0])


def generate_response(llm_client, question, context):
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

    response = llm_client.chat.completions.create(
        model="z-ai/glm-4.5",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1200,  # raised from 400 -- CTE-heavy queries were getting cut off mid-SQL
    )
    return response.choices[0].message.content.strip()


def parse_response(raw):
    if raw.upper().startswith("SQL:"):
        return "sql", raw[4:].strip()
    if raw.upper().startswith("EXPLANATION:"):
        return "explanation", raw[len("EXPLANATION:"):].strip()
    if raw.strip().upper().startswith("SELECT") or raw.strip().upper().startswith("WITH"):
        return "sql", raw.strip()
    return "explanation", raw.strip()


def is_safe_select(sql):
    """
    Allow SELECT queries, including ones that start with a WITH clause (CTEs).
    Reject anything containing a write or DDL statement.
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


def run_readonly_query(sql):
    if not is_safe_select(sql):
        raise ValueError(f"Refusing to run a non-SELECT query: {sql}")
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    df = pd.read_sql(sql, conn)
    conn.close()
    return df


# --- Chat state ---
if "messages" not in st.session_state:
    st.session_state.messages = []

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        if msg["role"] == "assistant":
            if msg["mode"] == "explanation":
                st.write(msg["content"])
            else:
                st.code(msg["content"], language="sql")
                st.dataframe(msg["result"])
        else:
            st.write(msg["content"])

question = st.chat_input("e.g. which brand has the highest stockout rate?")

if question:
    st.session_state.messages.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.write(question)

    with st.chat_message("assistant"):
        try:
            with st.spinner("Retrieving context and thinking..."):
                collection = get_collection()
                llm_client = get_llm_client()
                context = retrieve_context(collection, question)
                raw = generate_response(llm_client, question, context)
                mode, content = parse_response(raw)

            if mode == "explanation":
                st.write(content)
                st.session_state.messages.append({"role": "assistant", "mode": "explanation", "content": content})
            else:
                st.code(content, language="sql")
                with st.spinner("Running query..."):
                    result_df = run_readonly_query(content)
                st.dataframe(result_df)
                st.session_state.messages.append({"role": "assistant", "mode": "sql", "content": content, "result": result_df})

        except Exception as e:
            st.error(f"Something went wrong: {e}")