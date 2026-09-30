"""mql-bench scorer — deterministic structural scoring for MQL5 answers.

No MQL compiler runs in CI, so this scorer does not execute code. It scores
three things, in decreasing order of signal:

1. **Required API surface** (78%/70% weight) — the calls and constants a
   correct answer cannot avoid (``SYMBOL_VOLUME_STEP`` for lot normalization,
   ``POSITION_MAGIC`` for position filtering, ``INIT_PARAMETERS_INCORRECT`` for
   input validation, ...). Checks are literal substrings, or regexes when
   prefixed with ``re:``.
2. **MQL4 contamination** (22%/20% weight) — bare ``Ask``/``Bid``/``Point``/
   ``Digits``, ``OrderClose``/``OrderSelect``/``OrderModify``, the eleven-argument
   ``OrderSend``, ``AccountBalance()``, ``MarketInfo()``, ``OP_BUY``, ``Close[1]``
   series arrays, ``IsTradeAllowed()``. Each hit costs a third of this component;
   any hit also fails the sample outright. This is the failure mode that makes a
   small model look fluent and produce code MetaEditor rejects.
3. **Optional good practice** (10% weight, folded into the others when a task
   declares none) — ``MathFloor`` on lot steps, ``NormalizeDouble`` on prices,
   backward iteration while modifying positions.

Matching runs on a comment- and string-stripped copy of the extracted source, so
a model that *explains* "MQL5 has no Ask/Bid" in a comment is not penalized for
the words. Line numbers are preserved through the strip so hits are reportable.

``is_correct`` is deliberately strict: every required check matched **and** zero
MQL4-isms. ``score`` is the graded version, usable as a gate signal for
LLM-guided spec search.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from openjarvis.evals.core.scorer import Scorer
from openjarvis.evals.core.types import EvalRecord

LOGGER = logging.getLogger(__name__)

_WEIGHT_REQUIRED = 0.70
_WEIGHT_SAFETY = 0.20
_WEIGHT_OPTIONAL = 0.10
_SAFETY_PENALTY_PER_HIT = 1.0 / 3.0

# ---------------------------------------------------------------------------
# MQL4-only idioms that must not appear in MQL5 code
# ---------------------------------------------------------------------------

MQL4_ISM_PATTERNS: List[Tuple[str, str]] = [
    (
        "predefined Ask/Bid (use SymbolInfoDouble with SYMBOL_ASK/SYMBOL_BID)",
        r"(?<![\w_])(Ask|Bid)(?![\w_])",
    ),
    (
        "predefined Point/Digits (use _Point/_Digits)",
        r"(?<![\w_])(Point|Digits)(?![\w_])",
    ),
    (
        "predefined Bars variable (use Bars(_Symbol, _Period))",
        r"(?<![\w_])Bars(?![\w_(])",
    ),
    (
        "MQL4 series array (use iTime/iClose/CopyClose with ArraySetAsSeries)",
        r"(?<![\w_])(Time|Open|High|Low|Close|Volume)\s*\[",
    ),
    (
        "MQL4 order-pool function (positions and orders are separate in MQL5)",
        r"(?<![.\w_])(OrderClose|OrderModify|OrderDelete|OrderSelect|OrderType"
        r"|OrderTicket|OrderLots|OrderProfit|OrderMagicNumber|OrderComment"
        r"|OrderSymbol|OrderOpenPrice|OrderClosePrice|OrderStopLoss"
        r"|OrderTakeProfit|OrderOpenTime|OrderCloseTime)\s*\(",
    ),
    (
        "MQL4 account function (use AccountInfoDouble/AccountInfoInteger)",
        r"(?<![.\w_])(AccountBalance|AccountEquity|AccountFreeMargin|AccountMargin"
        r"|AccountProfit|AccountNumber|AccountCurrency|AccountLeverage"
        r"|AccountCompanyName)\s*\(",
    ),
    (
        "MQL4 MarketInfo (use SymbolInfoDouble/SymbolInfoInteger)",
        r"(?<![.\w_])MarketInfo\s*\(",
    ),
    (
        "MQL4 trade/market enum constant",
        r"(?<![\w_])(OP_BUY|OP_SELL|OP_BUYLIMIT|OP_SELLLIMIT|OP_BUYSTOP|OP_SELLSTOP"
        r"|MODE_SPREAD|MODE_ASK|MODE_BID|MODE_POINT|MODE_DIGITS|MODE_MINLOT"
        r"|MODE_MAXLOT|MODE_LOTSTEP|MODE_STOPLEVEL|MODE_FREEZELEVEL"
        r"|MODE_TICKVALUE|MODE_TICKSIZE|MODE_EXPIRATION)\b",
    ),
    (
        "MQL4 terminal-state function (use TerminalInfoInteger/MQLInfoInteger)",
        r"(?<![.\w_])(IsTradeAllowed|IsTradeContextBusy|IsConnected|IsTesting"
        r"|IsOptimization|IsVisualMode|IsDemo|IsDllsAllowed)\s*\(",
    ),
    (
        "MQL4 conversion function (use DoubleToString/StringToDouble/...)",
        r"(?<![.\w_])(DoubleToStr|StrToDouble|StrToInteger|IntegerToStr|DoubleToStrMore"
        r")\s*\(",
    ),
    (
        "MQL4 start() entry point (MQL5 uses OnInit/OnTick/OnStart)",
        r"(?<![.\w_])int\s+start\s*\(\s*\)",
    ),
]

_MQL_MARKERS = (
    "#property",
    "OnInit",
    "OnTick",
    "OnCalculate",
    "OnTradeTransaction",
    "SymbolInfo",
    "CopyBuffer",
    "CTrade",
    "PositionSelect",
    "PositionsTotal",
    "input ",
)

_FENCE_RE = re.compile(
    r"```(?P<tag>[A-Za-z0-9_+-]*)[ \t]*\r?\n(?P<body>.*?)```",
    re.DOTALL,
)
# Preference order matters for the port task: a model may show the MQL4
# original and then the MQL5 port, so MQL5 tags and markers win over MQL4 ones.
_MQL5_TAGS = {"mql5", "mq5", "mql"}
_MQL4_TAGS = {"mql4", "mq4"}
_CODE_TAGS = {"cpp", "c++", "c", "code", ""}

_MQL5_MARKERS = (
    "SymbolInfoDouble",
    "SymbolInfoInteger",
    "AccountInfoDouble",
    "CTrade",
    "CopyBuffer",
    "PositionsTotal",
    "PositionGetTicket",
    "INIT_SUCCEEDED",
    "_Point",
    "_Digits",
)


# ---------------------------------------------------------------------------
# Source extraction
# ---------------------------------------------------------------------------


def extract_mql_source(answer: str) -> Tuple[str, bool]:
    """Extract the MQL source from a model answer.

    Returns ``(source, was_fenced)``. Preference order:

    1. a fence tagged ``mql5`` / ``mq5`` / ``mql``;
    2. a fence tagged ``mql4`` / ``mq4`` (only relevant for port tasks, where
       echoing the original is a legitimate failure signal);
    3. the *last* generic fence containing MQL5-specific API calls — models
       porting from MQL4 often show the original first and the port second;
    4. the last generic fence that looks like MQL at all;
    5. the first fence, then the whole answer.
    """
    fences = list(_FENCE_RE.finditer(answer or ""))
    if not fences:
        text = (answer or "").strip()
        return (text, False) if text else ("", False)

    for tags in (_MQL5_TAGS, _MQL4_TAGS):
        for match in fences:
            if match.group("tag").strip().lower() in tags:
                return match.group("body").strip(), True

    generic = [m for m in fences if m.group("tag").strip().lower() in _CODE_TAGS]
    mql5_generic = [m for m in generic if _looks_like_mql5(m.group("body"))]
    if mql5_generic:
        return mql5_generic[-1].group("body").strip(), True

    mql_generic = [m for m in generic if _looks_like_mql(m.group("body"))]
    if mql_generic:
        return mql_generic[-1].group("body").strip(), True

    if _looks_like_mql(fences[0].group("body")):
        return fences[0].group("body").strip(), True

    return fences[-1].group("body").strip(), True


def _looks_like_mql(body: str) -> bool:
    return any(marker in body for marker in _MQL_MARKERS)


def _looks_like_mql5(body: str) -> bool:
    return any(marker in body for marker in _MQL5_MARKERS)


def strip_comments_and_strings(code: str) -> str:
    """Blank out comments and string/char literals, preserving offsets.

    Replacing (rather than deleting) keeps line and column numbers aligned with
    the original source, so a forbidden-pattern hit can be reported as
    ``line:col``.
    """
    out: List[str] = []
    i = 0
    n = len(code)
    while i < n:
        ch = code[i]
        nxt = code[i + 1] if i + 1 < n else ""

        # line comment
        if ch == "/" and nxt == "/":
            while i < n and code[i] != "\n":
                out.append(" ")
                i += 1
            continue
        # block comment
        if ch == "/" and nxt == "*":
            while i < n and not (code[i] == "*" and i + 1 < n and code[i + 1] == "/"):
                out.append("\n" if code[i] == "\n" else " ")
                i += 1
            out.append("  ")
            i += 2
            continue
        # string literal
        if ch == '"':
            out.append(" ")
            i += 1
            while i < n and code[i] != '"':
                if code[i] == "\\" and i + 1 < n:
                    out.append("  ")
                    i += 2
                    continue
                out.append("\n" if code[i] == "\n" else " ")
                i += 1
            if i < n:
                out.append(" ")
                i += 1
            continue
        # char literal
        if ch == "'":
            out.append(" ")
            i += 1
            while i < n and code[i] != "'":
                out.append(" ")
                i += 1
            if i < n:
                out.append(" ")
                i += 1
            continue

        out.append(ch)
        i += 1
    return "".join(out)


def _line_of(code: str, index: int) -> int:
    return code.count("\n", 0, index) + 1


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def _matches(code: str, check: str) -> bool:
    if check.startswith("re:"):
        try:
            return re.search(check[3:], code) is not None
        except re.error:
            LOGGER.warning("invalid regex check %r — treated as literal", check)
            return check[3:] in code
    return check in code


_ORDERSEND_CALL_RE = re.compile(r"(?<![.\w_])OrderSend\s*\(")

#: MQL5's ``OrderSend(request, result)`` has exactly one top-level comma; the
#: MQL4 signature has ten. Three or more means the model emitted the MQL4 call.
_ORDERSEND_MQL4_MIN_COMMAS = 3


def _top_level_commas(code: str, open_paren: int) -> Tuple[int, int]:
    """Count top-level commas in the call starting at ``code[open_paren] == '('``.

    Returns ``(comma_count, index_after_close)``. Scanning by depth (rather than
    with a regex) is what makes ``OrderSend(Symbol(), OP_BUY, ...)`` detectable
    despite its nested parentheses.
    """
    depth = 0
    commas = 0
    i = open_paren
    while i < len(code):
        ch = code[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return commas, i + 1
        elif ch == "," and depth == 1:
            commas += 1
        i += 1
    return commas, i


def _find_mql4_ordersend(code: str) -> List[Dict[str, Any]]:
    """Flag ``OrderSend`` calls with an MQL4-shaped argument list."""
    hits: List[Dict[str, Any]] = []
    for match in _ORDERSEND_CALL_RE.finditer(code):
        open_paren = code.index("(", match.start())
        commas, _end = _top_level_commas(code, open_paren)
        if commas >= _ORDERSEND_MQL4_MIN_COMMAS:
            hits.append(
                {
                    "label": (
                        "MQL4-style OrderSend argument list "
                        "(MQL5 takes MqlTradeRequest + MqlTradeResult)"
                    ),
                    "match": f"OrderSend(... {commas} arguments)",
                    "line": _line_of(code, match.start()),
                }
            )
    return hits


def find_mql4_isms(code: str) -> List[Dict[str, Any]]:
    """Return every MQL4-only idiom found in *code*.

    Expects comment- and string-stripped source (see
    :func:`strip_comments_and_strings`) so that idioms mentioned in prose are
    not counted and so that offsets still map to original line numbers.
    """
    hits: List[Dict[str, Any]] = []
    for label, pattern in MQL4_ISM_PATTERNS:
        for match in re.finditer(pattern, code):
            hits.append(
                {
                    "label": label,
                    "match": match.group(0).strip(),
                    "line": _line_of(code, match.start()),
                }
            )
    hits.extend(_find_mql4_ordersend(code))
    hits.sort(key=lambda hit: hit["line"])
    return hits


class MQLBenchScorer(Scorer):
    """Structural scorer for the ``mql-bench`` dataset.

    Deterministic: no judge backend is used. The constructor accepts the same
    ``(judge_backend, judge_model)`` pair as every other scorer so the CLI
    factory stays uniform.
    """

    scorer_id = "mql_bench"

    def __init__(self, judge_backend: Any = None, judge_model: str = "") -> None:
        self._judge_backend = judge_backend
        self._judge_model = judge_model

    def score(
        self,
        record: EvalRecord,
        model_answer: str,
    ) -> Tuple[Optional[bool], Dict[str, Any]]:
        meta = record.metadata or {}
        required: List[str] = list(meta.get("required") or [])
        optional: List[str] = list(meta.get("optional") or [])
        forbid_mql4: bool = bool(meta.get("forbid_mql4", True))
        extra_forbidden: List[str] = list(meta.get("extra_forbidden") or [])

        source, fenced = extract_mql_source(model_answer or "")
        code = strip_comments_and_strings(source)

        missing = [check for check in required if not _matches(code, check)]
        matched_required = len(required) - len(missing)
        required_score = 1.0 if not required else matched_required / len(required)

        optional_matched = [c for c in optional if _matches(code, c)]
        optional_score = len(optional_matched) / len(optional) if optional else None

        isms = find_mql4_isms(code) if forbid_mql4 else []
        for pattern in extra_forbidden:
            for match in re.finditer(pattern, code):
                isms.append(
                    {
                        "label": f"task-specific forbidden pattern {pattern!r}",
                        "match": match.group(0).strip(),
                        "line": _line_of(code, match.start()),
                    }
                )
        safety_score = max(0.0, 1.0 - _SAFETY_PENALTY_PER_HIT * len(isms))

        if optional_score is None:
            # No bonus checks declared: fold their weight into the two real ones.
            total = _WEIGHT_REQUIRED + _WEIGHT_SAFETY
            score = (
                _WEIGHT_REQUIRED * required_score + _WEIGHT_SAFETY * safety_score
            ) / total
            bonus_component = None
        else:
            score = (
                _WEIGHT_REQUIRED * required_score
                + _WEIGHT_SAFETY * safety_score
                + _WEIGHT_OPTIONAL * optional_score
            )
            bonus_component = round(optional_score, 4)

        is_correct = (not missing) and (not isms) and bool(source.strip())

        metadata: Dict[str, Any] = {
            "task_id": meta.get("task_id", ""),
            "dialect": meta.get("dialect", "mql5"),
            "score": round(score, 4),
            "required_total": len(required),
            "required_matched": matched_required,
            "required_score": round(required_score, 4),
            "missing_required": missing,
            "optional_matched": optional_matched,
            "optional_score": bonus_component,
            "mql4_ism_count": len(isms),
            "mql4_isms": isms[:20],
            "safety_score": round(safety_score, 4),
            "fenced": fenced,
            "source_chars": len(source),
            "source_lines": source.count("\n") + 1 if source else 0,
        }
        if not source.strip():
            metadata["note"] = "no MQL source found in the model answer"
        return is_correct, metadata


__all__ = [
    "MQL4_ISM_PATTERNS",
    "MQLBenchScorer",
    "extract_mql_source",
    "find_mql4_isms",
    "strip_comments_and_strings",
]
