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

`SYMBOL_TRADE_MODE` limits what a symbol accepts: `SYMBOL_TRADE_MODE_DISABLED`
(no trading), `_LONGONLY`, `_SHORTONLY`, `_CLOSEONLY` (closing only) or `_FULL`
(MQL5 Reference, "Symbol Properties"). The terminal and account trade flags do
not cover it, so check it before opening a position and treat everything but
`_FULL` or the matching one-way mode as a refusal.

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

When the rounded size is below `SYMBOL_VOLUME_MIN`, skip the trade instead of
raising it to the minimum: a risk-percent EA that does so risks more than its
input says (the template's `NormalizeVolume` returns 0.0 and `OpenPosition`
skips).

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
datetime now      = TimeCurrent();               // server time of the tick (not the PC clock)
datetime bar_time = iTime(_Symbol, _Period, 0);  // open time of current bar
int      bars     = Bars(_Symbol, _Period);

MqlDateTime dt;
TimeToStruct(TimeCurrent(), dt);
bool in_session = (dt.hour >= 8 && dt.hour < 17 && dt.day_of_week >= 1 && dt.day_of_week <= 5);  // BROKER hours
bool is_friday = (dt.day_of_week == 5);

datetime series[];
CopyTime(_Symbol, _Period, 0, 5, series);        // 5 bars; index 0 is the OLDEST
ArraySetAsSeries(series, true);                  // now series[0] is the newest bar
```

### Which clock a time function reads

`TimeToStruct(TimeCurrent(), dt)` gives the **broker's** clock, so `in_session`
above is a window in server hours, not London or New York: how far a broker's
clock sits from any city's depends on the broker, and it may shift with
daylight saving. Make the window an `input` in server hours rather than a
constant that looks like a city.

| Function | Live | In the Strategy Tester |
|---|---|---|
| `TimeCurrent()` | Server time of the tick being handled in `OnTick`; independent of the PC's clock | Simulated from the history |
| `TimeTradeServer()` | Estimated server time, calculated in the terminal from the PC's time settings | Always equal to `TimeCurrent()` |
| `TimeLocal()` | The PC's clock | Always equal to `TimeCurrent()` |
| `TimeGMT()` | GMT calculated from the PC's local time, with the DST switch | Always equal to `TimeTradeServer()` — the simulated server clock, with no GMT offset removed |

(mql5.com/en/docs/dateandtime/timecurrent, .../timetradeserver, .../timelocal,
.../timegmt.) In the tester all four are the same server clock, and MT5 Help
says the equality is deliberate, so results do not depend on whether a server
connection exists (metatrader5.com/en/terminal/help/algotrading/testing_features).
Help's wording is that the server time "always corresponds to the GMT time"; the
reference defines `TimeGMT()` as `TimeTradeServer()` there, so nothing converts a
broker's offset away. So a
"13:00-17:00 GMT" filter written with `TimeGMT()` or `TimeLocal()` works live and
is shifted by the broker's offset in a backtest, and the two results cannot be
compared; `TimeCurrent()` is the only one that means the same thing in both.

### News filters do not run in the tester

The economic-calendar functions (`CalendarValueHistory`, `CalendarValueLast`,
`CalendarEventById`, ...) cannot be used in the tester: any call fails with
`ERR_FUNCTION_NOT_ALLOWED` (4014). The MQL5 book's advice is to save the calendar
records to files while the program runs on a live chart, then load and read them
in the tester (mql5.com/en/book/advanced/calendar). Two consequences for an EA:

* Record to `FILE_COMMON`. A file the live program writes to its own
  `MQL5\Files` is not in the tester agent's `MQL5\Files`, so the tester would
  find no calendar at all (see "What else the Strategy Tester changes").
* Decide what a failed call means *before* backtesting. Treating it as "no
  news" turns the news filter off for the whole run; treating it as "news now"
  blocks every trade; neither is the strategy you meant to test.
* Calendar times are trade-server time (`TimeTradeServer()`, with its time zone
  and DST), so a file of historic events has to be shifted for the stretches of
  the year where the DST state differs from the one that recorded it.

### What else the Strategy Tester changes

From MT5 Help, "Testing Features"
(metatrader5.com/en/terminal/help/algotrading/testing_features) and "Real and
Generated Ticks" (.../tick_generation):

| Live | In the tester |
|---|---|
| A stop, take-profit or pending order fills at the market price when it triggers, so slippage is possible | In the **"Open prices only"** and **"1 minute OHLC"** modes they fill at the price written in the order, with no slippage; only the accurate modes (every tick, real ticks) use the current Bid and Ask. A stop that holds in the cheap modes can slip in the accurate ones. "Real and Generated Ticks" adds that in "Open prices only" stops and pending orders "may trigger at a price different from the specified one", especially on higher timeframes |
| The spread moves with the market | Not modelled: it is read from the history, the last known spread is used when the history value is zero or less, and it is always floating. Generated ticks use the spread fixed in each minute bar; real ticks let it change within the minute |
| Graphical objects exist | Not plotted in a non-visual test or an optimization, so reading an object's properties returns zero (visual mode is exempt) |
| `GlobalVariable*` shares the terminal's list (F3) | Emulated: separate from the terminal's variables, and each testing agent has its own copy |
| `OnTick` runs on every tick | **"Open prices only"**: once per bar, at its open (W1 and MN1 bars are generated once a day). **"1 minute OHLC"**: four times a minute (open, high, low, close) even when the test runs on H1; the prices come from the history |
| Any timeframe can be read | **"Open prices only"**: nothing below the test timeframe, and a higher one must be a multiple of it (test on M20: H1 yes, M30 no); the same limit applies per symbol, set by the first timeframe it accesses. The random-delay mode cannot be used |
| A file written with `FileOpen` lands in the terminal's `MQL5\Files` | Lands in the testing agent's own `MQL5\Files`; only `FILE_COMMON` reaches the shared folder |
| `Print()` and trade messages reach the journal | Not recorded on a **remote** agent, which keeps a minimum of log lines; DLL calls are forbidden there (on a local agent they need "Allow import DLL") |
| `Alert()`, `SendNotification()`, `SendMail()`, `PlaySound()`, `MessageBox()`, `SendFTP()` and `WebRequest()` reach the outside world | Not executed at all (MQL5 Reference, "Testing Trading Strategies"). A test that has to prove an alert fires cannot do it; log the condition with `Print()` instead |
| Market Watch holds what you selected | Only the tested symbol at the start. Another symbol is connected on first access and the test pauses while its history syncs; each symbol gets its own tick sequence, so a new bar on one says nothing about another |

So an input such as a signal timeframe can make an "Open prices only" run fail on
data the same EA reads fine live, and a result from that mode says little about
stop slippage. An EA that keeps state in global variables or files, or reads
chart objects, also behaves differently under test.

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

## Alerts and notifications (what replaces a TradingView alert)

A Pine `alert()` or `alertcondition()` needs TradingView's servers to watch the
chart. An MQL5 program watches the chart inside the terminal, so the same
signal can raise an alert without any TradingView plan. From the MQL5
Reference:

| Function | What it does | Limits |
|---|---|---|
| `Alert(...)` | Opens a message window in the terminal | Up to 64 arguments; arrays must be printed element by element |
| `SendNotification(text)` | Push message to the phone: the MetaQuotes ID goes in the terminal's "Notifications" tab | 255 characters; at most 2 calls a second and 10 a minute, and the function can be disabled for breaking that. Errors: 4515 `ERR_NOTIFICATION_SEND_FAILED`, 4516 `ERR_NOTIFICATION_WRONG_PARAMETER`, 4517 `ERR_NOTIFICATION_WRONG_SETTINGS`, 4518 `ERR_NOTIFICATION_TOO_FREQUENT` |
| `SendMail(subject, text)` | Email to the address in the "Email" tab | Returns true once the mail is queued; sending can be prohibited in settings or the address left empty |

```mql5
// Alert once per closed bar, the way a Pine alert set to "Once Per Bar Close"
// behaves: look at the bar that just closed (shift 1), not the forming bar.
void RaiseSignal(const string text)
  {
   Alert(text);
   if(TerminalInfoInteger(TERMINAL_NOTIFICATIONS_ENABLED))
      if(!SendNotification(text))
         PrintFormat("SendNotification failed, error %d", GetLastError());
  }

