# MQL5 API cheatsheet for Expert Advisors

Snippets in the shapes that actually compile. Verify signatures against the
indexed MQL5 reference (`knowledge_search`) when something looks unfamiliar —
this page is a map, not the territory.

## Program skeleton

```mql5
#property copyright   "you"
#property version     "1.00"
#property description "one line shown in the EA tooltip"

#include <Trade\Trade.mqh>      // stdlib: angle brackets + backslash
#include "MyLib.mqh"            // your own file: quotes, relative path

input group "=== Strategy ==="   // groups inputs in the dialog
input int    InpPeriod = 14;     // display name after //
input double InpRisk   = 1.0;

int  OnInit()                        { return(INIT_SUCCEEDED); }
void OnDeinit(const int reason)      { }
void OnTick()                        { }
void OnTimer()                       { }
void OnTrade()                       { }
void OnChartEvent(const int id, const long &lparam,
                  const double &dparam, const string &sparam) { }
// Indicator event, kept here for reference: an Expert Advisor never receives it.
int  OnCalculate(const int rates_total, const int prev_calculated,
                 const datetime &time[], const double &open[],
                 const double &high[], const double &low[],
                 const double &close[], const long &tick_volume[],
                 const long &volume[], const int &spread[]) { return(rates_total); }
```

`OnInit` return values: `INIT_SUCCEEDED`; `INIT_FAILED` — the EA is unloaded
from the chart (an indicator stays but stops receiving events); and
`INIT_PARAMETERS_INCORRECT`, which nothing retries: in the tester that input set
is skipped and its row is marked red, so use it to *reject* a parameter set
during an optimization, not to wait for data that is not there yet. Too many
rejected sets distort a genetic optimization, which assumes the criterion is
smooth across the input space.

## Indicators: handles + CopyBuffer

```mql5
int h = iMA(_Symbol, _Period, InpPeriod, 0, MODE_SMA, PRICE_CLOSE);
if(h == INVALID_HANDLE) return(INIT_FAILED);      // create once, in OnInit

// in OnTick:
if(BarsCalculated(h) < InpPeriod + 2) return;      // not enough data yet
double buf[];
int copied = CopyBuffer(h, 0, 1, 3, buf);          // buffer 0, from shift 1, 3 values
if(copied != 3) return;                            // NEVER assume success
ArraySetAsSeries(buf, true);                       // buf[0] = shift 1 (latest closed)
double current = buf[0];
double previous = buf[1];

IndicatorRelease(h);                               // in OnDeinit
```

Common handles: `iMA`, `iRSI`, `iMACD`, `iATR`, `iStochastic`, `iBands`,
`iCustom(symbol, period, "MyIndicator", args...)`.

## Prices, symbol info

```mql5
double ask   = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
double bid   = SymbolInfoDouble(_Symbol, SYMBOL_BID);
double point = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
int    digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
long   spread = SymbolInfoInteger(_Symbol, SYMBOL_SPREAD);      // in points
double min_lot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
double max_lot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
double lot_step = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
long   stops    = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL);
long   filling  = SymbolInfoInteger(_Symbol, SYMBOL_FILLING_MODE);
ENUM_SYMBOL_CALC_MODE calc =
   (ENUM_SYMBOL_CALC_MODE)SymbolInfoInteger(_Symbol, SYMBOL_TRADE_CALC_MODE);
```

## Account info

```mql5
double balance = AccountInfoDouble(ACCOUNT_BALANCE);
double equity  = AccountInfoDouble(ACCOUNT_EQUITY);
double free    = AccountInfoDouble(ACCOUNT_MARGIN_FREE);
double used    = AccountInfoDouble(ACCOUNT_MARGIN);
long   login   = AccountInfoInteger(ACCOUNT_LOGIN);
ENUM_ACCOUNT_MARGIN_MODE mode =
   (ENUM_ACCOUNT_MARGIN_MODE)AccountInfoInteger(ACCOUNT_MARGIN_MODE);  // retail hedging vs netting
string currency = AccountInfoString(ACCOUNT_CURRENCY);
```

## Positions

