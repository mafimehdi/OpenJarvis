#!/usr/bin/env python3
"""MT5 bridge — expose a running MetaTrader 5 terminal as MCP tools.

Writing an Expert Advisor without the terminal is guesswork: the agent has to
assume a symbol's digits, stops level, contract size, tick value and filling
mode, and those assumptions are exactly what lot-sizing and stop-placement
code gets wrong. This server removes the guessing — an OpenJarvis agent can
ask the live terminal what a symbol actually allows, price a proposed trade
before the EA does, and (only if you opt in, and only on a demo account)
place an order to check that an execution path works.

Everything lives in this one file on purpose: the usual deployment is copying
it to the Windows machine that runs the terminal, so it must not drag a package
tree along. The one exception is optional: when ``tester_report.py`` sits next
to this file, the bridge also serves ``mt5_tester_report``,
``mt5_tester_compare``, ``mt5_tester_optimization`` and
``mt5_tester_forward_check`` so an agent can read Strategy Tester results as
numbers instead of guessing from a chart. Copy the whole ``mql_companion`` folder for
that; copy this file alone and the tools simply are not registered.

It speaks the MCP JSON-RPC protocol that ``openjarvis.mcp.client`` expects, so
it plugs into ``[tools.mcp]`` like any other server::

    [tools.mcp]
    enabled = true
    servers = '[{"name": "mt5", "command": "python",
                 "args": ["examples/mql_companion/mt5_mcp_server.py"]}]'

Two transports:

* **stdio** (default) — for a terminal on the same machine as OpenJarvis.
* **HTTP** (``--http --host 0.0.0.0 --port 8765 --token SECRET``) — for the
  common split where the terminal runs on a Windows box and OpenJarvis runs on
  a Linux VPS. Configure it as ``{"name": "mt5", "url":
  "http://windows-host:8765/", "token": "SECRET"}``.

Developing without a terminal? ``--stub`` serves a deterministic synthetic
market (fixed symbols, seeded price walk, two open positions) so the whole
path — protocol, schemas, validation, the agent's tool calls — is testable on
any OS. Every stub payload is marked ``"synthetic": true``.

Safety, in order of how much it matters:

1. ``mt5_order_send`` is not registered at all unless you pass
   ``--allow-trading``. An agent cannot call a tool it cannot see.
2. Even with that flag, the server refuses to trade any account whose
   ``trade_mode`` is not ``demo``. There is no flag that lifts this. If you
   accept the risk of agent-initiated live trading, the check is one function
   (``_assert_demo_account``) — edit it deliberately, not accidentally.
3. Market orders without a stop loss are refused (``--no-require-stops``
   disables that, and you should not).
4. Volumes are clamped to the symbol's min/max/step and prices normalized to
   its digits before anything is sent, so a hallucinated lot size becomes a
   clear error instead of a broker rejection.
5. The password is read only from the environment (``--password-env``,
   default ``MT5_PASSWORD``) so it never lands in a process listing or a
   shell history entry.

Stdout carries JSON-RPC and nothing else; all logging goes to stderr.
"""

from __future__ import annotations

import json
import logging
import math
import os
import random
import sys
import tempfile
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import click

# Allow running straight from a source checkout without installation.
_SRC = Path(__file__).resolve().parents[2] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# The tester-report reader is a sibling file, not a dependency: this bridge is
# often deployed on its own, and the live-market tools must keep working then.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from openjarvis.core.types import ToolResult  # noqa: E402
from openjarvis.mcp.protocol import (  # noqa: E402
    INTERNAL_ERROR,
    INVALID_REQUEST,
    PARSE_ERROR,
    MCPRequest,
    MCPResponse,
)
from openjarvis.mcp.server import MCPServer  # noqa: E402
from openjarvis.tools import BaseTool, ToolSpec  # noqa: E402

logger = logging.getLogger("mt5_mcp_server")

SERVER_NAME = "mt5"
SERVER_VERSION = "0.1.0"

try:
    import tester_report as tester_lib
except Exception as exc:  # pragma: no cover - depends on what was deployed
    tester_lib = None  # type: ignore[assignment]
    TESTER_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
else:
    TESTER_IMPORT_ERROR = ""

# Minutes per bar, which is what the stub sizes its bars with. These are NOT the
# integers the MT5 API takes: in MQL5 only M1..M30 equal their minute count, and
# PERIOD_H1 is 16385 (0x4001), D1 16408, W1 32769, MN1 49153
# (mql5.com/en/book/applications/timeseries/timeseries_symbol_period). The real
# backend therefore asks the package for its TIMEFRAME_* constant and falls back
# on ``_timeframe_code`` rather than passing these through.
TIMEFRAMES: Dict[str, int] = {
    "M1": 1,
    "M2": 2,
    "M3": 3,
    "M4": 4,
    "M5": 5,
    "M6": 6,
    "M10": 10,
    "M12": 12,
    "M15": 15,
    "M20": 20,
    "M30": 30,
    "H1": 60,
    "H2": 120,
    "H3": 180,
    "H4": 240,
    "H6": 360,
    "H8": 480,
    "H12": 720,
    "D1": 1440,
    "W1": 10080,
    "MN1": 43200,
}


def _timeframe_code(timeframe: str) -> int:
    """The ENUM_TIMEFRAMES integer for a name such as ``H4``.

    The encoding the MQL5 book spells out: minutes below an hour are the count
    itself, hours are ``0x4000 + n``, ``D1`` is ``0x4018`` and weeks and months
    are ``0x8001`` and ``0xC001``.
    """
    key = str(timeframe or "").strip().upper()
    if key not in TIMEFRAMES:
        raise Mt5Error(
            f"unknown timeframe {timeframe!r} — use one of: {', '.join(TIMEFRAMES)}"
        )
    if key == "D1":
        return 0x4018
    if key == "W1":
        return 0x8001
    if key == "MN1":
        return 0xC001
    count = int(key[1:])
    return count if key[0] == "M" else 0x4000 + count


TRADE_MODE_LABELS: Dict[int, str] = {0: "demo", 1: "contest", 2: "real"}
ORDER_TYPE_LABELS: Dict[int, str] = {
    0: "buy",
    1: "sell",
    2: "buy_limit",
    3: "sell_limit",
    4: "buy_stop",
    5: "sell_stop",
    6: "buy_stop_limit",
    7: "sell_stop_limit",
    8: "close_by",
}
#: ENUM_ACCOUNT_MARGIN_MODE, in the order the reference lists it (it prints no
#: numbers, so 0/1/2 are inferred from that order, as for the execution modes).
ACCOUNT_MARGIN_MODES: Dict[int, str] = {
    0: "retail_netting",
    1: "exchange",
    2: "retail_hedging",
}
#: ENUM_ACCOUNT_STOPOUT_MODE: whether the margin-call and stop-out levels are a
#: percentage of margin level or an amount in the deposit currency.
STOPOUT_MODES: Dict[int, str] = {0: "percent", 1: "money"}
#: SYMBOL_FILLING_MODE flags: FOK 1, IOC 2, BOC 4 ("Passive": book-or-cancel,
#: limit orders only). ``Return`` has no flag at all — the page lists it with "No
#: identifier" — because it is decided by the execution mode instead: allowed
#: always, except under Market execution
#: (mql5.com/en/docs/constants/environment_state/marketinfoconstants). Bit 4 was
#: once read here as "return", which it never was.
FILLING_BITS: Dict[int, str] = {1: "fok", 2: "ioc", 4: "boc"}
#: ENUM_SYMBOL_TRADE_EXECUTION, in the order the reference lists it.
EXECUTION_MODES: Dict[int, str] = {
    0: "request",
    1: "instant",
    2: "market",
    3: "exchange",
}
EXPIRATION_BITS: Dict[int, str] = {
    1: "gtc",
    2: "day",
    4: "specified",
    8: "specified_day",
}
#: ENUM_SYMBOL_SWAP_MODE, named by the reference's identifiers minus the
#: ``SYMBOL_SWAP_MODE_`` prefix. The reference's table carries no numbers; the
#: ones that a source states outright are DISABLED 0, POINTS 1, CURRENCY_SYMBOL
#: 2, CURRENCY_MARGIN 3 and INTEREST_OPEN 6 (the DoEasy library, mql5.com/en/
#: articles/7014, which also gives MQL4's numbering beside them). The rest follow
#: the declaration order, and CURRENCY_PROFIT is the identifier build 4540
#: appended, so 9 — the one value here no source writes as a number. Before this
#: table 3 was "percent", 5 "points_sl_tp" and 8 "points_currency_symbol", none
#: of which MQL5 has.
SWAP_MODES: Dict[int, str] = {
    0: "disabled",
    1: "points",
    2: "currency_symbol",
    3: "currency_margin",
    4: "currency_deposit",
    5: "interest_current",
    6: "interest_open",
    7: "reopen_current",
    8: "reopen_bid",
    9: "currency_profit",
}
#: What SYMBOL_SWAP_LONG and SYMBOL_SWAP_SHORT are measured in under each mode —
#: the reference says the mode "determines the units of measure" of both.
SWAP_UNITS: Dict[str, str] = {
    "disabled": "no swap",
    "points": "points",
    "currency_symbol": "base currency of the symbol",
    "currency_margin": "margin currency of the symbol",
    "currency_deposit": "deposit currency",
    "currency_profit": "profit currency",
    "interest_current": "annual % of the current price (360-day year)",
    "interest_open": "annual % of the position's open price (360-day year)",
    "reopen_current": "points; position reopened at the previous close",
    "reopen_bid": "points; position reopened at the new day's Bid",
}
#: Trade server return codes, transcribed from the MQL5 reference's "Return
#: Codes of the Trade Server" table
#: (mql5.com/en/docs/constants/errorswarnings/enum_trade_return_codes). Each
#: label is that page's constant name minus its ``TRADE_RETCODE_`` prefix, so the
#: table can be diffed against the documentation line by line — and
#: ``tests/examples/test_mt5_mcp_server.py`` pins exactly that, because three of
#: these labels were wrong (10027 called a client-side autotrading block a
#: timeout, 10030 called an invalid filling mode invalid stops, 10031 called a
#: lost server connection a closed market) and nothing caught it: the numeric
#: code beside each label was right, so a model reading only the number was fine
#: and a model reading the advice was misled. The vendor's table has no 10005 and
#: no 10037.
RETCODES: Dict[int, str] = {
    10004: "requote",
    10006: "reject",
    10007: "cancel",
    10008: "placed",
    10009: "done",
    10010: "done_partial",
    10011: "error",
    10012: "timeout",
    10013: "invalid",
    10014: "invalid_volume",
    10015: "invalid_price",
    10016: "invalid_stops",
    10017: "trade_disabled",
    10018: "market_closed",
    10019: "no_money",
    10020: "price_changed",
    10021: "price_off",
    10022: "invalid_expiration",
    10023: "order_changed",
    10024: "too_many_requests",
    10025: "no_changes",
    10026: "server_disables_at",
    10027: "client_disables_at",
    10028: "locked",
    10029: "frozen",
    10030: "invalid_fill",
    10031: "connection",
    10032: "only_real",
    10033: "limit_orders",
    10034: "limit_volume",
    10035: "invalid_order",
    10036: "position_closed",
    10038: "invalid_close_volume",
    10039: "close_order_exist",
    10040: "limit_positions",
    10041: "reject_cancel",
    10042: "long_only",
    10043: "short_only",
    10044: "close_only",
    10045: "fifo_close",
    10046: "hedge_prohibited",
}

#: The three codes that mean the request *succeeded*. ``PLACED`` is the one that
#: gets misread: a pending order that was accepted reports 10008, not 10009, so
#: treating "not DONE" as "did not happen" resends an order the server already
#: has. ``success`` is returned beside the label so the caller never has to
#: decide which codes are good.
RETCODE_SUCCESS: frozenset = frozenset({10008, 10009, 10010})

MAX_BARS = 2000
MAX_SYMBOLS = 500
_MAX_VOLUME_LOTS = 10_000.0


class Mt5Error(RuntimeError):
    """A bridge error meant for the agent to read and act on."""


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------


def _json_default(value: Any) -> Any:
    """numpy scalars, datetimes and anything exotic → JSON-able primitives."""
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return item()
        except (TypeError, ValueError):
            pass
    if isinstance(value, (datetime,)):
        return value.isoformat()
    return str(value)


def dumps(payload: Any) -> str:
    """Compact JSON: tool results go into a model's context window."""
    return json.dumps(payload, default=_json_default, separators=(",", ":"))


def _as_dict(obj: Any) -> Dict[str, Any]:
    """MT5 returns namedtuples, dicts and numpy records depending on call."""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return dict(obj)
    asdict = getattr(obj, "_asdict", None)
    if callable(asdict):
        return dict(asdict())
    names = getattr(getattr(obj, "dtype", None), "names", None)
    if names:
        return dict(zip(names, obj.tolist()))
    return {k: v for k, v in vars(obj).items() if not k.startswith("_")}


def _rows(obj: Any) -> List[Dict[str, Any]]:
    """Normalize a sequence (or numpy structured array) into dicts."""
    if obj is None:
        return []
    names = getattr(getattr(obj, "dtype", None), "names", None)
    if names:
        return [dict(zip(names, row)) for row in obj.tolist()]
    return [_as_dict(row) for row in obj]


def _iso(epoch_seconds: Any) -> Optional[str]:
    try:
        ts = float(epoch_seconds)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def _opt_float(value: Any) -> Optional[float]:
    """A float, or None when the terminal did not report the field at all."""
    return None if value is None else _f(value)


def _decode_bits(value: Any, bits: Dict[int, str]) -> List[str]:
    try:
        mask = int(value or 0)
    except (TypeError, ValueError):
        return []
    return [name for bit, name in bits.items() if mask & bit]


def _group_matches(name: str, group: str) -> bool:
    """``symbols_get(group=...)`` as the Python reference describes it.

    ``*`` is honoured at the beginning and the end of a condition only, so
    ``EUR*`` is a prefix, ``*XAU*`` a substring and a bare ``XAU`` is that exact
    name — it does not match ``XAUUSD``. Conditions are comma separated and
    applied in order, a leading ``!`` removing what earlier ones selected
    (mql5.com/en/docs/python_metatrader5/mt5symbolsget_py). That page words an
    exclusion without ``*`` as "names containing" it, so a bare ``!EUR`` is a
    substring test here although a bare inclusion is an exact name. The reference does
    not say whether matching is case sensitive, so this is not either.
    """
    conditions = [part.strip() for part in str(group or "").split(",") if part.strip()]
    if not conditions:
        return True
    wanted = name.upper()
    selected = False
    for condition in conditions:
        negate = condition.startswith("!")
        mask = (condition[1:] if negate else condition).strip().upper()
        head, tail = mask.startswith("*"), mask.endswith("*")
        core = mask.strip("*")
        if (head and tail) or (negate and not head and not tail):
            # A bare exclusion reads as "contains" in the reference's own
            # wording of ``"*,!EUR"`` (names containing EUR are dropped).
            hit = core in wanted
        elif head:
            hit = wanted.endswith(core)
        elif tail:
            hit = wanted.startswith(core)
        else:
            hit = wanted == core
        if hit:
            selected = not negate
    return selected


