# MQL5 compile errors and trade retcodes

Two lookup tables for the compile-fix loop: what the **compiler** says, and what
the **trade server** says at runtime. Message wording varies slightly between
MetaEditor builds — treat the pattern, not the exact text, as the key.

> Verify anything critical against the MQL5 reference on your own machine. The
> intended workflow is to index it once (`jarvis memory index ./mql5-reference/`)
> and let the agent check signatures with `knowledge_search` instead of trusting
> memory — including this file.

## Compiler errors

| Pattern | Usual cause | Fix |
|---|---|---|
| `'X' - undeclared identifier` | Typo, use before declaration, or an MQL4 predefined variable (`Ask`, `Bid`, `Point`, `Digits`) | Declare it, or replace with `SymbolInfoDouble(_Symbol, SYMBOL_ASK)` / `SYMBOL_BID` / `_Point` / `_Digits` |
| `';' - semicolon expected` | Missing `;`, or a macro/`input` line malformed | The reported position is where parsing broke — look at the *previous* line too |
| `'X' - function already defined` | Duplicate event handler or two definitions after a bad merge | Keep one definition; move shared logic into a helper |
| `wrong parameters count for function 'X'` | MQL4 call signature used in MQL5 (`iMA`, `OrderSend`, `iCustom`) | Use the MQL5 signature: `iMA(symbol, timeframe, period, shift, method, applied_price)` returns a **handle** |
| `cannot convert enum` | Passing e.g. `OP_BUY` (MQL4) where `ENUM_ORDER_TYPE` is expected | `ORDER_TYPE_BUY` / `ORDER_TYPE_SELL` |
| `declaration of 'X' hides global declaration` (warning) | Local variable shadows an input or global | Rename the local |
| `possible loss of data due to type conversion` (warning) | `double` → `int`, or `long` → `int` ticket | Cast explicitly: `(int)`, `(ulong)`, `(double)` |
| `'CTrade' - undeclared identifier` | Missing include | `#include <Trade\Trade.mqh>` (backslash, angle brackets) |
| `'X' - file not found` / `cannot open include file` | Wrong `/inc` directory, or `#include "..."` for a stdlib header | Compile with `/inc:<data folder>\MQL5`; use `#include <...>` for standard library, `"..."` for your own relative files |
| `expression not boolean` | Assignment inside `if`, or bitwise `&` instead of `&&` | `==` and `&&` |
| `'X' - not a class member` | Wrong CTrade/indicator method name | Check the class reference; `CTrade` has `PositionOpen`, `PositionClose`, `PositionModify`, `PositionClosePartial`, `OrderSend`, `Buy`, `Sell` |
| `array out of range` (runtime) | Indexing a copied buffer before it has enough bars | Compare the count `CopyBuffer` returns with the count you asked for (`-1` is an error; fewer than requested means the data is not there yet) and check `BarsCalculated(handle) < 0`; on the first ticks the buffer really is short |

### What a compile produces

`.mq5` → `.ex5` beside it, `.mq4` → `.ex4`. A `.mqh` header produces **no
artifact**: MetaEditor inlines it into whatever includes it, so editing a header
changes nothing until the program that includes it is recompiled, and a header
compiled on its own is a syntax check rather than a build.

Do not read the process exit code as the verdict. A published log shows
`metaeditor.exe` exiting `1` on a run whose own summary said
`Result: 0 error(s), 0 warning(s)` (mql5.com/en/forum/157533); the summary line
and the fresh artifact decide. The CLI can also report `0 errors, 0 warnings` and
write no `.ex5` at all — a silent failure seen on large modular projects and
fixed in build 5200 (mql5.com/en/forum/491543). The harness reports that case in
its `note`; treat the note as a failed build, not a clean one.

## Trade retcodes (`ENUM_TRADE_RETCODE`)

