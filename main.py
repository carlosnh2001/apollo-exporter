import os
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import List, Optional

app = FastAPI(title="PrimeCRM")

APOLLO_API_KEY = os.getenv("APOLLO_API_KEY", "")
APOLLO_BASE = "https://api.apollo.io/api/v1"


class PeopleSearch(BaseModel):
    q_person_name: Optional[str] = None
    q_organization_name: Optional[str] = None
    person_locations: Optional[List[str]] = None
    person_titles: Optional[List[str]] = None
    page: int = 1
    per_page: int = 25


@app.get("/", response_class=HTMLResponse)
async def home():
    with open("frontend.html", "r", encoding="utf-8") as f:
        return f.read()


@app.post("/api/apollo/people")
async def apollo_people(params: PeopleSearch):
    if not APOLLO_API_KEY:
        raise HTTPException(400, "APOLLO_API_KEY no configurada")
    payload = {
        "api_key": APOLLO_API_KEY,
        "page": params.page,
        "per_page": params.per_page,
    }
    if params.q_person_name:
        payload["q_person_name"] = params.q_person_name
    if params.q_organization_name:
        payload["q_organization_name"] = params.q_organization_name
    if params.person_locations:
        payload["person_locations"] = params.person_locations
    if params.person_titles:
        payload["person_titles"] = params.person_titles

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{APOLLO_BASE}/mixed_people/search", json=payload)

    if r.status_code != 200:
        raise HTTPException(r.status_code, r.text[:300])
    return r.json()


@app.get("/health")
async def health():
    return {"status": "ok", "apollo_configured": bool(APOLLO_API_KEY)}
