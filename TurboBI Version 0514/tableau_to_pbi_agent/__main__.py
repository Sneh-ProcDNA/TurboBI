"""Allow `python -m tableau_to_pbi_agent ...`."""
from .cli import main
import sys
sys.exit(main())
