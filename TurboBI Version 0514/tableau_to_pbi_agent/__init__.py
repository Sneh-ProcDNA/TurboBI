"""Agent layer that wraps tableau_to_pbi.

Run the deterministic converter, capture its [RESOLVE]/[FILTER]/[DS]
warnings, and ask Claude to propose hint mappings the converter can use
on a second pass. The original converter's behavior is unchanged when
no hints are supplied.

Public surface:

    from tableau_to_pbi_agent import run_with_agent
    run_with_agent("workbook.twbx")

See cli.py for the command-line entry point.
"""

from .orchestrator import run_with_agent  # noqa: F401

__all__ = ["run_with_agent"]
