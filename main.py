import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from foodtech_api import FoodtechAPIClient, FoodtechAPIError
from validator_engine import evaluate_record


APP_ACCESS_KEY = os.getenv("APP_ACCESS_KEY", "").strip()
API_BASE_URL = os.getenv("FOODTECH_API_BASE_URL", "").strip()
API_TOKEN = os.getenv("FOODTECH_API_TOKEN", "").strip()
SCORE_THRESHOLD = int(os.getenv("SCORE_THRESHOLD", "65"))
MAX_PARALLEL_CASES = max(1, min(10, int(os.getenv("MAX_PARALLEL_CASES", "5"))))

app = FastAPI(title="FOODTECH 2026 AI Validator", version="1.0.0")
batch_lock = threading.Lock()


class BatchRequest(BaseModel):
    max_records: int = Field(default=10, ge=1, le=10)
    after_id: int = Field(default=0, ge=0)


class PackageRequest(BaseModel):
    records: list[dict] = Field(min_length=1, max_length=10)


def require_access_key(x_api_key: str | None):
    if not APP_ACCESS_KEY:
        raise HTTPException(500, "APP_ACCESS_KEY no está configurada en Render.")
    if x_api_key != APP_ACCESS_KEY:
        raise HTTPException(401, "Clave de acceso incorrecta.")


def api_client() -> FoodtechAPIClient:
    return FoodtechAPIClient(API_BASE_URL, API_TOKEN)


def process_one(record: dict) -> dict:
    started = time.perf_counter()
    enrollment = str(record.get("enrollmentCode") or "").strip()
    visitor_id = str(record.get("IDVisitante") or "").strip()
    identity = enrollment or visitor_id or str(record.get("id") or "")
    base = {
        "id": record.get("id"),
        "enrollmentCode": enrollment,
        "IDVisitante": visitor_id,
        "RazonSocial": str(record.get("RazonSocial") or ""),
        "identity": identity,
    }
    try:
        evaluation = evaluate_record(record, SCORE_THRESHOLD)
    except Exception as exc:
        return {
            **base,
            "status": "evaluation_error",
            "error": f"{type(exc).__name__}: {exc}",
            "elapsed_seconds": round(time.perf_counter() - started, 2),
        }

    try:
        endpoint_reason = (
            f"{evaluation['score']}/100; {evaluation['type']}; "
            f"{evaluation['reason']}"
        )[:250]
        saved = api_client().save_validation(
            enrollment,
            visitor_id,
            evaluation["decision"],
            endpoint_reason,
        )
    except Exception as exc:
        return {
            **base,
            "status": "save_error",
            "evaluation": evaluation,
            "error": f"{type(exc).__name__}: {exc}",
            "elapsed_seconds": round(time.perf_counter() - started, 2),
        }

    return {
        **base,
        "status": "saved",
        "evaluation": evaluation,
        "reason_sent_to_endpoint": endpoint_reason,
        "save_response": saved,
        "elapsed_seconds": round(time.perf_counter() - started, 2),
    }


def process_records(records: list[dict]) -> dict:
    started = time.perf_counter()
    ordered_results = [None] * len(records)
    worker_count = min(MAX_PARALLEL_CASES, len(records))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_positions = {
            executor.submit(process_one, record): index
            for index, record in enumerate(records)
        }
        for future in as_completed(future_positions):
            ordered_results[future_positions[future]] = future.result()
    saved = sum(item["status"] == "saved" for item in ordered_results)
    allowed = sum(
        item.get("evaluation", {}).get("decision") == "allowed"
        and item["status"] == "saved"
        for item in ordered_results
    )
    banned = sum(
        item.get("evaluation", {}).get("decision") == "banned"
        and item["status"] == "saved"
        for item in ordered_results
    )
    return {
        "success": saved == len(records),
        "records_received": len(records),
        "records_saved": saved,
        "allowed": allowed,
        "banned": banned,
        "errors": len(records) - saved,
        "parallel_workers": worker_count,
        "threshold": SCORE_THRESHOLD,
        "elapsed_seconds": round(time.perf_counter() - started, 2),
        "results": ordered_results,
    }


def run_exclusively(operation):
    if not batch_lock.acquire(blocking=False):
        raise HTTPException(409, "Ya existe un lote ejecutándose en esta instancia.")
    try:
        return operation()
    finally:
        batch_lock.release()


@app.get("/health")
def health():
    return {
        "status": "ok",
        "foodtech_api_configured": bool(API_BASE_URL and API_TOKEN),
        "openai_configured": bool(os.getenv("OPENAI_API_KEY", "").strip()),
        "parallel_workers": MAX_PARALLEL_CASES,
        "threshold": SCORE_THRESHOLD,
        "batch_running": batch_lock.locked(),
    }


