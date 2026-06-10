"""Generate a markdown conversion report alongside the PBIP output.

Merged in from a sibling project. The idea: any real Qlik->PBI
conversion is partial, and the user needs to know exactly which
measures were translated cleanly, which fell through to the
``BLANK() /* qlik: ... */`` stub, and which visuals were emitted as
placeholders. Without a report, the user has to open the PBIP and
hunt for issues; with one, they get a punch list.

The report is emitted as ``conversion_report.md`` next to the PBIP
folder under ``--output``. Nothing in the converter depends on it --
it's a pure diagnostic artefact.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class ConversionIssue:
    severity: str  # "error" | "warning" | "info"
    component: str  # "model" | "measure" | "visual" | "script" | "relationship"
    artifact: str
    message: str
    suggestion: Optional[str] = None


@dataclass
class ConversionReport:
    """Per-run report. Pass it into the converter and writer steps and
    they'll append issues as they go. Then call :func:`write_report`."""

    app_name: str
    tables: int = 0
    measures_total: int = 0
    measures_translated: int = 0
    measures_stubbed: int = 0
    relationships: int = 0
    pages: int = 0
    visuals: int = 0
    variables: int = 0
    issues: List[ConversionIssue] = field(default_factory=list)
    # measure name -> (qlik_expr, dax) for measures that became stubs
    stubbed_measures: List[Dict[str, str]] = field(default_factory=list)
    # Pre-flight structural warnings from preflight.run_preflight.
    preflight_warnings: List[str] = field(default_factory=list)
    # Per-page / per-visual mapping summary.
    visual_mapping: List[Dict[str, Any]] = field(default_factory=list)
    # What-If parameters synthesised from Qlik variables.
    what_if_params: List[str] = field(default_factory=list)
    # Bookmarks translated.
    bookmarks: int = 0
    # Captured Qlik bookmark selection state:
    # [{"title": str, "selections": [{"field", "values", "count"}]}]
    bookmark_selections: List[Dict[str, Any]] = field(default_factory=list)
    # Native-vs-synthesised measure split.
    native_aggregations: int = 0
    # Script-derived partitions used (CSV / Excel / Qvd / SQL ...).
    script_partitions: List[Dict[str, str]] = field(default_factory=list)

    def add(
        self,
        severity: str,
        component: str,
        artifact: str,
        message: str,
        suggestion: Optional[str] = None,
    ) -> None:
        self.issues.append(ConversionIssue(
            severity=severity,
            component=component,
            artifact=artifact,
            message=message,
            suggestion=suggestion,
        ))


# ---------------------------------------------------------------------------
# Population from a built SemanticModel + pages
# ---------------------------------------------------------------------------

