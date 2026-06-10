"""DAX translator corpus + regression test bed for the converter.

This module is the upgrade-driver for the deterministic DAX translator
(``tableau_to_pbi.dax_translator``). It owns three responsibilities:

1. **Corpus analyzer** — walk every ``.twbx`` under
   ``Sample Dashboards/`` (one level up from the repo root, by default),
   parse each, build a minimal ``SemanticModel`` so the translator gets
   realistic context, then run every calc field through
   ``translate_tableau_to_dax``. Output: ``corpus.jsonl`` + ``patterns.md``.
2. **Regression bed generator** — given a curated list of Tableau
   formulas, run them through the *current* translator and write
   ``tableau_to_pbi/tests/test_dax_corpus.py`` with the actual outputs
   pinned as expected values. Regenerate after intentional translator
   changes; diff the file to confirm only the expected cases moved.
3. **Findings doc** — ``findings.md`` records what was discovered, what
   was shipped, and what remains. Update alongside translator changes
   so the project-state stays in sync.

CLI:
    python -m tableau_to_pbi_agent.dax_corpus analyze
    python -m tableau_to_pbi_agent.dax_corpus regen-tests

See the project's ``tableau_to_pbi/CLAUDE.md`` "Phase 3a" section for
the original motivation and the rules shipped under this workflow.
"""
