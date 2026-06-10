# User preferences

The user prefers wide feature batches over deep automated testing — they only have 3 sample dashboards so snapshot tests would lock in idiosyncrasies as "ground truth." Skip test-suite work unless they ask.

**Why:** Validated explicitly when they declined automated tests citing "with 3 dashboards, automated testing may not provide correct results."

**How to apply:**
- Default to manual verification via `--dry-run` / running the converter end-to-end and inspecting `conversion_report.md`.
- When proposing future work, lead with feature additions or data-fidelity wins; mention testing only as a "consider eventually" footnote.
- They like comprehensive proposal lists ("what else can be added?") followed by ambitious execution — when asked to explore, list 10+ options ranked by impact and then ship as many as possible in one batch.

Other quirks:
- They run the tool against their own QVFs in `New_Test`, `New_Trial`, `output` folders — these come and go; don't assume specific sample paths exist.
- They open PBI Desktop and forward error messages verbatim. Treat those as the source of truth over schema assumptions.
- They flag visual bugs by selecting a line in a JSON file and pointing at it — pay attention to the file path and line content.
