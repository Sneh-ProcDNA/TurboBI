"""Offline unbuild via Qlik Sense Desktop's Engine API.

This is the Desktop / WebSocket counterpart to the cloud-tied
``qlik app unbuild`` CLI. It walks a running Qlik Sense Desktop app
over a localhost JSON-RPC connection and writes the **same JSON
directory layout** that the cloud CLI produces, so the downstream
:mod:`qlik_to_pbi.parser` doesn't need to change.

What we write:

::

    <output>/
      app-properties.json
      script.qvs
      dimensions.json
      measures.json
      variables.json
      objects/
        sheet--<safeTitle>-<qId>.json
        masterobject-<safeTitle>-<qId>.json
        loadmodel---loadmodel.json

Strategy:

* One ``CreateSessionObject`` call enumerates everything (sheets,
  master objects, dimensions, measures, variables) into a single
  transient "session lists" object. Then ``GetLayout`` gives us the
  ids and surface metadata in one shot — no per-object round trip
  for the listing step.
* For each sheet and master object we then call
  ``GetObject(qId) -> GetFullPropertyTree`` which returns the
  ``{qProperty, qChildren}`` tree the parser expects byte-for-byte.
* Dimensions, measures, variables are fetched via the dedicated
  ``GetDimension`` / ``GetMeasure`` / ``GetVariableById`` methods
  plus a ``GetProperties`` on the returned handle.
* The loadmodel comes from ``GetObject("LoadModel") -> GetLayout``
  (Desktop exposes the data-model snapshot as a named generic
  object). On failure we synthesise a minimal stand-in from
  ``GetTablesAndKeys`` so the converter still has table/field info.

No cloud tenant, no API key, no data leaves the machine.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from ._logging import get_logger
from .engine_fetch import DEFAULT_ENGINE_URL, EngineClient
from .utils import safe_filename

_log = get_logger("UNBUILD")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def unbuild_via_engine(
    qvf_path: Optional[Path] = None,
    output_dir: Path = Path("."),
    engine_url: str = DEFAULT_ENGINE_URL,
    tenant: Optional[str] = None,
    api_key: Optional[str] = None,
    app_id: Optional[str] = None,
) -> Path:
    """Walk a Qlik app via Engine API and write the unbuild JSON layout.

    Two modes:

    * **Local Desktop** -- pass ``qvf_path`` to point at a file on disk.
      Connects to ``ws://localhost:4848`` (or ``engine_url``).
    * **Qlik Cloud** -- pass ``tenant`` + ``api_key`` + ``app_id``. No
      file path needed; the app lives in the cloud. This removes the
      "QVF must be opened in Desktop" dependency entirely and is the
      preferred path for server deployments.

    Returns the resolved output path so the caller can hand it to the
    converter. Raises ``RuntimeError`` on unrecoverable engine failures
    (engine unreachable, auth rejected, etc.).
    """
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "objects").mkdir(parents=True, exist_ok=True)

    client = EngineClient(
        qvf_path=qvf_path,
        engine_base_url=engine_url,
        tenant=tenant,
        api_key=api_key,
        app_id=app_id,
    )
    client.connect()
    try:
        _write_app_properties(client, output_dir)
        _write_script(client, output_dir)
        _write_loadmodel(client, output_dir)

        lists = _enumerate_app_lists(client)
        _write_dimensions(client, lists.get("dimensions", []), output_dir)
        _write_measures(client, lists.get("measures", []), output_dir)
        _write_variables(client, lists.get("variables", []), output_dir)
        _write_sheets(client, lists.get("sheets", []), output_dir)
        _write_master_objects(client, lists.get("master_objects", []), output_dir)
        # Bookmarks: the cloud unbuild CLI omits them; we recover via
        # the engine's BookmarkList listdef + per-bookmark GetProperties.
        _write_bookmarks(client, lists.get("bookmarks", []), output_dir)

        # Cloud CLI also writes inert config.yml and connections.yml.
        # The parser ignores them, but matching the layout makes it
        # easier to diff our output against a cloud unbuild.
        _write_text(output_dir / "config.yml", "files:\n  - app-properties.json\n")
        _write_text(output_dir / "connections.yml", "connections: []\n")
    finally:
        client.close()

    _log.info(f"Engine unbuild complete -> {output_dir}")
    return output_dir


# ---------------------------------------------------------------------------
# App-level files
# ---------------------------------------------------------------------------

def _write_app_properties(client: EngineClient, out: Path) -> None:
    """Write app-properties.json in the cloud-unbuild shape.

    Cloud `qlik app unbuild` writes a FLAT JSON object with the keys:

    ::

        {qTitle, qLastReloadTime, qSavedInProductVersion, qThumbnail,
         description, qUsage, published, hassectionaccess}

    Not all of these come from a single Engine API call -- ``qTitle``
    lives on the app layout while ``qLastReloadTime`` lives on the
    properties. We merge both and fill ``description`` etc. with
    sensible defaults so the parser's ``qTitle`` lookup always
    resolves.
    """
    props: Dict[str, Any] = {}
    try:
        result = client.request("GetAppProperties", client.app_handle, [])
        props = result.get("qProp") or {}
    except RuntimeError as exc:
        _log.warning(f"GetAppProperties failed: {exc}")

    layout: Dict[str, Any] = {}
    try:
        layout_res = client.request("GetAppLayout", client.app_handle, [])
        layout = layout_res.get("qLayout") or {}
    except RuntimeError as exc:
        _log.warning(f"GetAppLayout failed: {exc}")

    body = {
        "qTitle":                  layout.get("qTitle") or props.get("qTitle") or "Untitled",
        "qLastReloadTime":         layout.get("qLastReloadTime") or props.get("qLastReloadTime") or "",
        "qSavedInProductVersion":  props.get("qSavedInProductVersion") or "",
        "qThumbnail":              layout.get("qThumbnail") or {},
        "description":             (layout.get("qMeta") or {}).get("description") or "",
        "qUsage":                  layout.get("qUsage") or "ANALYTICS",
        "published":               bool(layout.get("published")) if "published" in layout else False,
        "hassectionaccess":        bool(layout.get("qHasSectionAccess")) if "qHasSectionAccess" in layout else False,
    }
    _write_json(out / "app-properties.json", body)


def _write_script(client: EngineClient, out: Path) -> None:
    """Write script.qvs (the LOAD script body)."""
    try:
        result = client.request("GetScript", client.app_handle, [])
        script = result.get("qScript") or ""
    except RuntimeError as exc:
        _log.warning(f"GetScript failed: {exc}")
        script = ""
    _write_text(out / "script.qvs", script)


def _write_loadmodel(client: EngineClient, out: Path) -> None:
    """Write objects/loadmodel---loadmodel.json.

    Desktop exposes the data-model snapshot as a named generic object
    called ``LoadModel``. The shape that the cloud `qlik app unbuild`
    writes -- with ``connectionMetaDataModels``, ``tables``, ``queries``,
    ``associations`` -- lives in the object's **properties** bag, not
    its runtime layout. So we use ``GetProperties`` (not ``GetLayout``).

    Fallback chain:

    1. ``GetObject("LoadModel") -> GetProperties`` -- full shape.
    2. ``GetObject("LoadModel") -> GetLayout`` -- some versions only
       populate this.
    3. ``GetTablesAndKeys`` -- minimal stand-in with tables+fields only,
       no queries / associations. Relationships will be empty but the
       PBIP still loads.
    """
    out_path = out / "objects" / "loadmodel---loadmodel.json"

    try:
        result = client.request(
            "GetObject", client.app_handle, ["LoadModel"],
        )
        obj_handle = ((result.get("qReturn") or {}).get("qHandle"))
        if isinstance(obj_handle, int) and obj_handle > 0:
            # First try GetProperties -- that's where the cloud-unbuild
            # shape (connectionMetaDataModels + queries + associations)
            # lives.
            try:
                props_res = client.request("GetProperties", obj_handle, [])
                bag = props_res.get("qProp") or {}
                if bag and bag.get("tables"):
                    _log.info(
                        "Loadmodel: pulled via "
                        "GetObject('LoadModel') -> GetProperties."
                    )
                    _write_json(out_path, bag)
                    return
            except RuntimeError as exc:
                _log.info(f"GetProperties on LoadModel failed: {exc}")
            # Fall back to GetLayout.
            try:
                layout_res = client.request("GetLayout", obj_handle, [])
                ql = layout_res.get("qLayout") or {}
                if ql and (ql.get("tables") or ql.get("qTables")):
                    _log.info(
                        "Loadmodel: pulled via "
                        "GetObject('LoadModel') -> GetLayout."
                    )
                    _write_json(out_path, ql)
                    return
            except RuntimeError as exc:
                _log.info(f"GetLayout on LoadModel failed: {exc}")
    except RuntimeError as exc:
        _log.info(
            f"GetObject('LoadModel') unavailable ({exc}); "
            "falling back to GetTablesAndKeys."
        )

    # Last resort: synthesise from GetTablesAndKeys (no queries /
    # associations -- relationships will be empty).
    try:
        result = client.request(
            "GetTablesAndKeys",
            client.app_handle,
            [
                {"qcx": 1000, "qcy": 1000},  # qWindowSize
                {"qcx": 0,    "qcy": 0},     # qNullSize
                30,                          # qCellHeight
                False,                       # qSyntheticMode
                False,                       # qIncludeSysVars
            ],
        )
    except RuntimeError as exc:
        _log.warning(f"GetTablesAndKeys also failed: {exc}")
        _write_json(out_path, {"tables": []})
        return

    synthesised = _synthesize_loadmodel(result)
    _write_json(out_path, synthesised)


def _synthesize_loadmodel(get_tables_response: Dict[str, Any]) -> Dict[str, Any]:
    """Build a minimal loadmodel-shaped dict from a GetTablesAndKeys reply.

    Schema we emit (matches the subset of fields the converter actually
    reads in :mod:`qlik_to_pbi.model`):

    ::

        {"tables": [
            {"id": "dsd.<TableAlias>",
             "tableAlias": "<TableAlias>",
             "fields": [{"id": "...", "name": "...", "alias": "..."}, ...]},
        ], "queries": [], "associations": {}}

    No ``queries`` / ``associations`` means no relationships -- the
    converter will emit an empty relationships set. Acceptable.
    """
    tables_out: List[Dict[str, Any]] = []
    for tbl in get_tables_response.get("qtr") or []:
        name = tbl.get("qName") or ""
        if not name:
            continue
        fields_out = []
        for fld in tbl.get("qFields") or []:
            fname = fld.get("qName") or ""
            if not fname:
                continue
            fields_out.append({
                "id":    f"dsd.{name}.{fname}",
                "name":  fname,
                "alias": fname,
            })
        tables_out.append({
            "id":         f"dsd.{name}",
            "tableAlias": name,
            "tableName":  name,
            "fields":     fields_out,
        })
    return {"tables": tables_out, "queries": [], "associations": {}}


# ---------------------------------------------------------------------------
# Bulk enumeration (one session-object call covers everything)
# ---------------------------------------------------------------------------

_SESSION_LISTS_DEF: Dict[str, Any] = {
    "qInfo":               {"qType": "SessionLists"},
    "qAppObjectListDef":   {
        "qType": "sheet",
        "qData": {
            # `qData` paths copy fields from the source object into the
            # list response, so we get sheet titles without a per-sheet
            # GetProperties round-trip just for the filename slug.
            "title":       "/qMetaDef/title",
            "description": "/qMetaDef/description",
            "rank":        "/rank",
        },
    },
    "qDimensionListDef":   {
        "qType": "dimension",
        "qData": {"title": "/qMetaDef/title"},
    },
    "qMeasureListDef":     {
        "qType": "measure",
        "qData": {"title": "/qMetaDef/title"},
    },
    "qVariableListDef":    {"qType": "variable",
                            "qShowConfig":  True,
                            "qShowReserved": False,
                            "qShowSession": False},
}

_MASTER_OBJECT_LIST_DEF: Dict[str, Any] = {
    "qInfo":             {"qType": "MasterObjectList"},
    "qAppObjectListDef": {
        "qType": "masterobject",
        "qData": {"title": "/qMetaDef/title"},
    },
}

# Bookmarks live behind their own list type ("bookmark"). The cloud
# unbuild CLI does NOT export these, so the only way to recover them
# offline is to call CreateSessionObject with this listdef and then
# follow each item's qId with GetBookmark / GetProperties.
#
# Critical: the correct listdef key is ``qBookmarkListDef``, NOT
# ``qAppObjectListDef`` with qType="bookmark" -- the engine accepts
# the latter but silently returns an empty qItems array. The bookmark
# list also surfaces under ``qLayout.qBookmarkList.qItems`` (not
# qAppObjectList), so we check that path first.
_BOOKMARK_LIST_DEF: Dict[str, Any] = {
    "qInfo":             {"qType": "BookmarkList"},
    "qBookmarkListDef":  {
        "qType": "bookmark",
        "qData": {
            "title":       "/qMetaDef/title",
            "description": "/qMetaDef/description",
            # ``sheetId`` lets us record which page the bookmark was
            # taken on, useful for the PBI bookmark's activeSection.
            "sheetId":     "/sheetId",
        },
    },
}


def _enumerate_app_lists(client: EngineClient) -> Dict[str, List[Dict[str, Any]]]:
    """One round-trip enumeration of every object the converter needs.

    Returns a dict ``{sheets, master_objects, dimensions, measures,
    variables}`` where each value is a list of qItems (the surface
    metadata; full bodies are fetched separately by the per-kind
    writers).
    """
    out: Dict[str, List[Dict[str, Any]]] = {
        "sheets": [], "master_objects": [],
        "dimensions": [], "measures": [], "variables": [],
        "bookmarks": [],
    }

    try:
        res = client.request(
            "CreateSessionObject", client.app_handle, [_SESSION_LISTS_DEF],
        )
        h = ((res.get("qReturn") or {}).get("qHandle"))
        layout = client.request("GetLayout", h, [])
        ql = layout.get("qLayout") or {}
        out["sheets"] = ((ql.get("qAppObjectList") or {}).get("qItems")) or []
        out["dimensions"] = ((ql.get("qDimensionList") or {}).get("qItems")) or []
        out["measures"] = ((ql.get("qMeasureList") or {}).get("qItems")) or []
        out["variables"] = ((ql.get("qVariableList") or {}).get("qItems")) or []
    except RuntimeError as exc:
        _log.warning(f"Session list enumeration failed: {exc}")

    try:
        res2 = client.request(
            "CreateSessionObject",
            client.app_handle,
            [_MASTER_OBJECT_LIST_DEF],
        )
        h2 = ((res2.get("qReturn") or {}).get("qHandle"))
        layout2 = client.request("GetLayout", h2, [])
        ql2 = layout2.get("qLayout") or {}
        out["master_objects"] = ((ql2.get("qAppObjectList") or {}).get("qItems")) or []
    except RuntimeError as exc:
        _log.info(f"Master-object enumeration skipped: {exc}")

    # Bookmarks. Same session-object pattern: declare a BookmarkList
    # session object, then read qBookmarkList.qItems from the layout.
    # Older engines may not expose the listdef; treat that as "no
    # bookmarks" rather than a hard failure.
    try:
        res3 = client.request(
            "CreateSessionObject",
            client.app_handle,
            [_BOOKMARK_LIST_DEF],
        )
        h3 = ((res3.get("qReturn") or {}).get("qHandle"))
        layout3 = client.request("GetLayout", h3, [])
        ql3 = layout3.get("qLayout") or {}
        out["bookmarks"] = (
            ((ql3.get("qBookmarkList") or {}).get("qItems"))
            # Some engines surface bookmarks under qAppObjectList when
            # the listdef qType is "bookmark"; check both shapes.
            or ((ql3.get("qAppObjectList") or {}).get("qItems"))
            or []
        )
    except RuntimeError as exc:
        _log.info(f"Bookmark enumeration skipped: {exc}")

    _log.info(
        f"Enumerated: {len(out['sheets'])} sheets, "
        f"{len(out['master_objects'])} master objects, "
        f"{len(out['dimensions'])} dimensions, "
        f"{len(out['measures'])} measures, "
        f"{len(out['variables'])} variables, "
        f"{len(out['bookmarks'])} bookmarks."
    )
    return out


# ---------------------------------------------------------------------------
# Per-kind writers
# ---------------------------------------------------------------------------

def _write_dimensions(
    client: EngineClient, items: List[Dict[str, Any]], out: Path,
) -> None:
    """Write dimensions.json — a list of full master-dimension property bags."""
    bodies = []
    for item in items:
        qid = (item.get("qInfo") or {}).get("qId") or ""
        if not qid:
            continue
        try:
            res = client.request("GetDimension", client.app_handle, [qid])
            handle = ((res.get("qReturn") or {}).get("qHandle"))
            if not isinstance(handle, int):
                continue
            props = client.request("GetProperties", handle, [])
            bodies.append(props.get("qProp") or props.get("qProperties") or {})
        except RuntimeError as exc:
            _log.warning(f"  dim {qid}: {exc}")
    _write_json(out / "dimensions.json", bodies)


def _write_measures(
    client: EngineClient, items: List[Dict[str, Any]], out: Path,
) -> None:
    """Write measures.json — a list of full master-measure property bags."""
    bodies = []
    for item in items:
        qid = (item.get("qInfo") or {}).get("qId") or ""
        if not qid:
            continue
        try:
            res = client.request("GetMeasure", client.app_handle, [qid])
            handle = ((res.get("qReturn") or {}).get("qHandle"))
            if not isinstance(handle, int):
                continue
            props = client.request("GetProperties", handle, [])
            bodies.append(props.get("qProp") or props.get("qProperties") or {})
        except RuntimeError as exc:
            _log.warning(f"  measure {qid}: {exc}")
    _write_json(out / "measures.json", bodies)


def _write_variables(
    client: EngineClient, items: List[Dict[str, Any]], out: Path,
) -> None:
    """Write variables.json matching the cloud-unbuild shape exactly.

    Cloud emits each variable as::

        {"qInfo": {"qId": ..., "qType": "variable"},
         "qMetaDef": {},
         "qName": ...,
         "qNumberPresentation": {"qType": ..., "qnDec": ..., "qUseThou": ...},
         "qDefinition": ...,
         "qComment": ...,  # optional
         "tags": []}       # optional

    We hit ``GetVariableById -> GetProperties`` per variable to get the
    full bag, since the session-list item lacks ``qMetaDef`` and
    ``qNumberPresentation``.
    """
    bodies = []
    for item in items:
        qid = (item.get("qInfo") or {}).get("qId") or ""
        if not qid:
            continue
        full: Dict[str, Any] = {}
        try:
            res = client.request(
                "GetVariableById", client.app_handle, [qid],
            )
            handle = ((res.get("qReturn") or {}).get("qHandle"))
            if isinstance(handle, int):
                props = client.request("GetProperties", handle, [])
                full = props.get("qProp") or {}
        except RuntimeError as exc:
            _log.info(f"  variable {qid}: {exc}")
        # Fall back to the session-list item if the per-variable
        # lookup didn't produce anything.
        if not full:
            full = item

        body = {
            "qInfo":                full.get("qInfo") or {"qId": qid, "qType": "variable"},
            "qMetaDef":             full.get("qMetaDef") or {},
            "qName":                full.get("qName") or item.get("qName") or "",
            "qNumberPresentation":  full.get("qNumberPresentation") or {
                "qType": "U", "qnDec": 10, "qUseThou": 0,
            },
            "qDefinition":          full.get("qDefinition") or item.get("qDefinition") or "",
        }
        if full.get("qComment"):
            body["qComment"] = full["qComment"]
        if full.get("tags"):
            body["tags"] = full["tags"]
        bodies.append(body)
    _write_json(out / "variables.json", bodies)


def _write_bookmarks(
    client: EngineClient, items: List[Dict[str, Any]], out: Path,
) -> None:
    """Write ``bookmarks.json`` -- a list of bookmark property bags.

    Each entry preserves the shape the converter's parser expects:

        {"qInfo":    {"qId": "<uuid>", "qType": "bookmark"},
         "qMetaDef": {"title": "<title>", "description": "<desc>"},
         "qBookmark": {<engine-side selection state>}, ...}

    Field selections inside ``qBookmark`` reference Qlik field IDs and
    don't translate 1:1 to Power BI filters; the writer downstream
    emits scaffold PBI bookmarks that the user completes in Desktop's
    bookmark pane. We still preserve the full body so anyone reading
    the unbuild dir can see the original selection.

    Empty input -- whether because the engine returned no bookmarks or
    the listdef call failed -- still writes an empty list so the
    parser's ``_read_json(...default=[])`` doesn't need to handle
    a missing-file edge case differently.
    """
    bodies = []
    for item in items:
        qid = (item.get("qInfo") or {}).get("qId") or ""
        if not qid:
            continue
        full: Dict[str, Any] = {}
        try:
            res = client.request("GetBookmark", client.app_handle, [qid])
            handle = ((res.get("qReturn") or {}).get("qHandle"))
            if isinstance(handle, int):
                props = client.request("GetProperties", handle, [])
                full = props.get("qProp") or props.get("qProperties") or {}
        except RuntimeError as exc:
            _log.info(f"  bookmark {qid}: {exc}")
        # When GetProperties fails (older engines, restricted apps),
        # keep at least the list-item surface so the bookmark name
        # still shows up downstream.
        if not full:
            full = item
        body = {
            "qInfo":     full.get("qInfo") or {"qId": qid, "qType": "bookmark"},
            "qMetaDef":  full.get("qMetaDef") or {},
        }
        # ``qBookmark`` is the engine's selection state; preserve it
        # verbatim. ``qMetaDef.title`` is what surfaces in PBI.
        if full.get("qBookmark"):
            body["qBookmark"] = full["qBookmark"]
        # Resolve the actual field selections by applying the bookmark
        # and reading current selections. The raw qBookmark layout only
        # lists which fields participate (qType=PRESENT) without values;
        # ApplyBookmark + the selection object yields field -> values.
        sels = _capture_bookmark_selections(client, qid)
        if sels:
            body["selections"] = sels
        bodies.append(body)
    # Leave the app in a clean state so a subsequent data extract isn't
    # filtered by the last bookmark we applied.
    try:
        client.request("ClearAll", client.app_handle, [True, ""])
    except RuntimeError:
        pass
    _write_json(out / "bookmarks.json", bodies)


def _capture_bookmark_selections(
    client: EngineClient, qid: str,
) -> List[Dict[str, Any]]:
    """Apply a bookmark and read back its field selections as
    ``[{"field": <name>, "values": [...], "count": <int>}]``.

    Qlik summarises large selections in the selection object's
    ``qSelected`` string (e.g. ``"16 of 63"``); when the count exceeds
    the inlined values we fall back to a per-field list object to
    recover the explicit selected values."""
    out: List[Dict[str, Any]] = []
    try:
        client.request("ClearAll", client.app_handle, [True, ""])
        client.request("ApplyBookmark", client.app_handle, [qid])
        so = client.request(
            "CreateSessionObject", client.app_handle,
            [{"qInfo": {"qType": "sel"}, "qSelectionObjectDef": {}}],
        )
        handle = (so.get("qReturn") or {}).get("qHandle")
        if not isinstance(handle, int):
            return out
        lay = client.request("GetLayout", handle, [])
        sels = (
            ((lay.get("qLayout") or {}).get("qSelectionObject") or {})
            .get("qSelections") or []
        )
        for s in sels:
            field = s.get("qField")
            if not field:
                continue
            count = int(s.get("qSelectedCount") or 0)
            raw = (s.get("qSelected") or "").strip()
            values = [v.strip() for v in raw.split(",")] if raw else []
            # A summary like "16 of 63" doesn't list the values --
            # detect (values shorter than count) and resolve explicitly.
            if count and len(values) < count:
                resolved = _selected_field_values(client, field)
                if resolved:
                    values = resolved
            out.append({"field": field, "values": values, "count": count})
        try:
            client.request("DestroySessionObject", client.app_handle, [handle])
        except RuntimeError:
            pass
    except RuntimeError as exc:
        _log.info(f"  bookmark {qid} selection capture skipped: {exc}")
    return out


def _selected_field_values(
    client: EngineClient, field: str, cap: int = 1000,
) -> List[str]:
    """Read the explicitly selected values of a field via a session
    list object (used when the selection object summarises them)."""
    values: List[str] = []
    try:
        lo = client.request(
            "CreateSessionObject", client.app_handle,
            [{
                "qInfo": {"qType": "ListObject"},
                "qListObjectDef": {
                    "qDef": {"qFieldDefs": [field]},
                    "qInitialDataFetch": [{"qTop": 0, "qLeft": 0,
                                            "qHeight": cap, "qWidth": 1}],
                },
            }],
        )
        handle = (lo.get("qReturn") or {}).get("qHandle")
        if not isinstance(handle, int):
            return values
        lay = client.request("GetLayout", handle, [])
        pages = (
            ((lay.get("qLayout") or {}).get("qListObject") or {})
            .get("qDataPages") or []
        )
        for page in pages:
            for row in page.get("qMatrix", []):
                for cell in row:
                    # qState 'S' = selected, 'L' = locked-selected.
                    if cell.get("qState") in ("S", "L"):
                        values.append(cell.get("qText", ""))
        try:
            client.request("DestroySessionObject", client.app_handle, [handle])
        except RuntimeError:
            pass
    except RuntimeError:
        pass
    return values


def _write_sheets(
    client: EngineClient, items: List[Dict[str, Any]], out: Path,
) -> None:
    """Write one objects/sheet--<slug>-<qid>.json per sheet.

    Uses ``GetFullPropertyTree`` which returns a recursive
    ``{qProperty, qChildren[{qProperty, qChildren...}]}`` tree --
    exactly the shape :mod:`qlik_to_pbi.parser` expects.
    """
    for item in items:
        qid = (item.get("qInfo") or {}).get("qId") or ""
        if not qid:
            continue
        title = (
            (item.get("qData") or {}).get("title")
            or (item.get("qMeta") or {}).get("title")
            or qid
        )
        slug = _slugify(title)
        # Match the cloud unbuild's lowercase-qid suffix pattern
        # (e.g. sheet--overview-khpbzsv.json for qId "kHpBzSv").
        fname = f"sheet--{slug}-{qid.lower()}.json"
        try:
            res = client.request("GetObject", client.app_handle, [qid])
            handle = ((res.get("qReturn") or {}).get("qHandle"))
            if not isinstance(handle, int):
                continue
            tree = client.request("GetFullPropertyTree", handle, [])
            body = tree.get("qPropEntry") or {}
            _write_json(out / "objects" / fname, body)
        except RuntimeError as exc:
            _log.warning(f"  sheet {qid}: {exc}")


def _write_master_objects(
    client: EngineClient, items: List[Dict[str, Any]], out: Path,
) -> None:
    """Write one objects/masterobject-<slug>-<qid>.json per master visualization."""
    for item in items:
        qid = (item.get("qInfo") or {}).get("qId") or ""
        if not qid:
            continue
        title = (
            (item.get("qData") or {}).get("title")
            or (item.get("qMeta") or {}).get("title")
            or qid
        )
        slug = _slugify(title)
        fname = f"masterobject-{slug}-{qid.lower()}.json"
        try:
            res = client.request("GetObject", client.app_handle, [qid])
            handle = ((res.get("qReturn") or {}).get("qHandle"))
            if not isinstance(handle, int):
                continue
            tree = client.request("GetFullPropertyTree", handle, [])
            body = tree.get("qPropEntry") or {}
            _write_json(out / "objects" / fname, body)
        except RuntimeError as exc:
            _log.warning(f"  masterobject {qid}: {exc}")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^A-Za-z0-9]+")


def _slugify(title: str) -> str:
    """Lower-case kebab-case slug for sheet / master-object filenames."""
    s = _SLUG_RE.sub("-", (title or "").strip().lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    if not s:
        s = "untitled"
    if len(s) > 60:
        s = s[:60].rstrip("-")
    return s


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text or "")
