"""Learn-and-update layer: observe converter output across a corpus of
twbx workbooks, propose data-level rule additions (visual_rules.json,
lookup tables, etc.), and merge them autonomously when a regression
gate passes.

Hard rules:
  * Every file we plan to edit gets backed up first.
  * After applying a change, we re-run the converter on every corpus
    workbook and compare to a pre-change snapshot. Visuals the
    observer flagged are allowed to change in the way the observer
    expected; everything else MUST be byte-identical.
  * On regression failure, every modified file is restored from
    backup. The user's converter is left exactly as it was.

Public CLI:
    python -m tableau_to_pbi_agent.learn run
    python -m tableau_to_pbi_agent.learn rollback [--to <ts>]
    python -m tableau_to_pbi_agent.learn status
"""
