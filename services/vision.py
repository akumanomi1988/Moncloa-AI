import logging

from openai import OpenAI

from config import settings
from core.vector_store import VectorStore

logger = logging.getLogger(__name__)


def generate_initial_vision(vectors: VectorStore, ideology: str = "") -> str:
    try:
        llm = OpenAI(
            base_url=f"{settings.LMSTUDIO_URL}/v1",
            api_key="not-needed",
            timeout=30,
            max_retries=0,
        )
        prompt = f"""
Genera una VISIÓN DE FUTURO para un gobierno de ideología {ideology or 'moderada'}.

Esta visión debe:
- Tener entre 3 y 5 párrafos
- Reflejar una dirección política clara y coherente
- Abordar: economía, bienestar social, instituciones y sostenibilidad
- Sonar como un discurso de investidura realista
- Estar escrita en español

Responde SOLO con el texto de la visión, sin introducciones ni comentarios.
"""
        resp = llm.chat.completions.create(
            model=settings.LLM_MODEL or "local-model",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.8,
        )
        vision = resp.choices[0].message.content.strip()
        vectors.store_vision(vision)
        logger.info("Visión inicial generada correctamente")
        return vision
    except Exception as e:
        logger.error(f"Error generando visión inicial: {e}")
        default = (
            "Construir una nación próspera, justa y sostenible, "
            "donde el crecimiento económico vaya de la mano de la protección social, "
            "la calidad institucional y el respeto al medio ambiente. "
            "Trabajar por una sociedad cohesionada, con igualdad de oportunidades "
            "y plena integración en la comunidad internacional."
        )
        vectors.store_vision(default)
        return default
