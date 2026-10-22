"""Automated experiments and baseline evaluation module.

Runs reproducible network scenarios comparing No QoS, Static QoS, and Adaptive QoS,
producing structured evidence reports (HTML, Markdown, JSON, CSV).
"""

from .runner import ExperimentRunner, ExperimentJob, ExperimentResult
from .scenarios import SCENARIOS, get_scenario

__all__ = [
    "ExperimentRunner",
    "ExperimentJob",
    "ExperimentResult",
    "SCENARIOS",
    "get_scenario",
]
