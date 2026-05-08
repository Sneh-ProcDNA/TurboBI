"""Concrete observers. Each one targets a single pattern."""

from .card_detector          import CardObserver
from .kpi_dim_label_detector  import KpiDimLabelObserver

__all__ = ["CardObserver", "KpiDimLabelObserver"]