def _timeframe_minutes(timeframe: str) -> int:
    key = str(timeframe or "").strip().upper()
    if key not in TIMEFRAMES:
        raise Mt5Error(
            f"unknown timeframe {timeframe!r} — use one of: {', '.join(TIMEFRAMES)}"
        )
    return TIMEFRAMES[key]


def _f(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


# ---------------------------------------------------------------------------
# Terminal backends
# ---------------------------------------------------------------------------


class Terminal(ABC):
    """What the bridge needs from a trading terminal.

    Both backends return plain JSON-friendly dicts with *symbolic* values
    (``"buy"``, ``"demo"``, ISO timestamps). The real backend translates to
    and from MetaTrader5's integer constants; the stub never has to.
    """

    label = "terminal"
    synthetic = False

    @abstractmethod
    def connect(self) -> Dict[str, Any]:
        """Attach to the terminal. Raises Mt5Error on failure."""

    @abstractmethod
    def status(self) -> Dict[str, Any]:
        """Terminal + account connectivity at a glance."""

    @abstractmethod
    def account(self) -> Dict[str, Any]:
        """Balance, equity, margin and — critically — the trade mode."""

    @abstractmethod
    def symbols(
        self, pattern: str, visible_only: bool, limit: int
    ) -> List[Dict[str, Any]]:
        """Market watch rows with the fields an EA actually needs."""

    @abstractmethod
    def symbol_info(self, symbol: str) -> Dict[str, Any]:
        """Full contract specification for one symbol."""

    @abstractmethod
    def tick(self, symbol: str) -> Dict[str, Any]:
        """Latest quote."""

    @abstractmethod
    def rates(
        self, symbol: str, timeframe: str, count: int, shift: int
    ) -> List[Dict[str, Any]]:
        """OHLC bars, oldest first."""

    @abstractmethod
    def positions(
        self, symbol: Optional[str], magic: Optional[int]
    ) -> List[Dict[str, Any]]:
        """Open positions."""

    @abstractmethod
    def orders(self, symbol: Optional[str]) -> List[Dict[str, Any]]:
        """Pending (unfilled) orders."""

    @abstractmethod
    def calc(
        self,
        symbol: str,
        side: str,
        volume: float,
        price_open: Optional[float],
        price_close: Optional[float],
    ) -> Dict[str, Any]:
        """Margin and profit for a proposed trade."""

    @abstractmethod
    def order_send(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a market order. ``request`` uses symbolic values."""

    def close(self) -> None:
        """Release the terminal connection (best effort)."""


# SYMBOL_ORDER_MODE flags (mql5.com/en/docs/constants/environment_state/
# marketinfoconstants): one flag per *family* of order types, plus whether a
# stop loss or take profit may be attached. Each family stands for a buy and a
# sell. This table used to hold one bit per ORDER_TYPE (1 buy, 2 sell, 4
# buy_limit, ...), which is a different numbering: a market-only symbol
# (SYMBOL_ORDER_MARKET = 1) decoded as "buy" alone.
_ORDER_MODE_FLAGS: Dict[int, str] = {
    1: "market",
    2: "limit",
    4: "stop",
    8: "stop_limit",
    16: "sl",
    32: "tp",
    64: "close_by",
}
_ORDER_TYPES_BY_FLAG: Dict[int, Tuple[str, ...]] = {
    1: ("buy", "sell"),
    2: ("buy_limit", "sell_limit"),
    4: ("buy_stop", "sell_stop"),
    8: ("buy_stop_limit", "sell_stop_limit"),
    64: ("close_by",),
}


def _order_types(mask: Any) -> List[str]:
    """The order types a SYMBOL_ORDER_MODE mask allows."""
    try:
        value = int(mask or 0)
    except (TypeError, ValueError):
        return []
    out: List[str] = []
    for flag, names in _ORDER_TYPES_BY_FLAG.items():
        if value & flag:
            out.extend(names)
    return out


def _mode_flag(mask: Any, flag: int) -> bool:
    try:
        return bool(int(mask or 0) & flag)
    except (TypeError, ValueError):
        return False


class MetaTraderTerminal(Terminal):
    """The official ``MetaTrader5`` Python package.

    Windows-only in practice: the package talks to a running terminal over
    IPC. The import is lazy so ``--list-tools`` and ``--stub`` work anywhere,
    and a failed connection is a warning at startup rather than a dead
    process — every call retries through ``_ensure()``.
    """

    label = "MetaTrader5"

    def __init__(
        self,
        *,
        path: Optional[str] = None,
        login: Optional[str] = None,
        password: Optional[str] = None,
        trade_server: Optional[str] = None,
        timeout_ms: int = 60_000,
    ) -> None:
        self._path = path
        self._login = login
        self._password = password
        self._trade_server = trade_server
        self._timeout_ms = timeout_ms
        self._mt5: Any = None
        self._connected = False

    # -- plumbing ---------------------------------------------------------

    def _mod(self) -> Any:
        if self._mt5 is None:
            try:
                import MetaTrader5 as mt5
            except ImportError as exc:  # pragma: no cover - platform dependent
                raise Mt5Error(
                    "the MetaTrader5 Python package is not importable here. It "
                    "is Windows-only and needs a running terminal: install it "
                    "with `pip install MetaTrader5` on that machine, or serve "
                    "this bridge over --http from Windows to OpenJarvis "
                    "elsewhere. For development without a terminal, use --stub."
                ) from exc
            self._mt5 = mt5
        return self._mt5

    def _fail(self, what: str) -> None:
        try:
            code, message = self._mod().last_error()
        except Exception:  # pragma: no cover - defensive
            code, message = -1, "unknown"
        raise Mt5Error(f"{what} failed: [{code}] {message}")

    def _ensure(self) -> Any:
        mt5 = self._mod()
        if not self._connected:
            self.connect()
        return mt5

    # -- Terminal ---------------------------------------------------------

    def connect(self) -> Dict[str, Any]:
        mt5 = self._mod()
        kwargs: Dict[str, Any] = {"timeout": int(self._timeout_ms)}
        if self._path:
            kwargs["path"] = self._path
        if self._login:
            kwargs["login"] = int(self._login)
            kwargs["password"] = self._password or ""
            kwargs["server"] = self._trade_server or ""
        if not mt5.initialize(**kwargs):
            self._fail("initialize()")
        self._connected = True
        logger.info("connected to MetaTrader 5 terminal")
        return self.status()

    def close(self) -> None:
        if self._mt5 is not None and self._connected:
            try:
                self._mt5.shutdown()
            except Exception as exc:  # pragma: no cover - best effort
                logger.debug("shutdown() raised: %s", exc)
        self._connected = False

    def status(self) -> Dict[str, Any]:
        mt5 = self._ensure()
        info = mt5.terminal_info()
        if info is None:
            self._fail("terminal_info()")
        d = _as_dict(info)
        out: Dict[str, Any] = {
            "backend": self.label,
            "synthetic": False,
            "connected": bool(d.get("connected")),
            "build": d.get("build"),
            "terminal_path": d.get("path"),
            "company": d.get("company"),
            # TERMINAL_TRADE_ALLOWED: the Algo Trading button.
            "trade_allowed": bool(d.get("trade_allowed")),
            # terminal_info() has no trade_expert field (the reference lists
            # none); ACCOUNT_TRADE_EXPERT lives on account_info(), set below.
            "trade_expert": None,
            "tradeapi_disabled": bool(d.get("tradeapi_disabled")),
            "ping_ms": round(_f(d.get("ping_last")) / 1000.0, 3),
        }
        acct = mt5.account_info()
        if acct is not None:
            a = _as_dict(acct)
            out["trade_expert"] = bool(a.get("trade_expert"))
            mode = int(_f(a.get("trade_mode"), -1))
            out["account"] = {
                "login": a.get("login"),
                "server": a.get("server"),
                "trade_mode": TRADE_MODE_LABELS.get(mode, f"unknown({mode})"),
                "currency": a.get("currency"),
            }
        return out

    def account(self) -> Dict[str, Any]:
        mt5 = self._ensure()
        info = mt5.account_info()
        if info is None:
            self._fail("account_info()")
        d = _as_dict(info)
        mode = int(_f(d.get("trade_mode"), -1))
        raw_margin_mode = d.get("margin_mode")
        margin_mode = -1 if raw_margin_mode is None else int(_f(raw_margin_mode, -1))
        return {
            "synthetic": False,
            "login": d.get("login"),
            "server": d.get("server"),
            "currency": d.get("currency"),
            "balance": _f(d.get("balance")),
            "equity": _f(d.get("equity")),
            "margin": _f(d.get("margin")),
            "margin_free": _f(d.get("margin_free")),
            "margin_level_pct": _f(d.get("margin_level")),
            "profit": _f(d.get("profit")),
            "leverage": int(_f(d.get("leverage"), 1)),
            "trade_mode": TRADE_MODE_LABELS.get(mode, f"unknown({mode})"),
            "trade_mode_raw": mode,
            "trade_allowed": bool(d.get("trade_allowed")),
            "trade_expert": bool(d.get("trade_expert")),
            "margin_mode": ACCOUNT_MARGIN_MODES.get(margin_mode, "unknown"),
            "margin_mode_raw": d.get("margin_mode"),
            # Netting keeps one position per symbol; only hedging allows several.
            "hedging": None if margin_mode < 0 else margin_mode == 2,
            "fifo_close": bool(d.get("fifo_close")),
            "limit_orders": int(_f(d.get("limit_orders"))),
            "stop_out_mode": STOPOUT_MODES.get(
                int(_f(d.get("margin_so_mode"), -1)), "unknown"
            ),
            "margin_call_level": _f(d.get("margin_so_call")),
            "stop_out_level": _f(d.get("margin_so_so")),
        }

    def symbols(
        self, pattern: str, visible_only: bool, limit: int
    ) -> List[Dict[str, Any]]:
        mt5 = self._ensure()
        items = mt5.symbols_get(group=pattern) if pattern else mt5.symbols_get()
        if items is None:
            self._fail(f"symbols_get({pattern!r})")
        out: List[Dict[str, Any]] = []
        for raw in items:
            d = _as_dict(raw)
            if visible_only and not d.get("visible"):
                continue
            out.append(self._symbol_row(mt5, d))
            if len(out) >= limit:
                break
        return out

    @staticmethod
    def _symbol_row(mt5: Any, d: Dict[str, Any]) -> Dict[str, Any]:
        name = str(d.get("name") or "")
        point = _f(d.get("point"))
        bid = ask = None
        try:
            t = mt5.symbol_info_tick(name)
            if t is not None:
                td = _as_dict(t)
                bid, ask = _f(td.get("bid")), _f(td.get("ask"))
        except Exception as exc:  # pragma: no cover - terminal quirk
            logger.debug("symbol_info_tick(%s) raised: %s", name, exc)
        spread = None
        if bid is not None and ask is not None and point > 0:
            spread = int(round((ask - bid) / point))
        return {
            "symbol": name,
            "description": str(d.get("description") or "").strip(),
            "digits": int(_f(d.get("digits"))),
            "point": point,
            "bid": bid,
            "ask": ask,
            "spread_points": spread,
            "volume_min": _f(d.get("volume_min")),
            "volume_max": _f(d.get("volume_max")),
            "volume_step": _f(d.get("volume_step")),
            "stops_level_points": int(_f(d.get("trade_stops_level"))),
            "freeze_level_points": int(_f(d.get("trade_freeze_level"))),
            "contract_size": _f(d.get("trade_contract_size")),
            "tick_size": _f(d.get("trade_tick_size")),
            "tick_value": _f(d.get("trade_tick_value")),
            "currency_profit": d.get("currency_profit"),
            "visible": bool(d.get("visible")),
        }

    def symbol_info(self, symbol: str) -> Dict[str, Any]:
        mt5 = self._ensure()
        info = mt5.symbol_info(symbol)
        if info is None:
            self._fail(f"symbol_info({symbol!r})")
        d = _as_dict(info)
        out = self._symbol_row(mt5, d)
        raw_execution = d.get("trade_exemode")
        execution = (
            EXECUTION_MODES.get(int(_f(raw_execution, -1)))
            if raw_execution is not None
            else None
        )
        out.update(
            {
                "synthetic": False,
                "filling_modes": _decode_bits(d.get("filling_mode"), FILLING_BITS),
                "expiration_modes": _decode_bits(
                    d.get("expiration_mode"), EXPIRATION_BITS
                ),
                "swap_mode": SWAP_MODES.get(
                    int(_f(d.get("swap_mode"), -1)), str(d.get("swap_mode"))
                ),
                "swap_unit": SWAP_UNITS.get(
                    SWAP_MODES.get(int(_f(d.get("swap_mode"), -1)), ""), "unknown"
                ),
                "swap_long": _f(d.get("swap_long")),
                "swap_short": _f(d.get("swap_short")),
                "margin_currency": d.get("currency_margin"),
                "base_currency": d.get("currency_base"),
                "margin_hedged": _f(d.get("margin_hedged")),
                "session_buy_from": d.get("session_buy_from"),
                "session_buy_to": d.get("session_buy_to"),
                "order_types": _order_types(d.get("order_mode")),
                "sl_allowed": _mode_flag(d.get("order_mode"), 16),
                "tp_allowed": _mode_flag(d.get("order_mode"), 32),
                "order_mode_raw": d.get("order_mode"),
                "execution_mode": execution,
                # Return is not a flag in filling_mode: it is allowed under every
                # execution mode except Market (None when the mode is unknown).
                "return_fill_allowed": (
                    None if execution is None else execution != "market"
                ),
                # SYMBOL_TRADE_TICK_VALUE is just the profit-side value; a losing
                # position has its own, which is the one to size a stop-loss with.
                "tick_value_profit": _opt_float(d.get("trade_tick_value_profit")),
                "tick_value_loss": _opt_float(d.get("trade_tick_value_loss")),
                # Cap on position + pending volume in one direction, as given.
                "volume_limit": _opt_float(d.get("volume_limit")),
                "digits_raw": d.get("digits"),
            }
        )
        return out

    def tick(self, symbol: str) -> Dict[str, Any]:
        mt5 = self._ensure()
        t = mt5.symbol_info_tick(symbol)
        if t is None:
            # A symbol outside the market watch has no quotes yet. Enabling it
            # is what an EA's SymbolSelect() does, and it is harmless.
            try:
                mt5.symbol_select(symbol, True)
                t = mt5.symbol_info_tick(symbol)
            except Exception as exc:  # pragma: no cover - terminal quirk
                logger.debug("symbol_select(%s) raised: %s", symbol, exc)
        if t is None:
            self._fail(
                f"symbol_info_tick({symbol!r}) — is the symbol available on "
                "this broker/account? Check Market Watch in the terminal"
            )
        d = _as_dict(t)
        info = _as_dict(mt5.symbol_info(symbol))
        point = _f(info.get("point"))
        bid, ask = _f(d.get("bid")), _f(d.get("ask"))
        return {
            "synthetic": False,
            "symbol": symbol,
            "time": _iso(d.get("time")),
            "time_epoch": d.get("time"),
            "bid": bid,
            "ask": ask,
            "last": _f(d.get("last")) or None,
            "volume": d.get("volume_real") or d.get("volume"),
            "digits": int(_f(info.get("digits"))),
            "point": point,
            "spread_points": int(round((ask - bid) / point)) if point > 0 else None,
        }

    def rates(
        self, symbol: str, timeframe: str, count: int, shift: int
    ) -> List[Dict[str, Any]]:
        mt5 = self._ensure()
        _timeframe_minutes(timeframe)  # validates the name
        key = str(timeframe).strip().upper()
        code = getattr(mt5, f"TIMEFRAME_{key}", None)
        if not isinstance(code, int):
            code = _timeframe_code(key)
        bars = mt5.copy_rates_from_pos(symbol, code, int(shift), int(count))
        if bars is None or len(bars) == 0:
            self._fail(
                f"copy_rates_from_pos({symbol!r}, {timeframe}) — no history "
                "for that symbol/period on this account"
            )
        out = []
        for r in _rows(bars):
            out.append(
                {
                    "time": _iso(r.get("time")),
                    "time_epoch": r.get("time"),
                    "open": _f(r.get("open")),
                    "high": _f(r.get("high")),
                    "low": _f(r.get("low")),
                    "close": _f(r.get("close")),
                    "tick_volume": int(_f(r.get("tick_volume"))),
                    "spread_points": int(_f(r.get("spread"))),
                }
            )
        return out

    def positions(
        self, symbol: Optional[str], magic: Optional[int]
    ) -> List[Dict[str, Any]]:
        mt5 = self._ensure()
        rows = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
        if rows is None:
            self._fail(f"positions_get({symbol!r})")
        out = []
        for raw in rows:
            d = _as_dict(raw)
            if magic is not None and int(_f(d.get("magic"))) != int(magic):
                continue
            out.append(
                {
                    "ticket": d.get("ticket"),
                    "symbol": d.get("symbol"),
                    "side": ORDER_TYPE_LABELS.get(int(_f(d.get("type"))), "unknown"),
                    "volume": _f(d.get("volume")),
                    "price_open": _f(d.get("price_open")),
                    "price_current": _f(d.get("price_current")),
                    "sl": _f(d.get("sl")) or None,
                    "tp": _f(d.get("tp")) or None,
                    "profit": _f(d.get("profit")),
                    "swap": _f(d.get("swap")),
                    # The Python TradePosition has no commission field (the
                    # reference's own column list shows none); charges live on
                    # the deals. Report what the terminal gave, never a made-up 0.
                    "commission": (
                        None if d.get("commission") is None else _f(d["commission"])
                    ),
                    "identifier": d.get("identifier"),
                    "magic": int(_f(d.get("magic"))),
                    "comment": d.get("comment"),
                    "time_open": _iso(d.get("time")),
                }
            )
        return out

    def orders(self, symbol: Optional[str]) -> List[Dict[str, Any]]:
        mt5 = self._ensure()
        rows = mt5.orders_get(symbol=symbol) if symbol else mt5.orders_get()
        if rows is None:
            self._fail(f"orders_get({symbol!r})")
        out = []
        for raw in rows:
            d = _as_dict(raw)
            out.append(
                {
                    "ticket": d.get("ticket"),
                    "symbol": d.get("symbol"),
                    "type": ORDER_TYPE_LABELS.get(int(_f(d.get("type"))), "unknown"),
                    "volume": _f(d.get("volume_current") or d.get("volume_initial")),
                    "price_open": _f(d.get("price_open")),
                    "price_stoplimit": _f(d.get("price_stoplimit")) or None,
                    "sl": _f(d.get("sl")) or None,
                    "tp": _f(d.get("tp")) or None,
                    # 0 means "no expiry" (a GTC order), not 1970-01-01.
                    "expiration": _iso(d.get("time_expiration") or None),
                    "magic": int(_f(d.get("magic"))),
                    "comment": d.get("comment"),
                    "time_setup": _iso(d.get("time_setup")),
                }
            )
        return out

    def calc(
        self,
        symbol: str,
        side: str,
        volume: float,
        price_open: Optional[float],
        price_close: Optional[float],
    ) -> Dict[str, Any]:
        mt5 = self._ensure()
        order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
        info = _as_dict(mt5.symbol_info(symbol))
        if not info:
            self._fail(f"symbol_info({symbol!r})")
        t = _as_dict(mt5.symbol_info_tick(symbol))
        bid, ask = _f(t.get("bid")), _f(t.get("ask"))
        open_price = _f(price_open) if price_open else (ask if side == "buy" else bid)
        close_price = (
            _f(price_close) if price_close else (bid if side == "buy" else ask)
        )
        volume = float(volume)
        margin = mt5.order_calc_margin(order_type, symbol, volume, open_price)
        if margin is None:
            self._fail("order_calc_margin()")
        profit = mt5.order_calc_profit(
            order_type, symbol, volume, open_price, close_price
        )
        if profit is None:
            self._fail("order_calc_profit()")
        per_lot = mt5.order_calc_margin(order_type, symbol, 1.0, open_price)
        account = _as_dict(mt5.account_info())
        return {
            "synthetic": False,
            "symbol": symbol,
            "side": side,
            "volume": volume,
            "price_open": open_price,
            "price_close": close_price,
            "margin_required": _f(margin),
            "margin_per_lot": _f(per_lot),
            "profit_at_close": _f(profit),
            "account_currency": account.get("currency"),
            "currency_profit": info.get("currency_profit"),
            "tick_size": _f(info.get("trade_tick_size")),
            "tick_value": _f(info.get("trade_tick_value")),
            "contract_size": _f(info.get("trade_contract_size")),
            "digits": int(_f(info.get("digits"))),
            "stops_level_points": int(_f(info.get("trade_stops_level"))),
            "volume_step": _f(info.get("volume_step")),
            "note": (
                "margin_required, margin_per_lot and profit_at_close are in the "
                "ACCOUNT currency (order_calc_margin and order_calc_profit "
                "return account currency, not the symbol's profit currency), so "
                "they compare with equity as they are"
            ),
        }

    def order_send(self, request: Dict[str, Any]) -> Dict[str, Any]:
        mt5 = self._ensure()
        filling = request.get("filling") or "ioc"
        native = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": request["symbol"],
            "volume": float(request["volume"]),
            "type": mt5.ORDER_TYPE_BUY
            if request["side"] == "buy"
            else mt5.ORDER_TYPE_SELL,
            "price": float(request["price"]),
            "deviation": int(request.get("deviation", 10)),
            "magic": int(request.get("magic", 0)),
            "comment": str(request.get("comment") or "")[:31],
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": {
                "fok": mt5.ORDER_FILLING_FOK,
                "ioc": mt5.ORDER_FILLING_IOC,
                "return": mt5.ORDER_FILLING_RETURN,
            }.get(str(filling), mt5.ORDER_FILLING_IOC),
        }
        if request.get("sl"):
            native["sl"] = float(request["sl"])
        if request.get("tp"):
            native["tp"] = float(request["tp"])
        result = mt5.order_send(native)
        if result is None:
            self._fail("order_send()")
        d = _as_dict(result)
        retcode = int(_f(d.get("retcode")))
        return {
            "retcode": retcode,
            "retcode_message": RETCODES.get(retcode, f"unknown({retcode})"),
            "success": retcode in RETCODE_SUCCESS,
            "deal": d.get("deal"),
            "order": d.get("order"),
            "volume": _f(d.get("volume")),
            "price": _f(d.get("price")),
            "comment": d.get("comment"),
            "request_id": d.get("request_id"),
        }


# ---------------------------------------------------------------------------
# Stub backend — a deterministic synthetic market
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _StubSymbol:
    name: str
    description: str
    digits: int
    base: float
    spread_points: int
    contract_size: float
    tick_value: float
    stops_level_points: int
    volume_min: float
    volume_max: float
    volume_step: float
    currency_profit: str = "USD"

    @property
    def point(self) -> float:
        """Smallest price increment — MT5 calls this SYMBOL_POINT."""
        return 10**-self.digits


STUB_ACCOUNT_CURRENCY = "USD"


def _stub_margin(spec: _StubSymbol, price: float, volume: float) -> float:
    """Margin in the account currency at leverage 100.

    Margin is charged in the symbol's base currency, so it is already in USD
    for USDJPY and needs the price for every symbol quoted *in* USD.
    """
    units = volume * spec.contract_size
    if spec.name[:3] == STUB_ACCOUNT_CURRENCY:
        return units / 100.0
    return units * price / 100.0


def _stub_to_account(spec: _StubSymbol, amount: float, price: float) -> float:
    """A profit-currency amount in the account currency (USD).

    The stub only has to convert a JPY profit on USDJPY: divide by that pair's
    own price. Every other stub symbol already earns in USD.
    """
    if spec.currency_profit == STUB_ACCOUNT_CURRENCY or price <= 0:
        return amount
    if spec.name == STUB_ACCOUNT_CURRENCY + spec.currency_profit:
        return amount / price
    return amount


_STUB_SYMBOLS: Tuple[_StubSymbol, ...] = (
    _StubSymbol(
        "EURUSD",
        "Euro vs US Dollar",
        5,
        1.08500,
        12,
        100_000.0,
        10.0,
        10,
        0.01,
        100.0,
        0.01,
    ),
    _StubSymbol(
        "GBPUSD",
        "Pound Sterling vs US Dollar",
        5,
        1.26750,
        15,
        100_000.0,
        10.0,
        12,
        0.01,
        100.0,
        0.01,
    ),
    _StubSymbol(
        "USDJPY",
        "US Dollar vs Japanese Yen",
        3,
        151.250,
        14,
        100_000.0,
        6.6,
        10,
        0.01,
        100.0,
        0.01,
        currency_profit="JPY",
    ),
    _StubSymbol(
        "XAUUSD",
        "Gold vs US Dollar",
        2,
        2350.00,
        25,
        100.0,
        1.0,
        30,
        0.01,
        50.0,
        0.01,
    ),
    _StubSymbol(
        "BTCUSD",
        "Bitcoin vs US Dollar",
        2,
        64000.00,
        2500,
        1.0,
        1.0,
        500,
        0.01,
        10.0,
        0.01,
    ),
)


class StubTerminal(Terminal):
    """A deterministic synthetic terminal for development and tests.

    Prices come from a seeded random walk indexed by ``(symbol, bar)``, so a
    given bar always has the same OHLC no matter when you ask, and two calls
    inside the same bar return identical quotes. Pass ``now_fn`` to pin the
    clock in tests.
    """

    label = "stub"
    synthetic = True

    def __init__(
        self,
        *,
        trade_mode: str = "demo",
        now_fn: Optional[Callable[[], float]] = None,
        balance: float = 10_000.0,
    ) -> None:
        self._trade_mode = trade_mode
        self._now_fn = now_fn or (lambda: datetime.now(tz=timezone.utc).timestamp())
        self._balance = balance
        self._by_name = {s.name: s for s in _STUB_SYMBOLS}
        self._next_ticket = 900_001
        self._positions: List[Dict[str, Any]] = self._seed_positions()
        self._orders: List[Dict[str, Any]] = [
            {
                "ticket": 900_101,
                "symbol": "EURUSD",
                "type": "buy_limit",
                "volume": 0.05,
                "price_open": self._price("EURUSD", 0) - 0.00200,
                "price_stoplimit": None,
                "sl": self._price("EURUSD", 0) - 0.00400,
                "tp": self._price("EURUSD", 0) + 0.00400,
                "expiration": None,
                "magic": 20260929,
                "comment": "stub limit",
                "time_setup": _iso(self._now()),
            }
        ]

    # -- synthetic market -------------------------------------------------

    def _now(self) -> float:
        return float(self._now_fn())

    def _sym(self, symbol: str) -> _StubSymbol:
        spec = self._by_name.get(str(symbol or "").upper())
        if spec is None:
            raise Mt5Error(
                f"unknown symbol {symbol!r} — stub market has: "
                f"{', '.join(self._by_name)}"
            )
        return spec

    @staticmethod
    def _walk(symbol: str, minutes: int, index: int, digits: int) -> float:
        """Seeded, reproducible relative drift for one bar.

        The seed includes the period, so M1 and H1 bars disagree (as they
        must), while any given (symbol, period, bar) is stable forever.
        Volatility scales with sqrt(period), the usual diffusion rule.
        """
        rng = random.Random(f"{symbol}:{minutes}:{index}")
        # ~0.006% per M1 bar, scaling with sqrt(period: ~0.05% on H1, ~0.25%
        # on D1 — the right order of magnitude for majors.
        sigma = 0.00006 * math.sqrt(minutes)
        drift = rng.gauss(0.0, sigma) + math.sin(index / 24.0) * sigma / 3.0
        return round(drift, digits + 4)

    def _price(self, symbol: str, shift: int = 0, minutes: int = 1) -> float:
        spec = self._sym(symbol)
        index = int(self._now() // (minutes * 60)) - int(shift)
        drift = self._walk(spec.name, minutes, index, spec.digits)
        return round(spec.base * (1.0 + drift), spec.digits)

    def _bid_ask(self, symbol: str, shift: int = 0) -> Tuple[float, float]:
        spec = self._sym(symbol)
        mid = self._price(symbol, shift)
        half = round(spec.spread_points * spec.point / 2.0, spec.digits)
        return round(mid - half, spec.digits), round(mid + half, spec.digits)

    def _bar(self, symbol: str, shift: int, minutes: int) -> Dict[str, Any]:
        """One synthetic bar.

        ``_price(index)`` is the *close* of that bar, so a bar opens at the
        previous bar's close. That keeps the series continuous without having
        to accumulate a walk from a fixed anchor.
        """
        spec = self._sym(symbol)
        index = int(self._now() // (minutes * 60)) - int(shift)
        rng = random.Random(f"bar:{symbol}:{minutes}:{index}")
        close = self._price(symbol, shift, minutes)
        open_ = self._price(symbol, shift + 1, minutes)
        span = max(
            spec.point,
            open_ * rng.uniform(0.00002, 0.00025) * math.sqrt(minutes),
        )
        high = round(max(open_, close) + rng.uniform(0, span), spec.digits)
        low = round(min(open_, close) - rng.uniform(0, span), spec.digits)
        ts = index * (minutes * 60)
        return {
            "time": _iso(ts),
            "time_epoch": ts,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "tick_volume": rng.randint(80, 900),
            "spread_points": spec.spread_points,
        }

    def _seed_positions(self) -> List[Dict[str, Any]]:
        out = []
        # (side, symbol, volume, magic, entry offset) — the offset is chosen
        # per symbol so both seeded positions start in profit.
        for side, symbol, volume, magic, offset in (
            ("buy", "EURUSD", 0.10, 20260929, -0.00150),
            ("sell", "XAUUSD", 0.05, 20260929, 12.50),
        ):
            spec = self._sym(symbol)
            bid, ask = self._bid_ask(symbol)
            entry = ask if side == "buy" else bid
            opened = round(entry + offset, spec.digits)
            current = bid if side == "buy" else ask
            direction = 1.0 if side == "buy" else -1.0
            profit = (current - opened) * direction * volume * spec.contract_size
            point = 10**-spec.digits
            out.append(
                {
                    "ticket": self._next_ticket,
                    "symbol": symbol,
                    "side": side,
                    "volume": volume,
                    "price_open": round(opened, spec.digits),
                    "price_current": current,
                    "sl": round(entry - direction * 250 * point, spec.digits),
                    "tp": round(entry + direction * 500 * point, spec.digits),
                    "profit": round(profit, 2),
                    "swap": -0.42,
                    "commission": -0.70,
                    "magic": magic,
                    "comment": "stub position",
                    "time_open": _iso(self._now() - 7200),
                }
            )
            self._next_ticket += 1
        return out

    # -- Terminal ---------------------------------------------------------

    def connect(self) -> Dict[str, Any]:
        return self.status()

    def status(self) -> Dict[str, Any]:
        return {
            "backend": self.label,
            "synthetic": True,
            "connected": True,
            "build": 0,
            "terminal_path": "(stub)",
            "company": "OpenJarvis Stub Broker",
            "trade_allowed": True,
            "trade_expert": True,
            "tradeapi_disabled": False,
            "ping_ms": 0.0,
            "account": {
                "login": 1000001,
                "server": "Stub-Demo",
                "trade_mode": self._trade_mode,
                "currency": "USD",
            },
            "note": (
                "synthetic market — numbers are deterministic fiction, not "
                "quotes. Use it to develop, never to decide anything real."
            ),
        }

    def account(self) -> Dict[str, Any]:
        live = [self._refresh(p) for p in self._positions]
        profit = round(sum(p["profit"] + p["swap"] + p["commission"] for p in live), 2)
        equity = round(self._balance + profit, 2)
        margin = round(sum(self._margin_for(p) for p in live), 2)
        mode = {"demo": 0, "contest": 1, "real": 2}.get(self._trade_mode, 0)
        return {
            "synthetic": True,
            "login": 1000001,
            "server": "Stub-Demo",
            "currency": "USD",
            "balance": self._balance,
            "equity": equity,
            "margin": margin,
            "margin_free": round(equity - margin, 2),
            "margin_level_pct": round(equity / margin * 100.0, 2) if margin else 0.0,
            "profit": profit,
            "leverage": 100,
            "trade_mode": self._trade_mode,
            "trade_mode_raw": mode,
            "trade_allowed": True,
            "trade_expert": True,
            "margin_mode": "retail_hedging",
            "margin_mode_raw": 2,
            "hedging": True,
            "fifo_close": False,
            "limit_orders": 200,
            "stop_out_mode": "percent",
            "margin_call_level": 100.0,
            "stop_out_level": 50.0,
        }

    def _refresh(self, position: Dict[str, Any]) -> Dict[str, Any]:
        """Re-price a position against the current synthetic quote.

        Both ``positions()`` and ``account()`` go through here, so floating
        profit and equity can never disagree with each other.
        """
        spec = self._by_name[str(position["symbol"])]
        bid, ask = self._bid_ask(spec.name)
        current = bid if position["side"] == "buy" else ask
        direction = 1.0 if position["side"] == "buy" else -1.0
        profit = _stub_to_account(
            spec,
            (current - position["price_open"])
            * direction
            * position["volume"]
            * spec.contract_size,
            current,
        )
        row = dict(position)
        row["price_current"] = current
        row["profit"] = round(profit, 2)
        row["synthetic"] = True
        return row

    def _margin_for(self, position: Dict[str, Any]) -> float:
        spec = self._by_name.get(str(position["symbol"]))
        if spec is None:
            return 0.0
        bid, ask = self._bid_ask(spec.name)
        price = ask if position["side"] == "buy" else bid
        return round(_stub_margin(spec, price, position["volume"]), 2)

    def symbols(
        self, pattern: str, visible_only: bool, limit: int
    ) -> List[Dict[str, Any]]:
        out = []
        for spec in _STUB_SYMBOLS:
            if not _group_matches(spec.name, pattern):
                continue
            out.append(self._symbol_row(spec))
            if len(out) >= limit:
                break
        return out

    def _symbol_row(self, spec: _StubSymbol) -> Dict[str, Any]:
        bid, ask = self._bid_ask(spec.name)
        point = 10**-spec.digits
        return {
            "symbol": spec.name,
            "description": spec.description,
            "digits": spec.digits,
            "point": point,
            "bid": bid,
            "ask": ask,
            "spread_points": spec.spread_points,
            "volume_min": spec.volume_min,
            "volume_max": spec.volume_max,
            "volume_step": spec.volume_step,
            "stops_level_points": spec.stops_level_points,
            "freeze_level_points": 0,
            "contract_size": spec.contract_size,
            "tick_size": point,
            "tick_value": spec.tick_value,
            "currency_profit": spec.currency_profit,
            "visible": True,
            "synthetic": True,
        }

    def symbol_info(self, symbol: str) -> Dict[str, Any]:
        row = self._symbol_row(self._sym(symbol))
        row.update(
            {
                "filling_modes": ["fok", "ioc"],
                "execution_mode": "instant",
                "return_fill_allowed": True,
                "expiration_modes": ["gtc", "day", "specified"],
                "swap_mode": "points",
                "swap_unit": SWAP_UNITS["points"],
                "tick_value_profit": row["tick_value"],
                "tick_value_loss": row["tick_value"],
                "volume_limit": 0.0,
                "swap_long": -1.2,
                "swap_short": -0.8,
                "margin_currency": "USD",
                "base_currency": row["symbol"][:3],
                "margin_hedged": 0.0,
                "session_buy_from": 0,
                "session_buy_to": 86_399,
                "order_types": _order_types(63),
                "sl_allowed": True,
                "tp_allowed": True,
                "order_mode_raw": 63,
                "digits_raw": row["digits"],
            }
        )
        return row

    def tick(self, symbol: str) -> Dict[str, Any]:
        spec = self._sym(symbol)
        bid, ask = self._bid_ask(symbol)
        now = self._now()
        rng = random.Random(f"vol:{symbol}:{int(now)}")
        return {
            "synthetic": True,
            "symbol": spec.name,
            "time": _iso(now),
            "time_epoch": int(now),
            "bid": bid,
            "ask": ask,
            "last": None,
            "volume": rng.randint(1, 40),
            "digits": spec.digits,
            "point": 10**-spec.digits,
            "spread_points": spec.spread_points,
        }

    def rates(
        self, symbol: str, timeframe: str, count: int, shift: int
    ) -> List[Dict[str, Any]]:
        spec = self._sym(symbol)
        minutes = _timeframe_minutes(timeframe)
        return [
            self._bar(spec.name, shift + i, minutes) for i in range(count - 1, -1, -1)
        ]

    def positions(
        self, symbol: Optional[str], magic: Optional[int]
    ) -> List[Dict[str, Any]]:
        out = []
        for p in self._positions:
            if symbol and p["symbol"] != str(symbol).upper():
                continue
            if magic is not None and p["magic"] != int(magic):
                continue
            out.append(self._refresh(p))
        return out

    def orders(self, symbol: Optional[str]) -> List[Dict[str, Any]]:
        out = []
        for o in self._orders:
            if symbol and o["symbol"] != symbol.upper():
                continue
            row = dict(o)
            row["synthetic"] = True
            out.append(row)
        return out

    def calc(
        self,
        symbol: str,
        side: str,
        volume: float,
        price_open: Optional[float],
        price_close: Optional[float],
    ) -> Dict[str, Any]:
        spec = self._sym(symbol)
        bid, ask = self._bid_ask(symbol)
        open_price = _f(price_open) if price_open else (ask if side == "buy" else bid)
        close_price = (
            _f(price_close) if price_close else (bid if side == "buy" else ask)
        )
        volume = float(volume)
        direction = 1.0 if side == "buy" else -1.0
        profit = _stub_to_account(
            spec,
            (close_price - open_price) * direction * volume * spec.contract_size,
            close_price,
        )
        point = 10**-spec.digits
        return {
            "synthetic": True,
            "symbol": spec.name,
            "side": side,
            "volume": volume,
            "price_open": round(open_price, spec.digits),
            "price_close": round(close_price, spec.digits),
            "margin_required": round(_stub_margin(spec, open_price, volume), 2),
            "margin_per_lot": round(_stub_margin(spec, open_price, 1.0), 2),
            "account_currency": STUB_ACCOUNT_CURRENCY,
            "currency_profit": spec.currency_profit,
            "profit_at_close": round(profit, 2),
            "tick_size": point,
            "tick_value": spec.tick_value,
            "contract_size": spec.contract_size,
            "digits": spec.digits,
            "stops_level_points": spec.stops_level_points,
            "volume_step": spec.volume_step,
            "note": (
                "synthetic margin model: lots x contract / 100 (leverage 100), "
                "times the price unless the base currency is USD. Margin and "
                "profit are in the account currency, as order_calc_margin and "
                "order_calc_profit return them"
            ),
        }

    def order_send(self, request: Dict[str, Any]) -> Dict[str, Any]:
        spec = self._sym(str(request["symbol"]))
        bid, ask = self._bid_ask(spec.name)
        side = request["side"]
        price = _f(request.get("price")) or (ask if side == "buy" else bid)
        ticket = self._next_ticket
        self._next_ticket += 1
        self._positions.append(
            {
                "ticket": ticket,
                "symbol": spec.name,
                "side": side,
                "volume": float(request["volume"]),
                "price_open": price,
                "price_current": bid if side == "buy" else ask,
                "sl": request.get("sl"),
                "tp": request.get("tp"),
                "profit": 0.0,
                "swap": 0.0,
                "commission": -0.70,
                "magic": int(request.get("magic", 0)),
                "comment": str(request.get("comment") or "stub fill"),
                "time_open": _iso(self._now()),
            }
        )
        return {
            "retcode": 10009,
            "retcode_message": RETCODES[10009],
            "success": 10009 in RETCODE_SUCCESS,
            "deal": ticket,
            "order": ticket,
            "volume": float(request["volume"]),
            "price": price,
            "comment": "stub fill",
            "request_id": int(request.get("magic", 0)),
        }


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------


def _s(desc: str) -> Dict[str, Any]:
    return {"type": "string", "description": desc}


def _enum(values: Sequence[str], desc: str) -> Dict[str, Any]:
    return {"type": "string", "enum": list(values), "description": desc}


def _num(desc: str, **extra: Any) -> Dict[str, Any]:
    return {"type": "number", "description": desc, **extra}


def _int(desc: str, **extra: Any) -> Dict[str, Any]:
    return {"type": "integer", "description": desc, **extra}


def _bool(desc: str, **extra: Any) -> Dict[str, Any]:
    return {"type": "boolean", "description": desc, **extra}


def _arr(desc: str, item_type: str = "string", **extra: Any) -> Dict[str, Any]:
    return {
        "type": "array",
        "items": {"type": item_type},
        "description": desc,
        **extra,
    }


def _schema(props: Dict[str, Any], required: Sequence[str] = ()) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "type": "object",
        "properties": props,
        "additionalProperties": False,
    }
    if required:
        out["required"] = list(required)
    return out


@dataclass(frozen=True)
class ToolDef:
    """One MCP tool: schema, handler, and how the client may treat it."""

    name: str
    description: str
    schema: Dict[str, Any]
    handler: Callable[..., Any]
    timeout: float = 30.0
    read_only: bool = True
    #: MCP ``openWorldHint``: does the answer come from outside a closed domain
    #: this process controls? Defaults to True, which is the spec's default and
    #: the honest answer for a bridge whose quotes, account state and orders all
    #: come from a broker's server. The tools that only parse files on this
    #: machine are listed in ``CLOSED_WORLD_TOOLS``.
    open_world: bool = True


#: Tools whose whole job is reading a file this machine already has. Everything
#: else reaches a terminal that is talking to a broker, so it stays open-world.
CLOSED_WORLD_TOOLS: frozenset = frozenset(
    {
        "mt5_tester_report",
        "mt5_tester_compare",
        "mt5_tester_optimization",
        "mt5_tester_forward_check",
    }
)


class BridgeTool(BaseTool):
    """Adapts a ``ToolDef`` to OpenJarvis's ``BaseTool`` interface."""

    tool_id = "mt5_bridge"

    def __init__(self, defn: ToolDef) -> None:
        self._defn = defn

    @property
    def defn(self) -> ToolDef:
        return self._defn

    @property
    def spec(self) -> ToolSpec:
        d = self._defn
        return ToolSpec(
            name=d.name,
            description=d.description,
            parameters=d.schema,
            category="trading",
            timeout_seconds=d.timeout,
            # ``requires_confirmation`` is deliberately left at its default.
            # ToolExecutor refuses a tool that declares it when no confirmation
            # callback is plumbed, and the MCP path this bridge is built for has
            # none — so setting it would not add a human to the loop, it would
            # make the tool uncallable. Enforcement stays where it can actually
            # run: the tool is not registered without --allow-trading (and
            # mt5_tester_run without --allow-tester), _assert_demo_account
            # refuses anything that is not a demo account, and the annotations
            # below tell a host that this reaches a broker so it can route the
            # call through its own approval flow. Adding a per-call prompt means
            # plumbing a callback through build_server (or queuing into
            # ApprovalStore), which is a change to the bridge's contract.
            metadata={
                "source": "mt5-mcp",
                "read_only": d.read_only,
                "open_world": d.open_world,
            },
        )

    def execute(self, **params: Any) -> ToolResult:
        d = self._defn
        allowed = set(d.schema.get("properties", {}))
        unknown = sorted(set(params) - allowed)
        if unknown:
            accepted = ", ".join(sorted(allowed)) or "(none)"
            return ToolResult(
                tool_name=d.name,
                success=False,
                content=(
                    f"Unknown argument(s): {', '.join(unknown)}. Accepted: {accepted}."
                ),
            )
        missing = [
            key for key in d.schema.get("required", []) if params.get(key) in (None, "")
        ]
        if missing:
            return ToolResult(
                tool_name=d.name,
                success=False,
                content=f"Missing required argument(s): {', '.join(missing)}.",
            )
        try:
            payload = d.handler(**params)
        except Mt5Error as exc:
            return ToolResult(tool_name=d.name, success=False, content=str(exc))
        return ToolResult(tool_name=d.name, success=True, content=dumps(payload))


def _assert_demo_account(terminal: Terminal) -> Dict[str, Any]:
    """The hard gate: no agent-initiated order on a non-demo account.

    Deliberately not configurable from the command line. If you decide to run
    an agent against live money, change this function on purpose and read the
    consequences in the README first.
    """
    account = terminal.account()
    mode = str(account.get("trade_mode") or "")
    if mode != "demo":
        raise Mt5Error(
            f"refusing to send an order: the connected account reports "
            f"trade_mode={mode!r}, not 'demo'. The bridge only executes trades "
            "on demo accounts, whatever flags it was started with. Test the EA "
            "in the Strategy Tester and on a demo account; if you truly want "
            "agent-initiated live trading, edit _assert_demo_account() in "
            "examples/mql_companion/mt5_mcp_server.py — do not route around it "
            "by accident."
        )
    return account


def _clamp_count(value: Any, default: int, maximum: int) -> int:
    try:
        n = int(value if value is not None else default)
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, maximum))