def populate_from_model(
    report: ConversionReport,
    model: Any,
    pages: Optional[List[Any]] = None,
) -> None:
    """Fill in counts and stubbed-measure list from a built SemanticModel.

    Works against the existing :class:`qlik_to_pbi.model.SemanticModel`
    structure (dict-based tables/measures, not the sibling project's
    dataclass IR). The integration is loose on purpose so the same
    report module can be reused elsewhere.
    """
    report.tables = len(getattr(model, "tables", []) or [])
    report.relationships = len(getattr(model, "relationships", []) or [])
    measures = getattr(model, "measures", []) or []
    report.measures_total = len(measures)
    stubbed = 0
    for m in measures:
        dax = (m.get("expression") if isinstance(m, dict) else None) or ""
        original = m.get("source") if isinstance(m, dict) else ""
        if dax.startswith("BLANK() /* qlik:"):
            stubbed += 1
            report.stubbed_measures.append({
                "name":  m.get("name", ""),
                "table": m.get("table", ""),
                "qlik":  original or "",
                "dax":   dax,
            })
    report.measures_stubbed = stubbed
    report.measures_translated = report.measures_total - stubbed

    if pages is not None:
        report.pages = len(pages)
        # Pages may be dataclass-like (.visuals attr) or plain dicts
        # (["visuals"] key) depending on which builder produced them.
        def _vcount(p: Any) -> int:
            if isinstance(p, dict):
                return len(p.get("visuals") or p.get("cells") or [])
            return len(getattr(p, "visuals", []) or [])
        report.visuals = sum(_vcount(p) for p in pages)

    # Pull variables from the IR if accessible.
    ir = getattr(model, "ir", {}) or {}
    report.variables = len(ir.get("variables", []) or [])
    bookmarks = ir.get("bookmarks", []) or []
    report.bookmarks = len(bookmarks)
    for bm in bookmarks:
        if not isinstance(bm, dict):
            continue
        sels = bm.get("selections") or []
        if not sels:
            continue
        title = ((bm.get("qMetaDef") or {}).get("title")
                 or (bm.get("qInfo") or {}).get("qId") or "")
        report.bookmark_selections.append({"title": title, "selections": sels})

    # What-If parameters: tables with the synthetic source tag.
    for t in getattr(model, "tables", []) or []:
        if isinstance(t, dict) and t.get("source") == "what_if_parameter":
            report.what_if_params.append(t.get("name", ""))

    # Script-derived partitions: enumerate the blocks the script
    # parser recovered and that actually map to real model tables.
    table_names_lower = {
        (t.get("name") or "").lower()
        for t in (getattr(model, "tables", []) or [])
    }
    for b in ir.get("script_blocks") or []:
        if (b.get("table") or "").lower() in table_names_lower:
            report.script_partitions.append({
                "table":       b.get("table", ""),
                "source":      b.get("source", ""),
                "source_type": b.get("source_type", ""),
            })

    # Visual mapping table: walk pages -> visuals and record
    # (page, qlik type, pbi type).
    if pages is not None:
        for p in pages:
            if isinstance(p, dict):
                page_name = p.get("displayName", "")
                visuals = p.get("visuals") or []
            else:
                page_name = getattr(p, "displayName", "")
                visuals = getattr(p, "visuals", []) or []
            for v in visuals:
                vb = v.get("visual", {}) if isinstance(v, dict) else {}
                pbi_type = vb.get("visualType", "?")
                report.visual_mapping.append({
                    "page":     page_name,
                    "pbi_type": pbi_type,
                })

    # Native-aggregation count -- read from the report-builder's
    # registry if the conversion built a ReportBuilder.
    rb = getattr(model, "_report_builder", None)
    if rb is not None:
        report.native_aggregations = len(
            getattr(rb, "_column_aggregations", {}) or {}
        )


# ---------------------------------------------------------------------------
# Markdown emit
# ---------------------------------------------------------------------------

_SEV_ORDER = ("error", "warning", "info")


