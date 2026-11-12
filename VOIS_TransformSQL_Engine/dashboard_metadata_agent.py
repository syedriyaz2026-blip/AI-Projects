#!/usr/bin/env python3
"""
Dashboard Metadata Agent

Authoritative-first extraction for Qlik Sense, Power BI, and SAP Analytics Cloud.
The dashboard URL selects a connector. Platform APIs/exports provide facts; Vertex AI
only normalizes descriptions and never invents dimensions, measures, or objects.

Run:
  pip install fastapi uvicorn httpx websockets pydantic google-genai openpyxl
  gcloud auth application-default login
  uvicorn dashboard_metadata_agent:app --host 0.0.0.0 --port 8080

POST /analyze
{
  "dashboard_url": "...",
  "output_format": "xlsx",
  "artifact_path": null
}

Important:
- A URL alone is not sufficient unless the runtime identity can read the dashboard.
- For Power BI, exact visual bindings require a PBIP/report.json artifact or an
  approved embedded-session capture. REST alone reliably provides report/pages and
  semantic-model metadata, not every visual binding.
- For SAC, exact page/widget bindings require an exported story artifact/manifest or
  a tenant API that exposes them. Widget API expects a widget ID; it is not a story
  crawler.
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import os
import re
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

try:
    from google import genai
    from google.genai import types as genai_types
except Exception:  # Vertex AI normalization is optional
    genai = None
    genai_types = None

APP_VERSION = "1.0.0"
OUT_DIR = Path(os.getenv("OUTPUT_DIR", "/tmp/dashboard-metadata"))
OUT_DIR.mkdir(parents=True, exist_ok=True)

Platform = Literal["powerbi", "qlik", "sac"]
EvidenceLevel = Literal["authoritative", "derived", "unverified"]


@dataclass
class FieldBinding:
    role: str
    kind: str  # dimension | measure | filter | unknown
    name: str
    expression: Optional[str] = None
    table: Optional[str] = None
    source_id: Optional[str] = None
    evidence_level: EvidenceLevel = "authoritative"


@dataclass
class DashboardObject:
    object_id: str
    object_type: str
    title: Optional[str]
    dimensions: list[FieldBinding] = field(default_factory=list)
    measures: list[FieldBinding] = field(default_factory=list)
    filters: list[FieldBinding] = field(default_factory=list)
    raw_path: Optional[str] = None
    evidence_level: EvidenceLevel = "authoritative"
    warnings: list[str] = field(default_factory=list)


@dataclass
class Sheet:
    sheet_id: str
    name: str
    ordinal: int
    objects: list[DashboardObject] = field(default_factory=list)
    filters: list[FieldBinding] = field(default_factory=list)
    evidence_level: EvidenceLevel = "authoritative"
    warnings: list[str] = field(default_factory=list)


@dataclass
class DashboardInventory:
    run_id: str
    platform: Platform
    dashboard_url: str
    dashboard_id: Optional[str]
    dashboard_name: Optional[str]
    extracted_at_epoch: int
    connector: str
    sheets: list[Sheet]
    global_dimensions: list[FieldBinding] = field(default_factory=list)
    global_measures: list[FieldBinding] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    completeness: dict[str, Any] = field(default_factory=dict)
    sha256: Optional[str] = None


class AnalyzeRequest(BaseModel):
    dashboard_url: str = Field(min_length=8)
    output_format: Literal["json", "csv", "xlsx"] = "xlsx"
    artifact_path: Optional[str] = None
    use_vertex_normalization: bool = True


class AnalyzeResponse(BaseModel):
    run_id: str
    platform: Platform
    output_file: str
    sheets: int
    objects: int
    warnings: list[str]
    completeness: dict[str, Any]


def detect_platform(url: str) -> Platform:
    host = urlparse(url).netloc.lower()
    text = url.lower()
    if "powerbi.com" in host or "app.powerbi" in text:
        return "powerbi"
    if "qlik" in host or "/sense/app/" in text or "/app/" in text and "sheet/" in text:
        return "qlik"
    if "sapanalytics.cloud" in host or "/sap/fpa/" in text or "/s2/" in text:
        return "sac"
    raise ValueError("Unsupported dashboard URL. Expected Power BI, Qlik Sense, or SAP Analytics Cloud.")


def stable_run_id(url: str) -> str:
    return hashlib.sha256(f"{url}|{time.time_ns()}".encode()).hexdigest()[:16]


def env_required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def deep_walk(value: Any, path: str = "$"):
    yield path, value
    if isinstance(value, dict):
        for k, v in value.items():
            yield from deep_walk(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from deep_walk(v, f"{path}[{i}]")


def first_string(d: dict[str, Any], keys: tuple[str, ...]) -> Optional[str]:
    for key in keys:
        value = d.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def dedupe_bindings(items: list[FieldBinding]) -> list[FieldBinding]:
    seen, result = set(), []
    for x in items:
        key = (x.kind, x.name, x.expression, x.table, x.role)
        if key not in seen:
            seen.add(key)
            result.append(x)
    return result


class Connector(ABC):
    @abstractmethod
    async def extract(self, url: str, artifact_path: Optional[str]) -> DashboardInventory:
        raise NotImplementedError


class PowerBIConnector(Connector):
    """Uses REST for inventory plus PBIP/report.json for exact visual bindings."""

    def __init__(self):
        self.base = "https://api.powerbi.com/v1.0/myorg"

    @staticmethod
    def ids_from_url(url: str) -> tuple[Optional[str], Optional[str]]:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        group_id = (qs.get("groupId") or qs.get("workspaceId") or [None])[0]
        report_id = (qs.get("reportId") or [None])[0]
        if not report_id:
            m = re.search(r"/reports/([0-9a-fA-F-]{20,})", parsed.path)
            report_id = m.group(1) if m else None
        if not group_id:
            m = re.search(r"/groups/([0-9a-fA-F-]{20,})", parsed.path)
            group_id = m.group(1) if m else None
        return group_id, report_id

    async def _token(self) -> str:
        direct = os.getenv("POWERBI_ACCESS_TOKEN")
        if direct:
            return direct
        tenant, client, secret = env_required("POWERBI_TENANT_ID"), env_required("POWERBI_CLIENT_ID"), env_required("POWERBI_CLIENT_SECRET")
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
                data={"grant_type": "client_credentials", "client_id": client, "client_secret": secret,
                      "scope": "https://analysis.windows.net/powerbi/api/.default"},
            )
            r.raise_for_status()
            return r.json()["access_token"]

    async def extract(self, url: str, artifact_path: Optional[str]) -> DashboardInventory:
        run_id = stable_run_id(url)
        group_id, report_id = self.ids_from_url(url)
        warnings: list[str] = []
        report_name = None
        pages: list[Sheet] = []
        if report_id:
            token = await self._token()
            headers = {"Authorization": f"Bearer {token}"}
            prefix = f"{self.base}/groups/{group_id}" if group_id else self.base
            async with httpx.AsyncClient(timeout=60, headers=headers) as c:
                rr = await c.get(f"{prefix}/reports/{report_id}")
                rr.raise_for_status()
                report_name = rr.json().get("name")
                pr = await c.get(f"{prefix}/reports/{report_id}/pages")
                pr.raise_for_status()
                for i, p in enumerate(pr.json().get("value", [])):
                    pages.append(Sheet(sheet_id=p.get("name", f"page_{i}"), name=p.get("displayName", p.get("name", f"Page {i+1}")), ordinal=i))
        else:
            warnings.append("Report ID was not found in the URL; provide a standard report URL or PBIP artifact.")

        artifact = Path(artifact_path).expanduser() if artifact_path else None
        if artifact and artifact.exists():
            parsed_pages, artifact_name = self._parse_pbip(artifact)
            pages = parsed_pages or pages
            report_name = report_name or artifact_name
        else:
            warnings.append("Exact Power BI visual dimensions/measures require a PBIP/report.json artifact; REST pages alone do not expose every visual binding.")

        object_count = sum(len(s.objects) for s in pages)
        complete = bool(artifact and artifact.exists() and object_count > 0)
        return DashboardInventory(run_id, "powerbi", url, report_id, report_name, int(time.time()),
                                  "PowerBIConnector", pages, warnings=warnings,
                                  completeness={"pages": bool(pages), "objects": complete, "bindings": complete,
                                                "authoritative": complete})

    def _parse_pbip(self, artifact: Path) -> tuple[list[Sheet], Optional[str]]:
        candidates: list[Path] = []
        if artifact.is_file():
            candidates = [artifact]
        else:
            candidates = list(artifact.rglob("report.json")) + list(artifact.rglob("*.json"))
        docs = []
        for p in candidates:
            try:
                data = json.loads(p.read_text(encoding="utf-8-sig"))
                if isinstance(data, dict):
                    docs.append((p, data))
            except Exception:
                continue
        sheets: list[Sheet] = []
        for p, doc in docs:
            sections = doc.get("sections") or doc.get("pages")
            if not isinstance(sections, list):
                continue
            for idx, sec in enumerate(sections):
                if not isinstance(sec, dict):
                    continue
                sheet = Sheet(str(sec.get("name", f"page_{idx}")), str(sec.get("displayName") or sec.get("name") or f"Page {idx+1}"), idx)
                visuals = sec.get("visualContainers") or sec.get("visuals") or []
                for vi, visual in enumerate(visuals if isinstance(visuals, list) else []):
                    obj = self._powerbi_visual(visual, vi, str(p))
                    sheet.objects.append(obj)
                sheets.append(sheet)
            if sheets:
                return sheets, artifact.parent.name
        return [], artifact.stem

    def _powerbi_visual(self, visual: dict[str, Any], idx: int, source: str) -> DashboardObject:
        blobs: list[Any] = [visual]
        for key in ("config", "query", "dataTransforms", "filters"):
            val = visual.get(key)
            if isinstance(val, str):
                try: blobs.append(json.loads(val))
                except Exception: pass
            elif val is not None:
                blobs.append(val)
        object_id = str(visual.get("name") or visual.get("id") or f"visual_{idx}")
        object_type, title = "unknown", None
        dims: list[FieldBinding] = []
        measures: list[FieldBinding] = []
        filters: list[FieldBinding] = []
        for blob in blobs:
            for path, node in deep_walk(blob):
                if isinstance(node, dict):
                    object_type = first_string(node, ("visualType", "type")) or object_type
                    title = title or first_string(node, ("title", "displayName"))
                    kind, name, expr, table = self._binding_from_node(node, path)
                    if name:
                        b = FieldBinding(role=path.rsplit(".", 1)[-1], kind=kind, name=name, expression=expr, table=table)
                        if kind == "measure": measures.append(b)
                        elif kind == "filter": filters.append(b)
                        elif kind == "dimension": dims.append(b)
        return DashboardObject(object_id, object_type, title, dedupe_bindings(dims), dedupe_bindings(measures),
                               dedupe_bindings(filters), source)

    @staticmethod
    def _binding_from_node(node: dict[str, Any], path: str) -> tuple[str, Optional[str], Optional[str], Optional[str]]:
        lower = path.lower()
        if "measure" in node and isinstance(node["measure"], dict):
            m = node["measure"]
            return "measure", first_string(m, ("property", "name")), json.dumps(m, separators=(",", ":")), first_string(m.get("expression", {}) if isinstance(m.get("expression"), dict) else {}, ("source",))
        if "column" in node and isinstance(node["column"], dict):
            c = node["column"]
            return ("filter" if "filter" in lower else "dimension"), first_string(c, ("property", "name")), json.dumps(c, separators=(",", ":")), None
        if "aggregation" in node and isinstance(node["aggregation"], dict):
            return "measure", first_string(node, ("name", "queryRef")), json.dumps(node, separators=(",", ":")), None
        if any(k in node for k in ("queryRef", "queryName", "field")):
            name = first_string(node, ("queryRef", "queryName", "field", "name"))
            kind = "filter" if "filter" in lower else ("measure" if any(x in lower for x in ("measure", "value", "y")) else "dimension")
            return kind, name, first_string(node, ("expression",)), first_string(node, ("table", "entity"))
        return "unknown", None, None, None


class QlikConnector(Connector):
    """Qlik Engine JSON-RPC over WebSocket. Requires an Engine websocket URL."""

    @staticmethod
    def app_sheet_from_url(url: str) -> tuple[Optional[str], Optional[str]]:
        path = unquote(urlparse(url).path)
        app = re.search(r"/app/([^/]+)", path)
        sheet = re.search(r"/sheet/([^/]+)", path)
        return (app.group(1) if app else None, sheet.group(1) if sheet else None)

    async def extract(self, url: str, artifact_path: Optional[str]) -> DashboardInventory:
        try:
            import websockets
        except ImportError as e:
            raise RuntimeError("Install websockets for Qlik extraction") from e
        run_id = stable_run_id(url)
        app_id, _ = self.app_sheet_from_url(url)
        app_id = app_id or os.getenv("QLIK_APP_ID")
        if not app_id:
            raise RuntimeError("Could not determine Qlik app ID; set QLIK_APP_ID.")
        ws_url = env_required("QLIK_ENGINE_WS_URL")
        if "{app_id}" in ws_url:
            ws_url = ws_url.format(app_id=app_id)
        headers = {}
        if os.getenv("QLIK_BEARER_TOKEN"):
            headers["Authorization"] = f"Bearer {os.environ['QLIK_BEARER_TOKEN']}"
        ssl_ctx = None
        cert = os.getenv("QLIK_CLIENT_CERT")
        key = os.getenv("QLIK_CLIENT_KEY")
        if cert and key:
            import ssl
            ssl_ctx = ssl.create_default_context(cafile=os.getenv("QLIK_CA_CERT"))
            ssl_ctx.load_cert_chain(cert, key)
        request_id = 0
        async with websockets.connect(ws_url, additional_headers=headers or None, ssl=ssl_ctx, max_size=50_000_000) as ws:
            async def rpc(handle: int, method: str, params: list[Any]) -> dict[str, Any]:
                nonlocal request_id
                request_id += 1
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": request_id, "handle": handle, "method": method, "params": params}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == request_id:
                        if "error" in msg: raise RuntimeError(f"Qlik {method}: {msg['error']}")
                        return msg.get("result", {})
            opened = await rpc(-1, "OpenDoc", [app_id, "", "", "", False])
            app_handle = opened["qReturn"]["qHandle"]
            props = {"qInfo": {"qType": "sheet-list"}, "qAppObjectListDef": {"qType": "sheet", "qData": {"title": "/qMetaDef/title", "rank": "/rank"}}}
            session = await rpc(app_handle, "CreateSessionObject", [props])
            layout = await rpc(session["qReturn"]["qHandle"], "GetLayout", [])
            items = layout.get("qLayout", {}).get("qAppObjectList", {}).get("qItems", [])
            sheets: list[Sheet] = []
            for si, item in enumerate(items):
                sid = item.get("qInfo", {}).get("qId")
                sname = item.get("qData", {}).get("title") or sid
                sheet = Sheet(sid, sname, si)
                sh = await rpc(app_handle, "GetObject", [sid])
                child = await rpc(sh["qReturn"]["qHandle"], "GetChildInfos", [])
                for ci, info in enumerate(child.get("qInfos", [])):
                    oid = info.get("qId")
                    oh = await rpc(app_handle, "GetObject", [oid])
                    handle = oh["qReturn"]["qHandle"]
                    op = await rpc(handle, "GetProperties", [])
                    ol = await rpc(handle, "GetLayout", [])
                    props_data = op.get("qProp", {})
                    layout_data = ol.get("qLayout", {})
                    cube_def = props_data.get("qHyperCubeDef", {})
                    cube = layout_data.get("qHyperCube", {})
                    dims, measures = self._qlik_bindings(cube_def, cube)
                    sheet.objects.append(DashboardObject(oid, props_data.get("qInfo", {}).get("qType", info.get("qType", "unknown")),
                                                         layout_data.get("title") or props_data.get("title"), dims, measures,
                                                         raw_path=f"qlik://{app_id}/{sid}/{oid}"))
                sheets.append(sheet)
        count = sum(len(s.objects) for s in sheets)
        return DashboardInventory(run_id, "qlik", url, app_id, None, int(time.time()), "QlikConnector", sheets,
                                  completeness={"pages": bool(sheets), "objects": count > 0, "bindings": count > 0, "authoritative": True})

    @staticmethod
    def _qlik_bindings(cube_def: dict[str, Any], cube: dict[str, Any]) -> tuple[list[FieldBinding], list[FieldBinding]]:
        dims, measures = [], []
        dim_info = cube.get("qDimensionInfo", [])
        for i, d in enumerate(cube_def.get("qDimensions", [])):
            qdef = d.get("qDef", {})
            names = qdef.get("qFieldDefs", [])
            name = d.get("qLibraryId") or (names[0] if names else None) or (dim_info[i].get("qFallbackTitle") if i < len(dim_info) else f"dimension_{i}")
            dims.append(FieldBinding("dimension", "dimension", str(name), json.dumps(qdef, separators=(",", ":")), source_id=d.get("qLibraryId")))
        measure_info = cube.get("qMeasureInfo", [])
        for i, m in enumerate(cube_def.get("qMeasures", [])):
            qdef = m.get("qDef", {})
            expr = qdef.get("qDef")
            name = m.get("qLibraryId") or qdef.get("qLabel") or (measure_info[i].get("qFallbackTitle") if i < len(measure_info) else None) or expr or f"measure_{i}"
            measures.append(FieldBinding("measure", "measure", str(name), expr, source_id=m.get("qLibraryId")))
        return dedupe_bindings(dims), dedupe_bindings(measures)


class SACConnector(Connector):
    """Parses an approved exported SAC story/manifest. URL-only crawling is rejected."""

    @staticmethod
    def story_id(url: str) -> Optional[str]:
        m = re.search(r"/s2/([A-Za-z0-9_-]+)", urlparse(url).path)
        return m.group(1) if m else None

    async def extract(self, url: str, artifact_path: Optional[str]) -> DashboardInventory:
        run_id, story_id = stable_run_id(url), self.story_id(url)
        warnings: list[str] = []
        if not artifact_path:
            warnings.append("SAC exact extraction needs an approved exported story/manifest. Widget API needs a known widget ID and is not a complete story crawler.")
            return DashboardInventory(run_id, "sac", url, story_id, None, int(time.time()), "SACConnector", [], warnings=warnings,
                                      completeness={"pages": False, "objects": False, "bindings": False, "authoritative": False})
        artifact = Path(artifact_path).expanduser()
        if not artifact.exists():
            raise RuntimeError(f"SAC artifact not found: {artifact}")
        candidates = [artifact] if artifact.is_file() else list(artifact.rglob("*.json"))
        docs = []
        for p in candidates:
            try:
                val = json.loads(p.read_text(encoding="utf-8-sig"))
                if isinstance(val, dict): docs.append((p, val))
            except Exception: pass
        sheets: list[Sheet] = []
        for p, doc in docs:
            pages = doc.get("pages") or doc.get("story", {}).get("pages") or doc.get("content", {}).get("pages")
            if not isinstance(pages, list): continue
            for pi, page in enumerate(pages):
                if not isinstance(page, dict): continue
                sheet = Sheet(str(page.get("id", f"page_{pi}")), str(page.get("name") or page.get("title") or f"Page {pi+1}"), pi)
                widgets = page.get("widgets") or page.get("objects") or page.get("components") or []
                for wi, widget in enumerate(widgets if isinstance(widgets, list) else []):
                    sheet.objects.append(self._sac_widget(widget, wi, str(p)))
                sheets.append(sheet)
            if sheets: break
        if not sheets: warnings.append("No recognized SAC pages/widgets were found in the supplied JSON artifact.")
        count = sum(len(s.objects) for s in sheets)
        return DashboardInventory(run_id, "sac", url, story_id, None, int(time.time()), "SACConnector", sheets, warnings=warnings,
                                  completeness={"pages": bool(sheets), "objects": count > 0, "bindings": count > 0, "authoritative": count > 0})

    @staticmethod
    def _sac_widget(widget: dict[str, Any], idx: int, source: str) -> DashboardObject:
        oid = str(widget.get("id") or widget.get("widgetId") or f"widget_{idx}")
        otype = str(widget.get("type") or widget.get("widgetType") or "unknown")
        title = first_string(widget, ("title", "name"))
        dims, measures, filters = [], [], []
        for path, node in deep_walk(widget):
            if not isinstance(node, dict): continue
            name = first_string(node, ("dimension", "dimensionId", "field", "name"))
            measure = first_string(node, ("measure", "measureId", "formula", "expression"))
            if name and any(x in path.lower() for x in ("dimension", "binding", "axis", "filter")):
                kind = "filter" if "filter" in path.lower() else "dimension"
                target = filters if kind == "filter" else dims
                target.append(FieldBinding(path.rsplit(".", 1)[-1], kind, name, json.dumps(node, separators=(",", ":"))))
            if measure and any(x in path.lower() for x in ("measure", "binding", "value", "formula")):
                measures.append(FieldBinding(path.rsplit(".", 1)[-1], "measure", measure, json.dumps(node, separators=(",", ":"))))
        return DashboardObject(oid, otype, title, dedupe_bindings(dims), dedupe_bindings(measures), dedupe_bindings(filters), source)


async def vertex_normalize(inv: DashboardInventory) -> DashboardInventory:
    """Normalize labels only. Facts remain from connector evidence."""
    if not genai or not os.getenv("GOOGLE_CLOUD_PROJECT"):
        inv.warnings.append("Vertex normalization skipped: google-genai or GOOGLE_CLOUD_PROJECT is unavailable.")
        return inv
    names = sorted({b.name for s in inv.sheets for o in s.objects for b in (o.dimensions + o.measures + o.filters) if b.name})
    if not names:
        return inv
    schema = {"type": "OBJECT", "properties": {"labels": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {"source": {"type": "STRING"}, "normalized": {"type": "STRING"}}, "required": ["source", "normalized"]}}}, "required": ["labels"]}
    client = genai.Client(vertexai=True, project=os.environ["GOOGLE_CLOUD_PROJECT"], location=os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"))
    prompt = "Normalize spacing/capitalization only. Do not expand acronyms, infer meanings, add, remove, or merge fields. Input:\n" + json.dumps(names)
    try:
        response = await asyncio.to_thread(client.models.generate_content, model=os.getenv("VERTEX_MODEL", "gemini-2.5-flash"), contents=prompt,
            config=genai_types.GenerateContentConfig(response_mime_type="application/json", response_schema=schema, temperature=0))
        mapping = {x["source"]: x["normalized"] for x in json.loads(response.text)["labels"] if x.get("source") in names}
        for s in inv.sheets:
            for o in s.objects:
                for b in o.dimensions + o.measures + o.filters:
                    b.name = mapping.get(b.name, b.name)
    except Exception as e:
        inv.warnings.append(f"Vertex normalization failed without affecting authoritative extraction: {type(e).__name__}: {e}")
    finally:
        try: client.close()
        except Exception: pass
    return inv


def inventory_dict(inv: DashboardInventory) -> dict[str, Any]:
    data = asdict(inv)
    data["sha256"] = None
    payload = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    data["sha256"] = hashlib.sha256(payload).hexdigest()
    inv.sha256 = data["sha256"]
    return data


def write_json(inv: DashboardInventory) -> Path:
    path = OUT_DIR / f"dashboard_inventory_{inv.run_id}.json"
    path.write_text(json.dumps(inventory_dict(inv), indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def flat_rows(inv: DashboardInventory) -> list[dict[str, Any]]:
    rows = []
    for sheet in inv.sheets:
        for obj in sheet.objects:
            bindings = obj.dimensions + obj.measures + obj.filters
            if not bindings:
                rows.append({"platform": inv.platform, "dashboard_id": inv.dashboard_id, "sheet_id": sheet.sheet_id, "sheet_name": sheet.name,
                             "object_id": obj.object_id, "object_type": obj.object_type, "object_title": obj.title,
                             "binding_kind": None, "binding_role": None, "field_name": None, "expression": None,
                             "table": None, "source_id": None, "evidence_level": obj.evidence_level, "warnings": " | ".join(obj.warnings)})
            for b in bindings:
                rows.append({"platform": inv.platform, "dashboard_id": inv.dashboard_id, "sheet_id": sheet.sheet_id, "sheet_name": sheet.name,
                             "object_id": obj.object_id, "object_type": obj.object_type, "object_title": obj.title,
                             "binding_kind": b.kind, "binding_role": b.role, "field_name": b.name, "expression": b.expression,
                             "table": b.table, "source_id": b.source_id, "evidence_level": b.evidence_level, "warnings": " | ".join(obj.warnings)})
    return rows


def write_csv(inv: DashboardInventory) -> Path:
    path = OUT_DIR / f"dashboard_inventory_{inv.run_id}.csv"
    rows = flat_rows(inv)
    fields = list(rows[0]) if rows else ["platform", "dashboard_id", "sheet_id", "sheet_name", "object_id", "object_type", "object_title", "binding_kind", "binding_role", "field_name", "expression", "table", "source_id", "evidence_level", "warnings"]
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(rows)
    return path


def write_xlsx(inv: DashboardInventory) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter
    path = OUT_DIR / f"dashboard_inventory_{inv.run_id}.xlsx"
    wb = Workbook(); ws = wb.active; ws.title = "Object Bindings"
    rows = flat_rows(inv)
    headers = list(rows[0]) if rows else ["platform", "dashboard_id", "sheet_id", "sheet_name", "object_id", "object_type", "object_title", "binding_kind", "binding_role", "field_name", "expression", "table", "source_id", "evidence_level", "warnings"]
    ws.append(headers)
    for row in rows: ws.append([row.get(h) for h in headers])
    fill = PatternFill("solid", fgColor="1F4E78")
    for c in ws[1]: c.font = Font(color="FFFFFF", bold=True); c.fill = fill
    ws.freeze_panes = "A2"; ws.auto_filter.ref = ws.dimensions
    for i, h in enumerate(headers, 1):
        values = [str(ws.cell(r, i).value or "") for r in range(1, min(ws.max_row, 200) + 1)]
        ws.column_dimensions[get_column_letter(i)].width = min(max(len(h) + 2, max((len(v) for v in values), default=0) + 2), 60)
    meta = wb.create_sheet("Run Metadata")
    meta_rows = [("run_id", inv.run_id), ("platform", inv.platform), ("dashboard_url", inv.dashboard_url), ("dashboard_id", inv.dashboard_id),
                 ("dashboard_name", inv.dashboard_name), ("connector", inv.connector), ("app_version", APP_VERSION),
                 ("completeness", json.dumps(inv.completeness)), ("warnings", " | ".join(inv.warnings)), ("sha256", inventory_dict(inv)["sha256"])]
    for r in meta_rows: meta.append(r)
    meta.column_dimensions["A"].width = 24; meta.column_dimensions["B"].width = 100
    wb.save(path); return path


CONNECTORS: dict[Platform, Connector] = {"powerbi": PowerBIConnector(), "qlik": QlikConnector(), "sac": SACConnector()}
app = FastAPI(title="Dashboard Metadata Agent", version=APP_VERSION)


@app.get("/health")
def health():
    return {"status": "ok", "version": APP_VERSION}


@app.post("/analyze", response_model=AnalyzeResponse)
async def analyze(req: AnalyzeRequest):
    try:
        platform = detect_platform(req.dashboard_url)
        inv = await CONNECTORS[platform].extract(req.dashboard_url, req.artifact_path)
        if req.use_vertex_normalization:
            inv = await vertex_normalize(inv)
        writer = {"json": write_json, "csv": write_csv, "xlsx": write_xlsx}[req.output_format]
        path = writer(inv)
        return AnalyzeResponse(run_id=inv.run_id, platform=platform, output_file=str(path), sheets=len(inv.sheets),
                               objects=sum(len(s.objects) for s in inv.sheets), warnings=inv.warnings, completeness=inv.completeness)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"{type(e).__name__}: {e}") from e


@app.get("/download/{run_id}/{fmt}")
def download(run_id: str, fmt: Literal["json", "csv", "xlsx"]):
    path = OUT_DIR / f"dashboard_inventory_{run_id}.{fmt}"
    if not path.exists(): raise HTTPException(404, "Output not found")
    return FileResponse(path, filename=path.name)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("dashboard_metadata_agent:app", host="0.0.0.0", port=int(os.getenv("PORT", "8080")), reload=False)
