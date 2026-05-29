"""Anomaly monitoring — detect price/volume events and attribute via web search."""

from v2.monitoring.attributor import attribute
from v2.monitoring.detectors import detect
from v2.monitoring.models import Anomaly, MonitorConfig, NewsSource
from v2.monitoring.monitor import run_monitoring

DEFAULT_CONFIG = MonitorConfig()

__all__ = [
    "Anomaly",
    "DEFAULT_CONFIG",
    "MonitorConfig",
    "NewsSource",
    "attribute",
    "detect",
    "run_monitoring",
]
