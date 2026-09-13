"""Load the compiled strategy without modifying, rewriting, or reverse-engineering it.

The strategy ships as Python 3.13 bytecode (``Strategy/s1_cheap_buy.cpython-313.pyc``),
originally built inside the package ``polybot.strategies`` and using *package-relative*
imports::

    from ..market_data import MarketState   # -> polybot.market_data
    from .base import Strategy, MAX_TRADE_VALUE, shares_for_notional  # -> polybot.strategies.base

Those framework modules were never shipped. We provide them from
:mod:`polybot.strategy.base` / :mod:`polybot.strategy.market_data` (which physically
live in ``polybot/strategy/``) and alias them into ``sys.modules`` under the exact
dotted names the bytecode resolves to, then execute the compiled module under the
``polybot.strategies`` package. The bytecode runs exactly as shipped; only the
surrounding framework is supplied by us.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from importlib.machinery import SourcelessFileLoader
from pathlib import Path

import polybot
from polybot.strategy import base as _base
from polybot.strategy import market_data as _market_data

# Repo root: .../Bot-s1  (this file is polybot/strategy/loader.py -> parents[2]).
_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STRATEGY_PYC = _REPO_ROOT / "Strategy" / "s1_cheap_buy.cpython-313.pyc"

# Package the compiled bytecode believes it lives in (from its relative imports).
_STRATEGY_PACKAGE = "polybot.strategies"


def _alias(name: str, module: types.ModuleType) -> None:
    """Register ``module`` in ``sys.modules`` under ``name``, guarding collisions."""
    existing = sys.modules.get(name)
    if existing is not None and existing is not module:
        raise RuntimeError(
            f"sys.modules[{name!r}] is already bound to {existing!r}; "
            "refusing to load the compiled strategy against the wrong module."
        )
    sys.modules[name] = module


def _install_framework_aliases() -> types.ModuleType:
    """Wire framework modules onto the dotted names the bytecode imports."""
    # `from ..market_data import MarketState`  ->  polybot.market_data
    _alias("polybot.market_data", _market_data)
    setattr(polybot, "market_data", _market_data)

    # Synthetic parent package `polybot.strategies` for the compiled module + base.
    pkg = sys.modules.get(_STRATEGY_PACKAGE)
    if pkg is None:
        pkg = types.ModuleType(_STRATEGY_PACKAGE)
        pkg.__package__ = _STRATEGY_PACKAGE
        pkg.__path__ = []  # presence of __path__ marks this as a package
        sys.modules[_STRATEGY_PACKAGE] = pkg
        setattr(polybot, "strategies", pkg)

    # `from .base import ...`  ->  polybot.strategies.base
    _alias("polybot.strategies.base", _base)
    setattr(pkg, "base", _base)
    return pkg


def load_strategy_class(
    pyc_path: str | Path = DEFAULT_STRATEGY_PYC,
    *,
    class_name: str = "CheapBuyStrategy",
) -> type:
    """Import the compiled strategy and return its :class:`base.Strategy` subclass.

    Args:
        pyc_path: Path to the compiled ``.pyc`` strategy file.
        class_name: Expected strategy class name; falls back to the first
            ``base.Strategy`` subclass found in the module.

    Raises:
        FileNotFoundError: if the ``.pyc`` file is missing.
        ImportError / TypeError: if no valid strategy class can be loaded.
    """
    pyc_path = Path(pyc_path)
    if not pyc_path.is_file():
        raise FileNotFoundError(f"compiled strategy not found: {pyc_path}")

    pkg = _install_framework_aliases()

    stem = pyc_path.name.split(".")[0]  # "s1_cheap_buy.cpython-313.pyc" -> "s1_cheap_buy"
    module_name = f"{_STRATEGY_PACKAGE}.{stem}"  # polybot.strategies.s1_cheap_buy
    loader = SourcelessFileLoader(module_name, str(pyc_path))
    spec = importlib.util.spec_from_file_location(module_name, str(pyc_path), loader=loader)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"cannot create an import spec for {pyc_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    setattr(pkg, stem, module)
    # Runs the strategy's package-relative top-level imports against our framework.
    spec.loader.exec_module(module)

    strategy_cls = getattr(module, class_name, None)
    if strategy_cls is None:
        for obj in vars(module).values():
            if isinstance(obj, type) and issubclass(obj, _base.Strategy) and obj is not _base.Strategy:
                strategy_cls = obj
                break
    if strategy_cls is None:
        raise ImportError(f"no base.Strategy subclass found in {pyc_path}")
    if not (isinstance(strategy_cls, type) and issubclass(strategy_cls, _base.Strategy)):
        raise TypeError(f"{class_name!r} in {pyc_path} is not a base.Strategy subclass")
    return strategy_cls
