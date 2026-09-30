"""MQL bench — Expert Advisor code generation for MetaTrader 5.

Function/component-level MQL5 tasks with deterministic structural checks.
There is no MQL compiler in CI, so correctness is *not* executed; instead each
task declares the API surface a correct answer must touch (``required``), the
bonus practices that separate a good answer from a working one (``optional``),
and — the highest-signal check in this benchmark — the MQL4-only idioms that
must **not** appear in MQL5 code (``forbid_mql4``).

That last check is what makes this benchmark useful for model selection: MQL5
is a low-resource language, and the most common failure mode of a small local
model is not a syntax slip but confidently emitting MQL4 (``Ask``/``Bid``,
``OrderSend`` with eleven arguments, ``OrderSelect``, ``AccountBalance``,
``Close[1]``). Those compile-fail or, worse, compile and trade wrongly.

Run it with::

    jarvis eval run -b mql-bench -m qwen3.5:9b --backend jarvis-direct
    python -m openjarvis.evals run mql-bench --model qwen3.5:35b --max-samples 6

Add your own tasks by appending to ``_TASKS``: keep ``required`` to things a
correct answer cannot avoid, and put nice-to-haves in ``optional``.
"""

from __future__ import annotations

import random
from typing import Iterable, List, Optional

from openjarvis.evals.core.dataset import DatasetProvider
from openjarvis.evals.core.splits import apply_split
from openjarvis.evals.core.types import EvalRecord

_PROMPT_TEMPLATE = """You are writing MQL5 for MetaTrader 5 (build 4000+). Write the code described below.

{spec}

Rules:
- MQL5 only. There is no predefined Ask, Bid, Point, Digits, Bars, or series
  array (Time[], Close[]) in MQL5 - use SymbolInfoDouble / SymbolInfoInteger /
  _Point / _Digits / iTime / CopyClose.
- Trade through CTrade from <Trade\\Trade.mqh> unless the task says otherwise.
- Return ONLY the code in a single ```mql5 fence. No prose inside the fence.
{extra}"""

_PORT_PROMPT_EXTRA = """
Port this MQL4 snippet to MQL5. Keep the behaviour identical:

```mql4
{mql4_source}
```
"""

