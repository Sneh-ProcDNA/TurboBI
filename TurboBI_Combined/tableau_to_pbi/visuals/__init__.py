"""Per-visual-family builders.

Each module in this package owns the construction of one PBI visual
family (page navigator, textbox, action button, slicer, chart, etc.).
The module's public API is a small set of pure-ish functions that take
geometry, styling, and the ingredients they need explicitly — instead
of reaching into ``ReportBuilder`` state.

``report.ReportBuilder`` keeps the role of the dispatcher: it owns the
shared resolution and projection helpers, decides which visual family
applies to a given Tableau zone, and delegates the actual JSON
construction to one of these modules.

This split exists so each visual family can evolve (and be tested)
without dragging the entire 3700-line builder along. New visual
support — drillthrough buttons, tooltip pages, KPI cards — lands as a
new module in this package rather than as another method appended to
``ReportBuilder``.
"""
