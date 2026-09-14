import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
import yaml

from config import settings
from core.memory_store import MemoryStore
from core.vector_store import VectorStore
from core.api_client import NSClient
from core.deliberation_engine import DeliberationEngine, EngineStatus
from services.reporter import generate_acta, generate_micro_summary, generate_press_release
from services.vision import generate_initial_vision
from web.ws_handler import WSManager

logger = logging.getLogger(__name__)

memory = MemoryStore(settings.SQLITE_PATH)
vectors = VectorStore(settings.CHROMA_PATH, settings.LMSTUDIO_URL, settings.EMBEDDING_MODEL)
api = NSClient(settings.NATION_NAME, settings.NATION_PASSWORD, settings.CONTACT_EMAIL)
ws_manager = WSManager()
engine = DeliberationEngine(memory, vectors, api, on_event=ws_manager.make_event_handler())

_jinja_env = Environment(
    loader=FileSystemLoader(str(settings.PROJECT_ROOT / "web" / "templates")),
    autoescape=select_autoescape(["html", "xml"]),
    cache_size=0,
)

def from_json_filter(value):
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value or []

def chr_filter(code):
    return chr(code)

_jinja_env.filters["from_json"] = from_json_filter
_jinja_env.filters["chr"] = chr_filter

def render(name: str, **context) -> str:
    template = _jinja_env.get_template(name)
    return template.render(**context)


@asynccontextmanager
async def lifespan(app: FastAPI):
    memory.recover_stale_sessions()
    if vectors.get_vision() is None:
        default_vision = (
            "Construir una nación próspera, justa y sostenible, "
            "donde el crecimiento económico vaya de la mano de la protección social, "
            "la calidad institucional y el respeto al medio ambiente."
        )
        vectors.store_vision(default_vision)
        if memory.get_active_vision() is None:
            memory.save_vision(default_vision, summary=default_vision[:100], author_role="presidente")

    async def _background_init():
        await asyncio.sleep(2)
        try:
            await asyncio.to_thread(generate_initial_vision, vectors)
        except Exception as e:
            logger.warning(f"No se pudo generar visión con IA: {e}")

    asyncio.create_task(_background_init())
    asyncio.create_task(engine.main_loop())
    yield
    memory.close()

app = FastAPI(title="Moncloa-AI", lifespan=lifespan)

app.mount("/static", StaticFiles(directory=str(settings.PROJECT_ROOT / "web" / "static")), name="static")

@app.middleware("http")
async def log_errors(request, call_next):
    try:
        return await call_next(request)
    except Exception as e:
        logger.exception(f"Error handling {request.method} {request.url.path}: {e}")
        from fastapi.responses import HTMLResponse
        return HTMLResponse(f"<h1>500 Error</h1><pre>{e}</pre>", status_code=500)