@app.post("/api/run-batch")
def run_batch(payload: BatchRequest, x_api_key: str | None = Header(default=None)):
    require_access_key(x_api_key)

    def operation():
        pending = api_client().consult_pending(payload.max_records, payload.after_id)
        records = pending.get("data") or []
        if not records:
            return {
                "success": True,
                "message": "No se encontraron registros pendientes.",
                "records_received": 0,
                "pending_response": {
                    "recordsFound": pending.get("recordsFound", 0),
                    "lastId": pending.get("lastId", payload.after_id),
                    "hasMore": pending.get("hasMore", False),
                },
                "results": [],
            }
        result = process_records(records)
        result["pending_response"] = {
            "recordsFound": pending.get("recordsFound", len(records)),
            "lastId": pending.get("lastId", payload.after_id),
            "hasMore": pending.get("hasMore", False),
        }
        return result

    try:
        return run_exclusively(operation)
    except HTTPException:
        raise
    except FoodtechAPIError as exc:
        raise HTTPException(502, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, f"{type(exc).__name__}: {exc}") from exc


@app.post("/api/process-package")
def process_package(
    payload: PackageRequest,
    x_api_key: str | None = Header(default=None),
):
    require_access_key(x_api_key)
    return run_exclusively(lambda: process_records(payload.records))


@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse(HTML_PAGE)


HTML_PAGE = r'''<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FOODTECH 2026 AI Validator</title>
<style>
body{margin:0;background:#eef3f0;font-family:Arial,sans-serif;color:#17221b}main{max-width:1080px;margin:38px auto;padding:0 18px}.card{background:white;padding:28px;border-radius:16px;box-shadow:0 12px 34px #1733221f}h1{margin:0;color:#174b32}p{color:#56665c}label{display:block;font-weight:bold;margin:15px 0 6px}input{width:100%;padding:11px;border:1px solid #b9c8bf;border-radius:8px;box-sizing:border-box}button{margin-top:18px;padding:12px 20px;border:0;border-radius:8px;background:#18864b;color:white;font-weight:bold;cursor:pointer}button:disabled{opacity:.55;cursor:wait}#status{margin-top:16px;font-weight:bold}.error{background:#fee;color:#8e1e1e;padding:12px;border-radius:8px;white-space:pre-wrap}.summary{display:flex;gap:10px;flex-wrap:wrap;margin:18px 0}.pill{background:#edf6f0;padding:10px 13px;border-radius:8px}table{width:100%;border-collapse:collapse;font-size:14px}th,td{border:1px solid #d6dfd9;padding:9px;vertical-align:top}th{background:#174b32;color:white}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f5f7f6;padding:14px;border-radius:8px}details{margin-top:16px}</style></head>
<body><main><section class="card"><h1>FOODTECH 2026 AI Validator</h1><p>Consulta hasta diez pendientes, los evalúa en paralelo y guarda cada dictamen confirmado.</p>
<label>Clave de acceso</label><input id="key" type="password" placeholder="APP_ACCESS_KEY">
<label>Número de casos</label><input id="count" type="number" min="1" max="10" value="10">
<label>Después del ID</label><input id="after" type="number" min="0" value="0">
<button id="run" onclick="runBatch()">Procesar lote</button><div id="status"></div><div id="error" class="error" style="display:none"></div>
<div id="result" style="display:none"><div id="summary" class="summary"></div><div style="overflow:auto"><table><thead><tr><th>ID</th><th>Empresa</th><th>Estado</th><th>Puntaje</th><th>Decisión</th><th>Razón</th><th>Segundos</th></tr></thead><tbody id="rows"></tbody></table></div><details><summary>Respuesta JSON completa</summary><pre id="json"></pre></details></div>
</section></main><script>
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));
async function runBatch(){const button=document.getElementById('run'),status=document.getElementById('status'),error=document.getElementById('error'),result=document.getElementById('result');error.style.display='none';result.style.display='none';button.disabled=true;status.textContent='Consultando y analizando el lote. No cierre esta página...';try{const response=await fetch('/api/run-batch',{method:'POST',headers:{'Content-Type':'application/json','X-API-Key':document.getElementById('key').value},body:JSON.stringify({max_records:Number(document.getElementById('count').value),after_id:Number(document.getElementById('after').value)})});const raw=await response.text();let data;try{data=JSON.parse(raw)}catch(_){throw new Error(`HTTP ${response.status}: ${raw.slice(0,500)}`)}if(!response.ok)throw new Error(data.detail||JSON.stringify(data));document.getElementById('summary').innerHTML=`<span class="pill">Recibidos: ${data.records_received}</span><span class="pill">Guardados: ${data.records_saved??0}</span><span class="pill">Aprobados: ${data.allowed??0}</span><span class="pill">Rechazados: ${data.banned??0}</span><span class="pill">Errores: ${data.errors??0}</span><span class="pill">Tiempo: ${data.elapsed_seconds??0} s</span>`;document.getElementById('rows').innerHTML=(data.results||[]).map(x=>`<tr><td>${esc(x.identity)}</td><td>${esc(x.RazonSocial)}</td><td>${esc(x.status)}</td><td>${esc(x.evaluation?.score)}</td><td>${esc(x.evaluation?.decision)}</td><td>${esc(x.evaluation?.reason||x.error)}</td><td>${esc(x.elapsed_seconds)}</td></tr>`).join('');document.getElementById('json').textContent=JSON.stringify(data,null,2);result.style.display='block';status.textContent='Lote terminado.'}catch(e){error.textContent=e.message;error.style.display='block';status.textContent=''}finally{button.disabled=false}}
</script></body></html>'''
