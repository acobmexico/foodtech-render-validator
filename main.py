import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from foodtech_api import FoodtechAPIClient, FoodtechAPIError
from excel_results import append_analysis_result, build_excel, result_count
from validator_engine import evaluate_record


APP_ACCESS_KEY = os.getenv("APP_ACCESS_KEY", "").strip()
API_BASE_URL = os.getenv("FOODTECH_API_BASE_URL", "").strip()
API_TOKEN = os.getenv("FOODTECH_API_TOKEN", "").strip()
SCORE_THRESHOLD = int(os.getenv("SCORE_THRESHOLD", "65"))
MAX_PARALLEL_CASES = max(1, min(10, int(os.getenv("MAX_PARALLEL_CASES", "5"))))
AUTO_RUN_ON_START = os.getenv("AUTO_RUN_ON_START", "false").strip().lower() in {
    "1", "true", "yes", "si", "sí"
}

app = FastAPI(title="FOODTECH 2026 AI Validator", version="1.0.0")
batch_lock = threading.Lock()
job_lock = threading.Lock()
job_stop_event = threading.Event()
job_thread: threading.Thread | None = None
job_state = {
    "status": "idle", "started_at": None, "finished_at": None,
    "batch_size": 10, "after_id": 0, "last_id": 0, "batches": 0,
    "records_received": 0, "records_saved": 0, "allowed": 0,
    "banned": 0, "errors": 0, "elapsed_seconds": 0,
    "last_error": None, "recent_errors": [],
}


class BatchRequest(BaseModel):
    max_records: int = Field(default=10, ge=1, le=10)
    after_id: int = Field(default=0, ge=0)


class PackageRequest(BaseModel):
    records: list[dict] = Field(min_length=1, max_length=10)


class RunAllRequest(BaseModel):
    batch_size: int = Field(default=10, ge=1, le=10)
    after_id: int = Field(default=0, ge=0)


def require_access_key(x_api_key: str | None):
    if not APP_ACCESS_KEY:
        raise HTTPException(500, "APP_ACCESS_KEY no está configurada en Render.")
    if x_api_key != APP_ACCESS_KEY:
        raise HTTPException(401, "Clave de acceso incorrecta.")


def api_client() -> FoodtechAPIClient:
    return FoodtechAPIClient(API_BASE_URL, API_TOKEN)


def append_excel_safely(result: dict, record: dict) -> None:
    try:
        append_analysis_result(result, record)
    except Exception as exc:
        result["excel_error"] = f"{type(exc).__name__}: {exc}"


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
        result = {
            **base,
            "status": "evaluation_error",
            "error": f"{type(exc).__name__}: {exc}",
            "elapsed_seconds": round(time.perf_counter() - started, 2),
        }
        append_excel_safely(result, record)
        return result

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
        result = {
            **base,
            "status": "save_error",
            "evaluation": evaluation,
            "error": f"{type(exc).__name__}: {exc}",
            "elapsed_seconds": round(time.perf_counter() - started, 2),
        }
        append_excel_safely(result, record)
        return result

    result = {
        **base,
        "status": "saved",
        "evaluation": evaluation,
        "reason_sent_to_endpoint": endpoint_reason,
        "save_response": saved,
        "elapsed_seconds": round(time.perf_counter() - started, 2),
    }
    append_excel_safely(result, record)
    return result


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


def job_snapshot() -> dict:
    with job_lock:
        return dict(job_state)


def update_job(**changes):
    with job_lock:
        job_state.update(changes)


