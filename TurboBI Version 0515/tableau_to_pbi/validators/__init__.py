"""Per-visual-type slot validators.

After the report builder produces projections, validators run a final
pass to ensure each visual has the slots it actually NEEDS to render.
This catches cases where Tableau's encoding shelf semantics don't map
1:1 to PBI's slot expectations — e.g. a filled map where the country
field landed on Details instead of Location.

Each validator gets the worksheet dict and the projections dict, can
mutate projections in place, and returns whatever it modified for the
log line.
"""

from .slot_validator import validate_slots

__all__ = ["validate_slots"]
