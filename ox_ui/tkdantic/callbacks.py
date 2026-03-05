"""Pydantic models for function callbacks.
"""

from typing import Callable, Optional
from pydantic import BaseModel


class SimpleCallback(BaseModel):
    """Simple function callback
    """

    function: Callable
    args: Optional[list] = None
    kwargs: Optional[dict] = None


class TimedCallback(SimpleCallback):
    """Callback that should occur after some interval (in seconds).
    """

    interval: float