def _normalize_volume(volume: Any, info: Dict[str, Any], symbol: str) -> float:
    step = _f(info.get("volume_step"), 0.01) or 0.01
    low = _f(info.get("volume_min"), step)
    high = _f(info.get("volume_max"), _MAX_VOLUME_LOTS)
    try:
        raw = float(volume)
    except (TypeError, ValueError):
        raise Mt5Error(f"volume must be a number, got {volume!r}") from None
    if not math.isfinite(raw) or raw <= 0:
        raise Mt5Error(f"volume must be positive, got {raw}")
    # Deliberately strict: rounding a requested volume silently changes the
    # risk of the trade, so a non-multiple is an error, not a convenience.
    steps = raw / step
    if abs(steps - round(steps)) > 1e-6:
        below = math.floor(steps + 1e-9) * step
        above = math.ceil(steps - 1e-9) * step
        raise Mt5Error(
            f"{symbol}: volume {raw} is not a multiple of the lot step {step} "
            f"(nearest valid: {round(below, 8)} or {round(above, 8)})"
        )
    normalized = round(round(steps) * step, 8)
    if normalized < low - 1e-9:
        raise Mt5Error(f"{symbol}: volume {normalized} is below the minimum {low}")
    if normalized > high + 1e-9:
        raise Mt5Error(f"{symbol}: volume {normalized} is above the maximum {high}")
    return normalized


