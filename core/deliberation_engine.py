import asyncio
import logging
from collections import Counter
from enum import Enum
from typing import Optional, Callable

import yaml
from openai import OpenAI

from config import settings
from core.memory_store import MemoryStore
from core.vector_store import VectorStore
from core.api_client import NSClient
from core import agent_core
from services.reporter import generate_micro_summary

logger = logging.getLogger(__name__)

ROLES = ["presidente", "economia", "sociales", "justicia"]
ROLES_DEBATE = ["economia", "sociales", "justicia"]

class EngineStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    DELIBERATING = "deliberating"

class DeliberationEngine:
    def __init__(self, memory: MemoryStore, vectors: VectorStore,
                 api: NSClient, on_event: Optional[Callable] = None):
        self.memory = memory
        self.vectors = vectors
        self.api = api
        self.on_event = on_event or (lambda e: None)
        self.status = EngineStatus.IDLE
        self._check_lock = asyncio.Lock()
        self._personalities: dict = {}
        self._llm = OpenAI(
            base_url=f"{settings.LMSTUDIO_URL}/v1",
            api_key="not-needed",
            timeout=30.0,
            max_retries=0,
        )

        with open(settings.PERSONALITIES_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
            self._personalities = data.get("roles", {})

    def _chat(self, prompt: str, temperature: float = 0.7) -> str:
        resp = self._llm.chat.completions.create(
            model=settings.LLM_MODEL or "local-model",
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
            timeout=30,
        )
        return resp.choices[0].message.content or ""

    async def emit(self, event: str, data: dict | None = None, **extra):
        try:
            payload = {"event": event, **(data or {}), **extra}
            coro = self.on_event(payload)
            if coro is not None:
                await coro
        except Exception as e:
            logger.debug(f"Error emitiendo evento {event}: {e}")

    # --- Lifecycle ---

    def pause(self):
        self.status = EngineStatus.PAUSED
        logger.info("Engine PAUSED")

    def resume(self):
        if self.status == EngineStatus.PAUSED:
            self.status = EngineStatus.RUNNING
            logger.info("Engine RESUMED")

    async def main_loop(self):
        self.status = EngineStatus.RUNNING
        logger.info(f"Main loop started (interval: {settings.CHECK_INTERVAL_MINUTES} min)")
        while True:
            try:
                if self.status == EngineStatus.PAUSED:
                    await asyncio.sleep(5)
                    continue

                if self.status == EngineStatus.RUNNING:
                    await self._check_and_process()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error en main loop: {e}", exc_info=True)

            await asyncio.sleep(settings.CHECK_INTERVAL_MINUTES * 60)

    async def _check_and_process(self):
        if self._check_lock.locked():
            logger.info("A check is already running; skipping overlapping request")
            return
        async with self._check_lock:
            await self._check_and_process_unlocked()

    async def _check_and_process_unlocked(self):
        logger.info("Checking for pending issues...")

        try:
            state = await asyncio.to_thread(self.api.get_nation_full)
        except Exception as e:
            logger.warning(f"No se pudo obtener estado de la nación: {e}")
            last = self.memory.get_latest_snapshot()
            state = dict(last) if last else {
                "approval": 50, "economy": "N/A", "deficit": "N/A",
                "population": 0, "civil_rights": "N/A",
                "economy_freedom": "N/A", "political_freedom": "N/A",
                "census": {}
            }
        try:
            self.memory.save_snapshot(state)
            await self.emit("snapshot", state)
        except Exception as e:
            logger.warning(f"Error guardando snapshot: {e}")

        try:
            issues = await asyncio.to_thread(self.api.get_pending_issues)
        except Exception as e:
            logger.warning(f"Error obteniendo issues: {e}")
            issues = []

        if not issues:
            logger.info("No pending issues found")
            try:
                await self.emit("no_issues", {})
            except Exception:
                pass
            return

        logger.info(f"Found {len(issues)} pending issue(s)")
        try:
            await self.emit("issues_found", {"count": len(issues)})
        except Exception:
            pass

        for issue in issues:
            if self.status == EngineStatus.PAUSED:
                break
            try:
                await self._process_issue(issue, state)
            except Exception as e:
                logger.error(f"Error procesando issue {issue.get('id', '?')}: {e}")
                if self.status not in (EngineStatus.PAUSED, EngineStatus.IDLE):
                    self.status = EngineStatus.RUNNING

    async def _process_issue(self, issue: dict, state: dict):
        logger.info(f"Processing issue: {issue['title']}")

        session_id = self.memory.create_session(
            issue_id=issue.get("id", "unknown"),
            title=issue.get("title", "Unknown Issue"),
            description=issue.get("text", ""),
            options=[o["text"] for o in issue.get("options", [])],
            option_ids=[str(o.get("id", "")) for o in issue.get("options", [])],
        )

        try:
            await self.emit("session_started", {
                "session_id": session_id,
                "issue": issue,
            })
        except Exception:
            pass

        self.status = EngineStatus.DELIBERATING
        self.memory.update_session_status(session_id, "analyzing")
        await self.emit("phase_changed", {
            "session_id": session_id, "phase": 1, "status": "analyzing"
        })

        try:
            context = await asyncio.wait_for(
                asyncio.to_thread(self._build_context, issue, state), timeout=20
            )
        except Exception as e:
            logger.warning(f"No se pudo cargar memoria contextual; continuando sin ella: {e}")
            context = self._build_context_without_history(issue, state)

        # Phase 1: Individual Analysis
        phase1_results = await self._phase1_analysis(session_id, issue, context)
        if not phase1_results:
            self.memory.update_session_status(session_id, "error", error="Phase 1 failed")
            self.status = EngineStatus.RUNNING
            return

        # Phase 2: Debate
        self.memory.update_session_status(session_id, "debating")
        await self.emit("phase_changed", {
            "session_id": session_id, "phase": 2, "status": "debating"
        })
        phase2_results, close_debate = await self._phase2_debate(
            session_id, issue, context, phase1_results
        )

        # Phase 3: Voting & Outcome
        self.memory.update_session_status(session_id, "voting")
        await self.emit("phase_changed", {
            "session_id": session_id, "phase": 3, "status": "voting"
        })
        outcome = self._determine_outcome(phase2_results, close_debate)

        # Validate that outcome matches a valid option ID
        valid_ids = [o["id"] for o in issue.get("options", [])]
        if valid_ids:
            clean = (outcome or "").strip()
            if clean not in valid_ids:
                letters = {"A":"1","B":"2","C":"3","D":"4","E":"5","F":"6"}
                converted = letters.get(clean.upper())
                if converted in valid_ids:
                    outcome = converted
                    logger.info(f"Outcome convertido de letra a numero: {outcome}")
                elif clean.isdigit() and str(int(clean) - 1) in valid_ids:
                    outcome = str(int(clean) - 1)
                    logger.info(f"Outcome convertido de índice a ID NationStates: {outcome}")
                elif clean.isdigit() and clean in valid_ids:
                    pass
                else:
                    valid_votes = [
                        str(result.get("vote")) for result in phase2_results
                        if str(result.get("vote")) in valid_ids
                    ]
                    if valid_votes:
                        outcome = Counter(valid_votes).most_common(1)[0][0]
                        logger.warning(f"Outcome invalido '{clean}', usando la mayoría válida: {outcome}")
                    else:
                        self.memory.update_session_status(
                            session_id, "error", error=f"Ningún agente produjo una opción válida: {clean}"
                        )
                        self.status = EngineStatus.RUNNING
                        return

        # Submit to NationStates FIRST, before marking as completed
        submitted = False
        if not self.memory.get_session(session_id).get("is_simulation"):
            submitted = await asyncio.to_thread(self.api.submit_decision, issue["id"], outcome)
            if submitted:
                logger.info(f"Issue {issue['id']} resuelto en NationStates")
            else:
                logger.error(f"FALLO al enviar decision {issue['id']} a NationStates")
                self.memory.update_session_status(
                    session_id, "error", outcome=outcome,
                    error="Fallo al enviar a NationStates"
                )
                self.status = EngineStatus.RUNNING
                await self.emit("session_failed", {
                    "session_id": session_id,
                    "issue_title": issue["title"],
                    "error": "Fallo al enviar a NationStates",
                })
                return

        if submitted or self.memory.get_session(session_id).get("is_simulation"):
            self.memory.update_session_status(
                session_id, "completed", outcome=outcome
            )

        if submitted or self.memory.get_session(session_id).get("is_simulation"):
            all_text = self._build_transcript(session_id)
            try:
                await asyncio.to_thread(
                    self.vectors.add_decision,
                    session_id=session_id,
                    issue_title=issue["title"],
                    issue_description=issue["text"],
                    final_reasoning=self._get_final_reasoning(phase2_results, outcome),
                    vote_choice=outcome,
                    all_interventions_text=all_text,
                    topic_tags="",
                )
            except Exception as e:
                logger.warning(f"No se pudo indexar la decisión {session_id}: {e}")

            summary = generate_micro_summary(self.memory, session_id)
            self.memory.update_session_summary(session_id, summary)

        # Generate/update vision
        await self._maybe_update_vision(session_id, outcome)

        await self.emit("session_completed", {
            "session_id": session_id,
            "outcome": outcome,
            "issue_title": issue["title"],
        })

        self.status = EngineStatus.RUNNING

    # --- Phase 1 ---

    async def _phase1_analysis(self, session_id: int, issue: dict,
                               context: dict) -> list[dict]:
        results = []

        async def analyze(role: str):
            config = self._personalities[role]
            prompt = agent_core.build_prompt(config, context)
            try:
                raw = await asyncio.to_thread(self._chat, prompt, 0.7)
            except Exception as e:
                logger.error(f"LLM error for {role}: {e}")
                return None

            parsed = agent_core.parse_llm_response(raw)
            self.memory.add_intervention(
                session_id=session_id,
                phase=1,
                round_num=1,
                role=role,
                name=config["name"],
                vote=parsed["decision"],
                reasoning=parsed["reasoning"],
                content=raw,
            )

            await self.emit("intervention", {
                "session_id": session_id,
                "phase": 1,
                "role": role,
                "name": config["name"],
                "vote": parsed["decision"],
                "reasoning": parsed["reasoning"],
            })

            return {
                "role": role,
                "name": config["name"],
                "vote": parsed["decision"],
                "reasoning": parsed["reasoning"],
                "raw": raw,
            }

        tasks = [analyze(r) for r in ROLES]
        completed = await asyncio.gather(*tasks)

        for r in completed:
            if r is not None:
                results.append(r)

        return results if len(results) == 4 else []

    # --- Phase 2 ---

    async def _phase2_debate(self, session_id: int, issue: dict,
                              context: dict, phase1_results: list[dict]):
        pres_config = self._personalities["presidente"]

        # President summarizes
        summary_prompt = agent_core.build_president_summary_prompt(
            pres_config, context, phase1_results
        )
        try:
            pres_raw = await asyncio.to_thread(self._chat, summary_prompt, 0.6)
        except Exception as e:
            logger.error(f"LLM error for president summary: {e}")
            president = next(r for r in phase1_results if r["role"] == "presidente")
            pres_raw = (
                f"<razonamiento>{president['reasoning']}</razonamiento>"
                f"<decision>{president['vote']}</decision>"
            )

        pres_parsed = agent_core.parse_llm_response(pres_raw)
        close_debate = pres_parsed.get("close_debate", False)
        if close_debate is None:
            close_debate = False

        self.memory.add_intervention(
            session_id=session_id,
            phase=2,
            round_num=1,
            role="presidente",
            name=pres_config["name"],
            vote=pres_parsed["decision"],
            reasoning=pres_parsed["reasoning"],
            content=pres_raw,
        )

        await self.emit("intervention", {
            "session_id": session_id,
            "phase": 2,
            "role": "presidente",
            "name": pres_config["name"],
            "vote": pres_parsed["decision"],
            "reasoning": pres_parsed["reasoning"],
            "close_debate": close_debate,
        })

        # Update president's position
        new_results = []
        for r in phase1_results:
            if r["role"] == "presidente":
                new_results.append({
                    **r,
                    "vote": pres_parsed["decision"],
                    "reasoning": pres_parsed["reasoning"],
                })
            else:
                new_results.append(r)

        # Councilors respond
        summary_text = pres_parsed["reasoning"]
        others_positions = self._format_others_positions(new_results)

        async def respond(role: str):
            config = self._personalities[role]
            prompt = agent_core.build_debate_prompt(
                config, context, summary_text, others_positions
            )
            try:
                raw = await asyncio.to_thread(self._chat, prompt, 0.7)
            except Exception as e:
                logger.error(f"LLM error for debate {role}: {e}")
                return None

            parsed = agent_core.parse_llm_response(raw)
            self.memory.add_intervention(
                session_id=session_id,
                phase=2,
                round_num=2,
                role=role,
                name=config["name"],
                vote=parsed["decision"],
                reasoning=parsed["reasoning"],
                content=raw,
            )

            await self.emit("intervention", {
                "session_id": session_id,
                "phase": 2,
                "role": role,
                "name": config["name"],
                "vote": parsed["decision"],
                "reasoning": parsed["reasoning"],
            })

            return role, parsed["decision"], parsed["reasoning"], raw

        debate_tasks = [respond(r) for r in ROLES_DEBATE]
        debate_results = await asyncio.gather(*debate_tasks)

        final_results = []
        for r in new_results:
            if r["role"] == "presidente":
                final_results.append(r)
            else:
                for dr in debate_results:
                    if dr and dr[0] == r["role"]:
                        final_results.append({
                            "role": dr[0],
                            "name": r["name"],
                            "vote": dr[1],
                            "reasoning": dr[2],
                            "raw": dr[3],
                        })
                        break
                else:
                    final_results.append(r)

        # A final presidential synthesis makes the debate produce an explicit
        # agreement instead of ending with an opaque weighted vote.
        final_prompt = agent_core.build_final_consensus_prompt(
            pres_config, context, summary_text, final_results
        )
        try:
            final_raw = await asyncio.to_thread(self._chat, final_prompt, 0.5)
            final_parsed = agent_core.parse_llm_response(final_raw)
            if final_parsed.get("decision"):
                self.memory.add_intervention(
                    session_id=session_id,
                    phase=2,
                    round_num=3,
                    role="presidente",
                    name=pres_config["name"],
                    vote=final_parsed["decision"],
                    reasoning=final_parsed["reasoning"],
                    content=final_raw,
                )
                await self.emit("intervention", {
                    "session_id": session_id,
                    "phase": 2,
                    "role": "presidente",
                    "name": pres_config["name"],
                    "vote": final_parsed["decision"],
                    "reasoning": final_parsed["reasoning"],
                    "consensus": True,
                })
                for result in final_results:
                    if result["role"] == "presidente":
                        result["vote"] = final_parsed["decision"]
                        result["reasoning"] = final_parsed["reasoning"]
                        break
                close_debate = True
        except Exception as e:
            logger.error(f"LLM error en síntesis final: {e}")

        return final_results, close_debate

    # --- Outcome ---

    def _determine_outcome(self, results: list[dict], close_debate: bool) -> str:
        votes = {}
        for r in results:
            v = r["vote"]
            votes[v] = votes.get(v, 0) + 1

        sorted_votes = sorted(votes.items(), key=lambda x: -x[1])

        if close_debate:
            pres_result = next(r for r in results if r["role"] == "presidente")
            return pres_result["vote"]

        if sorted_votes:
            top_score = sorted_votes[0][1]
            tied = [vote for vote, score in sorted_votes if score == top_score]
            if len(tied) == 1:
                return tied[0]
            president = next((r["vote"] for r in results if r["role"] == "presidente"), None)
            if president in tied:
                return president
            return tied[0]

        return results[0]["vote"] if results else "A"

    # --- Helpers ---

    def _build_context(self, issue: dict, state: dict) -> dict:
        try:
            relevant = self.vectors.search_similar(
                f"{issue.get('title', '')} {issue.get('text', '')}", n=5
            )
        except Exception as e:
            logger.warning(f"Memoria semántica no disponible: {e}")
            relevant = []
        if not relevant:
            relevant = [
                {
                    "metadata": {
                        "date": item.get("ended_at", "?"),
                        "issue_title": item.get("issue_title", "?"),
                        "vote": item.get("outcome", "?"),
                        "reasoning_preview": item.get("summary", ""),
                    }
                }
                for item in self.memory.get_recent_completed_sessions(5)
            ]
        history_lines = []
        for r in relevant:
            m = r["metadata"]
            history_lines.append(
                f"- [{m.get('date', '?')}] {m.get('issue_title', '?')} → "
                f"Se eligió opción {m.get('vote', '?')}. "
                f"{m.get('reasoning_preview', '')[:200]}"
            )

        active_vision = self.memory.get_active_vision()
        vision = (active_vision or {}).get("content") or self.vectors.get_vision() or ""

        return {
            "issue": issue,
            "approval": state.get("approval", 50),
            "economy": state.get("economy", "N/A"),
            "deficit": state.get("deficit", "N/A"),
            "population": state.get("population", 0),
            "civil_rights": state.get("civil_rights", "N/A"),
            "economy_freedom": state.get("economy_freedom", "N/A"),
            "political_freedom": state.get("political_freedom", "N/A"),
            "vision": vision,
            "relevant_history": "\n".join(history_lines) if history_lines else "",
        }

    @staticmethod
    def _build_context_without_history(issue: dict, state: dict) -> dict:
        return {
            "issue": issue,
            "approval": state.get("approval", 50),
            "economy": state.get("economy", "N/A"),
            "deficit": state.get("deficit", "N/A"),
            "population": state.get("population", 0),
            "civil_rights": state.get("civil_rights", "N/A"),
            "economy_freedom": state.get("economy_freedom", "N/A"),
            "political_freedom": state.get("political_freedom", "N/A"),
            "vision": "",
            "relevant_history": "",
        }

    @staticmethod
    def _format_others_positions(results: list[dict]) -> str:
        lines = []
        for r in results:
            if r["role"] == "presidente":
                lines.append(f"  Presidente ({r['name']}): Opción {r['vote']}")
            lines.append(f"  {r['name']}: Opción {r['vote']}. {r['reasoning'][:200]}")
        return "\n".join(lines)

    def _build_transcript(self, session_id: int) -> str:
        interventions = self.memory.get_session_interventions(session_id)
        lines = []
        for i, inv in enumerate(interventions, 1):
            lines.append(
                f"[{inv['phase']}.{inv['round']}] {inv['councilor_name']} "
                f"({inv['councilor_role']}): "
                f"Voto={inv['vote_choice'] or 'N/A'} | "
                f"{inv['reasoning'] or inv['content'][:200]}"
            )
        return "\n".join(lines)

    def _get_final_reasoning(self, results: list[dict], outcome: str) -> str:
        for r in results:
            if r["vote"] == outcome:
                return r["reasoning"]
        return ""

    async def _maybe_update_vision(self, session_id: int, outcome: str):
        # SQLite is the source of truth; failed or legacy vector entries must not
        # advance the government's decision count.
        count = self.memory.get_stats()["completed"]
        if count % 5 == 0:
            pres_config = self._personalities["presidente"]
            state = self.memory.get_latest_snapshot() or {}

            vision_context = self._build_context({}, state)
            prompt = f"""
Eres el Presidente del Gobierno de España.

Han pasado {count} decisiones desde el inicio de tu legislatura.

Basándote en tu ideología ({pres_config['ideology']}), las decisiones tomadas hasta ahora,
y el estado actual de la nación, redacta una VISIÓN DE FUTURO actualizada para el Gobierno.

Esta visión debe:
- Tener entre 2 y 4 párrafos
- Reflejar la dirección que quieres para la nación a largo plazo
- Ser coherente con las decisiones tomadas hasta ahora
- Servir como guía para futuras decisiones del Consejo de Ministros

Estado actual:
- Aprobación: {state.get('approval', 'N/A')}
- Economía: {state.get('economy', 'N/A')}

Responde SOLO con el texto de la visión, sin etiquetas adicionales.
"""
            try:
                vision_text = await asyncio.to_thread(self._chat, prompt, 0.8)
                vision_text = vision_text.strip()
                self.vectors.store_vision(vision_text, session_id)
                self.memory.save_vision(vision_text, summary=vision_text[:100],
                                         author_role="presidente", session_id=session_id)
                logger.info("Visión de futuro actualizada")
            except Exception as e:
                logger.error(f"Error updating vision: {e}")

    @property
    def personalities(self) -> dict:
        return self._personalities
