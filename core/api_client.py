import logging
from typing import Optional
from xml.etree import ElementTree as ET

import sans

logger = logging.getLogger(__name__)


def _safe_xml(resp):
    try:
        return resp.xml
    except Exception as e:
        logger.warning(f"Error parsing XML response: {e}")
        return ET.fromstring("<error/>")


class NSClient:
    def __init__(self, nation_name: str, password: str, contact_email: str):
        self.nation_name = nation_name
        self.password = password
        self.contact_email = contact_email
        sans.set_agent(nation_name)
        self._auth: Optional[sans.NSAuth] = None

    @property
    def auth(self) -> sans.NSAuth:
        if self._auth is None:
            self._auth = sans.NSAuth(password=self.password)
            try:
                sans.get(
                    sans.Nation(self.nation_name, "ping"),
                    auth=self._auth,
                )
                logger.info("Autenticacion NationStates establecida")
            except Exception as e:
                logger.warning(f"No se pudo autenticar en NationStates: {e}")
        return self._auth

    def get_nation_full(self) -> dict:
        try:
            req = sans.Nation(
                self.nation_name,
                "fullname population industry economy freedoms census",
                mode="score",
                scale="65 66 67",
            )
            root = _safe_xml(sans.get(req, auth=self.auth))
            return self._parse_nation_xml(root)
        except Exception as e:
            logger.warning(f"No se pudo obtener estado de la naci�n: {e}")
            return {
                "approval": 50.0, "population": 0, "economy": "N/A",
                "deficit": "N/A", "civil_rights": "N/A",
                "economy_freedom": "N/A", "political_freedom": "N/A",
                "census": {},
            }

    def get_pending_issues(self) -> list[dict]:
        try:
            req = sans.Nation(self.nation_name, "issues")
            root = _safe_xml(sans.get(req, auth=self.auth))
            issues = []
            nation_elem = root.find(".//NATION")
            source = nation_elem if nation_elem is not None else root
            for issue_elem in source.findall(".//ISSUE"):
                issues.append({
                    "id": issue_elem.get("id"),
                    "title": (issue_elem.find("TITLE").text or "").strip(),
                    "text": (issue_elem.find("TEXT").text or "").strip(),
                    "author": (issue_elem.find("AUTHOR").text or "").strip(),
                    "options": [
                        {"id": opt.get("id"), "text": (opt.text or "").strip()}
                        for opt in issue_elem.findall("OPTION")
                    ],
                })
            return issues
        except Exception as e:
            logger.warning(f"No se pudieron obtener issues pendientes: {e}")
            return []

    def submit_decision(self, issue_id: str, option_id: str) -> bool:
        try:
            req = sans.Nation(
                self.nation_name,
                c="issue", issue=issue_id, option=option_id,
            )
            root = _safe_xml(sans.get(req, auth=self.auth))
            ok = root.find(".//OK")
            success = root.find(".//SUCCESS")
            error = root.find(".//ERROR")
            if error is not None:
                logger.error(f"NationStates rechazó la decisión {issue_id}: {error.text or ''}")
                return False
            if (ok is not None and (ok.text or "").strip() == "1") or success is not None:
                logger.info(f"Issue {issue_id} resuelto con opcion {option_id}")
                return True
            debug = ET.tostring(root, encoding="unicode")[:500]
            logger.warning(f"Respuesta de NationStates sin confirmación: {debug}")
            return False
        except Exception as e:
            logger.error(f"Error al enviar decision: {e}")
            return False

    def _parse_nation_xml(self, root) -> dict:
        if root.tag == "error":
            return {"approval": 50.0, "population": 0}

        ns = root if root.tag == "NATION" else root.find(".//NATION")
        if ns is None:
            ns = root

        def txt(tag):
            el = ns.find(tag)
            return el.text.strip() if el is not None and el.text else ""

        result = {
            "name": txt("NAME") or self.nation_name,
            "fullname": txt("FULLNAME"),
            "population": self._int(txt("POPULATION")),
            "industry": txt("INDUSTRY"),
            "economy": txt("ECONOMY"),
            "deficit": txt("DEFICIT") or txt("TAX"),
        }

        freedoms = ns.find("FREEDOMS")
        if freedoms is not None:
            def freedom_txt(tag):
                el = freedoms.find(tag)
                return el.text.strip() if el is not None and el.text else ""

            result["civil_rights"] = freedom_txt("CIVILRIGHTS")
            result["economy_freedom"] = freedom_txt("ECONOMY")
            result["political_freedom"] = freedom_txt("POLITICALFREEDOM")

        census = ns.find("CENSUS")
        if census is not None:
            census_data = {}
            for scale in census.findall("SCALE"):
                sid = scale.get("id")
                score = scale.find("SCORE")
                rank = scale.find("RANK")
                if sid:
                    census_data[sid] = {
                        "score": float(score.text) if score is not None and score.text else 0,
                        "rank": int(rank.text) if rank is not None and rank.text else 0,
                    }
            result["census"] = census_data
            if "65" in census_data:
                result["approval"] = census_data["65"]["score"]
            if "66" in census_data:
                result["economy_score"] = census_data["66"]["score"]
            if "67" in census_data:
                result["freedom_score"] = census_data["67"]["score"]

        if "approval" not in result:
            result["approval"] = 50.0

        return result

    @staticmethod
    def _int(val: str) -> int:
        try:
            return int(val.replace(",", ""))
        except (ValueError, AttributeError):
            return 0