Printed by `trade.ResultRetcode()` after any CTrade call. Log
`ResultRetcodeDescription()` too — but branch on the numeric code. Three codes
mean *success*: `10008`, `10009` and `10010`. `mt5_order_send` returns a
`success` flag so nothing has to remember which, and the bridge's `RETCODES`
table holds the same labels as this one — a test pins the two against the MQL5
reference's own table, because a correct number beside a wrong label still reads
as a wrong answer to whoever trusts the label.

Full list: mql5.com/en/docs/constants/errorswarnings/enum_trade_return_codes
(there is no 10005 and no 10037).

| Code | Meaning | What to do |
|---|---|---|
| 10009 | `DONE` — request completed | Success path |
| 10010 | `DONE_PARTIAL` | Success, partial fill — re-check position volume |
| 10004 | `REQUOTE` | Re-read prices and retry a bounded number of times |
| 10006 | `REJECT` | Do not hammer; log and inspect SL/TP/volume validity |
| 10008 | `PLACED` — a *pending* order was accepted | **Success.** The order is on the server; resending because the code was not `10009` places a second one |
| 10007 | `CANCEL` — canceled by the trader | Withdrawn, not refused, and not a connection problem |
| 10012 | `TIMEOUT` — canceled by timeout | The ambiguous one: the server may have taken the request anyway. Re-read positions and pending orders before any retry |
| 10031 | `CONNECTION` | No connection with the trade server — *this* is the code that means "check the connection" |
| 10036 | `POSITION_CLOSED` | The position you addressed is already gone: the "vanished mid-loop" case below, reported by the server |
| 10014 | `INVALID_VOLUME` | Lot not on `SYMBOL_VOLUME_STEP` or outside MIN/MAX |
| 10015 | `INVALID_PRICE` | Price not normalized to `SYMBOL_DIGITS`, or stale |
| 10016 | `INVALID_STOPS` | SL/TP too close to market (< `SYMBOL_TRADE_STOPS_LEVEL`) or on the wrong side |
| 10017 | `TRADE_DISABLED` | AutoTrading off, or symbol not tradable — stop retrying |
| 10018 | `MARKET_CLOSED` | Session closed — retry on the next session, not the next tick |
| 10019 | `NO_MONEY` | Margin insufficient — reduce volume or check `OrderCalcMargin` |
| 10020 | `PRICE_CHANGED` | Requote-like; refresh prices and retry once |
| 10024 | `TOO_MANY_REQUESTS` | You are spamming the server — add a cooldown |
| 10025 | `NO_CHANGES` | Modify sent identical SL/TP — skip no-op modifications |
| 10026 | `SERVER_DISABLES_AT` | AutoTrading disabled server-side |
| 10027 | `CLIENT_DISABLES_AT` | AutoTrading disabled in the terminal |
| 10030 | `INVALID_FILL` | Wrong filling mode — use `SetTypeFillingBySymbol()` or read `SYMBOL_FILLING_MODE` |

## Runtime errors worth guarding

| Error | Guard |
|---|---|
| `ERR_TRADE_DISABLED` (4752, "Trading by Expert Advisors prohibited") | Check `TerminalInfoInteger(TERMINAL_TRADE_ALLOWED)` and `MQLInfoInteger(MQL_TRADE_ALLOWED)` before trading |
| `ERR_TRADE_POSITION_NOT_FOUND` (4753) | The position you selected is gone — re-select by ticket (`PositionSelectByTicket`) and handle the "already closed" path instead of assuming the modify worked |
| Positions vanishing mid-loop | Iterate `for(int i = PositionsTotal() - 1; i >= 0; i--)` and re-read `PositionGetTicket(i)` each pass |
| Wrong position touched | Always compare `PositionGetString(POSITION_SYMBOL)` **and** `PositionGetInteger(POSITION_MAGIC)` |
| Indicator handle invalid after symbol/timeframe change | Recreate handles in `OnInit`, never lazily inside `OnTick` |
| Tester vs live divergence | Avoid `TimeLocal()`, real tick assumptions, and news-calendar lookups; the tester cannot reproduce them |
