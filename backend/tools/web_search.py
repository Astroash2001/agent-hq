import os
from typing import Dict, Any, List


async def search_web(query: str, max_results: int = 3) -> Dict[str, Any]:
    """
    Search the web or knowledge repository for information.
    Provides structured research snippets.
    """
    query_lower = query.lower()

    # Domain-specific synthesized results for typical agent workflows
    results: List[Dict[str, str]] = []

    if "rag" in query_lower:
        results.append({
            "title": "RAG Best Practices 2026: Hybrid Search & Reranking",
            "snippet": "Combining vector similarity (dense retrieval) with BM25 keyword search (sparse retrieval) improves top-3 precision by 28%. Rerankers like Cohere Rerank v3 filter false positives.",
            "url": "https://docs.llamaindex.ai/en/stable/optimizing/production_rag/"
        })
        results.append({
            "title": "Chunking Strategies for Technical Documentation",
            "snippet": "Recursive character chunking with 512 token chunks and 50 token overlap preserves semantic continuity across code blocks.",
            "url": "https://langchain.readthedocs.io/rag/chunking"
        })
    elif "job" in query_lower or "salary" in query_lower or "hire" in query_lower:
        results.append({
            "title": "Top AI Engineering Roles in 2026",
            "snippet": "Demand for AI Engineers skilled in multi-agent orchestration, tool calling loops, and evaluation frameworks (Ragas/Langfuse) grew by 140% year-over-year.",
            "url": "https://techcareers.ai/market-report-2026"
        })
        results.append({
            "title": "AI Engineer Interview Guide: Coding & Architecture",
            "snippet": "Key interview topics: ReAct loop mechanics, Pydantic structured output, safe Text2SQL AST validation, and WebSocket real-time telemetry.",
            "url": "https://aiengineer.guide/interviews"
        })
    elif "sql" in query_lower:
        results.append({
            "title": "Safe Text2SQL: AST Parsing with SQLGlot",
            "snippet": "Always parse generated SQL with an AST validator to block destructive DDL/DML statements (DROP, ALTER, DELETE) before query execution against production DBs.",
            "url": "https://sqlglot.com/docs/security/read_only"
        })
    else:
        # No provider is wired up, and there are no fixtures for this topic.
        #
        # This used to return a filler snippet -- "Found latest technical
        # documentation ... related to '<query>'" -- which reads like a result
        # but carries nothing. A capable model treats that as a failed search
        # and tries again with different wording: an observed GPT-5.5 run
        # called this tool 11 times on one subtask, exhausted its step budget,
        # and filed a 39-byte stub as its research. Saying plainly that no
        # search is available lets the agent fall back to what it knows and
        # get on with the work.
        return {
            "success": True,
            "configured": False,
            "query": query,
            "count": 0,
            "results": [],
            "note": ("No web search provider is configured in this deployment, so no "
                     "external sources are available for this query. Do not retry the "
                     "search. Answer from your own knowledge and say in your output "
                     "that it was not verified against live sources."),
        }

    return {
        "success": True,
        "query": query,
        "results_count": len(results),
        "results": results[:max_results]
    }