void OnTick()
  {
   if(!IsNewBar()) return;                        // see the new-bar guard above
   // ...read the indicator buffers at shift 1 here...
   // if(fast_prev <= slow_prev && fast_now > slow_now) RaiseSignal("BUY cross");
  }
```

`TERMINAL_NOTIFICATIONS_ENABLED` (and `TERMINAL_EMAIL_ENABLED` for mail) are what
the Reference's own examples test before sending. The terminal must be running
for any of this to fire, so a laptop that sleeps sends nothing; the Reference
points to a MetaTrader VPS for that. The same program in the tester sends
nothing (see the table above).

What this does not replace: a TradingView alert can post to a webhook URL, and
that part needs a paid TradingView plan. Sending the signal from MT5 to another
service means `WebRequest()`, which needs the URL on the terminal's allowed
list; it is not executed in the tester either.

## Logging and state

```mql5
Print("plain message");
PrintFormat("retcode=%d volume=%.2f", code, vol);
Comment("on-chart text");                         // expensive on every tick; use sparingly

int fh = FileOpen("state.csv", FILE_READ|FILE_WRITE|FILE_CSV|FILE_ANSI, ',');
if(fh != INVALID_HANDLE) { /* FileWrite / FileRead... */ FileClose(fh); }
```

On a chart, files land in `MQL5\Files` (or `<data folder>\MQL5\Files`) — never
outside the sandbox unless you pass `FILE_COMMON`. **In the tester that is a
different folder:** every file operation of a test happens in the testing
agent's own `<agent folder>\MQL5\Files`, isolated from the platform and from
other agents (MT5 Help, "Testing Features"). A state file written on a chart
is therefore invisible to the same EA under test, and the other way round,
unless both sides use `FILE_COMMON`, the shared folder of the platforms.

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