def _normalize_price(price: float, info: Dict[str, Any], symbol: str) -> float:
    """Snap a price to the symbol's tick grid and digit count.

    This is the Python equivalent of MQL5's ``NormalizeDouble`` with
    ``SYMBOL_TRADE_TICK_SIZE`` — moving a price by less than half a tick is
    what every EA does before sending, so it is a convenience, not a risk.
    A price that is *far* from the market is a different mistake and is
    caught by ``_check_price_is_tradable``.
    """
    digits = int(_f(info.get("digits"), 5))
    tick_size = _f(info.get("tick_size")) or (10**-digits)
    return round(round(price / tick_size) * tick_size, digits)


def _check_price_is_tradable(
    price: float, live: float, point: float, deviation: int, symbol: str
) -> None:
    """Reject a market order whose price is nowhere near the quote.

    The broker would answer with a requote or ``price_off``, but an agent
    learns far more from "you invented this number" than from retcode 10021.
    """
    if live <= 0:
        return
    tolerance = max(20 * max(1, int(deviation)) * point, live * 0.002)
    if abs(price - live) > tolerance:
        raise Mt5Error(
            f"{symbol}: price {price} is {abs(price - live):.6f} away from the "
            f"current quote {live} — too far for a market order (tolerance "
            f"{tolerance:.6f}). Omit `price` to trade at the live quote, or "
            "use mt5_tick to read it first."
        )


