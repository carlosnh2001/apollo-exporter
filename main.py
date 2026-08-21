import os
import csv
import io
import asyncio
from datetime import datetime
from typing import List, Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

app = FastAPI(title="Apollo Exporter")

APOLLO_API_KEY = os.getenv("APOLLO_API_KEY", "")
APOLLO_BASE = "https://api.apollo.io/api/v1"

# In-memory export history
export_history: list = []

# ---------- Pydantic Models ----------

class SearchParams(BaseModel):
    locations: List[str] = ["Comunidad de Madrid, Spain"]
    employee_ranges: List[str] = [
        "51,100", "101,200", "201,500", "501,1000",
        "1001,2000", "2001,5000", "5001,10000", "10001,999999"
    ]
    keyword_tags: List[str] = []
    revenue_min: Optional[int] = None
    revenue_max: Optional[int] = None
    api_key: Optional[str] = None  # override env var if provided

# ---------- Helpers ----------

def get_api_key(override: Optional[str] = None) -> str:
    key = override or APOLLO_API_KEY
    if not key:
        raise HTTPException(
            status_code=400,
            detail="Apollo API key not configured. Set APOLLO_API_KEY env var or pass api_key in request."
        )
    return key


def build_payload(params: SearchParams, page: int = 1, per_page: int = 100) -> dict:
    payload: dict = {
        "page": page,
        "per_page": per_page,
        "organization_locations": params.locations,
        "organization_num_employees_ranges": params.employee_ranges,
    }
    if params.keyword_tags:
        payload["q_organization_keyword_tags"] = params.keyword_tags
    if params.revenue_min is not None or params.revenue_max is not None:
        payload["revenue_range"] = {}
        if params.revenue_min is not None:
            payload["revenue_range"]["min"] = params.revenue_min
        if params.revenue_max is not None:
            payload["revenue_range"]["max"] = params.revenue_max
    return payload


def extract_company(entry: dict, entry_type: str) -> dict:
    if entry_type == "account":
        domain = entry.get("domain") or entry.get("primary_domain", "")
        phone = (entry.get("primary_phone") or {}).get("number", "") or entry.get("phone", "")
        org_id = entry.get("organization_id", entry.get("id", ""))
    else:
        domain = entry.get("primary_domain", "")
        phone = (entry.get("primary_phone") or {}).get("number", "") or entry.get("phone", "")
        org_id = entry.get("id", "")

    return {
        "apollo_id": org_id,
        "nombre": entry.get("name", ""),
        "dominio": domain,
        "web": entry.get("website_url", ""),
        "linkedin": entry.get("linkedin_url", ""),
        "twitter": entry.get("twitter_url", ""),
        "facebook": entry.get("facebook_url", ""),
        "telefono": phone,
        "ciudad": entry.get("city", "") or entry.get("organization_city", ""),
        "provincia": entry.get("state", "") or entry.get("organization_state", ""),
        "pais": entry.get("country", "") or entry.get("organization_country", ""),
        "anno_fundacion": entry.get("founded_year", ""),
        "revenue": entry.get("organization_revenue_printed", "") or (
            str(int(entry.get("organization_revenue"))) if entry.get("organization_revenue") else ""
        ),
        "naics": ", ".join(entry.get("naics_codes", []) or []),
        "sic": ", ".join(entry.get("sic_codes", []) or []),
        "crecimiento_headcount_6m": round((entry.get("organization_headcount_six_month_growth") or 0) * 100, 1),
        "crecimiento_headcount_12m": round((entry.get("organization_headcount_twelve_month_growth") or 0) * 100, 1),
        "tipo_registro": entry_type,
    }


async def fetch_page(client: httpx.AsyncClient, api_key: str, payload: dict) -> dict:
    resp = await client.post(
        f"{APOLLO_BASE}/mixed_companies/search",
        headers={"x-api-key": api_key, "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=f"Apollo API error: {resp.text[:300]}")
    return resp.json()


# ---------- Routes ----------

@app.get("/", response_class=HTMLResponse)
async def index():
    with open(os.path.join(os.path.dirname(__file__), "frontend.html"), encoding="utf-8") as f:
        return f.read()


@app.post("/api/preview")
async def preview(params: SearchParams):
    api_key = get_api_key(params.api_key)
    payload = build_payload(params, page=1, per_page=1)
    async with httpx.AsyncClient() as client:
        data = await fetch_page(client, api_key, payload)
    pagination = data.get("pagination", {})
    return {
        "total": pagination.get("total_entries", 0),
        "pages": pagination.get("total_pages", 0),
    }


@app.post("/api/export")
async def export_csv(params: SearchParams):
    api_key = get_api_key(params.api_key)

    async with httpx.AsyncClient() as client:
        first_data = await fetch_page(client, api_key, build_payload(params, page=1, per_page=100))

    total_pages = first_data.get("pagination", {}).get("total_pages", 1)
    total_entries = first_data.get("pagination", {}).get("total_entries", 0)

    fieldnames = [
        "apollo_id", "nombre", "dominio", "web", "linkedin", "twitter", "facebook",
        "telefono", "ciudad", "provincia", "pais", "anno_fundacion", "revenue",
        "naics", "sic", "crecimiento_headcount_6m", "crecimiento_headcount_12m", "tipo_registro",
    ]

    async def generate():
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        yield output.getvalue().encode("utf-8-sig")

        seen_ids = set()

        def process_page_data(data):
            rows = []
            for acc in data.get("accounts", []):
                eid = acc.get("organization_id", acc.get("id", ""))
                if eid and eid not in seen_ids:
                    seen_ids.add(eid)
                    rows.append(extract_company(acc, "account"))
            for org in data.get("organizations", []):
                eid = org.get("id", "")
                if eid and eid not in seen_ids:
                    seen_ids.add(eid)
                    rows.append(extract_company(org, "organization"))
            return rows

        for row in process_page_data(first_data):
            out = io.StringIO()
            w = csv.DictWriter(out, fieldnames=fieldnames)
            w.writerow(row)
            yield out.getvalue().encode("utf-8")

        async with httpx.AsyncClient() as client:
            for page in range(2, total_pages + 1):
                payload = build_payload(params, page=page, per_page=100)
                data = await fetch_page(client, api_key, payload)
                for row in process_page_data(data):
                    out = io.StringIO()
                    w = csv.DictWriter(out, fieldnames=fieldnames)
                    w.writerow(row)
                    yield out.getvalue().encode("utf-8")
                await asyncio.sleep(0.2)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    location_slug = (params.locations[0] if params.locations else "export").replace(", ", "_").replace(" ", "_")[:30]
    filename = f"apollo_{location_slug}_{timestamp}.csv"

    export_history.append({
        "filename": filename,
        "timestamp": datetime.now().isoformat(),
        "filters": {
            "locations": params.locations,
            "employee_ranges": params.employee_ranges,
        },
        "total": total_entries,
    })
    if len(export_history) > 50:
        export_history.pop(0)

    return StreamingResponse(
        generate(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/history")
async def get_history():
    return list(reversed(export_history))


@app.get("/health")
async def health():
    return {"status": "ok", "api_key_set": bool(APOLLO_API_KEY)}