# --- WebSocket ---

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws_manager.connect(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(ws)


# --- Pages ---

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    state = memory.get_latest_snapshot() or {}
    stats = memory.get_stats()
    recent = memory.list_sessions(limit=10)
    for session in recent:
        if not session.get("summary") and session.get("status") == "completed":
            session["summary"] = generate_micro_summary(memory, session["id"])
    html = render("dashboard.html",
        state=state, stats=stats, recent=recent,
        engine_status=engine.status.value,
        personalities=engine.personalities,
        progress=memory.get_snapshots(60),
        pending_count=len(memory.get_pending_sessions()),
    )
    return HTMLResponse(html)


@app.get("/consejo/{session_id}", response_class=HTMLResponse)
async def deliberation_view(request: Request, session_id: int):
    session = memory.get_session(session_id)
    if not session:
        raise HTTPException(404, "Sesión no encontrada")
    interventions = memory.get_session_interventions(session_id)
    html = render("deliberation.html",
        session=session, interventions=interventions,
        personalities=engine.personalities,
        engine_status=engine.status.value,
    )
    return HTMLResponse(html)


@app.get("/historial", response_class=HTMLResponse)
async def historial(request: Request):
    sessions = memory.list_sessions(limit=100)
    for session in sessions:
        if not session.get("summary") and session.get("status") == "completed":
            session["summary"] = generate_micro_summary(memory, session["id"])
    html = render("historial.html",
        sessions=sessions,
        engine_status=engine.status.value,
    )
    return HTMLResponse(html)


@app.get("/historial/{session_id}", response_class=HTMLResponse)
async def acta_view(request: Request, session_id: int):
    session = memory.get_session(session_id)
    if not session:
        raise HTTPException(404, "Sesión no encontrada")
    interventions = memory.get_session_interventions(session_id)
    acta = generate_acta(memory, session_id, settings.ACTS_PATH)
    html = render("acta.html",
        session=session, interventions=interventions, acta=acta,
        engine_status=engine.status.value,
    )
    return HTMLResponse(html)


@app.get("/reglas", response_class=HTMLResponse)
async def rules_view(request: Request):
    html = render("rules.html",
        engine_status=engine.status.value,
    )
    return HTMLResponse(html)


@app.get("/configurar", response_class=HTMLResponse)
async def config_view(request: Request):
    html = render("config.html",
        personalities=engine.personalities,
        engine_status=engine.status.value,
    )
    return HTMLResponse(html)


# --- API ---

@app.get("/api/status")
async def api_status():
    state = memory.get_latest_snapshot()
    stats = memory.get_stats()
    pending = memory.get_pending_sessions()
    return {
        "engine_status": engine.status.value,
        "state": state,
        "stats": stats,
        "pending_count": len(pending),
        "active_session": pending[0] if pending else None,
        "progress": memory.get_snapshots(30),
    }


@app.get("/api/progress")
async def api_progress(limit: int = 60):
    return {"snapshots": memory.get_snapshots(limit)}


@app.get("/api/feed")
async def api_feed(limit: int = 12):
    sessions = memory.list_sessions(limit=max(1, min(limit, 50)))
    return {
        "items": [
            {**session, "summary": session.get("summary") or generate_micro_summary(memory, session["id"])}
            for session in sessions
            if session.get("status") == "completed"
        ]
    }


@app.post("/api/loop/pause")
async def api_pause():
    engine.pause()
    return {"status": "paused"}


@app.post("/api/loop/resume")
async def api_resume():
    engine.resume()
    return {"status": "running"}


@app.post("/api/config/personality")
async def api_update_personality(data: dict):
    role = data.get("role")
    field = data.get("field")
    value = data.get("value")
    if role not in engine.personalities:
        raise HTTPException(400, "Rol inválido")
    if field not in engine.personalities[role]:
        raise HTTPException(400, "Campo inválido")
    engine.personalities[role][field] = value

    data_path = settings.PERSONALITIES_PATH
    with open(data_path, "r", encoding="utf-8") as f:
        yaml_data = yaml.safe_load(f)
    if role in yaml_data.get("roles", {}):
        yaml_data["roles"][role][field] = value
        with open(data_path, "w", encoding="utf-8") as f:
            yaml.dump(yaml_data, f, allow_unicode=True, default_flow_style=False)

    return {"status": "ok", "role": role, "field": field}


@app.get("/api/session/{session_id}")
async def api_session(session_id: int):
    session = memory.get_session(session_id)
    if not session:
        raise HTTPException(404)
    interventions = memory.get_session_interventions(session_id)
    return {
        "session": session,
        "interventions": interventions,
        "summary": session.get("summary") or generate_micro_summary(memory, session_id),
        "press_release": generate_press_release(memory, session_id),
    }


@app.get("/api/sessions/recent")
async def api_recent_sessions(limit: int = 10):
    sessions = memory.list_sessions(limit=limit)
    return {"sessions": sessions}


@app.post("/api/check-now")
async def api_check_now():
    """Forzar una comprobación inmediata de issues pendientes."""
    if engine._check_lock.locked():
        raise HTTPException(409, "Ya hay una comprobación o deliberación en curso")
    asyncio.create_task(engine._check_and_process())
    return {"status": "checking"}


@app.post("/api/session/{session_id}/retry")
async def api_retry_session(session_id: int):
    session = memory.get_session(session_id)
    if not session:
        raise HTTPException(404, "Sesión no encontrada")
    if engine._check_lock.locked():
        raise HTTPException(409, "Ya hay una deliberación en curso")
    memory.recover_session(session_id)
    asyncio.create_task(engine._check_and_process())
    return {"status": "retrying", "session_id": session_id}


@app.post("/api/simular")
async def api_simulate(data: dict):
    issue = {
        "id": "sim_" + uuid.uuid4().hex[:12],
        "title": data.get("title", "Issue de simulación"),
        "text": data.get("description", ""),
        "author": "Simulador",
        "options": [
            {"id": str(i), "text": opt}
            for i, opt in enumerate(data.get("options", ["Opción A", "Opción B", "Opción C"]))
        ],
    }
    if not issue["text"].strip() or len(issue["options"]) < 2:
        raise HTTPException(400, "La simulación necesita descripción y al menos dos opciones")
    if engine._check_lock.locked():
        raise HTTPException(409, "Ya hay una comprobación o deliberación en curso")

    state = await asyncio.to_thread(api.get_nation_full) if settings.NATION_NAME else {
        "approval": 50, "economy": "Fuerte", "deficit": "2.5%",
        "population": 50000000, "civil_rights": "Excelentes",
        "economy_freedom": "Robusta", "political_freedom": "Muy alta",
    }

    async with engine._check_lock:
        session_id = memory.create_session(
            issue_id=issue["id"],
            title=issue["title"],
            description=issue["text"],
            options=[o["text"] for o in issue["options"]],
            option_ids=[o["id"] for o in issue["options"]],
            is_simulation=True,
        )
        await engine._process_issue(issue, state)

    session = memory.get_session(session_id)
    interventions = memory.get_session_interventions(session_id)

    return {
        "session_id": session_id,
        "outcome": session.get("outcome"),
        "summary": session.get("summary"),
        "phase1": [i for i in interventions if i["phase"] == 1],
        "phase2": [i for i in interventions if i["phase"] == 2],
    }