def write_report(report: ConversionReport, output_path: Path) -> Path:
    """Write the markdown report to ``output_path``.

    Returns the path written. Overwrites any existing file at that
    location.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    severity_counts = Counter(i.severity for i in report.issues)
    component_counts = Counter(i.component for i in report.issues)

    lines: List[str] = []
    lines.append(f"# Conversion Report: {report.app_name}\n")
    lines.append("## Summary\n")
    lines.append(f"- Tables: **{report.tables}**")
    lines.append(
        f"- Measures: **{report.measures_total}** "
        f"(translated cleanly: {report.measures_translated}, "
        f"stubbed: {report.measures_stubbed})"
    )
    lines.append(f"- Relationships: **{report.relationships}**")
    lines.append(f"- Pages: **{report.pages}**")
    lines.append(f"- Visuals: **{report.visuals}**")
    lines.append(f"- Variables: **{report.variables}**")
    lines.append(f"- Bookmarks: **{report.bookmarks}**")
    if report.native_aggregations:
        lines.append(
            f"- Native aggregations (no DAX measure needed): "
            f"**{report.native_aggregations}**"
        )
    if report.what_if_params:
        lines.append(
            f"- What-If parameters synthesised: "
            f"**{len(report.what_if_params)}**"
        )
    if report.script_partitions:
        lines.append(
            f"- Script-derived partitions: "
            f"**{len(report.script_partitions)}**"
        )
    lines.append("")

    if report.preflight_warnings:
        lines.append("## Pre-flight Warnings\n")
        lines.append(
            "Structural checks that ran AFTER the PBIP was written. "
            "These flag the kind of issue PBI Desktop reports only "
            "as a generic load failure. Resolve before opening.\n"
        )
        for w in report.preflight_warnings:
            lines.append(f"- {w}")
        lines.append("")

    if report.visual_mapping:
        from collections import Counter as _Counter
        pbi_counts = _Counter(v["pbi_type"] for v in report.visual_mapping)
        lines.append("## Visual Coverage\n")
        lines.append("| PBI visual type | Count |")
        lines.append("|---|---|")
        for vt, n in pbi_counts.most_common():
            lines.append(f"| {vt} | {n} |")
        lines.append("")

    if report.script_partitions:
        lines.append("## Script-derived Partitions\n")
        lines.append(
            "The script parser recovered the original Qlik LOAD "
            "source for these tables, so the PBIP carries a real "
            "Power Query partition instead of an empty stub.\n"
        )
        lines.append("| Table | Source type | Source path |")
        lines.append("|---|---|---|")
        for sp in report.script_partitions:
            src = (sp.get("source") or "").replace("|", "\\|")
            lines.append(f"| {sp['table']} | {sp['source_type']} | `{src}` |")
        lines.append("")

    if report.what_if_params:
        lines.append("## What-If Parameters\n")
        lines.append(
            "Qlik variables with a numeric default were materialised "
            "as PBI What-If parameters. A slicer on the parameter "
            "table lets users scrub the value the same way Qlik's "
            "Variable Input extension does.\n"
        )
        for vn in report.what_if_params:
            lines.append(f"- `{vn}`")
        lines.append("")

    if report.bookmark_selections:
        lines.append("## Bookmarks\n")
        lines.append(
            "Each Qlik bookmark is emitted as a PBI bookmark scaffold "
            "(name + landing page). The field selections captured from "
            "the Qlik engine are listed below so you can reproduce each "
            "bookmark's filtered view in Power BI Desktop (select the "
            "values on the relevant slicers / filter pane, then "
            "**Update** the bookmark).\n"
        )
        for bm in report.bookmark_selections:
            lines.append(f"### {bm['title']}")
            for sel in bm["selections"]:
                vals = sel.get("values") or []
                count = sel.get("count")
                shown = ", ".join(str(v) for v in vals[:25])
                if len(vals) > 25:
                    shown += f", … (+{len(vals) - 25} more)"
                suffix = f" ({count} selected)" if count and count != len(vals) else ""
                lines.append(f"- **{sel.get('field')}**{suffix}: {shown}")
            lines.append("")

    lines.append("### Issues by severity")
    for sev in _SEV_ORDER:
        lines.append(f"- {sev}: {severity_counts.get(sev, 0)}")
    lines.append("")

    if component_counts:
        lines.append("### Issues by component")
        for comp, n in component_counts.most_common():
            lines.append(f"- {comp}: {n}")
        lines.append("")

    # Detailed issues sorted by severity then component.
    if report.issues:
        lines.append("## Detailed Issues\n")
        ordered = sorted(
            report.issues,
            key=lambda i: (_SEV_ORDER.index(i.severity)
                           if i.severity in _SEV_ORDER else 99),
        )
        for issue in ordered:
            lines.append(
                f"### [{issue.severity.upper()}] "
                f"{issue.component}: {issue.artifact}"
            )
            lines.append(issue.message)
            if issue.suggestion:
                lines.append(f"**Action:** {issue.suggestion}")
            lines.append("")

    # Stubbed measures table -- the big punch list.
    lines.append("## Measures Requiring Manual Review\n")
    if report.stubbed_measures:
        lines.append(
            "These measures could not be translated cleanly. The original "
            "Qlik expression is preserved as a DAX comment so you can "
            "rewrite them by hand in Power BI Desktop.\n"
        )
        for m in report.stubbed_measures:
            label = (
                f"{m['table']}.{m['name']}" if m.get("table")
                else m["name"]
            )
            lines.append(f"### {label}")
            qlik = (m.get("qlik") or "").replace("`", "\\`")
            lines.append(f"**Qlik:** `{qlik}`")
            lines.append("")
    else:
        lines.append("_All measures translated successfully._\n")

    output_path.write_text("\n".join(lines), encoding="utf-8")
    return output_path
