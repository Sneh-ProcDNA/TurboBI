"""Corpus analyzer for Tableau calc fields → DAX translation.

For every .twbx in ``Sample Dashboards/`` (one level above the repo
root by default), this script:

1. Unzips to find the .twb, parses datasources.
2. Builds a minimal ``SemanticModel`` so the translator gets realistic
   ``field_to_pbi`` / ``measure_refs`` / ``parameter_refs`` context.
3. Walks every calc field on every datasource. For each one captures
   workbook, datasource, name/caption, role, tmdlType, the raw Tableau
   formula, the translator output (or None on drop), and a coarse
   pattern bucket (which Tableau functions appear in the formula).
4. Writes ``corpus.jsonl`` (one record per calc field) and
   ``patterns.md`` (pattern roll-up with per-pattern count, success
   rate, illustrative examples) into this directory.

Use this to identify common formula shapes, see what currently works
vs fails, and decide which patterns deserve dedicated translation
rules.

Run from project root:
    python -m tableau_to_pbi_agent.dax_corpus analyze
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import traceback
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from tableau_to_pbi.parser import TWBParser
from tableau_to_pbi.model import SemanticModel
from tableau_to_pbi.dax_translator import translate_tableau_to_dax


HERE         = Path(__file__).parent            # tableau_to_pbi_agent/dax_corpus
PROJECT_ROOT = HERE.parent.parent               # TurboBI Version 0515
DEFAULT_CORPUS_DIR = PROJECT_ROOT.parent / "Sample Dashboards"
OUT_JSONL = HERE / "corpus.jsonl"
OUT_MD    = HERE / "patterns.md"


# Function tokens we look for in Tableau formulas to bucket each calc.
# Order matters: the first match wins as the primary bucket; secondary
# buckets are tracked separately.
PATTERN_TOKENS = [
    # LOD
    ("LOD_FIXED",      r"\{\s*FIXED\b"),
    ("LOD_INCLUDE",    r"\{\s*INCLUDE\b"),
    ("LOD_EXCLUDE",    r"\{\s*EXCLUDE\b"),
    # Table calcs (window / running)
    ("WINDOW_AGG",     r"\bWINDOW_(SUM|AVG|MIN|MAX|COUNT|STDEV|VAR)\b"),
    ("RUNNING_AGG",    r"\bRUNNING_(SUM|AVG|MIN|MAX|COUNT)\b"),
    ("LOOKUP",         r"\bLOOKUP\s*\("),
    ("PREVIOUS_VALUE", r"\bPREVIOUS_VALUE\s*\("),
    ("RANK",           r"\bRANK\s*\(|\bINDEX\s*\("),
    ("PERCENTILE",     r"\bPERCENTILE\s*\("),
    ("FIRST_LAST",     r"\bFIRST\s*\(|\bLAST\s*\("),
    # Aggregations (only count if NO higher-level construct already matched)
    ("AGG_ATTR",       r"\bATTR\s*\("),
    ("AGG_BASIC",      r"\b(SUM|AVG|COUNT|COUNTD|MIN|MAX|MEDIAN|STDEV|VAR)\s*\("),
    # Date functions
    ("DATE_FN",        r"\b(DATEPART|DATETRUNC|DATEDIFF|DATEADD|DATENAME|DATEPARSE|MAKEDATE|MAKETIME|MAKEDATETIME|YEAR|QUARTER|MONTH|DAY|WEEK|WEEKDAY|HOUR|MINUTE|SECOND|TODAY|NOW)\s*\("),
    # Logical
    ("BLOCK_CASE",     r"\bCASE\b[\s\S]+?\bEND\b"),
    ("BLOCK_IF",       r"\bIF\b[\s\S]+?\bTHEN\b[\s\S]+?\bEND\b"),
    ("FN_IIF",         r"\bIIF\s*\("),
    ("FN_IF",          r"\bIF\s*\("),
    # String
    ("STRING_FN",      r"\b(LEFT|RIGHT|MID|LEN|UPPER|LOWER|TRIM|LTRIM|RTRIM|CONTAINS|REGEXP_MATCH|REGEXP_EXTRACT|REGEXP_REPLACE|SPLIT|STARTSWITH|ENDSWITH|REPLACE|SUBSTRING)\s*\("),
    # Type conversions
    ("TYPE_CAST",      r"\b(STR|INT|FLOAT|DATE|DATETIME|BOOL)\s*\("),
    # Group / bin (rare in formulas — usually in <calculation class='group'>)
    ("GROUP",          r"<calculation\s+class\s*=\s*['\"]group"),
    # Plain ref / literal / arithmetic
    ("REF_ONLY",       r"^\s*\[[^\]]+\]\s*$"),
    ("LITERAL_ONLY",   r"^\s*('[^']*'|\"[^\"]*\"|-?\d+(\.\d+)?)\s*$"),
]


def classify(formula: str) -> Tuple[str, List[str]]:
    """Return ``(primary_bucket, all_matching_buckets)`` for a formula."""
    matches: List[str] = []
    for name, pat in PATTERN_TOKENS:
        if re.search(pat, formula, re.IGNORECASE):
            matches.append(name)
    if not matches:
        return ("OTHER", [])
    return (matches[0], matches)


def extract_twb_to_temp(twbx_path: Path, tmp: Path) -> Optional[Path]:
    """Unzip a ``.twbx`` and return the path of the inner ``.twb``."""
    try:
        with zipfile.ZipFile(twbx_path, "r") as z:
            for name in z.namelist():
                if name.lower().endswith(".twb"):
                    z.extract(name, tmp)
                    return tmp / name
    except zipfile.BadZipFile:
        return None
    return None


def calc_fields_from_ds(ds: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return columns flagged as calculated."""
    return [
        c for c in (ds.get("columns") or [])
        if c.get("isCalc") and (c.get("formula") or "").strip()
    ]