def _resolve_filling(
    requested: Optional[str], info: Dict[str, Any], symbol: str
) -> str:
    """Pick the filling policy a market order on this symbol can use.

    FOK and IOC are flags in SYMBOL_FILLING_MODE, which only Market and
    Exchange execution consult (Request and Instant accept both whatever the
    flags say); RETURN is allowed under every execution mode except Market.
    A policy the symbol does not allow comes back
    from the server as retcode 10030, so it is refused here with the allowed
    set named. With none requested IOC wins, then FOK, then RETURN. When
    ``info`` carries no filling data at all the old IOC default stands.
    """
    flags = info.get("filling_modes")
    return_ok = info.get("return_fill_allowed")
    if flags is None and return_ok is None:
        return str(requested or "ioc")
    free = info.get("execution_mode") in ("request", "instant")
    usable = [mode for mode in ("ioc", "fok") if free or mode in (flags or [])]
    if return_ok:
        usable.append("return")
    allowed = ", ".join(usable) or "none"
    if requested:
        want = str(requested).lower()
        if want not in usable and not (want == "return" and return_ok is None):
            raise Mt5Error(
                f"{symbol}: filling {want!r} is not allowed for this symbol "
                f"(execution {info.get('execution_mode') or 'unknown'}, "
                f"allowed for a market order: {allowed}). Omit filling to "
                "have the bridge choose."
            )
        return want
    if not usable:
        raise Mt5Error(
            f"{symbol}: no filling policy is usable for a market order "
            f"(flags {flags}, execution {info.get('execution_mode')}). Read "
            "mt5_symbol_info; this symbol may not accept market orders here."
        )
    return usable[0]


def _check_stops(
    side: str,
    entry: float,
    sl: Optional[float],
    tp: Optional[float],
    info: Dict[str, Any],
    symbol: str,
    quote: Optional[Dict[str, Any]] = None,
) -> None:
    """Reject the mistakes an EA would otherwise learn from a broker error.

    The side rules are against the *entry* price (a buy stop sits below it).
    The stops-level distance is not: ``SYMBOL_TRADE_STOPS_LEVEL`` is measured
    from the price the position is **closed** at - Bid for a buy, Ask for a
    sell (mql5.com/en/articles/2555; ``Bid - SL >= level`` and ``TP - Bid >=
    level`` for a buy, ``SL - Ask`` and ``Ask - TP`` for a sell). That is the
    opposite side of the quote from the entry, so a stop typed ``n`` points
    from the entry is only ``n - spread`` from the price that counts, and TP is
    ``spread`` further than it looks. Measuring from the entry (as this check
    once did) waves through the former and refuses valid cases of the latter.
    """
    digits = int(_f(info.get("digits"), 5))
    point = _f(info.get("point")) or (10**-digits)
    min_distance = int(_f(info.get("stops_level_points"))) * point
    quote = quote or {}
    closing = _f(quote.get("bid") if side == "buy" else quote.get("ask"))
    if closing <= 0:  # no quote to measure from: the entry is the best we have
        closing = entry
    closing_name = "bid" if side == "buy" else "ask"
    if sl is not None:
        if side == "buy" and sl >= entry:
            raise Mt5Error(
                f"{symbol}: a buy stop loss must be below the entry price "
                f"({sl} >= {entry})"
            )
        if side == "sell" and sl <= entry:
            raise Mt5Error(
                f"{symbol}: a sell stop loss must be above the entry price "
                f"({sl} <= {entry})"
            )
        sl_distance = (closing - sl) if side == "buy" else (sl - closing)
        if sl_distance < min_distance - 1e-12:
            raise Mt5Error(
                f"{symbol}: stop loss is {sl_distance:.{digits}f} from the "
                f"{closing_name} ({closing}), the price a {side} is closed at, "
                f"but the broker requires at least {min_distance:.{digits}f} "
                f"({int(_f(info.get('stops_level_points')))} points)"
            )
    if tp is not None:
        if side == "buy" and tp <= entry:
            raise Mt5Error(
                f"{symbol}: a buy take profit must be above the entry price "
                f"({tp} <= {entry})"
            )
        if side == "sell" and tp >= entry:
            raise Mt5Error(
                f"{symbol}: a sell take profit must be below the entry price "
                f"({tp} >= {entry})"
            )
        tp_distance = (tp - closing) if side == "buy" else (closing - tp)
        if tp_distance < min_distance - 1e-12:
            raise Mt5Error(
                f"{symbol}: take profit is {tp_distance:.{digits}f} from the "
                f"{closing_name} ({closing}), the price a {side} is closed at, "
                f"but the broker requires at least {min_distance:.{digits}f} "
                f"({int(_f(info.get('stops_level_points')))} points)"
            )