```mql5
for(int i = PositionsTotal() - 1; i >= 0; i--)          // backwards if you modify
  {
   ulong ticket = PositionGetTicket(i);                  // selects it as a side effect
   if(ticket == 0) continue;
   if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;
   if(PositionGetInteger(POSITION_MAGIC) != (long)InpMagic) continue;

   ENUM_POSITION_TYPE type = (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);
   double volume = PositionGetDouble(POSITION_VOLUME);
   double open   = PositionGetDouble(POSITION_PRICE_OPEN);
   double sl     = PositionGetDouble(POSITION_SL);
   double tp     = PositionGetDouble(POSITION_TP);
   double profit = PositionGetDouble(POSITION_PROFIT);
  }

// by symbol (one call, no loop) — netting accounts:
if(PositionSelect(_Symbol))
   double vol = PositionGetDouble(POSITION_VOLUME);
```

`PositionsTotal()` = open positions. `OrdersTotal()` = **pending orders only**.
History: `HistorySelect(from, to)`, `HistoryDealsTotal()`, `HistoryDealGetTicket(i)`.

## Trading with CTrade

```mql5
CTrade trade;
trade.SetExpertMagicNumber(InpMagic);
trade.SetDeviationInPoints(InpSlippagePoints);
trade.SetTypeFillingBySymbol(_Symbol);     // avoids retcode 10030
trade.LogLevel(LOG_LEVEL_ERRORS);

trade.Buy(lots, _Symbol, 0.0, sl, tp, "comment");       // 0.0 = market price
trade.Sell(lots, _Symbol, 0.0, sl, tp, "comment");
trade.PositionOpen(_Symbol, ORDER_TYPE_BUY, lots, price, sl, tp, "comment");
trade.PositionClose(ticket);
trade.PositionClosePartial(ticket, lots_to_close);
trade.PositionModify(ticket, sl, tp);
trade.BuyLimit(lots, price, _Symbol, sl, tp, ORDER_TIME_GTC, 0, "comment");
trade.OrderDelete(order_ticket);

if(!trade.PositionModify(ticket, sl, tp) ||
   !RetcodeIsSuccess(trade.ResultRetcode()))             // see below: both halves
   PrintFormat("modify not accepted: %u %s",
               trade.ResultRetcode(), trade.ResultRetcodeDescription());
```

**The `bool` a CTrade call returns is not the server's verdict.** The reference
defines it as "successful check of the basic structures" and adds that
"successful completion … does not always mean successful execution of the trade
operation" — read `ResultRetcode()`
(mql5.com/en/docs/standardlibrary/tradeclasses/ctrade/ctradepositionopen).
`if(!trade.Buy(...)) Print(...)` therefore stays silent on a server rejection
such as `10016` or `10019`. Success is the three codes in `compile-errors.md`:

```mql5
bool RetcodeIsSuccess(const uint retcode)
  {
   return(retcode == (uint)TRADE_RETCODE_PLACED ||        // 10008 pending accepted
          retcode == (uint)TRADE_RETCODE_DONE ||          // 10009
          retcode == (uint)TRADE_RETCODE_DONE_PARTIAL);   // 10010
  }
```

### Stops level: measured from the *closing* price

`SYMBOL_TRADE_STOPS_LEVEL` is the minimum distance from the price the position
is closed at — **Bid for a buy, Ask for a sell**
(mql5.com/en/articles/2555; the freeze level uses the same references):

| | SL must satisfy | TP must satisfy |
|---|---|---|
| Buy | `Bid - SL >= level` | `TP - Bid >= level` |
| Sell | `SL - Ask >= level` | `Ask - TP >= level` |

The closing price is the *opposite* side of the quote from the entry (a buy
enters at the Ask and closes at the Bid), so an SL typed `n` points from the entry
is only `n - spread` from the price that counts — buy and sell alike. With level
300 and spread 20, `n = 300` passes a naive `n >= level` test on either side and
the server answers `10016`. (TP goes the other way: `spread` *further* than typed.) Check the real SL *and* TP prices against Bid/Ask
(`StopsAreValid()` in `templates/ea-template.mq5`); checking only the SL, or only
the distance you typed, is the bug.

### Rounding a lot down to the step

```mql5
double lots = MathFloor(volume / lot_step + 1e-8) * lot_step;   // epsilon is not optional
```

`0.3 / 0.1` is `2.9999999999999996` in a double, so a bare `MathFloor` turns an
exact `0.3` into `0.2` — and with a 0.01 step, 61 of the first 500 exact lot
sizes lose a step the same way.