def build_field_to_pbi(model: SemanticModel, ds_name: str) -> Dict[str, Tuple[str, str]]:
    """Best-effort field-to-pbi mapping for one ds: walk col_locator and
    pick the first candidate for each Tableau name."""
    out: Dict[str, Tuple[str, str]] = {}
    for (d, name), cands in (getattr(model, "col_locator", {}) or {}).items():
        if d != ds_name or not cands:
            continue
        out.setdefault(name, cands[0])
    return out


def analyze_workbook(twbx_path: Path) -> List[Dict[str, Any]]:
    """Return one record per calc field found in this workbook."""
    records: List[Dict[str, Any]] = []
    workbook_name = twbx_path.stem
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        if twbx_path.suffix.lower() == ".twbx":
            twb = extract_twb_to_temp(twbx_path, tmp)
            if twb is None:
                return records
        else:
            twb = twbx_path

        parser = TWBParser(str(twb))
        try:
            parser.parse()
        except Exception:
            print(f"[WARN] parse failed for {workbook_name}: "
                  f"{traceback.format_exc(limit=2).strip()}")
            return records

        try:
            model = SemanticModel(
                parser.datasources,
                parameters=parser.parameters,
                worksheets=parser.worksheets,
            )
            model.build()
        except Exception:
            print(f"[WARN] model.build failed for {workbook_name}; "
                  f"emitting formulas with empty resolution context")
            model = None

        param_refs: set = set()
        if model is not None:
            param_refs = getattr(model, "_parameter_refs", set()) or set()

        for ds in parser.datasources:
            calcs = calc_fields_from_ds(ds)
            if not calcs:
                continue
            ds_name = ds.get("name", "")
            ds_caption = ds.get("caption", ds_name)
            cols = ds.get("columns") or []
            aliases = ds.get("columnAliases", {}) or {}

            target_table = ""
            if model is not None:
                for t in model.tables:
                    if t.get("datasource") == ds_name:
                        target_table = t.get("name", "")
                        break

            field_to_pbi: Dict[str, Tuple[str, str]] = (
                build_field_to_pbi(model, ds_name) if model else {}
            )

            measure_refs = {
                (target_table, (c.get("caption") or c.get("name") or "").strip())
                for c in calcs
                if (c.get("caption") or c.get("name"))
            }

            for col in calcs:
                formula = col.get("formula", "")
                name = col.get("name", "")
                caption = col.get("caption", "")
                role = col.get("role", "")
                tmdl_type = col.get("tmdlType", "")
                primary, all_pats = classify(formula)
                try:
                    dax = translate_tableau_to_dax(
                        formula, target_table or ds_name, aliases, cols,
                        field_to_pbi=field_to_pbi,
                        parameter_refs=param_refs,
                        measure_refs=measure_refs,
                    )
                    err = None
                except Exception as e:
                    dax = None
                    err = f"{type(e).__name__}: {e}"

                records.append({
                    "workbook": workbook_name,
                    "datasource": ds_caption or ds_name,
                    "ds_name": ds_name,
                    "table": target_table,
                    "name": name,
                    "caption": caption,
                    "role": role,
                    "tmdlType": tmdl_type,
                    "formula": formula.strip(),
                    "dax": dax,
                    "translator_error": err,
                    "primary_bucket": primary,
                    "all_buckets": all_pats,
                    "dropped": dax is None,
                })
    return records


