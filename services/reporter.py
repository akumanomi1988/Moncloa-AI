from datetime import datetime
import json
import re
from pathlib import Path

from core.memory_store import MemoryStore


def generate_acta(memory: MemoryStore, session_id: int, output_dir: str | Path) -> str:
    session = memory.get_session(session_id)
    if not session:
        return ""

    interventions = memory.get_session_interventions(session_id)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    date_str = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"acta_{session_id}_{date_str}.md"
    filepath = output_dir / filename

    lines = [
        f"# Acta del Consejo de Ministros",
        f"**Sesión ID:** {session_id}",
        f"**Issue:** {session['issue_title']}",
        f"**Fecha:** {session.get('started_at', '?')}",
        f"**Estado:** {session['status']}",
        f"**Resultado:** Opción {session.get('outcome', 'N/A')}",
        f"",
        f"---",
        f"## Descripción del dilema",
        f"",
        session.get("issue_description", ""),
        f"",
        f"---",
        f"## Intervenciones",
        f"",
    ]

    for inv in interventions:
        lines.extend([
            f"### {inv['councilor_name']} ({inv['councilor_role']}) - Fase {inv['phase']}.{inv['round']}",
            f"",
            f"**Voto:** {inv.get('vote_choice', 'No emite voto')}",
            f"",
            f"{inv.get('reasoning') or inv.get('content', '')}",
            f"",
            f"---",
            f"",
        ])

    lines.append(f"*Acta generada el {datetime.utcnow().isoformat()}*")

    content = "\n".join(lines)
    filepath.write_text(content, encoding="utf-8")

    return content


def generate_press_release(memory: MemoryStore, session_id: int) -> str:
    session = memory.get_session(session_id)
    if not session:
        return ""

    interventions = memory.get_session_interventions(session_id)
    pres_interventions = [
        i for i in interventions if i["councilor_role"] == "presidente"
    ]
    final_reasoning = pres_interventions[-1]["reasoning"] if pres_interventions else ""

    lines = [
        f"# COMUNICADO OFICIAL - MONCLOA",
        f"",
        f"**Asunto:** {session['issue_title']}",
        f"**Decisión:** Opción {session.get('outcome', 'N/A')}",
        f"",
        f"El Consejo de Ministros, reunido en sesión extraordinaria,",
        f"ha adoptado la siguiente decisión:",
        f"",
        f"{final_reasoning}",
        f"",
        f"---",
        f"*Palacio de la Moncloa, {datetime.utcnow().strftime('%d de %B de %Y')}*",
    ]

    return "\n".join(lines)


def generate_micro_summary(memory: MemoryStore, session_id: int) -> str:
    """Create a short, deterministic status update suitable for the dashboard feed."""
    session = memory.get_session(session_id)
    if not session:
        return ""

    try:
        options = json.loads(session.get("options") or "[]")
    except json.JSONDecodeError:
        options = []
    try:
        option_ids = json.loads(session.get("option_ids") or "[]")
    except json.JSONDecodeError:
        option_ids = []
    outcome = str(session.get("outcome") or "N/A")
    selected = outcome
    if option_ids and outcome in [str(value) for value in option_ids]:
        selected = options[[str(value) for value in option_ids].index(outcome)]
    else:
        try:
            index = int(outcome) - 1
            if 0 <= index < len(options):
                selected = options[index]
        except ValueError:
            pass

    interventions = memory.get_session_interventions(session_id)
    reasoning = next(
        (i.get("reasoning") for i in reversed(interventions)
         if i.get("councilor_role") == "presidente" and i.get("reasoning")),
        "",
    )
    reasoning = re.sub(r"\s+", " ", reasoning).strip()
    summary = f"{session['issue_title']}: se elige {selected}."
    if reasoning:
        summary += f" {reasoning}"
    return summary[:277].rstrip() + ("..." if len(summary) > 280 else "")
