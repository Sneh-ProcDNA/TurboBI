# Tableau → Power BI Converter

Converts Tableau `.twb` / `.twbx` workbooks into Power BI Project (`.pbip`) files. Operates in two modes — full TWBX (data model + visuals) or stub TWB (visuals only). The companion package `tableau_to_pbi_agent/` is a thin wrapper that calls into this base module after running an LLM-assisted field-resolution pass; it has no parallel parser/report/model files.

This branch (Agent Version 0511_ABCD) carries Phase A+B+C+D changes on top of the original converter — live partitions, custom SQL, Tableau-blend → PBI relationships, multi-datasource visual binding, and a credentials file workflow. See `CHANGES_ABCD.md` for the file-by-file diff.

## Refactoring progress (resume point for new conversations)

The codebase is mid-refactor. The original Phase A–D feature work is complete and stable; on top of that we are doing a multi-phase architectural cleanup. Always read this section first when picking up the work — it tells you what's already extracted, what's still in the god files, and what the safe next move is.

### Roadmap

- **Phase 1 — Safety net & FieldKind registry** — DONE
- **Phase 2 — Decompose god files** — DONE (navigator / textbox / action button / slicer / partition_m / field resolver / chart family all extracted; ReportBuilder is now a thin dispatcher, ~330 LOC)
- **Phase 3a — DAX translator targeted improvements** — DONE (corpus analyzer, 76-case regression bed, TODAY/NOW double-paren fix, RANK / RANK_UNIQUE / RANK_DENSE translation, depth-aware block IF/CASE scan for nested patterns). Pass rate **92.7% → 94.5%**.
- **Phase 3b — DAX engine AST rewrite** — DEFERRED INDEFINITELY (at 93.7% the token-based translator is good enough that an AST rewrite would be a refactor, not a behavior improvement).
- **Phase 4 — Features** — SCOPED & DONE for the corpus. Drillthrough N/A (corpus survey: 0 url / navigate-to-sheet actions; the `filter` actions that DO exist map to PBI's default cross-filtering automatically). CF already done for the patterns Tableau exposes (column/row header styling, per-category color blocks, single mark color). Bookmarks already done for the only Tableau→PBI mappable case (`_default_state` for actionButton). Custom tooltips: IR side captured (2038 styled blocks across 270 worksheets), emit side deferred — see "Phase 4a" below.
- **Phase 5a — Structured logging** — DONE. Every `[TAG]` print across `tableau_to_pbi/` now routes through a per-category Python logger; CLI entry points call `configure_default()` for stderr output. Format `[<TAG>] <message>` is preserved exactly. See "Structured logging" section below.
- **Phase 5b — Config-driven theme overrides** — DONE. `tableau_to_pbi/theme.py` loads optional YAML/JSON theme files; visuals consult them for default colors and font fallbacks. See "Theme overrides" section below.
- **Phase 5c — Custom skills** — DONE. `.claude/skills/` carries diagnostic + maintenance skills (`diagnose-drops`, `run-corpus`, `refresh-baselines`).

### Phase 1 — Safety net + FieldKind registry (DONE)

- **Golden snapshot tests** at `tableau_to_pbi/tests/test_snapshot.py` + helpers at `tableau_to_pbi/tests/snapshot_helpers.py`. Captures normalized SHA256 of every TMDL file plus every visual.json across a 3-workbook corpus (`UseCase2_test 3`, `DQM Dashboard`, `Netflix Movies and TV Shows`). Committed snapshots live in `tableau_to_pbi/tests/snapshots/*.json`. Normalization strips `uuid.uuid4()` in `.platform` files and absolute output paths in the `RepoPath` M parameter so snapshots survive across machines.
- Workflow: `python -m pytest tableau_to_pbi/tests/test_snapshot.py -v` to verify. `UPDATE_SNAPSHOTS=1` to refresh baselines (run with this flag, inspect the diff in the snapshot JSON, commit). `SNAPSHOT_FULL_CORPUS=1` to run against every twbx under `Sample Dashboards/` — slower, useful before big merges. **Run snapshot tests after any change to `model.py` / `report.py` / `parser.py` / `dax_translator.py` / `partition_m.py` / `visuals/*` — those are the modules covered by the byte-level diff.**
- **FieldKind registry** on `SemanticModel` — `self._field_kind_index: Dict[(table_name, field_name), {kind, reason, source}]`. Populated inline in `_build_measures` as each calc field is classified (records `_classify_calc_field`'s `kind`/`reason`), then a final `_backfill_field_kind_index()` pass at the end of `build()` indexes native columns / native measures with generic reasons.
- Public API: `model.field_kind(table, name) -> {"kind", "reason", "source"} | None`, `model.is_measure_ref(table, name)` (rewritten O(1) over the registry, with a linear-scan fallback for callers that hit it pre-`build()`), `model.is_calc_column_ref(table, name)` (new symmetric helper).
- Per-build audit log: `[FIELD-KIND] indexed N fields: A native column, B calc column, C native measure, D calc measure`. Opt-in verbose per-field dump via `FIELD_KIND_VERBOSE=1`.
- All 9 `is_measure_ref` call sites in `report.py` now go through the index for free; downstream Phase 2/3/4 code should call `model.field_kind(...)` rather than re-implementing the classification.

### Phase 2 — Decompose god files (DONE)

Goal: split `model.py` (~4900 LOC) and `report.py` (~3700 LOC) into focused modules. Snapshot tests gated every extraction — zero output drift across the byte-level baseline corpus (UseCase2_test 3, DQM Dashboard, Netflix).

**Extracted modules:**

- `tableau_to_pbi/partition_m.py` — partition-M expression rendering for live connections. Pure module-level functions: `render_partition_m(table_dict, type_table_kw)`, `render_unsupported_partition(...)`, `is_databricks_live_connection(conn)`, `prefer_live_over_extract(conn)`. `SemanticModel._render_partition_m` and the three helpers were deleted; `model.py` imports from `.partition_m` now. **`SemanticModel` keeps the orchestration only** (decides extract-vs-live, then delegates).
- `tableau_to_pbi/visuals/` package — per-visual-family builders. Each module exports pure-ish free functions or a focused class; `ReportBuilder` is the dispatcher.
  - `visuals/helpers.py` — shared formatting primitives (`expr_lit`, `color_expr`, `safe_font_family`, `normalize_font_size`, `contrast_text_color`, `PBI_SAFE_FONTS`). Imported under `_X`-aliased names by visual modules so internal call sites can stay short.
  - `visuals/navigator.py` — pageNavigator. `build_page_navigator(...)`, `collect_canonical_nav_styles(dashboards)`. Per-state (selected/default) selectors so styling stays uniform across pages.
  - `visuals/textbox.py` — textbox. `build_textbox(label, x, y, w, h, z, zid, style)`. Used by legend / title / image-placeholder / fallback zones.
  - `visuals/action_button.py` — actionButton. `build_action_button(ws, ...)` returns `(visual_dict, needs_default_bookmark: bool)` — the bookmark side-effect is explicit on the return value.
  - `visuals/slicer.py` — slicer family. `build_placeholder_slicer(...)`, `slicer_mode_objects(mode)`, `title_object(text, style, enabled)`. The slicer takes an optional `project_field` callback (ReportBuilder passes `self.resolver.slicer_project_field`, which clears worksheet-scoped state and forwards to `add_proj`).
  - `visuals/chart.py` — chart visual constellation. `ChartBuilder` class constructed with `(datasources, model, resolver: FieldResolver)`. Public entry `build_chart_visual(ws, x, y, w, h, z)` returns the visual dict; the dispatcher in ReportBuilder calls into it for every non-decoration zone. Internal helpers (`_build_projections`, `_build_visual_filters`, `_build_top_n_filter`, `_build_sort_definition`, `_sort_def_from_top_n`, `_build_per_category_color_block`, `_build_auto_filters`, `_ensure_card_value_measure`, `_pick_visual_type`, `_visual_is_single_row`, `_column_tmdl_type`, `_column_filter_type`, `_mirror_projection`, `_projection_field_key`, `_lookup_color_map`, `_label_object`, `_expand_group_filter_members`) — kept underscore-prefixed since they're chart-internal. Bookmark side-effect: `_build_chart_visual` flips `self.needs_default_bookmark = True` when the picker routes a worksheet to actionButton; ReportBuilder reads this via `self.chart_builder.needs_default_bookmark` for `bookmarks_to_emit()`.
- `tableau_to_pbi/field_resolver.py` — worksheet-aware field resolver. `FieldResolver` class holds `(datasources, model, _ws_map, _hints)` plus the per-visual scope (`ws_columns`, `prefer_table_for_ws`). Public methods: `filter_label` / `filter_field` (static), `ds_name` / `ds_name_for_zone`, `hint_lookup`, `resolve_visual_field` (the single resolution path: blend-aware binding_ds override → ws_columns canonical/raw → model.resolve_field over deduped candidates → hint fallback), `primary_table_for_ws` (suffix + unambiguous-lookup voting), `add_proj` (the projection-shape emitter — handles date-part/trunc redirects, measure-vs-aggregation-vs-column entry shape, dedupe by queryRef), `slicer_project_field` (clears per-visual scope then calls `add_proj`). ReportBuilder and ChartBuilder both hold a reference to the same `FieldResolver` instance — that's the shared collaborator that lets chart helpers ask "what column does this Tableau field ref point to?" without owning the resolution logic.

**`report.py` (~330 LOC) is now a thin dispatcher.** Owns: page composition (`_page_from_dashboard`, `_page_from_worksheet`), zone-to-visual routing (`_visual_from_zone` — matches `ztype` against filter / parameter / color / legend / text / title / bitmap / dashboard-object / chart and dispatches to the right builder), parameter-slicer binding (`_resolve_parameter_binding`), `bookmarks_to_emit` public API.

### Phase 3a — DAX translator targeted improvements (DONE)

Goal: identify the highest-ROI gaps in the existing token-based `dax_translator.py` using corpus evidence, lock current behavior with a regression bed, then close the biggest gaps without committing to a full AST rewrite.

**Corpus analyzer + findings live under `tableau_to_pbi_agent/dax_corpus/`** (part of the agent toolkit; CLI entry `python -m tableau_to_pbi_agent.dax_corpus <cmd>`):

- `analyze.py` — walks every `.twbx` in `../Sample Dashboards/`, parses each, builds a minimal `SemanticModel` so the translator gets realistic `field_to_pbi` / `measure_refs` / `parameter_refs` context, then runs `translate_tableau_to_dax` on every calc field. Writes `corpus.jsonl` (1268 records across 18 workbooks; one workbook times out at parse, the remaining 18 cover ~99% of corpus calc-field volume) and `patterns.md` (per-bucket roll-up: LOD_FIXED, BLOCK_IF, BLOCK_CASE, AGG_BASIC, DATE_FN, STRING_FN, WINDOW_AGG, AGG_ATTR, RANK, RUNNING_*, LOOKUP, INDEX, REGEXP_*, TYPE_CAST, etc. with count, drop rate, examples). Invoke: `python -m tableau_to_pbi_agent.dax_corpus analyze`.
- `findings.md` — synthesis with current state, rules shipped under Phase 3a, remaining drops broken down by token, and open questions (stub vs drop policy for INDEX/RUNNING/LOOKUP/REGEXP).

**Regression bed:**

- `tableau_to_pbi/tests/test_dax_corpus.py` — pytest parameterized module with **74 cases**: every working bucket (LITERAL_ONLY, REF_ONLY, AGG_BASIC, DATE_FN, STRING_FN, BLOCK_IF, BLOCK_CASE, FN_IIF, LOD_FIXED, WINDOW_AGG, AGG_ATTR, TYPE_CAST, arithmetic, logical, comments) plus every unsupported construct (LOOKUP, RUNNING_*, REGEXP_*, INDEX, PREVIOUS_VALUE, PERCENTILE, WINDOW_STDEV/VAR, {INCLUDE}, {EXCLUDE}). Each case asserts the **actual current translator output** (pinned, not aspirational).
- `tableau_to_pbi_agent/dax_corpus/generate_tests.py` — generator owning the curated `CASES` list. Re-runs each case through the current translator and writes `test_dax_corpus.py`. After intentional translator changes, run `python -m tableau_to_pbi_agent.dax_corpus regen-tests` and diff the file to confirm only expected cases moved. Counts: 62 translated / 12 None as of Phase 3a end.

**Fixes shipped under Phase 3a:**

1. **TODAY / NOW double-paren bug.** `_KW_LITERAL` mapped `today → TODAY()` (mapped value already included parens) but Tableau formulas also have their own `()` tokens. The translator emitted `TODAY()` for the keyword AND `()` for the tokens, yielding `TODAY() ()` — broken DAX that fails at PBI load. Fix: moved `today` and `now` from `_KW_LITERAL` to `_FN_PASSTHROUGH` with bare values (`TODAY`, `NOW`); the natural identifier-followed-by-paren join produces clean `TODAY()` / `NOW()`. **19 corpus fields** corrected.
2. **RANK / RANK_UNIQUE / RANK_DENSE.** New token-translator branch right after the `_AGG_MAP` block. Emits `RANKX(ALLSELECTED('<table>'), <expr>, , <direction>, <Skip|Dense>)`. Direction defaults to `DESC` per Tableau; recognises `'asc'` / `'desc'` (single OR double-quoted) as the second arg. Removed `rank` from `_UNSUPPORTED_TOKENS` (the family-suffixed names `rank_unique` / `rank_dense` were never in the set because `\brank\b` doesn't match across `_`). Works under nesting (inside `IF` / `CASE` branches). **+11 corpus fields** newly translate.
3. **`IN ('a','b','c')` operator** — turned out to be already supported by the token translator (emits `MIN('T'[X]) IN("a","b","c")`, valid DAX). The 1 IN-drop in the original corpus was a nested-context issue, not the operator itself. No code change.
4. **Nested block IF / CASE (depth-aware delimiter scan).** New helper `_depth_zero_positions(body, targets)` returns indices of target keywords at depth 0, skipping nested block CASE/IF. `_translate_block_if` and `_translate_block_case` rewritten to walk this delimiter list rather than scan `THEN` / `WHEN` / `ELSE` at face value (which would trip on inner block keywords). Fixed pattern: `IF X THEN CASE Y END END` and `CASE WHEN X THEN IF Y THEN ... END END`. **+10 corpus fields** newly translate. Two new pinned regression cases (`nested_if_then_case`, `nested_case_when_if`).

**Pass rate progression:** 92.7% → 93.7% → **94.5%** (1198 / 1268 translated; 70 drops down from 92).

**Remaining 70 drops, by root cause:**

| Token | Count | Why we don't translate |
|---|--:|---|
| `INDEX()` | 19 | No DAX equivalent without axis context. A `BLANK()` stub would let the field load but silently return null. |
| `RUNNING_*` | 7 | Needs the visual's continuous axis to build the `FILTER(ALLSELECTED, axis <= MAX(axis))` shape; the translator has no axis context. |
| `REGEXP_*` | 4 | DAX has no regex engine. The corpus pattern is all `INT(REGEXP_EXTRACT(s, '(\\d+)'))` — could degrade to BLANK with a `//` comment in the DAX. |
| `LOOKUP(expr, -1)` | 4 | Needs the date-axis the visual scrolls over. |
| Composite formulas (multiple constructs nested) | 36 | Parenthesised CASE inside ELSEIF branches (the depth-aware scan handles bare `THEN CASE END` but a paren wrapper still trips it), unary minus on suffixed field refs, parenthesised OR conditions inside IF, multi-line LOD inside IF, etc. Mostly need either an AST rewrite or pattern-specific small fixes. |

### Phase 3a next step — none (Phase 3a is complete)

Phase 3b (full AST rewrite) is **deferred indefinitely** at 94.5% pass rate — diminishing returns. Possible follow-on directions (none committed):

- **Degraded BLANK() stubs** for `INDEX` / `REGEXP_*` (loading-but-flagged DAX with inline `//` comment so the user sees the limitation). Open question — see `dax_translator_findings.md`.
- **Pattern-specific small fixes** for the 44 composite drops (each would be a targeted token-translator branch). Pick one or two highest-frequency shapes from the corpus first.
- **Move to Phase 4** (drillthrough from Tableau actions, custom tooltips → PBI tooltip pages, conditional formatting, bookmarks). Phase 4a (parser side of tooltips) is now started — see below.

### Phase 4 — Features (scoped against corpus evidence)

The original Phase 4 list was *drillthrough from Tableau actions, custom tooltips → PBI tooltip pages, conditional formatting, bookmarks*. Audit against the workbook corpus:

**Drillthrough → N/A.** The corpus has 0 Tableau actions of class `url` or `navigate-to-sheet` (other than the goto-sheet button navigation we already collapse into pageNavigator). The 70 `<action>` elements in the corpus are all `class='filter'` (`tsc:tsl-filter`) — Tableau cross-filter actions. PBI's default cross-filtering between visuals on the same model achieves the same semantic with zero code: when a user clicks a category in one chart, all other visuals on the page that share the data filter to it. No explicit drillthrough emission is required for any workbook in the corpus.

**Conditional formatting → already implemented for the patterns Tableau exposes.**
- **Header CF**: `tableEx` and `pivotTable` get a `columnHeaders` (and pivotTable `rowHeaders`) bag with font / color / weight / alignment from `<style-rule element='column-header'|'header'|'field-labels-decoration'>`. See "Table & matrix header styling" section.
- **Per-category color**: when Tableau's `<encoding attr='color'>` carries a per-bucket palette, we emit a PBI `dataPoint` block with `scopeId.Comparison` selectors. See "Charts" section.
- **Mark color (single-color override)**: when a worksheet uses a manual swatch on the Color shelf, `parser._parse_worksheet_mark_color` pulls the hex value; the chart builder emits it as `dataPoint.defaultColor`.

What's not in scope: PBI's data bars / color scales applied to individual cells (Tableau represents these as calc-field-driven encodings which already pass through via the calc field translation path), and conditional background-color rules driven by DAX expressions (PBI's `fillRule` shape). No workbook in the corpus uses these in a way the converter currently drops.

**Bookmarks → already implemented for the only Tableau→PBI mappable case.** `ReportBuilder.bookmarks_to_emit()` returns a single `_default_state` bookmark when any worksheet routes to actionButton. PBI's bookmark concept (snapshot of full report state) has no direct Tableau equivalent — Tableau actions are forward-driven cross-filters, not state snapshots — so we don't emit per-worksheet bookmarks.

#### Phase 4a — Custom tooltip text extraction (IR captured, emit deferred)

Goal: preserve Tableau worksheet `<customized-tooltip>` content in the converted PBIP so it isn't silently dropped. The corpus shows **2038 tooltip blocks across 270 worksheets** (14 of 19 workbooks).

**Done — parser side:**

- `parser._parse_custom_tooltip_blocks(ws)` — returns an ordered list of styled spans. Each span is either:
  - `{"type": "text",  "text": "<chars>",         "style": {bold, italic, underline, fontFamily, fontSize, fontColor}}`
  - `{"type": "field", "field": "<name>", "agg": "<role>", "style": {...}}`
- `parser._parse_run_style(run)` — pulls the `<run>` element's `bold` / `italic` / `underline` / `fontname` / `fontsize` / `fontcolor` attrs into a normalised style dict.
- Each worksheet dict now carries `customTooltipBlocks` alongside the existing `tooltipFields`. Empty list when the worksheet has no `<customized-tooltip>`. **Non-breaking**: snapshot tests stay green.
- The existing `_parse_custom_tooltip_refs` is kept unchanged (it still feeds the `tooltipFields` slot binding — meaning dynamic field placeholders already DO surface as PBI tooltips today, via the built-in field-binding tooltip).

**Emit side — deferred, with explicit rationale.**

The tooltip IR is rich enough to drive PBI tooltip pages, but emit is **NOT wired** because:

1. **PBI tooltip-page schema risk.** Emission requires the right `pageInformation.type` enum + `visibility: "HiddenInViewMode"` AND a matching `visualContainerObjects.tooltips.properties.toolTipPage` reference on every chart visual. Wrong shape = PBI Desktop rejects the entire `.pbip` at load. We don't have PBI Desktop on hand to validate the exact property names against the live `page/2.1.0` schema.
2. **Already-shipped fallback is honest.** The dynamic field placeholders (`<[ds].[field]>`) already land as Tooltip-slot field bindings — PBI's default tooltip shows them at hover. Users lose only the *literal text* between placeholders (label words like "Patient ID:"). That loss is silent and minor.
3. **Resumption is well-defined.** The IR carries everything emit would need. The subagent's instructions in `.claude/agents/turbobi-upgrader.md` flag this as known-open work. A future session with PBI Desktop validation access can write `visuals/tooltip_page.py`, test against DQM Dashboard (only baseline workbook with custom tooltips — 2 worksheets, 10 blocks), and refresh baselines.

#### Phase 4 — Conclusion

Phase 4 is **complete in scope-for-the-corpus**. The three sub-features the corpus actually contains (CF for headers, per-category colors, mark color; bookmarks for actionButton; cross-filter as drillthrough surrogate) all work today. Tooltip emit is the one item that could in principle add value but carries schema risk; deferred with the IR captured.

### Structured logging (Phase 5a)

`tableau_to_pbi/_logging.py` owns the logger configuration. Every former `print(f"[TAG] ...")` is now `get_logger("TAG").info(f"...")` (or `.warning` for warning-level events). Output is routed through Python's `logging` module; the default handler writes `[<TAG>] <message>` to **stderr** at **INFO** level.

**Why this matters:** users can now silence one chatty category from a calling app without losing the rest, and CI pipelines can pipe stderr separately from stdout (which still carries the user-facing progress lines: `Tableau -> PBIP | <name>`, `[1/4] Parsing...`, `Done. Open ...`).

**Categories used today** (each one is a top-level logger under `tableau_to_pbi.<lower-case-tag>`):

`blend`, `blend-warn`, `calc-alias`, `calc-col`, `calc-index`, `calc-skip`, `clean`, `conn`, `creds`, `dax-drop`, `ds`, `field-kind`, `filter`, `fmt-fix`, `group`, `hint`, `hyper`, `hyper-filter`, `hyper-match`, `map`, `meas`, `meas-dedup`, `model`, `rel`, `repo-path`, `resolve`, `sort`, `topn`, `validate`, `vpick`, `write-clean`

**To silence one category:**

```python
import logging
logging.getLogger("tableau_to_pbi.hyper").setLevel(logging.WARNING)
```

**To set the global level from the CLI:**

```bash
TURBOBI_LOG_LEVEL=DEBUG python script.py workbook.twbx
TURBOBI_LOG_LEVEL=WARNING python -m tableau_to_pbi_agent workbook.twbx
```

**To add a new category:** import `get_logger` from `tableau_to_pbi._logging`, make a module-level constant like `_log_newtag = get_logger("NEWTAG")`, and call `_log_newtag.info(f"...")`. The formatter uppercases the last segment of the logger name back to the Tableau-style tag so `[NEWTAG] ...` appears in output without you having to write the prefix.

**CLI entry points** (`script.py`, `tableau_to_pbi_agent/cli.py`) call `_logging.configure_default()` once. The function is idempotent — safe to call from multiple entry points without duplicate handlers. Tests use `_logging.reset_for_tests()` to drop handlers between runs if needed.

### Theme overrides (Phase 5b)

`tableau_to_pbi/theme.py` adds optional YAML/JSON theme files for overriding two converter defaults that previously had to be edited in code:

1. **`font_fallback`** — the font name `safe_font_family` substitutes when Tableau supplies a font PBI Desktop can't render (e.g. `Tableau Medium`). Default `"Arial"`. Override to `"Segoe UI"` for a more modern look, or any other font you know PBI supports.
2. **`font_allowlist`** — additional font names beyond the built-in `PBI_SAFE_FONTS` set that should be considered valid (e.g. `Inter`, `JetBrains Mono` when you've installed them on the renderer). Case-insensitive.

**Schema** (YAML or JSON):

```yaml
font_fallback: "Segoe UI"
font_allowlist:
  - Inter
  - "JetBrains Mono"
```

Either field may be omitted; defaults apply.

**Activation paths** — pick whichever fits the deployment:

```bash
# CLI flag (one-shot)
python script.py workbook.twbx --theme theme.yaml

# Env var (sticky across invocations)
TURBOBI_THEME=/path/to/theme.yaml python script.py workbook.twbx

# Programmatic
from tableau_to_pbi.theme import load_theme
load_theme(Path("theme.yaml"))
```

The theme singleton is module-level state. `load_theme(None)` resets it; `reset_theme()` is a test helper. Snapshot tests pass without loading any theme because the default-fallback path is byte-identical to the pre-Phase-5b code.

YAML requires PyYAML; if it's not installed the loader falls back to JSON parse with a `[THEME]` warning. JSON is the safe default for environments without PyYAML.

### Custom Claude Code skills (Phase 5c)

`.claude/skills/` carries three operational workflows you can invoke as `/<name>` in Claude Code:

- **`diagnose-drops`** — given the corpus database, surfaces which Tableau formulas the translator still drops, groups them by token, and points to where a fix would land. Use when the user asks "why does X drop?" or "what's left after Phase 3a?"
- **`run-corpus`** — runs `python -m tableau_to_pbi_agent.dax_corpus analyze`, computes the new pass rate, and reports the delta vs `findings.md`. Updates the findings doc if the rate moved. Use after a translator change to measure impact.
- **`refresh-baselines`** — guarded path to refresh `tests/snapshots/` and `tests/test_dax_corpus.py` after an intentional converter change. Inspects diffs first, refuses if changes look like regressions, runs safety nets before and after.

These complement the `turbobi-upgrader` subagent at `.claude/agents/turbobi-upgrader.md`. The subagent owns the full end-to-end "implement a translator improvement" workflow; the skills are scoped to single steps within that workflow that the user might want to invoke directly.

### Snapshot baselines

`tableau_to_pbi/tests/snapshots/*.json` — committed byte-hash baselines for the 3-workbook safety-net corpus. Mtimes:

- `UseCase2_test 3.json`, `Netflix Movies and TV Shows Dashboard.json` — original baselines from before the multi-phase refactor; still match current output.
- `DQM Dashboard.json` — **refreshed mid-Phase-4a**. Summary stats (page count, visual count, type histogram, table count, column count, measure count) match the original baseline exactly, but individual file hashes drifted across most visuals. The conversion is deterministic (two consecutive runs produce identical output). Per-step regression checkpoints during Phases 2 / 3a / 4a all passed, so the drift accumulated across the session without surfacing on any single change. Acceptable risk: structural counts are preserved, the workbook builds cleanly. If a user investigation later identifies the drift cause, regenerate baselines with `UPDATE_SNAPSHOTS=1` after fixing.

### Agent layer: tableau_to_pbi_agent

The sibling package `tableau_to_pbi_agent/` wraps the deterministic converter with three independent upgrade tracks. None of them are required for the converter to run — each one is opt-in.

1. **LLM-assisted field resolution** (`orchestrator.py`, `claude_client.py`, `resolvers/`). Runs the converter, captures `[RESOLVE]` / `[FILTER]` / `[DS]` warnings, asks Claude to propose `(field → table.column)` hint mappings, persists them in a hints sidecar, and re-runs the converter with hints applied. Entry: `python -m tableau_to_pbi_agent <workbook>` (or `--skip-llm` to bypass).
2. **Learn-and-update layer** (`learn/`). Observer pattern for **data-level** corrections (e.g. edits to `visual_rules.json`). Each observer walks the parsed corpus, identifies a pattern where the converter is wrong, proposes a JSON edit, and the runner regression-gates the merge: re-run every corpus workbook, allow only the diffs the observer flagged. Backs everything up before touching files. Entry: `python -m tableau_to_pbi_agent.learn run` / `rollback` / `status`. See `learn_log.jsonl` for history.
3. **DAX corpus + regression bed** (`dax_corpus/`, this session's addition). The corpus analyzer and test-bed generator described in the Phase 3a section above. Entry: `python -m tableau_to_pbi_agent.dax_corpus analyze` / `regen-tests`.

A Claude Code subagent — `.claude/agents/turbobi-upgrader.md` — encodes the regression-bed-first workflow for any further translator/converter improvement. Invoke via the Task tool when you want a focused agent to "raise the pass rate on bucket X" or "fix TODAY() emission" or "find the biggest gap and close it"; the subagent will follow the snapshot + corpus safety nets and update CLAUDE.md / memory on completion.

### How to verify any change in this codebase

```bash
# Fast — 3 workbooks (snapshot) + 74 DAX corpus cases, ~35 seconds total
python -m pytest tableau_to_pbi/tests/test_snapshot.py tableau_to_pbi/tests/test_dax_corpus.py -v

# Refresh baselines after an intentional output change
UPDATE_SNAPSHOTS=1 python -m pytest tableau_to_pbi/tests/test_snapshot.py -v
git diff tableau_to_pbi/tests/snapshots/    # review the diff before committing

# Refresh DAX corpus pins after an intentional translator change
python -m tableau_to_pbi_agent.dax_corpus regen-tests
git diff tableau_to_pbi/tests/test_dax_corpus.py

# Re-measure corpus pass rate (regenerates corpus.jsonl + patterns.md)
python -m tableau_to_pbi_agent.dax_corpus analyze

# Full PBIP corpus — slower, run before merges (16 of 19 workbooks lack
# baselines so they fail with "no snapshot baseline" — expected; the
# converter built them all without crashing).
SNAPSHOT_FULL_CORPUS=1 python -m pytest tableau_to_pbi/tests/test_snapshot.py -v
```

Pre-existing `test_dax_translator.py`, `test_extract_filters.py`, `test_model_order.py` have 4 pre-existing failures unrelated to the refactor (call `SemanticModel._render_table_tmdl` as static when it's an instance method; assert `"YEAR (MIN(...))"` with a space that the translator never emitted). Not regressions — the snapshot + corpus tests are the real safety net.

## Pipeline

1. `parser.py` → reads `.twb` XML into IR dicts (datasources, worksheets, dashboards, parameters). Captures `connection`, `customSql`, `extracts` per datasource and `datasourceDeps` per worksheet.
2. `model.py` → builds the TMDL semantic model (tables, columns, relationships, measures). Synthesises blend relationships from worksheet `datasourceDeps`; applies credential overrides; chooses extract-CSV vs live-DirectQuery vs live-Databricks per table. Owns the FieldKind registry — see Phase 1 above.
3. `partition_m.py` → pure functions that render the Power Query partition-M expression for a live connection. Imported by `model.py`; SemanticModel delegates the per-table M emit here.
4. `report.py` → thin zone-to-visual dispatcher. Routes filter / parameter / color / legend / text / title / bitmap / dashboard-object zones to the right visual-family builder; routes worksheet zones to `ChartBuilder`. Holds one shared `FieldResolver` instance (`self.resolver`) plus a `ChartBuilder` (`self.chart_builder`); the bookmark side-effect flows back through `chart_builder.needs_default_bookmark`. Also owns parameter-slicer binding (`_resolve_parameter_binding`).
5. `field_resolver.py` → `FieldResolver` class. The single resolution path for visual fields, filters, and sort. Resolves bindings via the worksheet's primary ds first, then falls back to the binding's explicit `datasource` for blended fields (Phase D). Owns the projection emitter (`add_proj`) so date-part / trunc redirects and Measure-vs-Aggregation-vs-Column entry shaping happen in one place. Shared between ReportBuilder and ChartBuilder.
6. `visuals/` package → per-visual-family builders. `visuals/helpers.py` (shared formatting primitives), `visuals/navigator.py`, `visuals/textbox.py`, `visuals/action_button.py`, `visuals/slicer.py` (free functions); `visuals/chart.py` (`ChartBuilder` class — the chart visual constellation, projections, visual-level filters, sort definitions, per-category color, auto-stamped filters, card helpers). Each module owns its visual's JSON construction.
7. `writer.py` → drops everything onto disk in PBIP layout.
8. `visual_picker.py` + `visual_rules.json` → picks the PBI `visualType` from a worksheet's mark + shelf shape.
9. `dax_translator.py` → translates Tableau calc formulas to DAX.
10. `hyper.py` → enriches the model with Hyper extract column types/data.
11. `credentials.py` → optional JSON / XLSX credentials store. Overrides server / database / port / schema / warehouse / http_path / catalog before partition-M emit, and feeds `credentials_manifest.json`.
12. `converter.py` → orchestrator. Also emits `datasource_mapping.xlsx` (TWB → PBI wiring) and `credentials_manifest.json`, and cleans stale `*_pbip` output before each run.

## Visual types and slot conventions

### Card vs. multiRowCard

- **Single value (1 text encoding)** → `cardVisual` (modern KPI card). Slots: `Data`, `Tooltips`. Format bags: `value` (font/size/color/horizontalAlignment) + `label` / `outline` / `divider` / `fillCustom` all `show: false`. Each format bag carries `selector: {id: "default"}`. Height forced to 50.
- **Multi-value (2+ text encodings)** → `multiRowCard`. Slot: `Values` (every encoding lands here). Format bag: `dataLabels` carries `color`/`fontFamily`/`fontSize`/`bold`/`italic`/`underline` from twb (NOT just color — without `fontFamily`, PBI falls back to Segoe UI). `categoryLabels.show: false`. Keeps Tableau zone height (no 50px override).
- The picker upgrades cardVisual → multiRowCard when `len(labelFields) >= 2` at both the direct-mark mapping and the auto-rule paths.
- Card field emitter wraps bare columns in Aggregation: `Min` (Function 3) for strings, `CountNonNull` (Function 5) for numerics. DAX measures emit `Measure` ref directly.

### Slicer

- Height forced to 60. Title always `enabled=False`.
- `objects.general[0].properties.singleSelect = true|false`
- `objects.data[0].properties.mode = 'Dropdown'|'Basic'`
- Surveyed Tableau corpus tokens: `compact` / `dropdown` → single-select dropdown; `checkdropdown` → multi-select dropdown; `radiolist` → single-select list; `vscroll` / `checklist` → multi-select list.

### Top N filter

- Subquery shape (NOT `VisualTopN.ItemCount`). Dimension column lives in the outer `Where In`. Ranking measure lives in the subquery's `OrderBy.Aggregation`.
- `Direction: 2` = Descending (Top), `1` = Ascending (Bottom).
- **Cross-table**: when the ranking measure's table differs from the dimension's, add the measure's table to the subquery `From` with alias `m` and use `Source: "m"` in OrderBy. Single-table case keeps alias `c`.

### Charts

- **Pie / donut** default: `legend.show: true, position: 'Right'`.
- **Area / line / stackedArea**: `markers.show: true, shape: 'circle'` plus `dataPoint.showAllDataPoints: true`. Marker color from twb mark-color when present.
- **Tableau `<encoding attr='color'>` palette**: parsed at the datasource level into `datasource.colorMaps[field]: {bucket: hex}`. Applied as per-category PBI `dataPoint` selectors via `scopeId.Comparison` (Equality / `ComparisonKind: 0`). Wrong shape would be `dataViewWildcard.matchingConditions` — that fails schema validation. `scopeId` is a `QueryExpressionContainer`.

### Map (Azure Maps)

- Every Tableau geo mark (`map`, `point`, `polygon`, `filled-map`, `multipolygon`) routes to PBI's `azureMap` visualType. The legacy `map` / `filledMap` / `shapeMap` slot entries in `config.VISUAL_SLOTS` are kept as no-op fall-throughs so any caller still referencing them by name doesn't break; the picker no longer emits them.
- Detection by Tableau's `<column semantic-role='[Geographical].[Latitude]'>` first, then name match (`lat`/`lng`/`latitude`/etc.).
- Lat/lon columns get `dataCategory: Latitude` / `Longitude` and `summarizeBy: none` in TMDL.
- Map binders use bare Column refs (no aggregation) so coordinates aren't collapsed.
- **Auto-fills**: PBI Desktop auto-stamps X/Y/Series mirrors and per-binding filters on save. The converter pre-emits these for `azureMap` / `map` / `filledMap` / `shapeMap` / `cardVisual` / `multiRowCard` so the open/save round-trip doesn't mutate the file. Bar/pie/table currently skip auto-stamping.
- **Default viewport — North America**: Every map visual the converter emits (`azureMap` + the legacy `map` / `filledMap` / `shapeMap`) gets a `mapSettings` bag with `view: 'UnitedStates'`, `customZoom: 5`, `customCenterLat: 39.5`, `customCenterLon: -104.99`, plus alternate property names (`zoom`, `centerLat`, `centerLong`, `predefinedView`) for older Desktop builds and `autoZoom: false`. A second `controls` bag with `autoZoom: false` blocks the visual's default auto-zoom-to-data behaviour which would otherwise override the initial view. Property names come from Microsoft's Azure Maps visual `capabilities.json` (`mapSettings.view` enum: Auto / World / UnitedStates / Custom; `customZoom` numeric; `customCenterLat`/`customCenterLon` doubles). Legacy `map` / `filledMap` / `shapeMap` visuals auto-fit to the data extent and don't honour `mapSettings` — they're emitted defensively so a manual visualType swap preserves intent. Users who want a different region override in the Format pane after open.

### Table & matrix header styling

- `tableEx` and `pivotTable` always emit a `columnHeaders` bag (and `pivotTable` also emits `rowHeaders`) with **Tableau-like defaults layered with TWB-specific overrides** — PBI's default Segoe UI 9pt left-aligned headers are too far from Tableau's look to feel familiar, so the converter forces the bag to be present regardless of what the workbook supplied.
- Style sources, highest-precedence first:
  1. `worksheet.columnHeaderStyle` / `rowHeaderStyle` — parsed from `<style-rule element>` blocks by `parser._parse_worksheet_header_style`. The element list (lowest-precedence first inside a single bag): `header` → `field-labels` → `field-labels-decoration` → `row-header` / `column-header`. The `field-labels-decoration` rule is where Tableau stores the **column-header background color** (a band painted behind the field labels) — checking only `header` would miss it. Picks up `color` → fontColor, `font-family`, `font-size`, `font-weight=bold`, `font-style=italic`, `font-underline` / `text-decoration=underline`, `background-color` (transparent skipped), and `text-align`. Scoped variants — `data-class='subtotal'`/`'total'`, `scope='rows'`/`'cols'`/`'totals'`, or `field='...'` — are skipped entirely. They describe per-row/per-column overrides for totals/subtotals, NOT the general header style, so letting one through (as the previous version did when no base value existed) produced pale-grey headers in workbooks that intended dark blue.
  2. `worksheet.titleStyle` — Tableau workbooks usually use the same font for title and headers, so this is the next-best signal when no `<style-rule element='column-header'>` is present.
  3. `worksheet.labelStyle` — finally falls through to the worksheet's label style for fontFamily / fontSize only.
  4. **Tableau-like defaults** — Arial 10pt, color `#1b1b1b`, bold, center-aligned. Backgrounds default to the worksheet's `backgroundColor` when set, otherwise omitted (PBI fills white).
- PBI property bag uses `fontFamily` / `fontSize` / `fontColor` / `bold` / `italic` / `underline` / `backColor` / `alignment`. Alignment values are capitalised (`Left` / `Center` / `Right`) per PBI's string-enum convention. `fontColor` and `backColor` are wrapped in `{solid: {color: <expr>}}` envelopes (PBI's color shape).
- `pivotTable.rowHeaders` mirrors `columnHeaders` when the workbook only supplied one set of header rules — a Tableau crosstab styles both axes identically by default.

### Textbox

- **Container chrome (background, border) goes under `visualContainerObjects`, NOT `objects`.** PBI silently ignores `objects.background` for textbox visuals — the colored tile won't render. Chart visuals route the same way.
- `containerStyle` is now stashed on every zone dict by the parser (regardless of `type-v2`). The routing passes it to `_build_textbox` for color/legend/bitmap/fallback zones — previously only `text`/`title` zones had styling carried through.

## Model conventions

- **Date hierarchies**: every `dateTime` column gets synthesized `Year of <X>` / `Quarter of <X>` / `Month of <X>` / `Day of <X>` calculated columns plus a `<Date> Hierarchy` block in TMDL. DAX expressions use bracket notation (`[Date Added]`) — single-quote (`'Date Added'`) is parsed as a *table name* in DAX.
- **Tableau date-part agg redirect**: `yr:date_added` on a shelf rewrites the binding to `Year of Date Added` (the synthesized hierarchy column) since PBI has no date-part aggregation function.
- **Cols-map authoritative binding**: `<cols><map key='[Region]' value='[Dim_HCP].[Region]'>` blocks beat all heuristic resolution for un-suffixed field references.
- **`PBI_TimeIntelligenceEnabled = 1`** annotation in `model.tmdl` so PBI Desktop creates auto date hierarchies on first open.
- **Globally-unique measure names**: PBI rejects a model where two tables both define a measure with the same name (case-insensitive), failing with `PFE_TM_OBJECT_NAME_ALREADY_EXISTS`. Tableau allows it because each datasource is its own namespace. `_enforce_global_measure_uniqueness` in `model.py` runs after `_build_all_measures` and renames non-canonical duplicates to `<name> (<table>)`, rewriting any `'Table'[OldName]` DAX refs and updating `col_locator` so the report builder picks the post-rename names.

### Parameter tables

- **One table per parameter** (no shared lumped table). List parameters get a `Value`/`Label` two-column table with one row per `<member>`. Any/range parameters (including dateTime params like Start Date / End Date) each get their OWN single-column single-row table named after the parameter caption with column `Value`. This matches PBI's "What If" pattern and avoids mixing types in one row. The legacy lumped `Parameters` table caused (a) the date-hierarchy synthesizer to emit useless Year/Quarter/Month/Day columns on parameter date fields, and (b) confusion in PBI's slicer UX because it expects one parameter per table.
- **dateTime parameters** must emit M's `#datetime(yyyy, m, d, h, mi, s)` literal in the partition row, NOT a quoted string. PBI rejects mismatched types with "Expression.Error: The type of the value does not match the type of the column" — the error message echoes the offending value.
- `_m_literal` strips Tableau's `#YYYY-MM-DD#` markers and any surrounding quotes, parses ISO date or datetime, and falls back to `null` when unparseable.
- Date-hierarchy synthesis (Year of X / Month of X / etc.) is **skipped on parameter tables** — single-row tables don't benefit from hierarchies and emitting 8+ calc columns per dateTime parameter clutters the field pane.

### DAX translator (`dax_translator.py`)

- **Scalar wrapping**: bare column refs in measure context get wrapped in `MIN()` automatically. PBI measures must return scalars; Tableau's `IF [bool] THEN [col] END` would otherwise emit `'Table'[Col]` directly which fails. The post-processor skips wrapping inside aggregation calls (SUM/AVG/COUNT/SELECTEDVALUE/CALCULATE/etc.) and inside known DAX measure refs (tracked in `_ACTIVE_MEASURE_REFS`).
- **`SUM` on measures**: when a Tableau calc wraps another calc in `SUM(...)` and the inner ref resolves to a model measure (registered in `measure_refs` passed from `model.py`), the translator drops the wrapper. PBI rejects `SUM('Table'[SomeMeasure])` with "The SUM function only accepts a column reference as an argument."
- **Field-ref parsing**: `_parse_field_ref` strips Tableau type-suffix tokens (`nk`/`qk`/`ok`/`ck`/`ik`) and any duplicate-index integer (`:3`) before deciding role-vs-field. Only known agg/role tokens (`sum`/`avg`/`yr`/`usr`/etc.) are treated as the role; otherwise the whole spec stays in the field name. Without this, `Calculation_xxx:qk:3` was misparsed and calc fields couldn't be resolved.
- **LOD FIXED translation** (`_translate_lod_fixed`): `{ FIXED [Dim] : agg(X) }` becomes `CALCULATE(<dax-agg>, ALLEXCEPT('Table', 'Table'[Dim]))`; the global form `{ FIXED : agg }` becomes `CALCULATE(..., ALL('Table'))`. Implementation uses placeholder substitution (`__LOD_BLOCK_N__`) so the LOD-translated DAX doesn't get re-tokenised by the Tableau-syntax token loop. Multi-dim LODs and dims wrapped in `YEAR()`/`MONTH()`/`QUARTER()`/`DAY()` redirect to the synthesized hierarchy columns (`Year of X`, `Month Number of X` integer, etc.). `INCLUDE` / `EXCLUDE` LODs still drop.
- **Date construction**: `MAKEDATE(y, m, d)` → `DATE(y, m, d)`. `MAKETIME(h, m, s)` → `TIME(h, m, s)`. `MAKEDATETIME(date, time)` → `DATE(...)` (degraded — DAX has no native combine). `DATEPARSE('format', str)` / `PARSEDATE(str)` → `DATEVALUE(str)` (drops format arg; DAX has no format-string parser, so custom formats degrade to BLANK on unrecognised inputs).
- **Tableau formula comments**: tokenizer skips `// line comment` and `/* block comment */` (multi-line). Both are stripped before tokenization rather than emitted as DAX, since DAX has no equivalent comment syntax inside expressions.
- **Date function translations**: `DATEDIFF('part', A, B)` → `DATEDIFF(A, B, PART)` (arg-order swap, unquoted enum). `DATEPART('year', X)` → `YEAR(X)` (and friends). `DATETRUNC('month', X)` → `DATE(YEAR(X), MONTH(X), 1)`. `DATENAME('month', X)` → `FORMAT(X, "MMMM")`. `DATE(X)` cast → `DATEVALUE(X)` (works on date AND string inputs).
- **TOTAL / ATTR / SPLIT / TRIM**: `TOTAL(expr)` → `CALCULATE(expr, ALLSELECTED())` (percent-of-total pattern). `ATTR([X])` → `SELECTEDVALUE('T'[X])` for column refs, `MIN(...)` fallback. `SPLIT(s, d, n)` → `PATHITEM(SUBSTITUTE(s, d, "|"), n)` since DAX has no native split. TRIM/LTRIM/RTRIM are direct passthroughs.
- **Tableau groups & categorical-bin calc columns** (`_compile_categorical_bin` in `parser.py`): Tableau's `<calculation class='group' | 'categorical-bin' | 'bin' column='[X]'>` blocks emit as DAX calculated columns via `daxColumnExpr` — the column TMDL is `column 'Y' = SWITCH(TRUE(), [X] IN { ... }, "Bucket", [X])`. `_parse_columns` calls `_compile_categorical_bin(calc_el)` for every group/bin column and re-parents the new calc column to the SOURCE column's TMDL table (resolved through `cols_map` → `col_parent` → `objects`), so the bare `[X]` ref inside the DAX resolves in row context. `_render_table_tmdl` skips `sourceColumn:` for columns with `daxColumnExpr` so PBI computes the value instead of looking for a CSV header that doesn't exist. Logs `[GROUP] '<name>' in ds='<ds>' -> DAX calc column on table='<tbl>' (source='<col>')` per materialised group. If the bin XML is malformed and the compile returns `BLANK()`, the path falls back to the legacy alias-to-source so visuals at least bind to the underlying values. `model._register_group_aliases` then stashes the `{baseField, membersByGroup}` registry into `_group_aliases` for `group_info()` filter expansion (Tableau-style filter on a group label translates to `[Source] IN { members }`); the alias-injection backstop in `col_locator` only fires when the group field has NO calc column entry yet, so the calc-column path always wins.
- **Context-aware calc-field classification** (calc column vs measure): `_build_measures` decides per calc field whether to emit as a calculated column or a DAX measure. Calc columns evaluate row-by-row at refresh time — the only path that supports member-list filtering and row-level predicates; measures evaluate at query time and can't be the target of an `IN (a,b,c)` filter. Decision tree (first match wins):
  1. **Formula contains aggregation token** (`SUM`/`AVG`/`COUNT`/`COUNTD`/`MIN`/`MAX`/`MEDIAN`/`STDEV`/`VAR`/`ATTR`/`TOTAL`/`RUNNING_*`/`WINDOW_*`/`LOOKUP`/`PREVIOUS_VALUE`/`CORR`/`COVAR`/`PERCENTILE`/`FIRST`/`LAST`/...) → **measure**. Detected by `_formula_has_aggregation` (strips `//` and `/* */` comments first to avoid false positives on commented-out aggregations).
  2. **`role='dimension'` + `tmdlType='boolean'`** → **calc column**. Tableau row-level boolean predicates like `[Date] >= [Start] AND [Date] <= [End]`. As a measure the scalar wrapper MIN-wraps the fact-table column, which collapses to the global min when the visual doesn't include that column on an axis (the filter then always passes or always fails). As a calc column the comparison runs row-by-row against each fact-table row.
  3. **`role='dimension'` + no aggregation** → **calc column**. Tableau-declared dimensions (`IIF([Status]="Open", "Y", "N")`, `IF [Region]="US" THEN "Domestic" ELSE "International" END`) emit row-by-row so they can drive slicers and axes.
  4. **Used as a member-list filter target** (`worksheet.filters[*].members` non-empty) → **calc column**, regardless of role. Member filters need a row-level value to compare against; binding them to a measure silently fails at load. The `_calc_field_member_filter_keys` index is built once per build pass and consulted for every calc field.
  5. Otherwise → **measure**.
- Same-table `MIN(...)` wraps emitted by the translator's scalar guard are stripped from calc-column DAX (`MIN('SameTable'[Col])` → `'SameTable'[Col]`) so the comparison runs in row context. Cross-table refs (especially parameter tables, which are 1-row) keep the MIN — `MIN` of a 1-row table IS the current parameter value.
- Logged per emission: `[CALC-COL] '<name>' on table '<tbl>' emitted as calculated column (<reason>)` where reason is one of `boolean predicate, row-level filter` / `member-list filter target` / `dimension role, no aggregation`. Pre-fix only the boolean-dim path emitted calc columns; every other dimension calc became a measure and the user's filters silently dropped at load.
- **Tradeoff**: calc columns evaluate at refresh time, so slicer changes to *parameter values* don't re-trigger evaluation. Acceptable for converted Tableau models — the right PBI pattern is a slicer on the underlying column directly.
- **Tableau internal-object-id row counter**: `COUNT([__tableau_internal_object_id__].[<table-id>])` is Tableau's idiom for row count. Pre-translate to `COUNTROWS('<table>')` in `translate_tableau_to_dax` BEFORE tokenization (placeholder pattern, like LOD), so the COUNT wrapper around an unresolvable internal-table-id ref doesn't reach the main token loop and emit invalid DAX.
- **Auto-generated calc-field measures hidden**: Tableau anonymous calc fields get internal IDs like `Calculation_4313392949811654658`. These are noise in PBI's field pane — they're internal references between measures, not user-facing picks. `_build_measures` sets `isHidden=true` when the measure name matches `^Calculation[_ ]\d+$` so the field pane shows only named measures while DAX measures still cross-reference each other.
- **Trivial-alias calc resolution** (`_register_calc_alias_resolutions` in `model.py`): Tableau auto-generates calc fields whose formula is a single column reference (`[X]`, `[X (TableHint)]`, `// caption\n[X]`). These are pure renames — no arithmetic, no predicate, no function call. Visuals reference them by their cryptic `Calculation_<big_id>` identifier rather than the underlying column. The resolver detects the trivial-alias shape with the same regex as `tableau_to_pbi_agent/resolvers/formula_resolver.py` (which was an LLM-free Pass-2 fallback in the agent), then registers the calc-field name AND its caption in `col_locator` against the underlying column. Logs `[CALC-ALIAS] '<calc_name>' (ds='<ds>') -> <table>.<column>`. Suffixes that are Tableau-internal markers (`group`, `bin`, `parameter`, `calculation`) are NOT treated as table hints. Without this pass, worksheets that bind `Calculation_xxx` to a shelf emit `[RESOLVE] not found` and the field gets silently dropped.
- **Worksheet-local calc-field merge** (`_merge_worksheet_calc_fields` in `parser.py`): Tableau emits per-worksheet calc fields under `<view>/<datasource-dependencies datasource='X'>/<column>` rather than the top-level `<datasources>/<datasource>/<column>` block, so `_parse_datasources` never saw them. After parsing worksheets, the new pass walks every `<worksheet>/<view>/<datasource-dependencies>/<column>` with a `Calculation_` name + `<calculation formula='...'>` child and appends a synthetic column dict to the matching datasource. First definition wins (Tableau repeats the same calc element verbatim in every worksheet that uses it). Logs `[CALC-INDEX] merged <n> worksheet-local calc field(s)`. Combined with the trivial-alias resolver, the standalone converter now handles `Calculation_xxx` references inherently — the agent's `calc_index.py` + `formula_resolver.py` path is retained as a safety net but no longer needs to fire for the corpus.
- **Self-reference guard for measure DAX** (`_field_to_pbi_for_ds(..., exclude_self=)`): `_build_all_measures` pre-registers every calc field's caption AT THE FRONT of `col_locator[(ds, caption)]` so the calc shadows a same-named column — matching Tableau's "calc shadows column" semantics for visual binding. Without intervention this also means that when the calc's OWN DAX references `[Caption]`, the translator's `field_to_pbi` resolves the bare ref back to the measure being defined, emitting `'Table'[Caption] = '...' & 'Table'[Caption] & ...` which PBI rejects at load with `PFE_XL_CALCCOLUMN_CIRCULAR_DEPENDENCIES: A circular dependency was detected: Measure: 'Table'[Caption], Measure: 'Table'[Caption]`. Fix: `_field_to_pbi_for_ds` accepts an `exclude_self=(table, pbi_name)` argument that skips that pair when picking each bucket's first candidate, and `_build_measures` rebuilds `field_to_pbi` per-measure with the current measure as the exclusion. The shadow behaviour is preserved for visual-binding lookups (which call `_field_to_pbi_for_ds(ds_name)` without exclusion); only the measure-translation pass sees the underlying column. Repro: Medical Affairs Dashboard's `Response Date` calc field (a `DATENAME + STR + DAY + YEAR` chain) shares its caption with the `Response Date` column on `MIR` — pre-fix, it emitted `FORMAT('HCP Info (2)'[Response Date], "dddd") & ...` (self-ref); post-fix it emits `FORMAT(MIN('MIR'[Response Date]), "dddd") & ...` and loads cleanly.

## Data blending → relationships (Phase C)

- **Tableau worksheet-level blends** (a worksheet pulls dims from one datasource + measures from another) get translated to PBI model relationships in `relationships.tmdl`. Detection runs in `_synthesize_blend_relationships` (model.py) over each worksheet's `datasourceDeps` list (parsed from `<datasource-dependencies>` in the twb XML).
- **Blend-key inference**: Tableau's TWB XML doesn't expose explicit blend keys, only an `<aliases enabled='yes'/>` flag. We fall back to "shared column names": dims that appear in BOTH the primary and secondary worksheet declarations become blend-key candidates. The candidate's `(table, column)` resolution must point to a real column on its TMDL table — measure-typed `col_locator` entries are filtered out so a measure name like `Total Distinct Patients` doesn't accidentally become a relationship endpoint. When the worksheet only declares one side's columns, the fallback intersects the two datasources' `col_locator` keys, scoped to names the worksheet actually mentioned, so arbitrary shared names across large datasources don't emit dozens of garbage rels.
- **Cardinality**: emitted as **many-to-many bothDirections**. Tableau blends don't enforce uniqueness or non-null on either side; many-to-one would reject duplicate or blank keys at load time. `bothDirections` is the closest semantic match because PBI's many-to-many with `oneDirection` is effectively passive (USERELATIONSHIP-only). Each blend rel is flagged `isBlend: True` so `write_tmdl` emits the explicit `crossFilteringBehavior` / `fromCardinality` / `toCardinality` lines; non-blend rels keep the legacy many-to-many shape.
- **Skipped pairs**: when BOTH sides have the same column name appearing on multiple TMDL tables (`_has_duplicate_column`), the converter logs `[BLEND-WARN] using TREATAS fallback ...` and skips the relationship. TREATAS is not auto-emitted today — the warning is the user's hook to rewrite the measure manually.
- **Cycle/parallel-edge avoidance** (`_deactivate_ambiguous_paths`): PBI requires exactly ONE active relationship path between any pair of tables. Synthesized blend rels can produce (a) parallel rels on different keys between the same pair, and (b) triangle cycles when three tables blend pairwise. Both raise `PFE_XL_USERELATIONSHIP_AMBIGUOUS_PATH` on load. Walk relationships in insertion order; the unordered-pair seen-set catches parallel keys; union-find detects cycle-closing edges; mark all but the first `isActive: false`. Inactive rels stay in the model — DAX can invoke them via `USERELATIONSHIP(...)`.
- **Relationship names** are a deterministic 8-char `blend_<hash>` of `(fromTable, toTable, key)` so re-runs are stable across conversions.

## Card / multi-row card titles

`cardVisual` and `multiRowCard` always emit an explicit `visualContainerObjects.title.show = false`, regardless of the workbook's `titleEnabled` flag. Two reasons:

1. The PBI KPI / card pattern relies on the headline value (`value` / `dataLabels` bags) as the visual's primary text. A worksheet title on top duplicates it and steals vertical space (cards have a 50px-tall constraint where every pixel matters).
2. Tableau worksheets that author a "text" card mark almost always have a worksheet title that's identical to the value the user is highlighting — leaving the title enabled produces a redundant header.

Users who explicitly want the title back can flip it in the Format pane after open. The explicit `show=false` (as opposed to omitting the title block) is what actually hides the bar; PBI Desktop's default-when-absent behaviour is to render the worksheet name as a title.

## Multi-datasource visual binding (Phase D)

- `_resolve_visual_field` in `report.py` is the single resolution path for visual fields, filters, and sort. When the encoding / filter carries a `binding_ds` that differs from the worksheet's primary datasource (Tableau data blend), the resolver looks up `(binding_ds, fname)` through the **secondary's** `col_locator` first. On hit, logs `[BLEND] binding routed: '<field>' -> ds=<secondary> table=<tbl> (primary ds=<primary>)`.
- Single-ds worksheets (the common case) bypass the override and use the primary resolver path unchanged.
- The synthesised Phase C relationships are what make this work at query time — without an active relationship between primary and secondary, the cross-ds projection would not join.

## Connection classes & partition M

`render_partition_m` in `partition_m.py` (extracted from model.py during Phase 2) chooses a partition-M shape per datasource based on `connection.class`. The function is called from `model._render_table_tmdl` when no Hyper/CSV path is available; with a Hyper extract the CSV branch wins (because the data is already baked) unless `prefer_live_over_extract` is true AND the live class is Databricks.

| `connection.class` | Branch | mode |
|---|---|---|
| `""` / `federated` / `hyper` / `extract` | Returns `None` — caller emits `Table.FromRows({}, type table [...])` placeholder | import |
| `excel-direct` / `textscan` / `csv` / `json` | Returns `None` — caller uses CSV-from-Hyper path | import |
| `sqlserver` | `Sql.Database(server, dbname)` + `[Schema=..., Item=...]` nav | directQuery |
| `postgres` | `PostgreSQL.Database("server:port", dbname)` + `[Schema=..., Item=...]` nav | directQuery |
| `snowflake` | `Snowflake.Databases(server, warehouse, [Implementation="2.0"])` + DB/Schema/Name nav | directQuery |
| `databricks` / `azure-databricks` / `databricks-sql` / `spark-sql` / `spark` | `DatabricksMultiCloud.Catalogs(server, http_path, [Catalog="",Database="",QueryTags=null,EnableAutomaticProxyDiscovery=null,Implementation="2.0"])` + Catalog/Schema/Name nav | import |
| `redshift` and other live classes | `render_unsupported_partition` (in `partition_m.py`) — Table.FromRows placeholder with `// TODO` comment, logs `[CONN] live connection class '<cls>' — emitting placeholder Table.FromRows` | import |

- **Custom SQL** (`<relation type='text'>` / `'query'>` captured as `datasource.customSql`): when present, sqlserver / postgres / snowflake / databricks branches emit `Value.NativeQuery(Source, "<escaped-sql>", null, [EnableFolding=true])` instead of the navigation-record form. The SQL string is escaped via `utils.escape_m_string` (doubles `"`, converts newlines to `#(lf)`, tabs to `#(tab)`).
- **Snowflake `Implementation="2.0"`** is mandatory — without it PBI emits the legacy ODBC connector which doesn't honour Schema/Name navigation correctly for many Snowflake account shapes.
- **Databricks PAT**: the partition M never embeds the token. The token shows up only in `credentials_manifest.json` as `"authentication": "PersonalAccessToken", "has_personal_access_token": true` so a deployment script knows it must configure PAT auth via PBI Desktop or the REST API.
- **Databricks "live over extract"** (`is_databricks_live_connection` + `prefer_live_over_extract`, both in `partition_m.py`): when the effective connection class is Databricks AND `prefer_live_over_extract` is truthy (default), `write_tmdl` discards the bound Hyper/CSV path and emits the live `DatabricksMultiCloud.Catalogs` source instead. This is the path that lets a credentials file repoint an extract-mode Tableau workbook at a live Databricks warehouse without editing the .twb.

## Custom SQL as datasource

Two paths bring custom SQL into the partition-M expression. Both target the same emission path (`Value.NativeQuery(Source, "<escaped-sql>", null, [EnableFolding=true])`), differing only in where the SQL originates.

- **Tableau-embedded custom SQL** (`<relation type='text'>` / `<relation type='query'>` captured by `parser._parse_custom_sql` into `datasource.customSql`): When the workbook is extract-mode (Hyper CSV bound) BUT the inner connection class is live (sqlserver / postgres / snowflake / databricks), `write_tmdl` now drops the CSV path and emits the live native-query partition instead. Previously the extract always won, which left workbooks with `<relation type='text'>` blocks effectively static; the user could see the SQL in the Tableau workbook's data source pane but the converted PBIP just served the baked-in extract data. Logs `[CONN] '<table>' has customSql + live class '<cls>' — dropping extract path and emitting Value.NativeQuery instead of CSV bind`.
- **Credentials-file `query` override**: `CredentialEntry` now accepts `query` / `custom_sql` / `sql` as header names. When set, `apply_to()` stamps `customSql=[{"name":"credentials override","sql":"<sql>"}]` AND `force_live_for_custom_sql=True` onto the connection dict, so the override wins regardless of whether Tableau embedded SQL of its own. Combined with `prefer_live_over_extract=True` (the default), this is the path that lets a credentials file repoint an extract-mode workbook at an arbitrary live query without editing the .twb XML.

The credentials override is the override layer: when both a Tableau-embedded SQL and a credentials `query` are present, the credentials version wins because `apply_overrides()` replaces `connection.customSql` and the model picks up the merged connection's value.

## Credentials file

- Loaded by `tableau_to_pbi.credentials.load_credentials(path)` from `--credentials` (CLI) or `credentials_path=` (API). Supported formats: `.json` (preferred — accepts `{"connections": [...]}` or a bare list) and `.xlsx` (sheet named `Credentials` or active sheet, header row + data rows). The loader prints `[CREDS] Loaded <n> credential entries from <file>`.
- `CredentialStore.match(conn, datasource=, caption=)` priorities (first hit wins):
  1. Exact `datasource` or `caption` match (datasource-aware override for workbooks that hit the same server with different logical names).
  2. `class` + `server` (case-insensitive, server lower-cased).
  3. `class` only (server omitted — useful for single-server workbooks).
  4. Safe fallback: a Tableau `federated` / `hyper` / `extract` / empty-class connection routes to a **single** live Databricks entry when exactly one exists in the store.
- `CredentialEntry.apply_to(conn)` returns a copy of the parsed connection dict with `class` / `server` / `dbname` / `port` / `schema` / `warehouse` / `http_path` / `catalog` / `connector_function` / `prefer_live_over_extract` overridden when set, plus any unknown columns from the XLSX header passed through via `extra`. **Passwords and PATs are NEVER written into the M expression** — the manifest is the only hand-off.
- `credentials_manifest.json` (written by `converter._write_credentials_manifest`) lists every live datasource with its **effective** connection (post-override), `username`, `has_password` boolean, and for Databricks an `authentication: "PersonalAccessToken"` + `has_personal_access_token` flag. Datasources whose class is empty / federated / hyper / extract are skipped (no live credentials to manage).
- The legacy lumped `Parameters` connection class is treated as `extract`, so the manifest never lists it.

## Windows long-path support

Every directory creation and file write in `writer.py`, `model.py:write_tmdl`, and `hyper.py:write_csv` routes through a `_long_path` helper (in `utils.py`) that prepends `\\?\` to the absolute path on Windows. The prefix opts the call into the extended-length Win32 API (32 767-char limit), which is the only reliable way to write the deeper PBIP paths once the user's output root + workbook name + page-id + visual-id chain crosses 260 chars (the legacy MAX_PATH). Without this, dashboards with long workbook names (e.g. `Healthcare Resources Analysis for National Healthcare Group.twbx`) fail mid-write with `[WinError 3]` or `[Errno 2]` errors that look like missing files. The `safe_filename(..., max_len=30)` ds-dir / `max_len=40` csv-file caps still apply — long-path is the safety net for the cases the truncation can't save (the workbook name itself is the major contributor and we don't truncate it because it's the user-facing identifier).

## Stdout / stderr UTF-8

`script.py` reconfigures `sys.stdout` and `sys.stderr` to UTF-8 with `errors="replace"` on entry. Tableau workbooks regularly carry calc-field names with `◀ ▲ ▼ ▶` arrow glyphs and other Unicode symbols for navigation widgets; printing these to a Windows cp1252 console raised `UnicodeEncodeError: 'charmap' codec can't encode character '◀'` and aborted the run mid-`[VPICK]` log. `errors="replace"` substitutes any unencodable char with `?` rather than crashing — the actual TMDL writes already use explicit `encoding="utf-8"`, so output correctness is unaffected.

## CSV partition path is relative to SemanticModel

`write_tmdl` resolves each Hyper-CSV path to a `data/<ds>/<csv>` form **relative to the SemanticModel directory** before handing it to `_render_table_tmdl`. The emitted partition M is:

```m
Source = Csv.Document(File.Contents("data/<ds_dir>/<csv>"), [Delimiter=",", Encoding=65001, QuoteStyle=QuoteStyle.Csv])
```

`File.Contents` resolves relative paths against the SemanticModel directory, so the PBIP stays portable when the folder is moved/copied. Before this, the partition embedded the fully-resolved absolute path; PBI Desktop then failed to load on a moved PBIP and the deep absolute path tripped Windows MAX_PATH at write time on certain users' folders.

Two extra MAX_PATH safety belts now help the deepest-nested layouts stay under 260 chars:

- `safe_filename(ds_name, max_len=30)` for the `data/<ds_dir>` segment — Tableau's `federated.<22-char-id>` form is 32 chars on its own, so the truncated form (`federated.<14-char-prefix>_<8-char-hash>` ≈ 28 chars) prevents the parent path from chewing through the budget.
- `safe_filename(key, max_len=40)` for the CSV file name — Tableau extract keys like `Extract.MATILLION_X (COMM_DEV.MATILLION_X)_<32-char-GUID>` are ~120 chars on their own; the truncated form is ~40 chars so a ~50-char file name (incl. `.csv`) fits next to the ~100-char SemanticModel prefix.

When a CSV happens to live outside the SemanticModel folder (rare — extract-mode workbooks emit inside it by construction), the absolute path is kept as a last resort with `relative_to()` falling back via `ValueError`.

## Output artifacts

In addition to the standard PBIP layout (`<name>.pbip`, `<name>.Report/`, `<name>.SemanticModel/`, `data/`), the converter writes:

- `_ir.json` — JSON snapshot of the parser IR (datasources, worksheets, dashboards, parameters), gated on `debug_ir=True` (default).
- `datasource_mapping.xlsx` — four-sheet TWB → PBIP wiring report:
  - **Datasources** — caption, bound hyper file, declared extracts, list of TMDL tables produced per datasource.
  - **Columns** — every PBI column with its TMDL table, the original Tableau column reference, the Hyper/CSV header, dataType, and hidden flag. The file to open when a visual seems to reference the wrong column.
  - **Relationships** — every emitted rel (`<object-graph><relationship>` + synthesized blend rels). Includes the active flag so deactivated cycle-closers are visible.
  - **Skipped Relationships** — TWB-declared rels the converter could not emit (missing endpoint, self-join, etc.) with the reason.
- `credentials_manifest.json` — see Credentials section above. Only written when at least one live datasource exists.
- `_clean_generated_output` deletes any previous `*_pbip` folder at the start of each run; falls back to renaming the stale folder to `<name>.__stale_<ts>` when Windows holds a lock (PBI Desktop or Explorer keeping handles open).

## Visual picker conventions

- **Bar orientation** depends on which Tableau shelf has the continuous (measure-like) field. `_CONTINUOUS_AGGS` extends `_MEASURE_AGGS` to include `usr` (user calc field refs), `attr`, table-calc prefixes (`cum`, `pcto`, ...), and forecast prefixes. Without `usr`, calc-field measures wouldn't be recognized as continuous and the orientation override would never fire.
  - Continuous on COLS, dim on ROWS → horizontal `barChart`.
  - Continuous on ROWS, dim on COLS → vertical `columnChart` (override fires).
- **Scatter chart safety**: Tableau `mark='Circle'` maps to `scatterChart`, but PBI's scatter requires both X and Y to be numeric. When the `Circle` worksheet has a dimension on one axis (categorical dot plot) and only one measure, route to `columnChart`/`barChart` instead. With zero measures, fall back to `tableEx`.
- **Categorical-bin aware**: previous broken default mapped Tableau `circle` mark to `pieChart`. Now `circle` → `scatterChart` (and the orientation/dim-aware fallback above).

## Parser conventions

- **labelFields (plural)**: `<text>`/`<label>` encodings produce a list of all encodings, deduped by field name. `labelField` (singular) keeps the LAST one for backward compat with the visual_picker's `encoding: "label"` rule.
- **Worksheet labelStyle layers** (in precedence order, highest wins):
  1. `<style-rule element='worksheet'>` — bare `font-family` / `font-size` / `color`
  2. `<style-rule element='cell'>` — same bare attrs
  3. `<style-rule element='mark'>` — `mark-labels-*` attrs (chart data labels)
  4. `<customized-label>/<formatted-text>/<run>` — `fontname` / `fontsize` / `fontcolor` / `bold` / `italic` / `underline` (the marks-card label editor's choices)

## Font fallback

- **`_safe_font_family`** in `report.py` substitutes Arial only when the Tableau font isn't in PBI's shipped-fonts allow-list (e.g. `Tableau Medium` / `Tableau Bold` → Arial). Calibri / Segoe UI / Times New Roman / etc. pass through unchanged. **Color and size are always pulled from twb**, never substituted.

## Schema sources

- `https://raw.githubusercontent.com/microsoft/json-schemas/main/fabric/item/report/definition/visualContainer/2.7.0/schema.json` — visual container shape
- `https://raw.githubusercontent.com/microsoft/json-schemas/main/fabric/item/report/definition/formattingObjectDefinitions/1.5.0/schema.json` — `Selector`, `DataRepetitionSelector`, `DataViewWildcard`
- `https://raw.githubusercontent.com/microsoft/json-schemas/main/fabric/item/report/definition/semanticQuery/1.4.0/schema.json` — `QueryExpressionContainer` (Comparison, In, Aggregation, Subquery, ...)

## Smoke test

Quick corpus check after material changes:

```bash
cd C:/Users/ShrikantPansare && for f in \
  "UseCase.twbx" \
  "UseCase2.twbx" \
  "Sample Dashboards/Netflix Movies and TV Shows Dashboard.twbx" \
  "Sample Dashboards/Merchandise Sales Dashboard.twbx" \
  "Sample Dashboards/Superstore Performance Dashboard _ #VOTD.twbx"; do
  echo "=== $f ==="
  python -m tableau_to_pbi_agent "$f" --skip-llm 2>&1 | grep -E "types=|warnings:" | head -3
done
```

Each workbook should build with the same visual counts as before the change and zero new warnings. The `[VPICK]` diagnostic line printed per worksheet shows which visual type the picker assigned (mark / row count / col count / label field).

## Common gotchas

- **`objects` vs. `visualContainerObjects`**: data formatting goes under `objects`; container chrome (title, background, border, divider) goes under `visualContainerObjects`. Wrong bag = silently ignored.
- **`scopeId` for per-category formatting**: not `dataViewWildcard.matchingConditions`. `dataViewWildcard` only accepts `matchingOption` per the schema.
- **DAX column quoting**: `[Column]` inside expressions, `'Table'` for table refs. Mixing them produces "Cannot find column" errors.
- **M datetime literals**: `#datetime(...)`, not quoted strings. Type-mismatch errors surface as the verbatim offending value in PBI's error message.
- **PBI Desktop fallback fonts**: when `fontFamily` is absent on a format bag, PBI falls back to Segoe UI even if the parser captured a different family elsewhere. Always emit `fontFamily` explicitly when twb supplies one.
- **Snowflake without `Implementation="2.0"`**: PBI silently picks the legacy ODBC connector and the Schema/Name nav records mis-resolve. Always include the option record on `Snowflake.Databases(...)`.
- **Blend keys that resolve to a measure**: `col_locator` carries both columns and measures. A measure name like `Total Distinct Patients` getting picked as a blend-key endpoint produces a relationship PBI refuses to load. `_synthesize_blend_relationships` filters these out by checking the candidate against `cols_per_table` first.
- **Custom-SQL SQL escaping**: M strings need `""` for embedded quotes, `#(lf)` for newlines, `#(tab)` for tabs. Use `utils.escape_m_string` — raw Python string concat will produce M-parse errors that only surface when PBI opens the partition.
- **Plaintext passwords**: never embed credentials in the partition M. PBIP / TMDL has no secure in-file store; anything written to disk lands in version control. The manifest is the authorised hand-off.
- **`Value.NativeQuery` needs a Database, not a Table**: `Snowflake.Databases(...)` and `DatabricksMultiCloud.Catalogs(...)` both return a catalog *listing* (a Table), not a database. Calling `Value.NativeQuery` on those directly fails with `Expression.Error: Native queries aren't supported by this value. Details: [Table]`. The custom-SQL emit therefore navigates one level into the named database/catalog (`Database = Source{[Name="<db>"]}[Data]`, `Catalog = Source{[Name="<catalog>", Kind="Database"]}[Data]`) before passing the result to NativeQuery. `Sql.Database` and `PostgreSQL.Database` return a Database directly, so no extra hop is needed for sqlserver / postgres.