_TASKS = [
    {
        "id": "risk-lot-size",
        "spec": (
            "Write a function `double CalcLotByRisk(const double sl_points)` that "
            "returns the lot size risking `InpRiskPercent` (a global input, percent "
            "of current equity) if the stop loss `sl_points` points away is hit. "
            "Convert the point distance into money with the symbol's tick value and "
            "tick size, then snap the result down to the broker's volume step and "
            "clamp it to the symbol's minimum and maximum volume."
        ),
        "reference": """double CalcLotByRisk(const double sl_points)
  {
   const double min_lot    = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   const double max_lot    = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   const double lot_step   = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   const double tick_value = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
   const double tick_size  = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   const double point      = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   const double equity     = AccountInfoDouble(ACCOUNT_EQUITY);

   if(sl_points <= 0.0 || tick_value <= 0.0 || tick_size <= 0.0 || point <= 0.0)
      return(min_lot);

   const double risk_money   = equity * InpRiskPercent / 100.0;
   const double loss_per_lot = sl_points * point / tick_size * tick_value;
   if(loss_per_lot <= 0.0)
      return(min_lot);

   double lots = MathFloor(risk_money / loss_per_lot / lot_step) * lot_step;
   return(MathMin(max_lot, MathMax(min_lot, lots)));
  }""",
        "required": [
            "AccountInfoDouble",
            "ACCOUNT_EQUITY",
            "SYMBOL_TRADE_TICK_VALUE",
            "SYMBOL_TRADE_TICK_SIZE",
            "SYMBOL_VOLUME_STEP",
            "SYMBOL_VOLUME_MIN",
            "SYMBOL_VOLUME_MAX",
        ],
        "optional": ["MathFloor", "MathMin", "MathMax", "NormalizeDouble"],
        "forbid_mql4": True,
    },
    {
        "id": "new-bar-guard",
        "spec": (
            "Write a function `bool IsNewBar()` that returns true exactly once per "
            "new bar on the chart's timeframe, using a persisted last-seen bar time. "
            "It must be safe to call from OnTick and must not return true twice for "
            "the same bar."
        ),
        "reference": """datetime g_last_bar_time = 0;

bool IsNewBar()
  {
   const datetime bar_time = iTime(_Symbol, PERIOD_CURRENT, 0);
   if(bar_time == 0 || bar_time == g_last_bar_time)
      return(false);
   g_last_bar_time = bar_time;
   return(true);
  }""",
        "required": [
            "re:(iTime\\s*\\(|CopyTime\\s*\\()",
            "re:(static\\s+datetime|datetime\\s+\\w+\\s*=\\s*0)",
            "bool IsNewBar",
        ],
        "optional": ["PERIOD_CURRENT", "_Period", "GetLastError"],
        "forbid_mql4": True,
    },
    {
        "id": "trailing-stop",
        "spec": (
            "Write `void TrailPositions(const double trail_points)` that trails the "
            "stop loss of every open position belonging to this EA on the current "
            "symbol. Use a global `CTrade trade;`. Only touch positions whose magic "
            "number equals `InpMagic`, never move a stop in the losing direction, and "
            "respect the broker's minimum stops level."
        ),
        "reference": """void TrailPositions(const double trail_points)
  {
   const double point    = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   const int    digits   = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
   const double min_dist =
      (double)SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL) * point;

   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      const ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol)
         continue;
      if(PositionGetInteger(POSITION_MAGIC) != (long)InpMagic)
         continue;

      const ENUM_POSITION_TYPE type =
         (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);
      const double sl = PositionGetDouble(POSITION_SL);
      const double tp = PositionGetDouble(POSITION_TP);
      const double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
      const double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);

      if(type == POSITION_TYPE_BUY)
        {
         const double candidate = bid - trail_points * point;
         if(candidate > sl + point && bid - candidate >= min_dist)
            trade.PositionModify(ticket, NormalizeDouble(candidate, digits), tp);
        }
      else
        {
         const double candidate = ask + trail_points * point;
         if((sl == 0.0 || candidate < sl - point) && candidate - ask >= min_dist)
            trade.PositionModify(ticket, NormalizeDouble(candidate, digits), tp);
        }
     }
  }""",
        "required": [
            "PositionsTotal",
            "re:(PositionGetTicket|PositionSelect)",
            "POSITION_MAGIC",
            "PositionModify",
            "SYMBOL_TRADE_STOPS_LEVEL",
        ],
        "optional": ["NormalizeDouble", "POSITION_TYPE_BUY", "i--"],
        "forbid_mql4": True,
    },
    {
        "id": "safe-indicator-read",
        "spec": (
            "Write `bool GetIndicatorValue(const int handle, const int buffer_index, "
            "const int shift, double &value)` that reads one value from an indicator "
            "buffer safely: reject an invalid handle, wait for enough calculated bars, "
            "check the CopyBuffer return value, and index the copied array as a time "
            "series."
        ),
        "reference": """bool GetIndicatorValue(const int handle, const int buffer_index,
                       const int shift, double &value)
  {
   if(handle == INVALID_HANDLE)
      return(false);
   if(BarsCalculated(handle) < shift + 1)
      return(false);

   double buffer[];
   if(CopyBuffer(handle, buffer_index, shift, 1, buffer) != 1)
      return(false);
   if(ArraySize(buffer) < 1)
      return(false);

   value = buffer[0];
   return(true);
  }""",
        "required": [
            "INVALID_HANDLE",
            "CopyBuffer",
            "re:(BarsCalculated|ArraySize|GetLastError)",
            "re:(!=\\s*1|<\\s*1|<=\\s*0|<\\s*0)",
        ],
        "optional": ["ArraySetAsSeries", "ArrayFree", "ResetLastError"],
        "forbid_mql4": True,
    },
    {
        "id": "port-ordersend",
        "spec": (
            "The snippet below opens a market buy with a fixed lot size in MQL4. "
            "Rewrite it as MQL5: read the ask price from symbol info, compute the "
            "stop distance in points, send the trade through CTrade with the EA's "
            "magic number, and check the result retcode."
        ),
        "mql4_source": """int start()
  {
   double lots = 0.1;
   double price = Ask;
   double sl = Ask - 300 * Point;
   double tp = Ask + 600 * Point;
   int ticket = OrderSend(Symbol(), OP_BUY, lots, price, 3, sl, tp,
                          "my ea", MagicNumber, 0, Blue);
   if(ticket < 0)
      Print("OrderSend failed ", GetLastError());
   return(0);
  }""",
        "reference": """#include <Trade\\Trade.mqh>

CTrade trade;

void OpenBuy()
  {
   trade.SetExpertMagicNumber(InpMagic);
   trade.SetDeviationInPoints(InpSlippagePoints);
   trade.SetTypeFillingBySymbol(_Symbol);

   const double point  = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   const int    digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
   const double ask    = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   const double sl     = ask - 300 * point;
   const double tp     = ask + 600 * point;

   if(!trade.Buy(0.1, _Symbol, NormalizeDouble(ask, digits),
                 NormalizeDouble(sl, digits), NormalizeDouble(tp, digits), "my ea"))
      PrintFormat("Buy failed: retcode=%d %s",
                  trade.ResultRetcode(), trade.ResultRetcodeDescription());
  }""",
        "required": [
            "SymbolInfoDouble",
            "re:(SYMBOL_ASK|SYMBOL_BID)",
            "re:(_Point|SYMBOL_POINT)",
            "re:(CTrade|MqlTradeRequest)",
            "re:(ResultRetcode|TRADE_RETCODE|retcode)",
        ],
        "optional": [
            "SetTypeFillingBySymbol",
            "SetDeviationInPoints",
            "NormalizeDouble",
        ],
        "forbid_mql4": True,
    },
    {
        "id": "count-own-positions",
        "spec": (
            "Write `int CountOwnPositions(const ulong magic)` returning how many open "
            "positions on the current symbol were opened by this EA (matching magic "
            "number). Iterate defensively: a ticket lookup can fail mid-loop."
        ),
        "reference": """int CountOwnPositions(const ulong magic)
  {
   int total = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      const ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol)
         continue;
      if((ulong)PositionGetInteger(POSITION_MAGIC) != magic)
         continue;
      total++;
     }
   return(total);
  }""",
        "required": [
            "PositionsTotal",
            "PositionGetTicket",
            "POSITION_SYMBOL",
            "POSITION_MAGIC",
        ],
        "optional": ["i--", "continue"],
        "forbid_mql4": True,
    },
    {
        "id": "daily-loss-limit",
        "spec": (
            "Write `bool DailyLossLimitHit(const double max_loss_percent)` that "
            "returns true once equity has fallen `max_loss_percent` below the balance "
            "recorded at the start of the current server trading day. The day-start "
            "balance must be captured once per day and reset when the date changes."
        ),
        "reference": """double   g_day_start_equity = 0.0;
int      g_day_stamp        = -1;

int DayStamp(const datetime when)
  {
   MqlDateTime dt;
   TimeToStruct(when, dt);
   return(dt.year * 10000 + dt.mon * 100 + dt.day);
  }

bool DailyLossLimitHit(const double max_loss_percent)
  {
   const int today = DayStamp(TimeCurrent());
   if(today != g_day_stamp)
     {
      g_day_stamp = today;
      g_day_start_equity = AccountInfoDouble(ACCOUNT_EQUITY);
     }
   if(g_day_start_equity <= 0.0)
      return(false);

   const double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   const double drawdown_pct =
      (g_day_start_equity - equity) / g_day_start_equity * 100.0;
   return(drawdown_pct >= max_loss_percent);
  }""",
        "required": [
            "AccountInfoDouble",
            "ACCOUNT_EQUITY",
            "re:(TimeCurrent|TimeToStruct|MqlDateTime)",
            "re:(static|int\\s+g_|double\\s+g_)",
        ],
        "optional": ["TimeToStruct", "MqlDateTime", "Print"],
        "forbid_mql4": True,
    },
    {
        "id": "atr-stop",
        "spec": (
            "Write `double AtrStopPoints(const int atr_handle, const double multiplier)` "
            "that returns a stop distance in points from the ATR value of the last "
            "closed bar. Read the ATR from its handle (do not create the handle inside "
            "the function), validate the copy, and convert the price distance to points "
            "using the symbol point size."
        ),
        "reference": """double AtrStopPoints(const int atr_handle, const double multiplier)
  {
   if(atr_handle == INVALID_HANDLE)
      return(0.0);

   double atr[];
   if(CopyBuffer(atr_handle, 0, 1, 1, atr) != 1)
      return(0.0);

   const double point = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   if(point <= 0.0)
      return(0.0);

   return(atr[0] * multiplier / point);
  }""",
        "required": [
            "CopyBuffer",
            "INVALID_HANDLE",
            "SYMBOL_POINT",
            "re:(!=\\s*1|<\\s*1)",
        ],
        "optional": ["ArraySetAsSeries", "iATR", "MathMax"],
        "forbid_mql4": True,
    },
    {
        "id": "partial-close",
        "spec": (
            "Write `bool CloseHalfPosition(const ulong ticket)` that closes half of an "
            "open position: select it, read its volume, halve it, normalize the result "
            "to the symbol volume step and minimum, and close that part with a global "
            "`CTrade trade;`. Refuse to act if the remaining volume would fall below the "
            "broker minimum."
        ),
        "reference": """bool CloseHalfPosition(const ulong ticket)
  {
   if(!PositionSelectByTicket(ticket))
      return(false);

   const double volume   = PositionGetDouble(POSITION_VOLUME);
   const double min_lot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   const double lot_step = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   if(volume <= 0.0 || lot_step <= 0.0)
      return(false);

   double part = MathFloor(volume * 0.5 / lot_step) * lot_step;
   if(part < min_lot || volume - part < min_lot)
      return(false);

   return(trade.PositionClosePartial(ticket, part));
  }""",
        "required": [
            "re:(PositionSelectByTicket|PositionSelect|PositionGetTicket)",
            "POSITION_VOLUME",
            "PositionClosePartial",
            "SYMBOL_VOLUME_MIN",
            "SYMBOL_VOLUME_STEP",
        ],
        "optional": ["MathFloor", "NormalizeDouble", "ResultRetcode"],
        "forbid_mql4": True,
    },
    {
        "id": "input-validation",
        "spec": (
            "Write an `int OnInit()` for an EA with inputs `InpFastPeriod`, "
            "`InpSlowPeriod`, `InpRiskPercent` and `InpStopLossPoints`. Validate that "
            "the fast period is positive and smaller than the slow period, that risk is "
            "in (0, 5] percent, and that the stop loss is positive. Log a readable "
            "message for each failure and return the MQL5 code that tells the terminal "
            "the parameters are wrong. On success create a fast/slow EMA handle pair and "
            "fail init if either handle is invalid."
        ),
        "reference": """input int    InpFastPeriod     = 12;
input int    InpSlowPeriod     = 48;
input double InpRiskPercent    = 1.0;
input int    InpStopLossPoints = 300;

int g_fast = INVALID_HANDLE;
int g_slow = INVALID_HANDLE;

int OnInit()
  {
   if(InpFastPeriod < 1 || InpFastPeriod >= InpSlowPeriod)
     {
      Print("Fast period must be >= 1 and smaller than the slow period");
      return(INIT_PARAMETERS_INCORRECT);
     }
   if(InpRiskPercent <= 0.0 || InpRiskPercent > 5.0)
     {
      Print("Risk percent must be in (0, 5]");
      return(INIT_PARAMETERS_INCORRECT);
     }
   if(InpStopLossPoints < 1)
     {
      Print("Stop loss must be positive");
      return(INIT_PARAMETERS_INCORRECT);
     }

   g_fast = iMA(_Symbol, PERIOD_CURRENT, InpFastPeriod, 0, MODE_EMA, PRICE_CLOSE);
   g_slow = iMA(_Symbol, PERIOD_CURRENT, InpSlowPeriod, 0, MODE_EMA, PRICE_CLOSE);
   if(g_fast == INVALID_HANDLE || g_slow == INVALID_HANDLE)
     {
      PrintFormat("Indicator handle creation failed (error %d)", GetLastError());
      return(INIT_FAILED);
     }
   return(INIT_SUCCEEDED);
  }""",
        "required": [
            "int OnInit",
            "INIT_PARAMETERS_INCORRECT",
            "INIT_SUCCEEDED",
            "re:(Print|PrintFormat)",
            "input ",
        ],
        "optional": ["INIT_FAILED", "INVALID_HANDLE", "GetLastError"],
        "forbid_mql4": True,
    },
    {
        "id": "trade-transaction-log",
        "spec": (
            "Write an `OnTradeTransaction` handler that logs every deal added to history "
            "for this EA's magic number (`InpMagic`): deal ticket, entry type, volume and "
            "profit. Ignore transactions that are not deal additions."
        ),
        "reference": """void OnTradeTransaction(const MqlTradeTransaction &trans,
                        const MqlTradeRequest &request,
                        const MqlTradeResult &result)
  {
   if(trans.type != TRADE_TRANSACTION_DEAL_ADD)
      return;
   if(!HistoryDealSelect(trans.deal))
      return;

   const long magic = HistoryDealGetInteger(trans.deal, DEAL_MAGIC);
   if(magic != (long)InpMagic)
      return;

   const ENUM_DEAL_ENTRY entry =
      (ENUM_DEAL_ENTRY)HistoryDealGetInteger(trans.deal, DEAL_ENTRY);
   const double volume = HistoryDealGetDouble(trans.deal, DEAL_VOLUME);
   const double profit = HistoryDealGetDouble(trans.deal, DEAL_PROFIT);

   PrintFormat("deal=%I64u entry=%s volume=%.2f profit=%.2f",
               trans.deal, EnumToString(entry), volume, profit);
  }""",
        "required": [
            "void OnTradeTransaction",
            "MqlTradeTransaction",
            "TRADE_TRANSACTION_DEAL_ADD",
            "re:(HistoryDealSelect|HistoryDealGetInteger)",
            "DEAL_MAGIC",
        ],
        "optional": ["DEAL_ENTRY", "DEAL_VOLUME", "DEAL_PROFIT", "EnumToString"],
        "forbid_mql4": True,
    },
    {
        "id": "entry-filters",
        "spec": (
            "Write `bool EntryAllowed(const int max_spread_points)` that returns true "
            "only when the symbol spread is within the limit, the terminal and the EA "
            "are allowed to trade, and the symbol is currently tradable. Use the MQL5 "
            "info functions, not the MQL4 market-info API."
        ),
        "reference": """bool EntryAllowed(const int max_spread_points)
  {
   if(max_spread_points > 0)
     {
      const long spread = SymbolInfoInteger(_Symbol, SYMBOL_SPREAD);
      if(spread > max_spread_points)
         return(false);
     }

   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED))
      return(false);
   if(!MQLInfoInteger(MQL_TRADE_ALLOWED))
      return(false);
   if(!AccountInfoInteger(ACCOUNT_TRADE_EXPERT))
      return(false);

   const ENUM_SYMBOL_TRADE_MODE mode =
      (ENUM_SYMBOL_TRADE_MODE)SymbolInfoInteger(_Symbol, SYMBOL_TRADE_MODE);
   return(mode == SYMBOL_TRADE_MODE_FULL);
  }""",
        "required": [
            "SymbolInfoInteger",
            "SYMBOL_SPREAD",
            "re:(TerminalInfoInteger|MQLInfoInteger|TERMINAL_TRADE_ALLOWED|MQL_TRADE_ALLOWED)",
        ],
        "optional": [
            "SYMBOL_TRADE_MODE",
            "ACCOUNT_TRADE_EXPERT",
            "SYMBOL_TRADE_MODE_FULL",
        ],
        "forbid_mql4": True,
    },
]


