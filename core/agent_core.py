import re
import logging
from typing import Optional

logger = logging.getLogger(__name__)

DECISION_RE = re.compile(r"<decision>\s*(.*?)\s*</decision>", re.IGNORECASE | re.DOTALL)
REASONING_RE = re.compile(r"<razonamiento>\s*(.*?)\s*</razonamiento>", re.IGNORECASE | re.DOTALL)


def build_prompt(role_config: dict, context: dict) -> str:
    template = role_config["system_prompt"]

    priorities = "\n".join(
        f"  {i + 1}. {p}" for i, p in enumerate(role_config.get("priorities", []))
    )

    relevant_history = context.get("relevant_history", "")
    if not relevant_history:
        relevant_history = "No hay decisiones previas relevantes sobre esta temática."

    vision = context.get("vision", "El Gobierno aún no ha definido una visión de futuro.")

    approval = context.get("approval", "N/A")
    economy = context.get("economy", "N/A")
    deficit = context.get("deficit", "N/A")
    population = context.get("population", "N/A")
    civil_rights = context.get("civil_rights", "N/A")
    economy_freedom = context.get("economy_freedom", "N/A")
    political_freedom = context.get("political_freedom", "N/A")

    filled = template.format(
        ideology=role_config.get("ideology", ""),
        loyalty=role_config.get("loyalty", ""),
        priorities=priorities,
        red_line=role_config.get("red_line", ""),
        approval=approval,
        economy_status=economy,
        deficit=deficit,
        population=population,
        civil_rights=civil_rights,
        economy_freedom=economy_freedom,
        political_freedom=political_freedom,
        vision=vision,
        relevant_history=relevant_history,
    )
    filled += (
        "\n\nPERFIL OPERATIVO DEL AGENTE:\n"
        f"- Estilo de negociación: {role_config.get('negotiation_style', 'directo y razonado')}\n"
        "- Debes aplicar esta personalidad de forma coherente en el análisis y en el debate."
    )

    issue = context.get("issue", {})
    title = issue.get("title", "")
    description = issue.get("text", "")
    options = issue.get("options", [])

    options_text = "\n".join(
        f"  ID {opt['id']}: {opt['text']}" for opt in options
    )

    prompt = f"{filled}\n\n---\n\n**ISSUE ACTUAL:**\n{title}\n\n{description}\n\n**OPCIONES DISPONIBLES:**\n{options_text}"

    return prompt


def build_debate_prompt(role_config: dict, context: dict,
                        summary: str, others_positions: str) -> str:
    base = build_prompt(role_config, context)
    options = context.get("issue", {}).get("options", [])

    debate_instruction = f"""

--- DEBATE DEL CONSEJO DE MINISTROS ---

El Presidente ha resumido la situación así:
{summary}

Posiciones de los otros miembros del Consejo:
{others_positions}

Instrucciones para esta ronda de debate:
- Puedes MANTENER tu postura anterior si crees que es la correcta
- Puedes CEDER y aceptar la posición de otro consejero
- Puedes PROPONER UNA ENMIENDA o un punto medio
- Puedes CONTRAARGUMENTAR si consideras que otra posición tiene fallos

Debes responder con tu postura final tras considerar los argumentos de tus colegas.

IMPORTANTE - FORMATO DE RESPUESTA:
Escribe SOLO el ID exacto de la opción dentro de <decision>. Los IDs pueden empezar en 0.
No escribas el texto de la opción ni inventes un índice.
Ejemplo: <decision>{options[0]['id'] if options else '0'}</decision>"""

    return base + debate_instruction


def build_president_summary_prompt(role_config: dict, context: dict,
                                   all_positions: list[dict]) -> str:
    base = build_prompt(role_config, context)

    positions_text = "\n".join(
        f"  - {p['name']} ({p['role']}): Opción {p['vote']}. {p['reasoning'][:300]}"
        for p in all_positions
    )

    summary_instruction = f"""

--- RESUMEN DEL CONSEJO DE MINISTROS (FASE 1) ---

Estas son las posiciones iniciales de todos los miembros (incluida la tuya):

{positions_text}

Como Presidente, debes:
1. Identificar los PUNTOS DE ACUERDO entre los consejeros
2. Identificar los PUNTOS DE CONFLICTO
3. PROPONER un camino de consenso o compromiso
4. Decidir si el debate debe continuar o puede cerrarse

Si crees que se puede alcanzar consenso, indica tu propuesta de compromiso.
Si ves que el debate no llevará a nada, puedes cerrarlo e imponer.

IMPORTANTE - FORMATO DE RESPUESTA:
Escribe SOLO el ID exacto de la opción dentro de <decision>. Los IDs pueden empezar en 0.
No escribas el texto de la opción ni inventes un índice.
Ejemplo: <decision>{context.get('issue', {}).get('options', [{}])[0].get('id', '0')}</decision>
<cerrar_debate>SI</cerrar_debate> o <cerrar_debate>NO</cerrar_debate>"""

    return base + summary_instruction


def build_final_consensus_prompt(role_config: dict, context: dict,
                                 summary: str, final_results: list[dict]) -> str:
    base = build_prompt(role_config, context)
    positions = "\n".join(
        f"- {item['name']} ({item['role']}): ID {item['vote']} | {item['reasoning'][:300]}"
        for item in final_results
    )
    first_id = context.get("issue", {}).get("options", [{}])[0].get("id", "0")
    return f"""{base}

--- SÍNTESIS FINAL DEL CONSEJO ---
Resumen presidencial:
{summary}

Posiciones tras escuchar a todos:
{positions}

Como Presidente, debes cerrar esta sesión con una decisión razonada.
Busca el consenso más sólido, pero prioriza tu personalidad, la visión de Gobierno
y las líneas rojas de los ministros. Explica brevemente por qué adoptas la opción.

FORMATO OBLIGATORIO:
<razonamiento>acuerdo final y justificación</razonamiento>
<decision>{first_id}</decision>"""


def parse_llm_response(response: str) -> dict:
    decision_match = DECISION_RE.search(response)
    reasoning_match = REASONING_RE.search(response)

    decision = decision_match.group(1).strip() if decision_match else None
    reasoning = reasoning_match.group(1).strip() if reasoning_match else response[:500]

    close_match = re.search(r"<cerrar_debate>\s*(SI|NO)\s*</cerrar_debate>", response, re.IGNORECASE)
    close_debate = close_match.group(1).upper() == "SI" if close_match else None

    return {
        "decision": decision,
        "reasoning": reasoning,
        "close_debate": close_debate,
        "raw": response,
    }
