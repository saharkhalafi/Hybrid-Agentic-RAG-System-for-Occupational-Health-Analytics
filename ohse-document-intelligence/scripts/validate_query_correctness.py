"""Validate diverse HSE queries after correctness fixes."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents.orchestrator.pipeline import QueryOrchestrator
from agents.session.store import SessionStore
from database.session import SessionLocal

QUERIES = [
    ("twe برای استونیتریل چقدره", "fresh-1"),
    ("حد مجاز فیبرهای سیلیکات آلومینیوم چقدره", "fresh-2"),
    ("STEL acetaldehyde 75-07-0", "fresh-3"),
    ("حد مجاز TWA برای بیس فنول آ 80-05-7", "fresh-4"),
    ("STEL for chlorine trifluoride CAS 7790-91-2", "fresh-5"),
    ("حد TWA بنزن چقدره؟", "fresh-6"),
    ("CAS 71-43-2", "fresh-7"),
    ("STEL?", "followup-1"),  # no session — should clarify
]

POLLUTED_SEQUENCE = [
    ("STEL acetonitrile 75-05-8", "polluted"),
    ("حد مجاز فیبرهای سیلیکات آلومینیوم چقدره", "polluted"),
    ("TWA?", "polluted"),
]


def run(session, store, query: str, session_id: str) -> dict:
    orch = QueryOrchestrator(session, session_store=store)
    r = orch.handle(query, session_id=session_id)
    slots = r.metadata.get("trace", {}).get("slots", {})
    return {
        "query": query,
        "session_id": session_id,
        "intent": r.intent,
        "success": r.success,
        "requires_clarification": r.requires_clarification,
        "cas": slots.get("cas"),
        "chemical": (slots.get("chemical_name") or "")[:60],
        "answer_preview": (r.answer or "")[:120],
    }


def main() -> None:
    session = SessionLocal()
    store = SessionStore()
    results = []
    try:
        for q, sid in QUERIES:
            results.append(run(session, store, q, sid))
        for q, sid in POLLUTED_SEQUENCE:
            results.append(run(session, store, q, sid))
    finally:
        session.close()
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