class MQLBenchDataset(DatasetProvider):
    """MQL5 Expert Advisor component benchmark (deterministic, no download)."""

    dataset_id = "mql-bench"
    dataset_name = "MQL Bench"

    def __init__(self) -> None:
        self._records: List[EvalRecord] = []

    def load(
        self,
        *,
        max_samples: Optional[int] = None,
        split: Optional[str] = None,
        seed: Optional[int] = None,
    ) -> None:
        rows = list(_TASKS)

        effective_seed = 42 if seed is None else seed
        if split in ("train", "test", "all"):
            rows = apply_split(rows, split=split, seed=effective_seed, train_frac=0.2)
        elif seed is not None:
            random.Random(seed).shuffle(rows)

        if max_samples is not None:
            rows = rows[:max_samples]

        self._records = []
        for task in rows:
            extra = ""
            if task.get("mql4_source"):
                extra = _PORT_PROMPT_EXTRA.format(mql4_source=task["mql4_source"])
            prompt = _PROMPT_TEMPLATE.format(spec=task["spec"], extra=extra)
            self._records.append(
                EvalRecord(
                    record_id=f"mql-bench-{task['id']}",
                    problem=prompt,
                    reference=task["reference"],
                    category="coding",
                    subject="mql_bench",
                    metadata={
                        "task_id": task["id"],
                        "dialect": "mql5",
                        "required": list(task.get("required", [])),
                        "optional": list(task.get("optional", [])),
                        "forbid_mql4": bool(task.get("forbid_mql4", True)),
                        "extra_forbidden": list(task.get("extra_forbidden", [])),
                    },
                )
            )

    def iter_records(self) -> Iterable[EvalRecord]:
        return iter(self._records)

    def size(self) -> int:
        return len(self._records)


__all__ = ["MQLBenchDataset"]
