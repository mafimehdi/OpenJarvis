# MQL4 → MQL5 porting checklist

The three structural changes that produce ~80% of the work:

1. **Orders → positions.** MQL5 separates *pending orders*, *open positions*
   and *historical deals*. The MQL4 order pool functions are gone.
2. **Trading API → request structs.** `OrderSend()` takes one
   `MqlTradeRequest` (or use `CTrade` from `<Trade\Trade.mqh>`).
3. **Predefined variables → symbol info calls.** `Ask`, `Bid`, `Point`,
   `Digits`, `Bars`, `Volume` do not exist as MQL4-style globals.

## Direct replacements

| MQL4 | MQL5 |
|---|---|
| `Ask` | `SymbolInfoDouble(_Symbol, SYMBOL_ASK)` |
| `Bid` | `SymbolInfoDouble(_Symbol, SYMBOL_BID)` |
| `Point` | `_Point` or `SymbolInfoDouble(_Symbol, SYMBOL_POINT)` |
| `Digits` | `_Digits` or `SymbolInfoInteger(_Symbol, SYMBOL_DIGITS)` |
| `Bars` | `Bars(_Symbol, _Period)` or `iBars(...)` |
| `Time[0]`, `Open[1]`, `Close[i]`, `High/Low/Volume` | `iTime`, `iOpen`, `iClose`, `iHigh`, `iLow`, `iVolume` — or `CopyTime`/`CopyClose` into an array (preferred in loops) |
| `AccountBalance()` | `AccountInfoDouble(ACCOUNT_BALANCE)` |
| `AccountEquity()` | `AccountInfoDouble(ACCOUNT_EQUITY)` |
| `AccountFreeMargin()` | `AccountInfoDouble(ACCOUNT_MARGIN_FREE)` |
| `AccountNumber()` | `AccountInfoInteger(ACCOUNT_LOGIN)` |
| `MarketInfo(sym, MODE_SPREAD)` | `SymbolInfoInteger(sym, SYMBOL_SPREAD)` |
| `MarketInfo(sym, MODE_MINLOT)` | `SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN)` |
| `MarketInfo(sym, MODE_LOTSTEP)` | `SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP)` |
| `MarketInfo(sym, MODE_STOPLEVEL)` | `SymbolInfoInteger(sym, SYMBOL_TRADE_STOPS_LEVEL)` |
| `iMA(NULL,0,p,0,MODE_EMA,PRICE_CLOSE)` → returns a **value** | `iMA(_Symbol, _Period, p, 0, MODE_EMA, PRICE_CLOSE)` → returns a **handle**; read values with `CopyBuffer` |
| `OrderSend(sym, OP_BUY, lots, price, slippage, sl, tp, comment, magic, expiry, color)` | `trade.Buy(lots, sym, price, sl, tp, comment)` or an `MqlTradeRequest` with `TRADE_ACTION_DEAL` |
| `OrderClose(ticket, lots, price, slippage)` | `trade.PositionClose(ticket)` / `PositionClosePartial(ticket, volume)` |
| `OrderModify(ticket, price, sl, tp, expiry)` | `trade.PositionModify(ticket, sl, tp)` (positions) / `trade.OrderModify(...)` (pending orders) |
| `OrderDelete(ticket)` | `trade.OrderDelete(ticket)` — pending orders only |
| `OrderSelect(i, SELECT_BY_POS)` + `OrderType()` | `PositionGetTicket(i)` + `PositionGetInteger(POSITION_TYPE)` |
| `OrdersTotal()` = open positions + pending orders | `PositionsTotal()` = open positions, `OrdersTotal()` = **pending orders only** |
| `OrderMagicNumber()`, `OrderLots()`, `OrderProfit()` | `PositionGetInteger(POSITION_MAGIC)`, `PositionGetDouble(POSITION_VOLUME)`, `PositionGetDouble(POSITION_PROFIT)` |
| `OP_BUY` / `OP_SELL` / `OP_BUYLIMIT` | `ORDER_TYPE_BUY` / `ORDER_TYPE_SELL` / `ORDER_TYPE_BUY_LIMIT` |
| `IsTradeAllowed()` | `TerminalInfoInteger(TERMINAL_TRADE_ALLOWED)` && `MQLInfoInteger(MQL_TRADE_ALLOWED)` |
| `IsConnected()` | `TerminalInfoInteger(TERMINAL_CONNECTED)` |
| `RefreshRates()` | Not needed — prices are read on demand via `SymbolInfoDouble` |
| `GetLastError()` | Unchanged (still useful around `CopyBuffer` and file ops) |
| `#property copyright` etc. | Unchanged; `#property strict` is MQL4-only and ignored in MQL5 |

## Things that break silently rather than at compile time

* **`OrdersTotal()` semantics.** An MQL4 loop over `OrdersTotal()` that closed
  open positions becomes a loop over *pending orders* in MQL5 — it compiles and
  does the wrong thing. Port it to `PositionsTotal()`.
* **Hedging vs netting accounts.** On a netting account a second opposite
  position reduces or closes the first instead of opening a new one. Check
  `AccountInfoInteger(ACCOUNT_MARGIN_MODE)`.
* **Filling mode.** MQL4 had no equivalent; MQL5 requires a mode the symbol
  accepts (`ORDER_FILLING_FOK`/`IOC`/`RETURN`). Use
  `trade.SetTypeFillingBySymbol(_Symbol)`.
* **Deviation is in points, not "slippage".** `trade.SetDeviationInPoints(n)`.
* **`iCustom` argument order** differs, and the called indicator must be
  compiled for MQL5 — an MQL4 `.mq4` indicator cannot be attached.
* **Series indexing.** In MQL5 a copied array is *not* series-ordered by
  default; call `ArraySetAsSeries(arr, true)` explicitly.
* **`OnStart()` vs `OnInit()`** return types: `int OnInit()` returns
  `INIT_SUCCEEDED` / `INIT_FAILED` / `INIT_PARAMETERS_INCORRECT`;
  `void OnTick()` returns nothing.
* **Money math.** `DoubleToStr` → `DoubleToString`, `StrToDouble` →
  `StringToDouble`, `StrToInteger` → `StringToInteger`.

## Porting procedure

1. Compile first, read the errors — the compiler finds the lexical MQL4-isms
   faster than reading does.
2. Replace the trading layer wholesale with `CTrade` rather than translating
   `OrderSend` calls one by one.
3. Convert every indicator call to the handle pattern: create in `OnInit`,
   `CopyBuffer` in `OnTick`, release in `OnDeinit`.
4. Replace the order loop with a position loop filtered on symbol + magic.
5. Recompile until clean, then run the ported EA in the Strategy Tester against
   the MQL4 original on the same period and compare trade-by-trade. Differences
   are usually filling mode, spread model, or a series-index off-by-one.
