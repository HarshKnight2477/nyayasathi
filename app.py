"""NyayaSathi - free, local study assistant for Indian law. Run: python app.py, then open http://localhost:8000"""

import os
import re
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

import law_ai
import sources
import topic

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="NyayaSathi")


class Query(BaseModel):
    q: str


def _error(message, status=400):
    return JSONResponse({"error": message}, status_code=status)


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


CASE_NAME = re.compile(r"\w\s+(?:v|vs|versus)\.?\s+\w", re.I)


@app.post("/api/search")
def search(req: Query):
    """One box: a case name/citation -> verify it; a named section -> explain it; anything else -> topic search."""
    q = req.q.strip()[:500]
    if len(q) < 2:
        return _error("Type a topic (e.g. 'murder', 'land acquisition'), a section, or a case.")
    try:
        if sources.CITE_RE.search(q) or CASE_NAME.search(q):
            return {"mode": "citation", **sources.case_lookup(q)}
        if sources.find_references(q):
            return {"mode": "section", **law_ai.answer(q)}
        return topic.search_topic(q)
    except law_ai.LawAIError as e:
        return _error(str(e), 503)
    except sources.RateLimited as e:
        return _error(str(e), 429)
    except Exception as e:
        return _error(f"Couldn't fetch results right now ({type(e).__name__}). Check your internet and retry.", 502)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", 8000)))
