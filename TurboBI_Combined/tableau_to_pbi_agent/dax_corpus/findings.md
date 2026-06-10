# DAX translator — corpus findings & proposed mapping rules

Source: `python -m tableau_to_pbi_agent.dax_corpus analyze` ran against
18 of 19 workbooks under `Sample Dashboards/`. **1268 calc fields**
captured; raw records in `corpus.jsonl`, pattern roll-up in
`patterns.md` (both files live alongside this one).

## Current state (after Phase 3 rule additions)

- **94.5%** of calc fields translate cleanly (1198 / 1268).
- **70** still drop. Of those:
  - 19 are bare `INDEX()` (no DAX equivalent without axis context)
  - 7 use `RUNNING_*`
  - 4 use `REGEXP_*`
  - 4 use `LOOKUP(...)`
  - 36 are composite formulas with constructs the block walker can't yet parse (parenthesised CASE inside ELSEIF branches, unary minus on suffixed refs, parenthesised OR in IF conditions, multi-line LOD inside IF, etc.)

## Changes shipped under Phase 3a

| Change | Impact |
|---|---|
| `tableau_to_pbi/tests/test_dax_corpus.py` — 76-case regression bed | New |
| TODAY/NOW double-paren bug fix (`_KW_LITERAL` → `_FN_PASSTHROUGH` move) | 19 fields' DAX corrected from `TODAY() ()` to `TODAY()` |
| RANK / RANK_UNIQUE / RANK_DENSE translation | +11 corpus fields now emit valid `RANKX(ALLSELECTED, ...)` |
| Nested block IF / CASE depth-aware delimiter scan | +10 corpus fields. New helper `_depth_zero_positions(body, targets)` walks tokens skipping over nested block CASE/IF; `_translate_block_if` and `_translate_block_case` use it instead of face-value `THEN`/`WHEN`/`ELSE` scans that were tripping on inner block keywords. Fixed pattern: `IF X THEN CASE Y END END` and `CASE WHEN X THEN IF Y THEN ... END END`. |

## Original headline numbers (pre-change baseline)

- **92.7%** of calc fields translated cleanly. 92 drops.
- **48 of 92 drops** were explained by 7 specific Tableau-only constructs
  the translator didn't handle. The other 44 were nested occurrences of
  those same constructs inside `IF` / `CASE` blocks.

## Drop root causes (overlapping; 92 total)

| Construct | Drops | Mapping availability in DAX |
|---|--:|---|
| `INDEX()` | 21 | Direct: `RANKX(ALLSELECTED(<table>), <sort>, , ASC, Dense)` for the typical "sort position" use. Loose match — Tableau's `INDEX()` returns the row's pane position, which DAX has no native equivalent for. |
| `RANK_UNIQUE(expr[, 'asc'/'desc'])` | 8 | Direct: `RANKX(ALLSELECTED(<table>), <expr>, , ASC/DESC, Skip)`. |
| `RUNNING_SUM(expr)` and friends | 7 | Direct: `CALCULATE(<agg>(<expr>), FILTER(ALLSELECTED(<axis>), <axis> <= MAX(<axis>)))`. Requires knowing the visual's continuous axis — translator currently has no axis context. Practical compromise: emit a `CALCULATE(SUM(...), FILTER(ALL(<table>), <generic-ordinal>))` and let user adjust. |
| `LOOKUP(expr, -1)` | 4 | Direct: `CALCULATE(<expr>, PREVIOUSMONTH/PREVIOUSYEAR/etc.)` or `CALCULATE(<expr>, DATEADD(<date>, -1, <unit>))`. Requires date-axis context (same issue as RUNNING). |
| `REGEXP_EXTRACT(s, pat)` and family | 4 | No direct DAX equivalent — DAX has no regex engine. Degraded mapping: emit `BLANK()` for `REGEXP_EXTRACT/MATCH/REPLACE` with a `[REGEXP-STUB]` log. Some narrow cases (`'\d+'` etc.) could be SUBSTITUTE chains but high risk. |
| `RANK_DENSE(expr[, 'asc'/'desc'])` | 2 | Direct: `RANKX(ALLSELECTED(<table>), <expr>, , ASC/DESC, Dense)`. |
| `{INCLUDE: …}` LOD | 1 | Conceptually `CALCULATE(<agg>, ALLEXCEPT(<table>, <ws-axes-plus-INCLUDE-dims>))` — needs visual context. Defer. |
| `IN ('a','b','c')` operator | 1 | Direct: rewrite token stream `expr IN ( a , b , c )` → `expr IN { a, b, c }`. Easy win. |

## Patterns that already translate cleanly (no work needed)