def build_tools(
    terminal: Terminal,
    *,
    allow_trading: bool = False,
    require_stops: bool = True,
    tester_dirs: Sequence[str] = (),
    allow_tester_run: bool = False,
) -> List[ToolDef]:
    """Define the tool table against a terminal backend."""

    def h_status() -> Dict[str, Any]:
        return terminal.status()

    def h_account() -> Dict[str, Any]:
        return terminal.account()

    def h_symbols(
        pattern: str = "", visible_only: bool = True, limit: int = 50
    ) -> Dict[str, Any]:
        rows = terminal.symbols(
            str(pattern or ""), bool(visible_only), _clamp_count(limit, 50, MAX_SYMBOLS)
        )
        return {
            "count": len(rows),
            "synthetic": terminal.synthetic,
            "symbols": rows,
        }

    def h_symbol_info(symbol: str) -> Dict[str, Any]:
        return terminal.symbol_info(str(symbol))

    def h_tick(symbol: str) -> Dict[str, Any]:
        return terminal.tick(str(symbol))

    def h_rates(
        symbol: str, timeframe: str = "H1", count: int = 100, shift: int = 0
    ) -> Dict[str, Any]:
        n = _clamp_count(count, 100, MAX_BARS)
        bars = terminal.rates(str(symbol), str(timeframe), n, max(0, int(shift or 0)))
        return {
            "symbol": str(symbol),
            "timeframe": str(timeframe).upper(),
            "count": len(bars),
            "order": "oldest_first",
            "synthetic": terminal.synthetic,
            "bars": bars,
        }

    def h_positions(symbol: Optional[str] = None, magic: Optional[int] = None) -> Any:
        rows = terminal.positions(
            str(symbol) if symbol else None, int(magic) if magic is not None else None
        )
        return {"count": len(rows), "positions": rows}

    def h_orders(symbol: Optional[str] = None) -> Any:
        rows = terminal.orders(str(symbol) if symbol else None)
        return {"count": len(rows), "orders": rows}

    def h_calc(
        symbol: str,
        side: str = "buy",
        volume: float = 0.1,
        price_open: Optional[float] = None,
        price_close: Optional[float] = None,
    ) -> Dict[str, Any]:
        which = str(side or "buy").lower()
        if which not in {"buy", "sell"}:
            raise Mt5Error(f"side must be 'buy' or 'sell', got {side!r}")
        return terminal.calc(
            str(symbol),
            which,
            float(volume),
            float(price_open) if price_open else None,
            float(price_close) if price_close else None,
        )

    def h_order_send(
        symbol: str,
        side: str,
        volume: float,
        price: Optional[float] = None,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        magic: int = 0,
        comment: str = "",
        deviation: int = 10,
        filling: Optional[str] = None,
    ) -> Dict[str, Any]:
        which = str(side or "").lower()
        if which not in {"buy", "sell"}:
            raise Mt5Error(f"side must be 'buy' or 'sell', got {side!r}")
        if not allow_trading:
            raise Mt5Error(
                "trading is disabled: this server was started without "
                "--allow-trading, so mt5_order_send cannot execute."
            )
        account = _assert_demo_account(terminal)
        info = terminal.symbol_info(str(symbol))
        quote = terminal.tick(str(symbol))
        entry = _f(quote.get("ask") if which == "buy" else quote.get("bid"))
        digits = int(_f(info.get("digits"), 5))
        point = _f(info.get("point")) or (10**-digits)
        deviation_points = max(0, int(deviation or 0))
        if price:
            live = _f(quote.get("ask") if which == "buy" else quote.get("bid"))
            _check_price_is_tradable(
                float(price), live, point, deviation_points, str(symbol)
            )
            entry = float(price)
        lots = _normalize_volume(volume, info, str(symbol))
        entry = _normalize_price(entry, info, str(symbol))
        stop = _normalize_price(float(sl), info, str(symbol)) if sl else None
        take = _normalize_price(float(tp), info, str(symbol)) if tp else None
        if require_stops and stop is None:
            raise Mt5Error(
                f"{symbol}: refusing to open a position without a stop loss. "
                "Pass sl (and tp if the strategy has one). The check can be "
                "lifted with --no-require-stops, which you should not do."
            )
        _check_stops(which, entry, stop, take, info, str(symbol), quote)
        request: Dict[str, Any] = {
            "symbol": str(symbol),
            "side": which,
            "volume": lots,
            "price": entry,
            "sl": stop,
            "tp": take,
            "magic": int(magic or 0),
            "comment": str(comment or "")[:31],
            "deviation": deviation_points,
            "filling": _resolve_filling(filling, info, str(symbol)),
        }
        result = terminal.order_send(request)
        return {
            "account": {
                "login": account.get("login"),
                "trade_mode": account.get("trade_mode"),
            },
            "request": request,
            "result": result,
            "warning": (
                "executed on a DEMO account by an agent. Validate the same "
                "logic in the Strategy Tester before it goes anywhere near "
                "real money."
            ),
        }

    defs: List[ToolDef] = [
        ToolDef(
            name="mt5_status",
            description=(
                "Terminal and account connectivity: build, company, whether "
                "the Algo Trading button is on (trade_allowed), whether the "
                "account lets Expert Advisors trade (trade_expert), whether "
                "the API is blocked (tradeapi_disabled), and the account's "
                "trade mode (demo or real). Call this first — every other tool "
                "assumes a live terminal."
            ),
            schema=_schema({}),
            handler=h_status,
            timeout=20.0,
        ),
        ToolDef(
            name="mt5_account",
            description=(
                "Account state: balance, equity, used and free margin, margin "
                "level, profit, currency, leverage, trade mode, and what an EA "
                "must adapt to: margin_mode (retail_netting allows one "
                "position per symbol, retail_hedging several), fifo_close, "
                "the pending-order limit and the margin-call/stop-out levels."
            ),
            schema=_schema({}),
            handler=h_account,
            timeout=20.0,
        ),
        ToolDef(
            name="mt5_symbols",
            description=(
                "List symbols in the market watch with the fields an EA needs: "
                "digits, point, bid/ask, spread in points, volume "
                "min/max/step, stops level, contract size, tick size and tick "
                "value. Filter with a pattern like 'EUR*' or '*XAU*'."
            ),
            schema=_schema(
                {
                    "pattern": _s(
                        "Name filter as MT5's symbols_get(group=) reads it: '*' "
                        "only at the start or end of a condition ('EUR*' prefix, "
                        "'*XAU*' contains), several conditions separated by "
                        "commas, '!' excludes ('*,!*USD*'). A bare 'XAU' matches "
                        "only a symbol named exactly XAU. Empty = all."
                    ),
                    "visible_only": _bool(
                        "Only symbols shown in the market watch (default true)."
                    ),
                    "limit": _int(
                        "Maximum rows to return (1-500).",
                        minimum=1,
                        maximum=MAX_SYMBOLS,
                    ),
                }
            ),
            handler=h_symbols,
            timeout=60.0,
        ),
        ToolDef(
            name="mt5_symbol_info",
            description=(
                "Full contract specification for one symbol: digits, point, "
                "spread, volume limits and step, stops and freeze level, "
                "contract size, tick size/value (profit and loss sides: use the "
                "loss one to size a stop), volume limit, filling flags "
                "(fok/ioc/boc) with "
                "the execution mode and whether RETURN is usable, expiration "
                "modes, swap mode and its unit, margin and profit currencies, "
                "which order types the broker accepts and whether SL/TP may "
                "be attached. Read this before "
                "writing order or lot-sizing code."
            ),
            schema=_schema({"symbol": _s("Symbol name, e.g. 'EURUSD'.")}, ["symbol"]),
            handler=h_symbol_info,
            timeout=20.0,
        ),
        ToolDef(
            name="mt5_tick",
            description=(
                "Latest quote for a symbol: bid, ask, last, volume, timestamp, "
                "plus digits/point and the spread in points."
            ),
            schema=_schema({"symbol": _s("Symbol name, e.g. 'EURUSD'.")}, ["symbol"]),
            handler=h_tick,
            timeout=20.0,
        ),
        ToolDef(
            name="mt5_rates",
            description=(
                "Historical OHLC bars, oldest first. Use it to sanity-check a "
                "strategy's assumptions (bar range vs stop distance, session "
                "behaviour) against real data instead of guessing."
            ),
            schema=_schema(
                {
                    "symbol": _s("Symbol name, e.g. 'EURUSD'."),
                    "timeframe": _enum(list(TIMEFRAMES), "Bar period (default 'H1')."),
                    "count": _int(
                        f"Number of bars, 1-{MAX_BARS} (default 100).",
                        minimum=1,
                        maximum=MAX_BARS,
                    ),
                    "shift": _int(
                        "Bars back from the current bar (0 = include the forming bar)."
                    ),
                },
                ["symbol"],
            ),
            handler=h_rates,
            timeout=60.0,
        ),
        ToolDef(
            name="mt5_positions",
            description=(
                "Open positions, optionally filtered by symbol and/or magic "
                "number: side, volume, entry and current price, SL/TP, "
                "floating profit and swap. commission is null on a real "
                "terminal (a position carries none; it is charged on the deals)."
            ),
            schema=_schema(
                {
                    "symbol": _s("Filter to one symbol (optional)."),
                    "magic": _int("Filter to one EA magic number (optional)."),
                }
            ),
            handler=h_positions,
            timeout=30.0,
        ),
        ToolDef(
            name="mt5_orders",
            description=(
                "Pending (unfilled) orders with their type, price, SL/TP and "
                "expiration."
            ),
            schema=_schema({"symbol": _s("Filter to one symbol (optional).")}),
            handler=h_orders,
            timeout=30.0,
        ),
        ToolDef(
            name="mt5_calc",
            description=(
                "Price a proposed trade before the EA does: margin required "
                "(also per lot) and profit at a close price, both in the "
                "account currency (see account_currency), plus the "
                "symbol's tick value, tick size, contract size, lot step and "
                "stops level. This is the ground truth for lot-sizing and "
                "risk-percentage code — compare it against what the EA "
                "computes."
            ),
            schema=_schema(
                {
                    "symbol": _s("Symbol name, e.g. 'EURUSD'."),
                    "side": _enum(["buy", "sell"], "Direction (default 'buy')."),
                    "volume": _num("Volume in lots (default 0.1).", minimum=0.0),
                    "price_open": _num("Entry price; defaults to the current ask/bid."),
                    "price_close": _num(
                        "Exit price used for the profit figure; defaults to "
                        "the current bid/ask (i.e. profit ≈ 0)."
                    ),
                },
                ["symbol"],
            ),
            handler=h_calc,
            timeout=30.0,
        ),
    ]

    if allow_trading:
        defs.append(
            ToolDef(
                name="mt5_order_send",
                description=(
                    "Send a MARKET order (demo accounts only). Volume must be a "
                    "multiple of the symbol's lot step and within its limits, "
                    "and prices are normalized to the tick size. A stop loss is "
                    "required unless the server was started with "
                    "--no-require-stops. Returns the broker retcode, its meaning "
                    "from MetaQuotes' own table, and a `success` flag: 10008 "
                    "placed, 10009 done and 10010 partial are all successes, and "
                    "a placed pending order is not a failure to resend. Use it to "
                    "verify an execution path, not to run a strategy — that is "
                    "what the EA on the chart is for."
                ),
                schema=_schema(
                    {
                        "symbol": _s("Symbol name, e.g. 'EURUSD'."),
                        "side": _enum(["buy", "sell"], "Direction."),
                        "volume": _num("Volume in lots.", minimum=0.0),
                        "price": _num("Entry price; defaults to the current ask/bid."),
                        "sl": _num(
                            "Stop loss price"
                            + (" (required)." if require_stops else " (optional).")
                        ),
                        "tp": _num("Take profit price (optional)."),
                        "magic": _int("EA magic number (default 0)."),
                        "comment": _s("Order comment, max 31 characters."),
                        "deviation": _int("Maximum slippage in points (default 10)."),
                        "filling": _enum(
                            ["ioc", "fok", "return"],
                            "Filling policy. Default: IOC if the symbol allows it, "
                            "else FOK, else RETURN (which Market execution "
                            "forbids). One the symbol does not allow is refused "
                            "before sending; see mt5_symbol_info.",
                        ),
                    },
                    ["symbol", "side", "volume"] + (["sl"] if require_stops else []),
                ),
                handler=h_order_send,
                timeout=60.0,
                read_only=False,
            )
        )

    # -- Strategy Tester reports ------------------------------------------
    #
    # These read files, not the terminal, so they work in --stub mode too:
    # backtest results are how an agent judges an EA when no market is open.

    def _tester_roots(search_dir: Optional[str] = None) -> Optional[List[Path]]:
        if search_dir:
            return [Path(str(search_dir)).expanduser()]
        return [Path(d).expanduser() for d in tester_dirs] if tester_dirs else None

    def _resolve_report(
        path: Optional[str], search_dir: Optional[str] = None
    ) -> Tuple[Path, str]:
        """An explicit report path, or the newest one under the roots."""
        if path:
            candidate = Path(str(path)).expanduser()
            if not candidate.is_file():
                nearby = tester_lib.find_reports(_tester_roots(search_dir), limit=5)
                hint = (
                    " Report files I did find: "
                    + ", ".join(str(found) for found in nearby)
                    if nearby
                    else ""
                )
                raise Mt5Error(
                    f"report not found: {candidate}. Pass a path to a Strategy "
                    "Tester report (.htm, .html or .xml)." + hint
                )
            return candidate, "explicit"
        found = tester_lib.find_reports(_tester_roots(search_dir), limit=1)
        if not found:
            roots = _tester_roots(search_dir)
            where = (
                ", ".join(str(root) for root in roots)
                if roots
                else "the terminal data folders and the working directory"
            )
            raise Mt5Error(
                f"no Strategy Tester report found under {where}. Save one from "
                "the tester's Report tab, or start this server with "
                "--tester-dir <folder> pointing at where reports are written."
            )
        return found[0], "newest"

    def _resolve_optimization(
        path: Optional[str], search_dir: Optional[str] = None
    ) -> Tuple[Any, str]:
        """An optimization report: explicit path, or the newest table of passes.

        A testing report and an optimization report can share a folder and an
        extension, and reading the wrong one is worse than reading none — a
        single backtest parsed as passes is one pass, and an optimization parsed
        as a backtest is the first pass's numbers presented as the EA's. So the
        file is sniffed, and the error says which tool fits what was found.
        """
        if path:
            candidate = Path(str(path)).expanduser()
            if not candidate.is_file():
                raise Mt5Error(
                    f"optimization report not found: {candidate}. An "
                    "optimization writes the XML table named by the ini's "
                    "Report= key (with .xml appended)."
                )
            try:
                parsed = tester_lib.parse_any_report(candidate)
            except (OSError, ValueError) as exc:
                raise Mt5Error(f"could not parse {candidate}: {exc}") from exc
            if not isinstance(parsed, tester_lib.OptimizationResult):
                raise Mt5Error(
                    f"{candidate} is a single-test report, not a table of "
                    "optimization passes; use mt5_tester_report for it."
                )
            return parsed, "explicit"
        found = tester_lib.find_reports(_tester_roots(search_dir), limit=8)
        for candidate in found:
            try:
                text = tester_lib.decode_report_bytes(candidate.read_bytes())
            except OSError:
                continue
            if tester_lib.is_optimization_text(text):
                return tester_lib.parse_optimization(candidate), "newest"
        where = ", ".join(str(item) for item in found) if found else "the search roots"
        raise Mt5Error(
            "no optimization report found. Looked at: "
            f"{where}. Run the tester with Optimization=1 or 2 and a Report= "
            "name, then pass that .xml here (or start the server with "
            "--tester-dir pointing at the folder MT5 writes it to)."
        )

    def _threshold_rules(**values: Optional[float]) -> Dict[str, Optional[float]]:
        return {key: value for key, value in values.items() if value is not None}

    def _report_payload(
        report: Any,
        rules: Optional[Dict[str, Optional[float]]] = None,
        summary: bool = True,
        found_by: str = "explicit",
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = report.to_dict()
        payload["found_by"] = found_by
        if isinstance(report, tester_lib.OptimizationResult):
            # A table of passes has no `metrics`, and format_for_prompt would
            # reach for one. Rank and check it the way mt5_tester_optimization
            # does, so a run that optimized comes back as useful as a read.
            analysis = tester_lib.analyze_optimization(report)
            payload["analysis"] = analysis
            if summary:
                payload["summary"] = tester_lib.format_optimization_for_prompt(
                    report, analysis
                )
            return payload
        if summary:
            payload["summary"] = tester_lib.format_for_prompt(report)
        if rules:
            results = tester_lib.check_thresholds(report, rules)
            failed = [r for r in results if not r.passed]
            payload["thresholds"] = {
                "all_passed": not failed,
                "passed": len(results) - len(failed),
                "failed": len(failed),
                "results": [
                    {
                        "metric": r.name,
                        "rule": r.rule,
                        "actual": r.actual,
                        "passed": r.passed,
                        # The CLI has always printed this and the bridge dropped
                        # it. Without it `actual: null, passed: false` cannot be
                        # told apart from a metric that was simply below the bar
                        # — and on a report this parser could not read, the
                        # reason is the whole answer.
                        "message": r.message,
                    }
                    for r in results
                ],
            }
        return payload

    def h_tester_report(
        path: Optional[str] = None,
        search_dir: Optional[str] = None,
        summary: bool = True,
        scan_log: bool = False,
        min_profit_factor: Optional[float] = None,
        min_recovery_factor: Optional[float] = None,
        min_sharpe_ratio: Optional[float] = None,
        min_net_profit: Optional[float] = None,
        min_trades: Optional[float] = None,
        min_history_quality_pct: Optional[float] = None,
        max_equity_drawdown_pct: Optional[float] = None,
        max_equity_drawdown: Optional[float] = None,
    ) -> Dict[str, Any]:
        report_path, found_by = _resolve_report(path, search_dir)
        try:
            parsed = tester_lib.parse_any_report(report_path)
        except (OSError, ValueError) as exc:
            raise Mt5Error(f"could not parse {report_path}: {exc}") from exc
        if isinstance(parsed, tester_lib.OptimizationResult):
            raise Mt5Error(
                f"{report_path} is a table of optimization passes, not a single "
                "test: it has rows to rank, not metrics to gate. Use "
                "mt5_tester_optimization for it."
            )
        report = parsed
        rules = _threshold_rules(
            min_profit_factor=min_profit_factor,
            min_recovery_factor=min_recovery_factor,
            min_sharpe_ratio=min_sharpe_ratio,
            min_net_profit=min_net_profit,
            min_total_trades=min_trades,
            min_history_quality_pct=min_history_quality_pct,
            max_equity_drawdown_relative_pct=max_equity_drawdown_pct,
            max_equity_drawdown=max_equity_drawdown,
        )
        payload = _report_payload(report, rules, bool(summary), found_by)
        if scan_log:
            logs = tester_lib.find_tester_logs(_tester_roots(search_dir), limit=1)
            payload["log"] = (
                tester_lib.scan_tester_log(logs[0])
                if logs
                else {"found": False, "hint": "no tester log under the search roots"}
            )
        return payload

    def h_tester_compare(
        paths: Sequence[str],
        keys: Optional[Sequence[str]] = None,
        summary: bool = False,
    ) -> Dict[str, Any]:
        candidates = [Path(str(item)).expanduser() for item in (paths or [])]
        if len(candidates) < 2:
            raise Mt5Error(
                "mt5_tester_compare needs at least two report paths; use "
                "mt5_tester_report for a single one."
            )
        reports = []
        for candidate in candidates:
            if not candidate.is_file():
                raise Mt5Error(f"report not found: {candidate}")
            try:
                parsed = tester_lib.parse_any_report(candidate)
            except (OSError, ValueError) as exc:
                raise Mt5Error(f"could not parse {candidate}: {exc}") from exc
            if isinstance(parsed, tester_lib.OptimizationResult):
                raise Mt5Error(
                    f"{candidate} is a table of optimization passes, not a single "
                    "test, so it has no metrics to compare. Rank it with "
                    "mt5_tester_optimization, or compare two single-test reports."
                )
            reports.append(parsed)
        comparison = tester_lib.compare_reports(
            reports, [str(key) for key in keys] if keys else None
        )
        payload: Dict[str, Any] = {
            "comparison": comparison,
            "reports": [report.to_dict() for report in reports],
            "note": (
                "Deltas compare the second report against the first. A metric "
                "listed in a report's `missing` was not in that file, so its "
                "delta is absent rather than zero — do not read a missing delta "
                "as 'unchanged'."
            ),
        }
        if summary:
            payload["summaries"] = [
                tester_lib.format_for_prompt(report) for report in reports
            ]
        return payload

    def h_tester_optimization(
        path: Optional[str] = None,
        search_dir: Optional[str] = None,
        top: int = 10,
        rank_by: str = "result",
        filter_junk: bool = True,
        set_path: Optional[str] = None,
        set_from_pass: Optional[int] = None,
        summary: bool = True,
    ) -> Dict[str, Any]:
        result, found_by = _resolve_optimization(path, search_dir)

        template = None
        set_payload: Optional[Dict[str, Any]] = None
        if set_path:
            set_file_path = Path(str(set_path)).expanduser()
            if not set_file_path.is_file():
                raise Mt5Error(f".set file not found: {set_file_path}")
            try:
                template = tester_lib.parse_set_file(set_file_path)
            except (OSError, ValueError) as exc:
                raise Mt5Error(f"could not parse {set_file_path}: {exc}") from exc
            set_payload = template.to_dict()

        try:
            analysis = tester_lib.analyze_optimization(
                result,
                rank_by=str(rank_by or "result"),
                top=max(1, int(top or 10)),
                use_filter=bool(filter_junk),
                set_file=template,
            )
        except (ValueError, KeyError) as exc:
            raise Mt5Error(f"could not rank the passes: {exc}") from exc

        payload: Dict[str, Any] = result.to_dict()
        payload["found_by"] = found_by
        payload["analysis"] = analysis
        if set_payload is not None:
            payload["set_file"] = set_payload
        if summary:
            payload["summary"] = tester_lib.format_optimization_for_prompt(
                result, analysis
            )

        if set_from_pass is not None:
            wanted = int(set_from_pass)
            chosen = next((row for row in result.passes if row.number == wanted), None)
            if chosen is None:
                present = [
                    row.number for row in result.passes[:15] if row.number is not None
                ]
                raise Mt5Error(
                    f"no pass {wanted} in {result.source or 'the report'}. Pass "
                    f"numbers present (first 15): {present}"
                )
            generated = tester_lib.set_from_pass(chosen, template=template)
            payload["set_from_pass"] = {
                "pass": chosen.number,
                "text": generated.to_text(),
                "inputs": generated.to_dict()["inputs"],
                "warnings": generated.warnings,
                "hint": (
                    "Save this text as <EA>.set in MQL5/Profiles/Tester (or "
                    "anywhere, then pass its file name as ExpertParameters), "
                    "and re-run ONE test with Optimization=0: the pass row is "
                    "what the optimizer measured, the single test is what you "
                    "can gate on."
                ),
            }
        payload["note"] = (
            "A pass is not a backtest. `analysis.warnings` lists the reasons the "
            "top of this table may not survive contact with the market: too few "
            "trades, a lone spike instead of a plateau, inputs pinned at the "
            "edge of the range that was optimized, and — for forward files — an "
            "in-sample ranking that does not hold out of sample."
        )
        return payload

    def h_tester_forward_check(
        path: Optional[str] = None,
        forward_path: Optional[str] = None,
        search_dir: Optional[str] = None,
        min_trades: int = 30,
        max_degradation_pct: float = 50.0,
        summary: bool = True,
    ) -> Dict[str, Any]:
        """Back half against forward half: did the edge survive new data?

        ``forward_path`` is optional — the ``.forward.`` file MT5 writes next to
        the back half is found *by name*. Only that name is trusted: a forward
        report from some other run in the same folder is not this run's
        out-of-sample half, and pairing them would compare two unrelated tests
        and call the result a forward check. The newest file after a forward run
        is often the forward half itself, so a resolved ``.forward.`` path is
        swapped with its back companion rather than compared against nothing.
        """
        back_path, found_by = _resolve_report(path, search_dir)
        forward_file: Optional[Path] = None
        forward_by = ""
        swapped = False

        if forward_path:
            candidate = Path(str(forward_path)).expanduser()
            if not candidate.is_file():
                raise Mt5Error(
                    f"forward report not found: {candidate}. A forward run "
                    "writes it beside the back half as <name>.forward.htm (or "
                    ".forward.xml)."
                )
            forward_file, forward_by = candidate, "explicit"
        else:
            if tester_lib.is_forward_report(back_path):
                for companion in tester_lib.forward_companion(back_path):
                    if companion.is_file():
                        back_path, forward_file = companion, back_path
                        forward_by, swapped = "companion", True
                        break
            else:
                for companion in tester_lib.forward_companion(back_path):
                    if companion.is_file():
                        forward_file, forward_by = companion, "companion"
                        break
        try:
            parsed_back = tester_lib.parse_any_report(back_path)
        except (OSError, ValueError) as exc:
            raise Mt5Error(f"could not parse {back_path}: {exc}") from exc
        if isinstance(parsed_back, tester_lib.OptimizationResult):
            raise Mt5Error(
                f"{back_path} is a table of optimization passes, not a testing "
                "report, so there is no back half to check. Its out-of-sample "
                "numbers are the Back Result and Forward Result columns of that "
                "same table — mt5_tester_optimization reads them, including "
                "whether the in-sample ranking survived."
            )
        back = parsed_back

        forward: Any = None
        if forward_file is not None:
            try:
                parsed = tester_lib.parse_any_report(forward_file)
            except (OSError, ValueError) as exc:
                raise Mt5Error(f"could not parse {forward_file}: {exc}") from exc
            if isinstance(parsed, tester_lib.OptimizationResult):
                raise Mt5Error(
                    f"{forward_file} is a table of optimization passes, not a "
                    "testing report. The forward half of an optimization is "
                    "inside the same table, as the Back Result and Forward "
                    "Result columns — use mt5_tester_optimization for it."
                )
            forward = parsed

        check = tester_lib.check_forward(
            back,
            forward,
            min_trades=int(min_trades),
            max_degradation_pct=float(max_degradation_pct),
        )
        payload: Dict[str, Any] = {
            "back": _report_payload(back, None, False, found_by),
            "forward_check": check.to_dict(),
        }
        if forward is not None:
            payload["forward"] = _report_payload(forward, None, False, forward_by)
        if summary:
            payload["summary"] = tester_lib.format_forward_for_prompt(check)
        notes = [
            "A forward check compares the half the search saw with the half it "
            "did not. `verdict` is holds_up, degrades or inconclusive — "
            "inconclusive means the comparison cannot support a verdict (too few "
            "trades in a half, or no metric in common), not that nothing "
            "changed. Money and trade counts are normalized per day, because the "
            "forward half is usually a fraction of the back half."
        ]
        if swapped:
            notes.append(
                f"the file found ({forward_file}) is the forward half, so its "
                f"back companion ({back_path}) was used as the in-sample side."
            )
        if forward is None:
            if tester_lib.is_forward_report(back_path):
                notes.append(
                    f"{Path(back_path).name} is itself the forward half, and no "
                    "back companion was found beside it, so there is nothing to "
                    "compare it with — pass the in-sample report as `path` (or "
                    "both files explicitly)."
                )
            else:
                notes.append(
                    "no forward report was found: the run had ForwardMode off, or "
                    "MT5 put both halves in one optimization table."
                )
        payload["note"] = " ".join(notes)
        return payload

    def h_tester_run(
        expert: str,
        symbol: str = "",
        period: str = "",
        from_date: str = "",
        to_date: str = "",
        deposit: Optional[float] = None,
        currency: str = "",
        leverage: str = "",
        model: Optional[int] = None,
        execution_mode: Optional[int] = None,
        optimization: Optional[int] = None,
        expert_parameters: str = "",
        report_path: Optional[str] = None,
        terminal_path: Optional[str] = None,
        timeout: float = 1800.0,
        portable: bool = False,
        summary: bool = True,
        forward_mode: Optional[int] = None,
        forward_date: str = "",
        min_forward_trades: int = 30,
        max_degradation_pct: float = 50.0,
    ) -> Dict[str, Any]:
        terminal_exe = tester_lib.find_terminal(terminal_path)
        if terminal_exe is None:
            raise Mt5Error(
                "terminal64.exe not found. Pass terminal_path, start the server "
                "with --path, or set TERMINAL_PATH. On Linux/macOS the terminal "
                "runs under Wine; point at the .exe inside the Wine prefix."
            )
        target = (
            Path(str(report_path)).expanduser()
            if report_path
            else Path(tempfile.gettempdir()) / "openjarvis-tester" / "TesterReport.xml"
        )
        ini_text = tester_lib.build_tester_ini(
            expert=str(expert),
            symbol=str(symbol or ""),
            period=str(period or ""),
            from_date=str(from_date or ""),
            to_date=str(to_date or ""),
            deposit=float(deposit) if deposit is not None else None,
            currency=str(currency or ""),
            leverage=str(leverage or ""),
            model=int(model) if model is not None else None,
            execution_mode=(
                int(execution_mode) if execution_mode is not None else None
            ),
            optimization=int(optimization) if optimization is not None else None,
            expert_parameters=str(expert_parameters or ""),
            report=str(target.with_suffix("")),
            forward_mode=(int(forward_mode) if forward_mode is not None else None),
            forward_date=str(forward_date or ""),
        )
        wait_for = max(60.0, float(timeout or 1800.0))
        try:
            outcome = tester_lib.run_tester(
                terminal=terminal_exe,
                ini_text=ini_text,
                report_path=target,
                timeout=wait_for,
                portable=bool(portable),
                # An optimization run produces a table of passes, not a set of
                # metrics, and which one it is depends on the ini: let the file
                # decide rather than assuming a testing report.
                parser=tester_lib.parse_any_report,
            )
        except (TimeoutError, FileNotFoundError, OSError) as exc:
            raise Mt5Error(f"tester run failed: {exc}") from exc
        report = outcome.pop("report")
        # The forward half arrives as a parsed report object; turn it into a
        # payload (and a verdict) before it reaches the JSON serializer.
        forward_report = outcome.pop("forward_report", None)
        forward_note = outcome.pop("forward_note", "")
        payload = _report_payload(report, None, bool(summary), "run")
        if forward_report is not None and not isinstance(
            forward_report, tester_lib.TesterReport
        ):
            payload["forward_note"] = (
                "the forward half of this run is a table of optimization passes, "
                "not a testing report, so there is no pair to check: its Back "
                "Result and Forward Result columns are the out-of-sample "
                "comparison, and mt5_tester_optimization reads them."
            )
        elif forward_report is not None:
            check = tester_lib.check_forward(
                report,
                forward_report,
                min_trades=int(min_forward_trades),
                max_degradation_pct=float(max_degradation_pct),
            )
            payload["forward"] = _report_payload(forward_report, None, False, "run")
            payload["forward_check"] = check.to_dict()
            if summary:
                payload["forward_summary"] = tester_lib.format_forward_for_prompt(check)
        elif forward_note:
            payload["forward_note"] = forward_note
        payload["run"] = dict(outcome, **report.run)
        payload["ini"] = ini_text
        return payload

    if tester_lib is None:
        logger.warning(
            "tester_report.py is not next to this file (%s), so the "
            "mt5_tester_* tools are not registered. Copy the whole "
            "examples/mql_companion folder to enable them.",
            TESTER_IMPORT_ERROR or "not importable",
        )
    else:
        defs.append(
            ToolDef(
                name="mt5_tester_report",
                description=(
                    "Read a MetaTrader 5 Strategy Tester report (.htm, .html or "
                    ".xml) and return its metrics as numbers: net profit, gross "
                    "profit/loss, profit factor, expected payoff, recovery "
                    "factor, Sharpe ratio, all four balance and equity drawdown "
                    "figures, trade and deal counts, win rate, largest and "
                    "average win/loss, longest losing streak, history quality. "
                    "Omit `path` to read the newest report under the configured "
                    "folders. Values the file does not contain are listed in "
                    "`missing` and `warnings` flags internally inconsistent "
                    "numbers — never fill either in from imagination. Optional "
                    "min_*/max_* arguments turn the call into a gate: "
                    "`thresholds.all_passed` is false when the EA does not clear "
                    "them."
                ),
                schema=_schema(
                    {
                        "path": _s(
                            "Report file path. Omit for the newest report found."
                        ),
                        "search_dir": _s(
                            "Folder to search when `path` is omitted; overrides "
                            "the server's --tester-dir."
                        ),
                        "summary": _bool(
                            "Include a compact text summary (default true)."
                        ),
                        "scan_log": _bool(
                            "Also scan the newest tester log for EA errors."
                        ),
                        "min_profit_factor": _num("Gate: profit factor at least this."),
                        "min_recovery_factor": _num(
                            "Gate: recovery factor at least this."
                        ),
                        "min_sharpe_ratio": _num("Gate: Sharpe ratio at least this."),
                        "min_net_profit": _num("Gate: net profit at least this."),
                        "min_trades": _num(
                            "Gate: total trades at least this — a great profit "
                            "factor on 4 trades means nothing."
                        ),
                        "min_history_quality_pct": _num(
                            "Gate: history quality percentage at least this."
                        ),
                        "max_equity_drawdown_pct": _num(
                            "Gate: worst equity drawdown percentage at most this."
                        ),
                        "max_equity_drawdown": _num(
                            "Gate: worst equity drawdown in money at most this."
                        ),
                    }
                ),
                handler=h_tester_report,
                timeout=60.0,
                read_only=True,
            )
        )
        defs.append(
            ToolDef(
                name="mt5_tester_compare",
                description=(
                    "Compare two or more Strategy Tester reports — two versions "
                    "of an EA, two symbols, or two optimization passes. Returns "
                    "each metric side by side with the delta against the first "
                    "report, plus which report wins on net profit, profit "
                    "factor, recovery factor, Sharpe ratio and equity drawdown. "
                    "A metric missing from one file has no delta; that is not "
                    "the same as no change."
                ),
                schema=_schema(
                    {
                        "paths": _arr("Two or more report file paths.", minItems=2),
                        "keys": _arr(
                            "Metric keys to compare; defaults to the headline set."
                        ),
                        "summary": _bool("Include a compact text summary per report."),
                    },
                    ["paths"],
                ),
                handler=h_tester_compare,
                timeout=60.0,
                read_only=True,
            )
        )
        defs.append(
            ToolDef(
                name="mt5_tester_optimization",
                description=(
                    "Read an MT5 optimization report (the XML table of passes) "
                    "and rank it: best passes by result, profit, profit factor, "
                    "recovery factor, Sharpe, expected payoff, drawdown or "
                    "trades, with the input values that produced each. Junk "
                    "passes are hidden by default using the platform's own "
                    "filters (no trades, no profit, drawdown over 50%, recovery "
                    "factor under 1, Sharpe under 0.5). `analysis.warnings` is "
                    "the part worth reading: a best pass on too few trades, a "
                    "lone spike instead of a plateau, inputs sitting at the edge "
                    "of the range that was optimized (pass `set_path` for that "
                    "check), and forward files whose in-sample ranking does not "
                    "hold out of sample. With `set_from_pass` it also returns "
                    "the .set text that reproduces one pass, so a winner can be "
                    "re-tested on its own. Omit `path` for the newest "
                    "optimization report found. Read-only: it writes nothing."
                ),
                schema=_schema(
                    {
                        "path": _s(
                            "Optimization report (.xml). Omit for the newest one "
                            "found under the search roots."
                        ),
                        "search_dir": _s(
                            "Folder to search when `path` is omitted; overrides "
                            "the server's --tester-dir."
                        ),
                        "top": _int("How many passes to return (default 10)."),
                        "rank_by": _enum(
                            [
                                "result",
                                "profit",
                                "payoff",
                                "profit_factor",
                                "recovery_factor",
                                "sharpe",
                                "drawdown",
                                "trades",
                                "custom",
                                "back_result",
                                "forward_result",
                            ],
                            "Metric to rank by (default result).",
                        ),
                        "filter_junk": _bool(
                            "Hide the passes MT5 itself hides (default true)."
                        ),
                        "set_path": _s(
                            "The .set file the optimization was run from: "
                            "supplies the input ranges used to spot an optimum "
                            "pinned at the edge, and the fixed inputs a "
                            "reproducing .set needs."
                        ),
                        "set_from_pass": _int(
                            "Pass number to turn into a .set (returned as text "
                            "in set_from_pass.text; nothing is written)."
                        ),
                        "summary": _bool("Include a compact text summary."),
                    }
                ),
                handler=h_tester_optimization,
                timeout=60.0,
                read_only=True,
            )
        )
        defs.append(
            ToolDef(
                name="mt5_tester_forward_check",
                description=(
                    "Compare a Strategy Tester report with the forward half of "
                    "its run — the period the optimization never saw — and say "
                    "whether the parameters survived it. Finds the "
                    "<name>.forward.htm file MT5 writes beside the back half (or "
                    "pass forward_path); a forward report from another run is "
                    "never paired, so a missing companion comes back as "
                    "`available: false` rather than as a comparison of two "
                    "unrelated tests. Returns a verdict of holds_up, degrades "
                    "or inconclusive with the reasons: a profit factor crossing "
                    "1, profit crossing zero, decay past max_degradation_pct on "
                    "profit factor / recovery factor / Sharpe, or too few trades "
                    "in either half to read a ratio at all. Money and counts are "
                    "normalized per day, since the forward half is usually a "
                    "fraction of the back one; drawdown growth and low history "
                    "quality come back as warnings, not verdicts. Omit `path` "
                    "for the newest report found. Read-only: it writes nothing."
                ),
                schema=_schema(
                    {
                        "path": _s(
                            "Back-half report (.htm/.xml). Omit for the newest "
                            "one found; a forward file passed here is swapped "
                            "for its back companion."
                        ),
                        "forward_path": _s(
                            "Forward-half report (<name>.forward.htm). Omit to "
                            "look for it beside the back half."
                        ),
                        "search_dir": _s(
                            "Folder to search when `path` is omitted; overrides "
                            "the server's --tester-dir."
                        ),
                        "min_trades": _int(
                            "Trades a half needs before the verdict is not "
                            "'inconclusive' (default 30)."
                        ),
                        "max_degradation_pct": _num(
                            "How far the gate ratios may fall before the verdict "
                            "is 'degrades' (default 50)."
                        ),
                        "summary": _bool("Include a compact text summary."),
                    }
                ),
                handler=h_tester_forward_check,
                timeout=60.0,
                read_only=True,
            )
        )
        if allow_tester_run:
            defs.append(
                ToolDef(
                    name="mt5_tester_run",
                    description=(
                        "Run a backtest in the Strategy Tester and return the "
                        "report it produced. LAUNCHES THE TERMINAL: it writes a "
                        "[Tester] .ini, starts terminal64.exe with /config, and "
                        "waits for the report, so it takes minutes and closes "
                        "the terminal when it finishes (ShutdownTerminal=1). Do "
                        "not call it while a chart session is open — MT5 ignores "
                        "/config for an already-running terminal, and shutting "
                        "it down would close someone's windows. Registered only "
                        "when the server was started with --allow-tester."
                    ),
                    schema=_schema(
                        {
                            "expert": _s(
                                "EA to test: path under MQL5/Experts, .ex5 "
                                "optional, e.g. 'Examples/MACD/MACD Sample'."
                            ),
                            "symbol": _s("Symbol, e.g. 'EURUSD'."),
                            "period": _s("Timeframe, e.g. 'H1'."),
                            "from_date": _s("Start date, YYYY.MM.DD."),
                            "to_date": _s("End date, YYYY.MM.DD."),
                            "deposit": _num("Initial deposit."),
                            "currency": _s("Deposit currency, e.g. 'USD'."),
                            "leverage": _s("Leverage, e.g. '1:100'."),
                            "model": _int(
                                "0 every tick, 1 one-minute OHLC, 2 open prices "
                                "only, 3 math calculations, 4 every tick from "
                                "real ticks."
                            ),
                            "execution_mode": _int(
                                "0 normal, -1 random delay, >0 delay in ms."
                            ),
                            "optimization": _int(
                                "0 off, 1 slow complete, 2 fast genetic, 3 all "
                                "Market Watch symbols."
                            ),
                            "expert_parameters": _s(
                                ".set file in MQL5/Profiles/Tester."
                            ),
                            "report_path": _s(
                                "Where to write the report; defaults to a temp folder."
                            ),
                            "terminal_path": _s("Override the terminal location."),
                            "timeout": _num(
                                "Seconds to wait for the report (default 1800)."
                            ),
                            "portable": _bool("Pass /portable to the terminal."),
                            "forward_mode": _int(
                                "Split the period and re-run on the far side: 0 "
                                "off, 1 = 1/2 of the period, 2 = 1/3, 3 = 1/4, "
                                "4 = custom (set forward_date). The run then "
                                "returns `forward` and `forward_check` with a "
                                "holds_up / degrades / inconclusive verdict. In "
                                "an optimization MT5 forward-runs only the best "
                                "10% (slow complete) or 25% (genetic) of passes."
                            ),
                            "forward_date": _s(
                                "Custom split date, YYYY.MM.DD. MT5 reads it "
                                "only with forward_mode=4."
                            ),
                            "min_forward_trades": _int(
                                "Trades a half needs for a forward verdict "
                                "(default 30)."
                            ),
                            "max_degradation_pct": _num(
                                "Allowed decay in the gate ratios before the "
                                "forward verdict is 'degrades' (default 50)."
                            ),
                            "summary": _bool("Include a compact text summary."),
                        },
                        ["expert"],
                    ),
                    handler=h_tester_run,
                    timeout=1860.0,
                    read_only=False,
                )
            )

    # Frozen dataclass, so the closed-world readers are rebuilt rather than
    # mutated: four tools parse a report file, and saying they stay inside a
    # closed domain is the one openWorldHint=False this bridge can justify.
    return [
        replace(d, open_world=False) if d.name in CLOSED_WORLD_TOOLS else d
        for d in defs
    ]


class Mt5MCPServer(MCPServer):
    """``MCPServer`` with this bridge's identity and tool annotations."""

    SERVER_NAME = SERVER_NAME
    SERVER_VERSION = SERVER_VERSION

    def __init__(self, tools: Sequence[BaseTool]) -> None:
        super().__init__(list(tools))
        self._annotations: Dict[str, Dict[str, Any]] = {}
        for tool in tools:
            if isinstance(tool, BridgeTool):
                read_only = tool.defn.read_only
                self._annotations[tool.defn.name] = {
                    "readOnlyHint": read_only,
                    "destructiveHint": not read_only,
                    "idempotentHint": read_only,
                    "openWorldHint": tool.defn.open_world,
                }

    def _handle_initialize(self, req: MCPRequest) -> MCPResponse:
        """Identify this bridge rather than the generic OpenJarvis server."""
        response = super()._handle_initialize(req)
        result = response.result or {}
        info = dict(result.get("serverInfo", {}))
        info["title"] = "MetaTrader 5 bridge"
        result["serverInfo"] = info
        return response

    def _handle_tools_list(self, req: MCPRequest) -> MCPResponse:
        response = super()._handle_tools_list(req)
        result = response.result or {}
        for entry in result.get("tools", []):
            annotations = self._annotations.get(entry.get("name", ""))
            if annotations:
                entry["annotations"] = annotations
        return response


def build_server(
    terminal: Terminal,
    *,
    allow_trading: bool = False,
    require_stops: bool = True,
    tester_dirs: Sequence[str] = (),
    allow_tester_run: bool = False,
) -> Mt5MCPServer:
    tools = [
        BridgeTool(defn)
        for defn in build_tools(
            terminal,
            allow_trading=allow_trading,
            require_stops=require_stops,
            tester_dirs=tester_dirs,
            allow_tester_run=allow_tester_run,
        )
    ]
    return Mt5MCPServer(tools)


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------


def _emit(stream: Any, response: MCPResponse) -> None:
    stream.write(response.to_json() + "\n")
    stream.flush()


def dispatch_line(server: MCPServer, line: str) -> Optional[MCPResponse]:
    """Handle one JSON-RPC line; ``None`` means "no response" (notification)."""
    line = line.strip()
    if not line:
        return None
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError as exc:
        return MCPResponse.error_response(None, PARSE_ERROR, f"Parse error: {exc}")
    if not isinstance(parsed, dict):
        return MCPResponse.error_response(
            None, INVALID_REQUEST, "Expected a JSON object"
        )
    if "id" not in parsed:
        logger.debug("notification: %s", parsed.get("method"))
        return None
    if "method" not in parsed:
        return MCPResponse.error_response(
            parsed.get("id"), INVALID_REQUEST, "Missing 'method'"
        )
    try:
        request = MCPRequest.from_json(line)
        return server.handle(request)
    except Exception as exc:  # noqa: BLE001 — never kill the transport
        logger.exception("failed handling %r", parsed.get("method"))
        return MCPResponse.error_response(
            parsed.get("id"), INTERNAL_ERROR, f"Server error: {exc}"
        )


def serve_stdio(server: MCPServer) -> int:
    """One JSON-RPC message per line. stdout is protocol traffic only."""
    logger.info("serving MCP over stdio")
    for line in sys.stdin:
        response = dispatch_line(server, line)
        if response is not None:
            _emit(sys.stdout, response)
    return 0


def serve_http(
    server: MCPServer,
    *,
    host: str,
    port: int,
    token: Optional[str] = None,
) -> int:
    """Streamable HTTP: POST a JSON-RPC message, get a JSON-RPC response.

    Notifications (no ``id``) answer 202 with an empty body, which is what
    ``openjarvis.mcp.transport.StreamableHTTPTransport`` expects.
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    loopback = host in {"127.0.0.1", "localhost", "::1", ""}
    if not loopback and not token:
        raise click.ClickException(
            f"--host {host} is not loopback: the bridge would be reachable by "
            "anything that can route to this machine. Pass --token SECRET (the "
            "client sends it as 'token' in the server config) or bind to "
            "127.0.0.1."
        )

    session_id = uuid.uuid4().hex

    class Handler(BaseHTTPRequestHandler):
        server_version = f"{SERVER_NAME}/{SERVER_VERSION}"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # stderr only
            logger.debug("%s - %s", self.address_string(), fmt % args)

        def do_POST(self) -> None:  # noqa: N802 — stdlib naming
            if token and self.headers.get("Authorization") != f"Bearer {token}":
                return self._raw(401, b'{"error":"unauthorized"}')
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            body = self.rfile.read(length).decode("utf-8") if length else ""
            response = dispatch_line(server, body)
            if response is None:
                return self._raw(202, b"")
            return self._raw(200, response.to_json().encode("utf-8"))

        def do_GET(self) -> None:  # noqa: N802 — a health probe, not a stream
            return self._raw(200, json.dumps({"server": SERVER_NAME}).encode())

        def _raw(self, code: int, payload: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Mcp-Session-Id", session_id)
            self.end_headers()
            if payload:
                self.wfile.write(payload)

    httpd = ThreadingHTTPServer((host, port), Handler)
    logger.info("serving MCP over HTTP at http://%s:%d/", host or "0.0.0.0", port)
    click.echo(
        f"MT5 MCP bridge listening on http://{host or '0.0.0.0'}:{port}/ "
        f"(session {session_id[:8]}…)",
        err=True,
    )
    try:
        httpd.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:  # pragma: no cover - interactive
        logger.info("interrupted")
    finally:
        httpd.server_close()
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_terminal(
    *,
    stub: bool,
    stub_trade_mode: str,
    path: Optional[str],
    login: Optional[str],
    trade_server: Optional[str],
    password_env: str,
    timeout: int,
) -> Terminal:
    if stub:
        return StubTerminal(trade_mode=stub_trade_mode)
    password = os.environ.get(password_env) or None
    if login and not password:
        logger.warning(
            "--login given but %s is not set; the terminal may prompt or fail",
            password_env,
        )
    return MetaTraderTerminal(
        path=path,
        login=login,
        password=password,
        trade_server=trade_server,
        timeout_ms=timeout,
    )


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--stub",
    is_flag=True,
    help="Serve a deterministic synthetic market instead of a real terminal.",
)
@click.option(
    "--stub-trade-mode",
    type=click.Choice(["demo", "contest", "real"]),
    default="demo",
    show_default=True,
    help="Trade mode the stub reports — use 'real' to exercise the safety gate.",
)
@click.option(
    "--path",
    default=None,
    help="Path to terminal64.exe when the terminal is not the default install.",
)
@click.option("--login", default=None, help="Account login to attach to.")
@click.option(
    "--server",
    "trade_server",
    default=None,
    help="Broker server name (with --login).",
)
@click.option(
    "--password-env",
    default="MT5_PASSWORD",
    show_default=True,
    help="Environment variable holding the account password (never a flag).",
)
@click.option(
    "--timeout",
    default=60_000,
    show_default=True,
    type=int,
    help="Terminal connect timeout in milliseconds.",
)
@click.option(
    "--allow-trading",
    is_flag=True,
    help="Register mt5_order_send (still demo accounts only).",
)
@click.option(
    "--require-stops/--no-require-stops",
    default=True,
    show_default=True,
    help="Refuse market orders that have no stop loss.",
)
@click.option(
    "--tester-dir",
    "tester_dirs",
    multiple=True,
    help=(
        "Folder to search for Strategy Tester reports (repeatable). Defaults to "
        "the terminal's data folders and the working directory."
    ),
)
@click.option(
    "--allow-tester",
    is_flag=True,
    help=(
        "Register mt5_tester_run, which launches terminal64.exe to run a "
        "backtest. Leave it off unless the machine is dedicated to testing."
    ),
)
@click.option(
    "--http",
    "use_http",
    is_flag=True,
    help="Serve Streamable HTTP instead of stdio (terminal on another machine).",
)
@click.option("--host", default="127.0.0.1", show_default=True, help="HTTP bind host.")
@click.option(
    "--port", default=8765, show_default=True, type=int, help="HTTP bind port."
)
@click.option(
    "--token",
    default=None,
    help="Bearer token required in HTTP mode (mandatory off loopback).",
)
@click.option(
    "--list-tools",
    is_flag=True,
    help="Print the tool table (names, schemas, annotations) and exit.",
)
@click.option(
    "--call",
    "call_tool",
    default=None,
    help="One-shot: call a tool by name and print its JSON result.",
)
@click.option(
    "--args",
    "call_args",
    default="{}",
    show_default=True,
    help="JSON arguments for --call.",
)
@click.option("-v", "--verbose", is_flag=True, help="Debug logging (to stderr).")
def main(
    stub: bool,
    stub_trade_mode: str,
    path: Optional[str],
    login: Optional[str],
    trade_server: Optional[str],
    password_env: str,
    timeout: int,
    allow_trading: bool,
    require_stops: bool,
    tester_dirs: Tuple[str, ...],
    allow_tester: bool,
    use_http: bool,
    host: str,
    port: int,
    token: Optional[str],
    list_tools: bool,
    call_tool: Optional[str],
    call_args: str,
    verbose: bool,
) -> None:
    """Serve a MetaTrader 5 terminal to OpenJarvis as MCP tools."""
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    terminal = _build_terminal(
        stub=stub,
        stub_trade_mode=stub_trade_mode,
        path=path,
        login=login,
        trade_server=trade_server,
        password_env=password_env,
        timeout=timeout,
    )
    try:
        terminal.connect()
    except Mt5Error as exc:
        # Do not die: the terminal may come up later, and --list-tools must
        # work without one. Every tool call retries the connection.
        logger.warning("terminal not connected yet: %s", exc)

    server = build_server(
        terminal,
        allow_trading=allow_trading,
        require_stops=require_stops,
        tester_dirs=tuple(tester_dirs),
        allow_tester_run=allow_tester,
    )

    try:
        if list_tools:
            _print_tools(server)
        elif call_tool:
            # click discards a callback's return value, so exit explicitly
            # (the `finally` below still runs on SystemExit).
            sys.exit(_call_once(server, call_tool, call_args))
        elif use_http:
            serve_http(server, host=host, port=port, token=token)
        else:
            serve_stdio(server)
    finally:
        terminal.close()


def _print_tools(server: Mt5MCPServer) -> None:
    """Human-readable tool table (debugging aid; not protocol traffic)."""
    response = server.handle(MCPRequest(method="tools/list", id=0))
    tools = (response.result or {}).get("tools", [])
    click.echo(f"{SERVER_NAME} {SERVER_VERSION} — {len(tools)} tool(s)\n")
    for tool in tools:
        annotations = tool.get("annotations", {})
        mode = "read-only" if annotations.get("readOnlyHint") else "WRITES"
        schema = tool.get("inputSchema", {})
        required = ", ".join(schema.get("required", [])) or "-"
        props = ", ".join(schema.get("properties", {})) or "-"
        click.echo(f"{tool['name']}  [{mode}]")
        click.echo(f"    {tool.get('description', '')}")
        click.echo(f"    args:     {props}")
        click.echo(f"    required: {required}\n")


def _call_once(server: Mt5MCPServer, name: str, args_json: str) -> int:
    """Invoke one tool locally — the fastest way to check the bridge."""
    try:
        arguments = json.loads(args_json) if args_json else {}
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"--args is not valid JSON: {exc}") from exc
    response = server.handle(
        MCPRequest(
            method="tools/call",
            params={"name": name, "arguments": arguments},
            id=1,
        )
    )
    if response.error is not None:
        click.echo(json.dumps(response.error, indent=2))
        return 1
    result = response.result or {}
    content = "".join(
        block.get("text", "")
        for block in result.get("content", [])
        if block.get("type") == "text"
    )
    try:
        click.echo(json.dumps(json.loads(content), indent=2, default=_json_default))
    except json.JSONDecodeError:
        click.echo(content)
    return 1 if result.get("isError") else 0


if __name__ == "__main__":
    main()
