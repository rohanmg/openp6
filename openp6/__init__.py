"""openP6 - a free, open-source critical-path scheduler for Primavera P6 data.

Reads P6 .xer exports without P6 or any paid tool, recomputes the schedule
(forward/backward pass) and renders a Gantt chart. Dependency-free core.
"""
from .xer import Xer
from .schedule import Scheduler, REL_TYPES

__all__ = ["Xer", "Scheduler", "REL_TYPES"]
__version__ = "0.1.0"