def continuous_worker(batch_size: int, after_id: int):
    started = time.perf_counter()
    cursor = after_id
    if not batch_lock.acquire(blocking=False):
        update_job(status="failed", last_error="Ya existe otro proceso ejecutándose.")
        return
    try:
        while not job_stop_event.is_set():
            pending = api_client().consult_pending(batch_size, cursor)
            records = pending.get("data") or []
            if not records:
                build_excel()
                update_job(
                    status="completed",
                    finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    elapsed_seconds=round(time.perf_counter() - started, 2),
                )
                return

            result = process_records(records)
            errors = [{
                "id": item.get("id"), "identity": item.get("identity"),
                "company": item.get("RazonSocial"), "status": item.get("status"),
                "error": item.get("error"),
            } for item in result["results"] if item.get("status") != "saved"]
            next_cursor = int(pending.get("lastId") or 0)
            if next_cursor <= cursor:
                numeric_ids = [int(r["id"]) for r in records if str(r.get("id") or "").isdigit()]
                next_cursor = max(numeric_ids, default=cursor)

            with job_lock:
                job_state["batches"] += 1
                for key in ("records_received", "records_saved", "allowed", "banned", "errors"):
                    job_state[key] += result[key]
                job_state["last_id"] = next_cursor
                job_state["elapsed_seconds"] = round(time.perf_counter() - started, 2)
                job_state["recent_errors"] = (job_state["recent_errors"] + errors)[-100:]

            if next_cursor <= cursor:
                raise RuntimeError("La API no avanzó lastId; se detuvo para evitar un ciclo infinito.")
            cursor = next_cursor

        update_job(
            status="stopped",
            finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            elapsed_seconds=round(time.perf_counter() - started, 2),
        )
        build_excel()
    except Exception as exc:
        try:
            if result_count():
                build_excel()
        except Exception:
            pass
        update_job(
            status="failed",
            finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            elapsed_seconds=round(time.perf_counter() - started, 2),
            last_error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        batch_lock.release()


def start_continuous_job(batch_size: int = 10, after_id: int = 0) -> dict:
    global job_thread
    with job_lock:
        if job_state["status"] in {"starting", "running"}:
            raise HTTPException(409, "El procesamiento completo ya está ejecutándose.")
        job_stop_event.clear()
        job_state.update({
            "status": "running", "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "finished_at": None, "batch_size": batch_size, "after_id": after_id,
            "last_id": after_id, "batches": 0, "records_received": 0,
            "records_saved": 0, "allowed": 0, "banned": 0, "errors": 0,
            "elapsed_seconds": 0, "last_error": None, "recent_errors": [],
        })
        job_thread = threading.Thread(
            target=continuous_worker, args=(batch_size, after_id),
            name="foodtech-continuous-validator", daemon=True,
        )
        job_thread.start()
        return dict(job_state)


@app.on_event("startup")
def startup_auto_run():
    openai_ready = bool(os.getenv("OPENAI_API_KEY", "").strip())
    if AUTO_RUN_ON_START and API_BASE_URL and API_TOKEN and openai_ready:
        start_continuous_job(10, 0)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "foodtech_api_configured": bool(API_BASE_URL and API_TOKEN),
        "openai_configured": bool(os.getenv("OPENAI_API_KEY", "").strip()),
        "parallel_workers": MAX_PARALLEL_CASES,
        "threshold": SCORE_THRESHOLD,
        "batch_running": batch_lock.locked(),
        "continuous_job": job_snapshot()["status"],
        "auto_run_on_start": AUTO_RUN_ON_START,
        "excel_rows": result_count(),
    }