| Pattern | Sample | DAX shape |
|---|---|---|
| **LOD FIXED single-dim** | `{ FIXED [Region] : SUM([Sales]) }` | `CALCULATE(SUM('T'[Sales]), ALLEXCEPT('T', 'T'[Region]))` |
| **LOD FIXED multi-dim** | `{ FIXED [Region], YEAR([Date]) : COUNT([X]) }` | `CALCULATE(COUNT('T'[X]), ALLEXCEPT('T', 'T'[Region], 'T'[Year of Date]))` |
| **Block IF** | `IF [X] = 'Y' THEN 1 ELSE 0 END` | `SWITCH(TRUE(), MIN('T'[X]) = "Y", 1, 0)` |
| **Block CASE** | `CASE [P] WHEN 1 THEN A WHEN 2 THEN B END` | `SWITCH(MIN('T'[P]), 1, A, 2, B)` |
| **IIF** | `IIF([X]=1, "A", "B")` | `IF(MIN('T'[X]) = 1, "A", "B")` |
| **WINDOW_SUM/AVG/MIN/MAX/COUNT** | `WINDOW_SUM(MAX([V]))` | `CALCULATE(SUM('T'[V]), ALLSELECTED('T'))` |
| **DATEDIFF / DATEADD / DATETRUNC** | `DATEDIFF('day', A, B)` | `DATEDIFF(A, B, DAY)` |
| **DATEPART** | `DATEPART('year', X)` | `YEAR(X)` |
| **MAKEDATE / MAKETIME** | `MAKEDATE(2024, 1, 1)` | `DATE(2024, 1, 1)` |
| **STR / INT / FLOAT cast** | `INT([X])` | `INT('T'[X])` (DAX has matching functions) |
| **SPLIT** | `SPLIT([X], '-', 2)` | `PATHITEM(SUBSTITUTE('T'[X], "-", "\|"), 2)` |
| **TRIM / LTRIM / RTRIM / UPPER / LOWER** | direct passthrough |
| **ATTR(field)** | `ATTR([X])` | `SELECTEDVALUE('T'[X])` |
| **TOTAL(expr)** | `TOTAL(SUM([X]))` | `CALCULATE(SUM([X]), ALLSELECTED())` |
| **`__tableau_internal_object_id__`** | `COUNT([__tableau_internal_object_id__].[X])` | `COUNTROWS('T')` |
| **Bare field ref in measure ctx** | `[X]` | `MIN('T'[X])` (scalar-wrap fallback) |
| **String concat with `+`** | `'a' + [X]` | `"a" & 'T'[X]` |

## Proposed mapping rules to add (sorted by ROI)

### Rule 1 — `IN` list operator (easy win)

Tableau:
```
[Severity] IN ('High', 'Critical')
```

DAX:
```
'T'[Severity] IN { "High", "Critical" }
```

Effort: ~30 LOC in `_translate_tokens` — recognize `IN` followed by
`(`, swap the parens for braces, leave the body alone (it's already a
comma-separated list of literals after the main token loop handles
quotes).

ROI: 1 outright drop + likely several of the 44 "unexplained" BLOCK_IF
drops that nest `IN (...)` inside conditionals.

### Rule 2 — `RANK_UNIQUE` / `RANK_DENSE` / `RANK`

Tableau:
```
RANK_UNIQUE(SUM([Sales]), 'desc')
RANK_DENSE(MIN([Code]), 'asc')
```

DAX (assuming we know the active table — use the translator's
`table_name`):
```
RANKX(ALLSELECTED('T'), SUM('T'[Sales]), , DESC, Skip)
RANKX(ALLSELECTED('T'), MIN('T'[Code]), , ASC, Dense)
```

