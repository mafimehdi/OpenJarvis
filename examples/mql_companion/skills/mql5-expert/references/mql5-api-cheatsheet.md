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

if(!trade.PositionModify(ticket, sl, tp))
   PrintFormat("modify failed: %d %s",
               trade.ResultRetcode(), trade.ResultRetcodeDescription());
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
double margin = 0.0;
if(OrderCalcMargin(ORDER_TYPE_BUY, _Symbol, lots, price, margin))
   bool ok = (margin <= AccountInfoDouble(ACCOUNT_MARGIN_FREE));

double profit = 0.0;
OrderCalcProfit(ORDER_TYPE_SELL, _Symbol, lots, price, sl, profit);
```

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
reports only server-side changes made without one (SL/TP, volume).

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