def write_jsonl(records: List[Dict[str, Any]], path: Path) -> None:
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def write_report(records: List[Dict[str, Any]], path: Path) -> None:
    by_bucket: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in records:
        by_bucket[r["primary_bucket"]].append(r)

    total = len(records)
    dropped = sum(1 for r in records if r["dropped"])
    by_book = Counter(r["workbook"] for r in records)
    book_drops = Counter(r["workbook"] for r in records if r["dropped"])

    lines: List[str] = []
    lines.append("# Calc-field corpus analysis\n")
    lines.append(f"- **Total calc fields:** {total}\n")
    lines.append(f"- **Translator drops (returned `None`):** {dropped} "
                 f"({100 * dropped / max(total, 1):.1f}%)\n")
    lines.append(f"- **Workbooks scanned:** {len(by_book)}\n")
    lines.append("\n")

    lines.append("## Per-workbook calc field counts\n\n")
    lines.append("| Workbook | Calc fields | Drops |\n")
    lines.append("|---|--:|--:|\n")
    for wb, n in by_book.most_common():
        lines.append(f"| {wb} | {n} | {book_drops[wb]} |\n")
    lines.append("\n")

    lines.append("## Per-bucket success rate\n\n")
    lines.append("| Primary bucket | Count | Drops | Drop rate |\n")
    lines.append("|---|--:|--:|--:|\n")
    for bucket, rs in sorted(by_bucket.items(),
                             key=lambda kv: (-len(kv[1]), kv[0])):
        n = len(rs)
        nd = sum(1 for r in rs if r["dropped"])
        rate = 100 * nd / max(n, 1)
        lines.append(f"| `{bucket}` | {n} | {nd} | {rate:.0f}% |\n")
    lines.append("\n")

    lines.append("## Examples by bucket\n\n")
    for bucket, rs in sorted(by_bucket.items(),
                             key=lambda kv: (-len(kv[1]), kv[0])):
        lines.append(f"### `{bucket}` ({len(rs)} fields)\n\n")
        ok = [r for r in rs if not r["dropped"]][:3]
        bad = [r for r in rs if r["dropped"]][:3]
        if ok:
            lines.append("**Translated cleanly:**\n\n")
            for r in ok:
                lines.append(f"- *{r['workbook']}* — `{r['caption'] or r['name']}` "
                             f"({r['role']}/{r['tmdlType']})\n")
                lines.append(f"  - Tableau: `{r['formula']}`\n")
                lines.append(f"  - DAX: `{r['dax']}`\n")
        if bad:
            lines.append("\n**Dropped (translator returned `None`):**\n\n")
            for r in bad:
                lines.append(f"- *{r['workbook']}* — `{r['caption'] or r['name']}` "
                             f"({r['role']}/{r['tmdlType']})\n")
                lines.append(f"  - Tableau: `{r['formula']}`\n")
                if r["translator_error"]:
                    lines.append(f"  - Error: `{r['translator_error']}`\n")
        lines.append("\n")

    sec_counter: Counter = Counter()
    for r in records:
        for p in r["all_buckets"][1:]:
            sec_counter[p] += 1
    if sec_counter:
        lines.append("## Co-occurring patterns (secondary buckets)\n\n")
        lines.append("| Bucket | Count |\n|---|--:|\n")
        for p, n in sec_counter.most_common():
            lines.append(f"| `{p}` | {n} |\n")
        lines.append("\n")

    path.write_text("".join(lines), encoding="utf-8")


def run(corpus_dir: Optional[Path] = None) -> int:
    """Run the analyzer; write ``corpus.jsonl`` + ``patterns.md`` into
    this module's directory. ``corpus_dir`` defaults to
    ``../Sample Dashboards`` (relative to the repo root)."""
    corpus_dir = corpus_dir or DEFAULT_CORPUS_DIR
    if not corpus_dir.exists():
        print(f"Corpus dir not found: {corpus_dir}")
        return 1
    twbx_files = sorted(corpus_dir.glob("*.twbx"))
    print(f"Found {len(twbx_files)} workbooks in {corpus_dir}")
    all_records: List[Dict[str, Any]] = []
    for tf in twbx_files:
        print(f"  {tf.name} ...", end=" ", flush=True)
        try:
            recs = analyze_workbook(tf)
            print(f"{len(recs)} calc field(s)")
            all_records.extend(recs)
        except Exception as e:
            print(f"FAILED: {e}")
            traceback.print_exc()

    write_jsonl(all_records, OUT_JSONL)
    write_report(all_records, OUT_MD)
    print(f"\nWrote {OUT_JSONL} ({len(all_records)} records)")
    print(f"Wrote {OUT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
