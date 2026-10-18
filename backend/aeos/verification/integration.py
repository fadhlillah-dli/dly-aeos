"""Calls into other control-plane modules, only through their frozen facades.

Modules are built independently; when one is not available (ImportError from the facade's lazy
import) the caller degrades **closed**: the step is reported as UNAVAILABLE, nothing is assumed to
have succeeded, and no gate is opened because of it. Policy/state refusals (``AeosError``) are
reported as REJECTED with their code. Other exceptions are bugs and propagate.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from aeos.core.errors import AeosError

log = logging.getLogger("aeos.verification.integration")


@dataclass
class FacadeCall:
    status: str  # APPLIED | UNAVAILABLE | REJECTED
    detail: str = ""
    result: Any = None

    @property
    def ok(self) -> bool:
        return self.status == "APPLIED"

    def label(self) -> str:
        return self.status if not self.detail else f"{self.status}: {self.detail}"[:64]


def call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> FacadeCall:
    try:
        result = fn(*args, **kwargs)
    except ImportError as exc:
        log.warning("facade call %s unavailable: %s", getattr(fn, "__qualname__", fn), exc)
        return FacadeCall("UNAVAILABLE", f"module not available ({getattr(exc, 'name', None) or exc})")
    except AeosError as exc:
        return FacadeCall("REJECTED", f"{exc.code}: {exc.message}")
    return FacadeCall("APPLIED", result=result)