`SetTypeFillingBySymbol` reads `SYMBOL_FILLING_MODE` for you, and the reference
documents the tie-break: when a symbol allows both `SYMBOL_FILLING_FOK` and
`SYMBOL_FILLING_IOC`, it sets **`ORDER_FILLING_FOK`** — all-or-nothing, so no
partial fills
(mql5.com/en/docs/standardlibrary/tradeclasses/ctrade/ctradesettypefillingbysymbol).
If a strategy needs partial filling, call `SetTypeFilling(ORDER_FILLING_IOC)`
yourself instead of assuming the symbol-derived choice.

### Which filling mode a request may carry

`ENUM_ORDER_TYPE_FILLING` has four members — `ORDER_FILLING_FOK`,
`ORDER_FILLING_IOC`, `ORDER_FILLING_RETURN` and `ORDER_FILLING_BOC` — and which
ones are legal depends on the symbol's **execution mode** (`SYMBOL_TRADE_EXEMODE`)
as well as on `SYMBOL_FILLING_MODE`
(mql5.com/en/docs/constants/tradingconstants/orderproperties#enum_order_type_filling):

| Execution mode | FOK / IOC | RETURN |
|---|---|---|
| Instant, Request | allowed regardless of the symbol's flags | always allowed |
| **Market** | only if the symbol's flags allow it | **disabled regardless of the symbol's flags** |
| Exchange | only if the symbol's flags allow it | always allowed |

The two ways this produces retcode `10030` (`INVALID_FILL`):

* On a **Market Execution** broker, `ORDER_FILLING_RETURN` is refused whatever
  `SYMBOL_FILLING_MODE` reports — so "the symbol accepts it" is not the whole test.
* A **pending order** should carry `ORDER_FILLING_RETURN` regardless of execution
  mode, because it is not meant to execute at the moment it is sent. `BOC` (book
  or cancel) exists only for limit and stop-limit orders.

Read both properties before choosing one by hand:

```mql5
ENUM_SYMBOL_TRADE_EXECUTION exe =
   (ENUM_SYMBOL_TRADE_EXECUTION)SymbolInfoInteger(_Symbol, SYMBOL_TRADE_EXEMODE);
long flags = SymbolInfoInteger(_Symbol, SYMBOL_FILLING_MODE);
bool fok_allowed = (flags & SYMBOL_FILLING_FOK) != 0;
bool ioc_allowed = (flags & SYMBOL_FILLING_IOC) != 0;
```

## Trading with a raw request (when CTrade is not enough)

```mql5
MqlTradeRequest req = {};
MqlTradeResult  res = {};
req.action   = TRADE_ACTION_DEAL;
req.symbol   = _Symbol;
req.volume   = lots;
req.type     = ORDER_TYPE_BUY;
req.price    = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
req.sl       = sl;
req.tp       = tp;
req.deviation= InpSlippagePoints;
req.magic    = InpMagic;
req.comment  = "openjarvis";
req.type_filling = ORDER_FILLING_IOC;      // must match SYMBOL_FILLING_MODE
if(!OrderSend(req, res))
   PrintFormat("OrderSend failed: %d (%s)", res.retcode, res.comment);
```

## Margin / profit math before sending

```mql5
// OrderCalcMargin(action, symbol, volume, price_open, margin)
// Documented to compute as if the account held no pending orders and no open
// positions, so this is the margin of *this* order alone — on a netting account
// it is not the new total after the order.
double margin = 0.0;
bool   fits   = false;
if(OrderCalcMargin(ORDER_TYPE_BUY, _Symbol, lots, price, margin))
   fits = (margin <= AccountInfoDouble(ACCOUNT_MARGIN_FREE));

// OrderCalcProfit(action, symbol, volume, price_open, price_close, profit)
// The fifth argument is a CLOSE PRICE, not a stop loss. Passing `sl` there is
// legal and useful — it prices the exit at the stop — but it is not "the
// profit of the trade", it is the loss if the stop is hit. Pass the price you
// actually intend to exit at when that is the question.
double profit = 0.0;
OrderCalcProfit(ORDER_TYPE_SELL, _Symbol, lots, price, sl, profit);   // loss at the stop

// both return false on failure (and an invalid order type) — check them:
if(!OrderCalcProfit(ORDER_TYPE_BUY, _Symbol, lots, price, price + 50 * _Point, profit))
   PrintFormat("OrderCalcProfit failed: %d", GetLastError());
```

Signatures as the reference gives them
(mql5.com/en/docs/trading/ordercalcmargin, .../ordercalcprofit): five arguments
for margin, six for profit, the last one in each case the variable written to.
Both are `[out]`-style, so the returned `bool` is the only success signal — an
unchecked `false` leaves the previous value of the variable sitting there, which
reads as a real number.

## Time and bars

```mql5
datetime now      = TimeCurrent();               // last known server time
datetime bar_time = iTime(_Symbol, _Period, 0);  // open time of current bar
int      bars     = Bars(_Symbol, _Period);

MqlDateTime dt;
TimeToStruct(TimeCurrent(), dt);
bool is_london = (dt.hour >= 8 && dt.hour < 17 && dt.day_of_week >= 1 && dt.day_of_week <= 5);
bool is_friday = (dt.day_of_week == 5);

datetime series[];
CopyTime(_Symbol, _Period, 0, 5, series);        // 5 bars; index 0 is the OLDEST
ArraySetAsSeries(series, true);                  // now series[0] is the newest bar
```

New-bar guard (do not trade every tick in a bar strategy):

```mql5
datetime last_bar = 0;                            // global
bool IsNewBar()
  {
   datetime t = iTime(_Symbol, _Period, 0);
   if(t == last_bar) return(false);
   last_bar = t;
   return(true);
  }
```

## Logging and state

```mql5
Print("plain message");
PrintFormat("retcode=%d volume=%.2f", code, vol);
Comment("on-chart text");                         // expensive on every tick; use sparingly

int fh = FileOpen("state.csv", FILE_READ|FILE_WRITE|FILE_CSV|FILE_ANSI, ',');
if(fh != INVALID_HANDLE) { /* FileWrite / FileRead... */ FileClose(fh); }
```

Files land in `MQL5\Files` (or `<data folder>\MQL5\Files`) — never outside the
sandbox unless you pass `FILE_COMMON`.

## Event-driven confirmation

```mql5
void OnTradeTransaction(const MqlTradeTransaction &trans,
                        const MqlTradeRequest &request,
                        const MqlTradeResult &result)
  {
   switch(trans.type)
     {
      case TRADE_TRANSACTION_DEAL_ADD:      break;  // a deal appeared in history
      case TRADE_TRANSACTION_POSITION:      break;  // position changed server-side, NOT by a deal
      case TRADE_TRANSACTION_ORDER_UPDATE:  break;  // an open order changed: price, SL/TP, state
      case TRADE_TRANSACTION_REQUEST:       break;  // result of our own request
     }
  }
```

Prefer this over polling `PositionsTotal()` right after `OrderSend`: the fill
arrives asynchronously. Watch `TRADE_TRANSACTION_DEAL_ADD` for opens and closes:
a position changed *by a deal* does not raise `TRADE_TRANSACTION_POSITION`, which
reports only server-side changes made without one (SL/TP, volume, open price). The
reference puts it the same way — "Position change (adding, changing or closing), as
a result of a deal execution, does not lead to the occurrence of
TRADE_TRANSACTION_POSITION"
(mql5.com/en/docs/constants/tradingconstants/enum_trade_transaction_type) — and
third-party write-ups of this event get it backwards often enough that the citation
is worth keeping beside the claim. For `TRADE_TRANSACTION_REQUEST` the same page
says only `type` is meaningful in the structure; the detail is in the handler's
`request` and `result` parameters.

## Trailing stop (the version that does not fight the broker)

```mql5
void TrailStops(const double trail_points)
  {
   const double point = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   const int    digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
   const double min_dist = (double)SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL) * point;

   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;
      if(PositionGetInteger(POSITION_MAGIC) != (long)InpMagic) continue;

      ENUM_POSITION_TYPE type = (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);
      double sl  = PositionGetDouble(POSITION_SL);
      double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
      double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);

      if(type == POSITION_TYPE_BUY)
        {
         double candidate = bid - trail_points * point;
         if(candidate > sl + point && bid - candidate >= min_dist)
            trade.PositionModify(ticket, NormalizeDouble(candidate, digits),
                                 PositionGetDouble(POSITION_TP));
        }
      else
        {
         double candidate = ask + trail_points * point;
         if((sl == 0.0 || candidate < sl - point) && candidate - ask >= min_dist)
            trade.PositionModify(ticket, NormalizeDouble(candidate, digits),
                                 PositionGetDouble(POSITION_TP));
        }
     }
  }
```
