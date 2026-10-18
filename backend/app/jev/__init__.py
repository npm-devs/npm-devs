"""
Jev classification module for Fleet Intelligence.

This module provides typed classification of fuel overconsumption alerts
using TypeSafe AI's Jev decision model.
"""

from .client import JevClient
from .classifier import AlertClassifier, AlertCriticality, ClassificationResult
from .config import JevConfig
from .questions import JEV_CRITICALITY_QUESTION, JEV_QUESTION_VERSION

__all__ = [
    "JevClient",
    "AlertClassifier",
    "AlertCriticality",
    "ClassificationResult",
    "JevConfig",
    "JEV_CRITICALITY_QUESTION",
    "JEV_QUESTION_VERSION",
]