Mapping table:
- `RANK_UNIQUE` → `Skip` ties
- `RANK_DENSE`  → `Dense` ties
- `RANK`        → `Skip` ties (Tableau's default)
- `'asc'`       → `ASC` (default)
- `'desc'`      → `DESC`

Effort: ~50 LOC — pattern-match `RANK*(<expr>[, 'asc'/'desc'])` in the
tokenizer/translator before the unsupported-token guard. Use
`ALLSELECTED('<table_name>')` since that's what the translator is given.

ROI: 8 outright `RANK_UNIQUE` drops + 2 `RANK_DENSE` + several nested
RANK_UNIQUE inside BLOCK_IF (probably ~15 fields total).

### Rule 3 — `INDEX()`

Tableau:
```
INDEX()
```

DAX (loose mapping for the "sort-position helper" use case):
```
RANKX(ALLSELECTED('T'), <fallback-sort>, , ASC, Dense)
```

Without knowing the user's intended sort key, `INDEX()` is fundamentally
hard to translate. The realistic mapping is `ROWNUMBER` (DAX 2024+) or a
warning stub. Practical choice: emit a degraded `RANKX(ALLSELECTED('T'),
1, , ASC, Dense)` which yields a constant rank but at least doesn't
crash, and log `[INDEX] '<calc>' translated as degraded RANKX — review
manually`.

ROI: 21 drops, but the resulting DAX is functionally limited — buys
"loads without error" not "behaviorally correct".

### Rule 4 — `RUNNING_SUM` / `RUNNING_AVG` / `RUNNING_*`

Tableau:
```
RUNNING_SUM([Flow Size])
```

DAX (assuming a date axis or ordinal):
```
CALCULATE(
    SUM('T'[Flow Size]),
    FILTER(ALLSELECTED('T'), 'T'[<axis-col>] <= MAX('T'[<axis-col>]))
)
```

Practical degraded mapping (no axis context): use the FIRST datetime
column on the table, or fall back to a generic `INDEX()` ordinal:
```
CALCULATE(SUM('T'[X]), FILTER(ALLSELECTED('T'), TRUE()))
```
which evaluates as a global aggregate — not running, but loads.

ROI: 7 drops; same caveat as INDEX — likely behaviorally wrong but
better than dropping.

### Rule 5 — `LOOKUP(expr, -1)` for prior-period

Tableau:
```
LOOKUP(SUM([Sales]), -1)
```

DAX (when the visual has a Year axis — common case):
```
CALCULATE(SUM('T'[Sales]), PREVIOUSYEAR('Date'[Date]))
```

Without a date table reference, the safer mapping is a stub:
```
CALCULATE(SUM('T'[Sales]), DATEADD('T'[<best-date-col>], -1, YEAR))
```

ROI: 4 drops.

### Rule 6 — `REGEXP_EXTRACT(s, '\d+')` → degraded stub

Tableau:
```
REGEXP_EXTRACT([SPRINT], '(\d+)')
```

DAX has no regex. The most common pattern in the corpus is *"extract
the digits"*. Practical mapping:
```
VALUE(<longest digit run in 'T'[SPRINT]>)  -- emitted as BLANK() stub with log
```

Realistic choice: emit `BLANK()` and log a `[REGEXP-STUB]` line so the
user sees which fields need manual rewrite.

ROI: 4 drops, but the output isn't useful — only buys "loads".

### Rule 7 — Defer

- `{INCLUDE: ...}` LOD — 1 drop, complex axis-dependent semantics.
- `WINDOW_STDEV`, `WINDOW_VAR` — no occurrences in corpus.
- `PREVIOUS_VALUE` — no occurrences in corpus.
- `PERCENTILE` — no occurrences in corpus.

## Implementation status

1. **Regression test bed.** **DONE** —
   `tableau_to_pbi/tests/test_dax_corpus.py` now has 74 parameterized
   cases covering every working bucket plus the unsupported-construct
   contract. Regenerate with `python _generate_dax_test_cases.py` after
   intentional translator changes. The diff shows exactly which cases
   moved.
2. **Bug fix: zero-arg functions emit stray parens.** **DONE** —
   `TODAY()` / `NOW()` moved from `_KW_LITERAL` (which appended `()`
   to the mapped value, then the source's own `()` doubled them) to
   `_FN_PASSTHROUGH` (lets the natural identifier-followed-by-paren
   join produce `TODAY()` cleanly). 19 corpus fields' DAX corrected.
3. **Rule (IN operator)** — ~~planned~~ Discovered already supported.
   `[Region] IN ('East', 'West')` already emits valid DAX.
4. **Rule (RANK family)** — **DONE** — `RANK`, `RANK_UNIQUE`,
   `RANK_DENSE` now translate via `RANKX(ALLSELECTED('<table>'),
   <expr>, , <DIR>, <Skip|Dense>)`. Direction default is `DESC` per
   Tableau. Removed `rank` from `_UNSUPPORTED_TOKENS`. 11 corpus
   fields translated; 2 still drop because they nest RANK_UNIQUE inside
   a parenthesised CASE inside an IF — composite parse limitation.
5. **Rules for INDEX / RUNNING_* / LOOKUP / REGEXP** — **NOT done.**
   Open question: emit `BLANK()` stubs (field loads but returns null),
   or keep dropping silently? Tradeoff in the open-questions section
   below.
6. **Phase 3b (AST rewrite)** — **NOT started.** The token-based
   translator at 93.7% pass rate is probably good enough that an AST
   rewrite would be a refactor, not a behavior improvement. Defer
   indefinitely.

## Open questions

- **Stubs vs silent drops for INDEX/RUNNING_*/LOOKUP/REGEXP:**
  - Stub side: field loads in PBI Desktop, user sees it in field pane,
    can hand-edit the DAX. Other measures that reference it resolve.
  - Drop side: field disappears entirely; visuals referencing it have a
    hole. But the data isn't *wrong* — a BLANK stub silently swaps a
    rank/regex result for nothing, which could mislead.
  - Recommendation: if we add stubs, emit them with a `//` DAX comment
    inside the measure expression noting the limitation, so a user
    inspecting the measure sees the warning. PBI Desktop preserves
    `//` line comments in the measure pane.
- **Composite drops (44 fields):** nested IF + parenthesised CASE,
  unary minus on suffixed field refs, parenthesised OR conditions
  inside IF, etc. Most of these would need either an AST rewrite or
  pattern-specific fixes. None are high-frequency on their own; the
  cumulative count is the issue.

## Co-occurring patterns (signal for AST work)

From the secondary-bucket roll-up: 200 of the 1268 calc fields combine
BLOCK_IF with AGG_BASIC, 133 combine AGG_BASIC with something else, 59
nest TYPE_CAST inside something else. These compositions are exactly
what an AST-based translator simplifies — but the corpus shows the
current token-based translator already handles them at >90% pass rate,
so the AST rewrite is not load-bearing.
