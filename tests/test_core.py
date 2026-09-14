import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

from core.api_client import NSClient
from core.memory_store import MemoryStore
from services.reporter import generate_micro_summary
from core.deliberation_engine import DeliberationEngine


class FakeResponse:
    def __init__(self, xml: str):
        self.xml = ET.fromstring(xml)


class CoreRegressionTests(unittest.TestCase):
    def test_consensus_pipeline_calls_all_roles_and_final_synthesis(self):
        class FakeMemory:
            def __init__(self):
                self.interventions = []

            def add_intervention(self, **data):
                self.interventions.append(data)

        class FakeVectors:
            def search_similar(self, *args, **kwargs):
                return []

        class FakeApi:
            pass

        with patch("core.deliberation_engine.OpenAI"), patch("core.deliberation_engine.yaml.safe_load") as load:
            load.return_value = {"roles": {
                role: {"name": role, "system_prompt": "role {ideology}", "priorities": []}
                for role in ("presidente", "economia", "sociales", "justicia")
            }}
            memory = FakeMemory()
            engine = DeliberationEngine(memory, FakeVectors(), FakeApi())
            responses = [
                "<razonamiento>Presidente inicial</razonamiento><decision>0</decision>",
                "<razonamiento>Economía inicial</razonamiento><decision>1</decision>",
                "<razonamiento>Sociales inicial</razonamiento><decision>0</decision>",
                "<razonamiento>Justicia inicial</razonamiento><decision>1</decision>",
                "<razonamiento>Resumen con conflicto</razonamiento><decision>0</decision><cerrar_debate>NO</cerrar_debate>",
                "<razonamiento>Economía debate</razonamiento><decision>1</decision>",
                "<razonamiento>Sociales debate</razonamiento><decision>0</decision>",
                "<razonamiento>Justicia debate</razonamiento><decision>0</decision>",
                "<razonamiento>Consenso final</razonamiento><decision>0</decision>",
            ]
            with patch.object(engine, "_chat", side_effect=responses):
                import asyncio
                issue = {"title": "Test", "text": "Dilema", "options": [
                    {"id": "0", "text": "A"}, {"id": "1", "text": "B"}
                ]}
                context = {"issue": issue, "relevant_history": "", "vision": ""}
                phase1 = asyncio.run(engine._phase1_analysis(1, issue, context))
                phase2, closed = asyncio.run(engine._phase2_debate(1, issue, context, phase1))
            self.assertEqual({item["role"] for item in phase1}, {"presidente", "economia", "sociales", "justicia"})
            self.assertTrue(closed)
            self.assertEqual(9, len(memory.interventions))

    def test_nationstates_ok_response_is_success(self):
        with patch("core.api_client.sans.set_agent"):
            client = NSClient("test_nation", "", "test@example.com")
        xml = "<NATION><ISSUE id='123' choice='1'><OK>1</OK></ISSUE></NATION>"
        with patch("core.api_client.sans.get", return_value=FakeResponse(xml)):
            self.assertTrue(client.submit_decision("123", "1"))

    def test_freedoms_are_read_from_freedoms_node(self):
        with patch("core.api_client.sans.set_agent"):
            client = NSClient("test_nation", "", "test@example.com")
        root = ET.fromstring(
            "<NATION><NAME>test</NAME><FREEDOMS>"
            "<CIVILRIGHTS>Good</CIVILRIGHTS><ECONOMY>Strong</ECONOMY>"
            "<POLITICALFREEDOM>Average</POLITICALFREEDOM></FREEDOMS></NATION>"
        )
        parsed = client._parse_nation_xml(root)
        self.assertEqual(parsed["civil_rights"], "Good")
        self.assertEqual(parsed["economy_freedom"], "Strong")
        self.assertEqual(parsed["political_freedom"], "Average")

    def test_summary_uses_nationstates_option_id(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = MemoryStore(Path(directory) / "test.db")
            session_id = memory.create_session(
                "issue-1", "Test issue", "Description", ["No", "Sí"],
                option_ids=["0", "1"],
            )
            memory.update_session_status(session_id, "completed", outcome="0")
            summary = generate_micro_summary(memory, session_id)
            self.assertIn("se elige No", summary)
            memory.close()


if __name__ == "__main__":
    unittest.main()