@app.get("/api/results.xlsx")
def download_results(x_api_key: str | None = Header(default=None)):
    require_access_key(x_api_key)
    try:
        path = build_excel()
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    return FileResponse(
        path=str(path),
        filename="FOODTECH_VALIDACION_RESULTADOS.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.post("/api/run-all")
def run_all(payload: RunAllRequest, x_api_key: str | None = Header(default=None)):
    require_access_key(x_api_key)
    return start_continuous_job(payload.batch_size, payload.after_id)


@app.get("/api/run-all/status")
def run_all_status(x_api_key: str | None = Header(default=None)):
    require_access_key(x_api_key)
    return job_snapshot()


@app.post("/api/run-all/stop")
def stop_run_all(x_api_key: str | None = Header(default=None)):
    require_access_key(x_api_key)
    snapshot = job_snapshot()
    if snapshot["status"] not in {"starting", "running"}:
        return {**snapshot, "message": "No hay un procesamiento completo activo."}
    job_stop_event.set()
    return {**snapshot, "message": "Se solicitó detener al terminar el lote actual."}


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
<body><main><section class="card"><h1>FOODTECH 2026 AI Validator</h1><p>Procesa todos los pendientes automáticamente, en bloques de diez y con guardado individual.</p>
<label>Clave de acceso</label><input id="key" type="password" placeholder="APP_ACCESS_KEY">
<button id="all" onclick="runAll()">Procesar todos los pendientes</button> <button id="stop" onclick="stopAll()" style="background:#9b2c2c">Detener</button>
<button id="download" onclick="downloadExcel()" style="background:#245a9b">Descargar Excel</button>
<div id="allstatus" class="summary"></div>
<hr style="margin:24px 0;border:0;border-top:1px solid #d6dfd9"><p><strong>Prueba o ejecución manual de un lote</strong></p>
<label>Número de casos</label><input id="count" type="number" min="1" max="10" value="10">
<label>Después del ID</label><input id="after" type="number" min="0" value="0">
<button id="run" onclick="runBatch()">Procesar lote</button><div id="status"></div><div id="error" class="error" style="display:none"></div>
<div id="result" style="display:none"><div id="summary" class="summary"></div><div style="overflow:auto"><table><thead><tr><th>ID</th><th>Empresa</th><th>Estado</th><th>Puntaje</th><th>Decisión</th><th>Razón</th><th>Segundos</th></tr></thead><tbody id="rows"></tbody></table></div><details><summary>Respuesta JSON completa</summary><pre id="json"></pre></details></div>
</section></main><script>
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));
const headers=()=>({'Content-Type':'application/json','X-API-Key':document.getElementById('key').value});
async function runAll(){try{const r=await fetch('/api/run-all',{method:'POST',headers:headers(),body:JSON.stringify({batch_size:10,after_id:0})});const d=await r.json();if(!r.ok)throw new Error(d.detail||JSON.stringify(d));renderAll(d);pollAll()}catch(e){alert(e.message)}}
async function stopAll(){try{const r=await fetch('/api/run-all/stop',{method:'POST',headers:headers()});const d=await r.json();if(!r.ok)throw new Error(d.detail||JSON.stringify(d));renderAll(d)}catch(e){alert(e.message)}}
async function downloadExcel(){try{const r=await fetch('/api/results.xlsx',{headers:headers()});if(!r.ok){let d;try{d=await r.json()}catch(_){d={detail:await r.text()}}throw new Error(d.detail||'No fue posible generar el Excel')}const blob=await r.blob(),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='FOODTECH_VALIDACION_RESULTADOS.xlsx';document.body.appendChild(a);a.click();a.remove();URL.revokeObjectURL(url)}catch(e){alert(e.message)}}
function renderAll(d){document.getElementById('allstatus').innerHTML=`<span class="pill">Estado: ${esc(d.status)}</span><span class="pill">Procesados: ${esc(d.records_received)}</span><span class="pill">Guardados: ${esc(d.records_saved)}</span><span class="pill">Aprobados: ${esc(d.allowed)}</span><span class="pill">Rechazados: ${esc(d.banned)}</span><span class="pill">Errores: ${esc(d.errors)}</span><span class="pill">Lotes: ${esc(d.batches)}</span><span class="pill">Tiempo: ${esc(d.elapsed_seconds)} s</span>${d.last_error?`<span class="error">${esc(d.last_error)}</span>`:''}`}
async function pollAll(){try{const r=await fetch('/api/run-all/status',{headers:headers()});const d=await r.json();if(!r.ok)throw new Error(d.detail||JSON.stringify(d));renderAll(d);if(['running','starting'].includes(d.status))setTimeout(pollAll,5000)}catch(e){document.getElementById('allstatus').innerHTML=`<span class="error">${esc(e.message)}</span>`}}
async function runBatch(){const button=document.getElementById('run'),status=document.getElementById('status'),error=document.getElementById('error'),result=document.getElementById('result');error.style.display='none';result.style.display='none';button.disabled=true;status.textContent='Consultando y analizando el lote. No cierre esta página...';try{const response=await fetch('/api/run-batch',{method:'POST',headers:{'Content-Type':'application/json','X-API-Key':document.getElementById('key').value},body:JSON.stringify({max_records:Number(document.getElementById('count').value),after_id:Number(document.getElementById('after').value)})});const raw=await response.text();let data;try{data=JSON.parse(raw)}catch(_){throw new Error(`HTTP ${response.status}: ${raw.slice(0,500)}`)}if(!response.ok)throw new Error(data.detail||JSON.stringify(data));document.getElementById('summary').innerHTML=`<span class="pill">Recibidos: ${data.records_received}</span><span class="pill">Guardados: ${data.records_saved??0}</span><span class="pill">Aprobados: ${data.allowed??0}</span><span class="pill">Rechazados: ${data.banned??0}</span><span class="pill">Errores: ${data.errors??0}</span><span class="pill">Tiempo: ${data.elapsed_seconds??0} s</span>`;document.getElementById('rows').innerHTML=(data.results||[]).map(x=>`<tr><td>${esc(x.identity)}</td><td>${esc(x.RazonSocial)}</td><td>${esc(x.status)}</td><td>${esc(x.evaluation?.score)}</td><td>${esc(x.evaluation?.decision)}</td><td>${esc(x.evaluation?.reason||x.error)}</td><td>${esc(x.elapsed_seconds)}</td></tr>`).join('');document.getElementById('json').textContent=JSON.stringify(data,null,2);result.style.display='block';status.textContent='Lote terminado.'}catch(e){error.textContent=e.message;error.style.display='block';status.textContent=''}finally{button.disabled=false}}
</script></body></html>'''
