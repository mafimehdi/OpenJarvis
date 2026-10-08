# MQL5 compile errors and trade retcodes

Two lookup tables for the compile-fix loop: what the **compiler** says, and what
the **trade server** says at runtime. Message wording varies slightly between
MetaEditor builds — treat the pattern, not the exact text, as the key. Where the
MQL5 reference lists the diagnostic, the row ends in `ref. N`: the reference
tables (`errors/errorscompile`, `errors/warningscompile`) give each message a
number and a description but not MetaEditor's literal wording, so the number is
the stable key when the text differs. A row with no `ref.` is one whose message
the tables do not list.

> Verify anything critical against the MQL5 reference on your own machine. The
> intended workflow is to index it once (`jarvis memory index ./mql5-reference/`)
> and let the agent check signatures with `knowledge_search` instead of trusting
> memory — including this file.

## Compiler errors

| Pattern | Usual cause | Fix |
|---|---|---|
| `'X' - undeclared identifier` (ref. 256) | Typo, use before declaration, an MQL4 predefined variable (`Ask`, `Bid`, a bare `Point` or `Digits`; the *functions* `Point()` and `Digits()` exist in MQL5), or an MQL4 constant such as `OP_BUY` | Declare it, or replace with `SymbolInfoDouble(_Symbol, SYMBOL_ASK)` / `SYMBOL_BID` / `_Point` / `_Digits` / `ORDER_TYPE_BUY` |
| `';' - semicolon expected` (ref. 154) | Missing `;`, or a macro/`input` line malformed | The reported position is where parsing broke — look at the *previous* line too |
| `'X' - function already defined` (ref. 163-165) | Duplicate event handler or two definitions after a bad merge | Keep one definition; move shared logic into a helper |
| `wrong parameters count for function 'X'` (ref. 199) | MQL4 call signature used in MQL5 (`iMA`, `OrderSend`, `iCustom`) | Use the MQL5 signature: `iMA(symbol, timeframe, period, shift, method, applied_price)` returns a **handle** |
| `cannot convert enum` (ref. 262, *Cannot convert to enumeration*) | A value of the wrong type where an enumeration is expected — an integer, or a member of a different enum. (`OP_BUY` itself is *undeclared* in MQL5, which is the first row, not this one.) | Pass the named member (`ORDER_TYPE_BUY`, `PERIOD_H1`). If a cast is deliberate, remember the numbers are not what MQL4 taught: `PERIOD_H1` is 16385, not 60, so `(ENUM_TIMEFRAMES)60` is not an hour chart |
| `declaration of 'X' hides global declaration` (warning, ref. 62; 61 is the local-variable form, 64 hides a predefined variable) | Local variable shadows an input or global | Rename the local |
| `possible loss of data due to type conversion` (warning, ref. 43) | `double` → `int`, or `long` → `int` ticket | Cast explicitly: `(int)`, `(ulong)`, `(double)` |
| `'CTrade' - undeclared identifier` | Missing include | `#include <Trade\Trade.mqh>` (backslash, angle brackets) |
| `'X' - file not found` / `cannot open include file` (ref. 106, *Error accessing a file in #include (probably the file does not exist)*) | Wrong `/inc` directory, or `#include "..."` for a stdlib header | Compile with `/inc:<data folder>\MQL5`; use `#include <...>` for standard library, `"..."` for your own relative files |
| `check operator precedence for possible error; use parentheses to clarify precedence` (warning, ref. 80) | Operators of different precedence mixed without parentheses: `a & b == c` (`==` binds tighter than `&`), or `x && y || z` | Add the parentheses; use `&&` / `||` for conditions and `&` / `|` only for bit masks |
| `'X' - undeclared identifier` on a method call (the tables describe it as *Method of structure or class is not declared*, ref. 213, or *No such structure member*, ref. 130) | Wrong CTrade/indicator method name or member | Check the class reference; `CTrade` has `PositionOpen`, `PositionClose`, `PositionModify`, `PositionClosePartial`, `OrderSend`, `Buy`, `Sell` |
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
| 10016 | `INVALID_STOPS` | SL/TP too close to market (< `SYMBOL_TRADE_STOPS_LEVEL`, measured from **Bid for a buy, Ask for a sell** — an SL typed from the entry price is a spread short on both sides) or on the wrong side |
| 10017 | `TRADE_DISABLED` | AutoTrading off, or symbol not tradable — stop retrying |
| 10018 | `MARKET_CLOSED` | Session closed — retry on the next session, not the next tick |
| 10019 | `NO_MONEY` | Margin insufficient — reduce volume or check `OrderCalcMargin` |
| 10020 | `PRICE_CHANGED` | Requote-like; refresh prices and retry once |
| 10024 | `TOO_MANY_REQUESTS` | You are spamming the server — add a cooldown |
| 10025 | `NO_CHANGES` | Modify sent identical SL/TP — skip no-op modifications |
| 10026 | `SERVER_DISABLES_AT` | AutoTrading disabled server-side |
| 10027 | `CLIENT_DISABLES_AT` | AutoTrading disabled in the terminal |
| 10030 | `INVALID_FILL` | Wrong filling mode. `SYMBOL_FILLING_MODE` is a flag set: `FOK` = 1, `IOC` = 2 (`BOC` = 4 for limit orders). `RETURN` has no flag; it is refused under Market Execution and is what pending orders use. `CTrade::SetTypeFillingBySymbol()` picks from the flags (FOK first when both are set) |
| 10034 | `LIMIT_VOLUME` | The volume of orders and positions for the symbol has reached the limit (`SYMBOL_VOLUME_LIMIT`, counted per direction, positions plus pending orders). Reduce the volume or skip; the template's `VolumeRoomFor` checks it first |
| 10040 | `LIMIT_POSITIONS` | The server caps the number of open positions on the account. On a netting account only symbols that already have a position can take a new order; on a hedging account pending orders count too. Skip the entry; do not retry per tick |
| 10042 | `LONG_ONLY` | The symbol allows only long positions (`SYMBOL_TRADE_MODE_LONGONLY`). A sell entry is refused: check `SYMBOL_TRADE_MODE` first |
| 10043 | `SHORT_ONLY` | The symbol allows only short positions (`SYMBOL_TRADE_MODE_SHORTONLY`). A buy entry is refused |
| 10044 | `CLOSE_ONLY` | The symbol allows only closing positions (`SYMBOL_TRADE_MODE_CLOSEONLY`): no new entry in either direction |
| 10046 | `HEDGE_PROHIBITED` | The hedging account forbids opposite positions on one symbol: with a Buy open, a Sell or a pending sell is refused |

## Runtime errors worth guarding

| Error | Guard |
|---|---|
| `ERR_TRADE_DISABLED` (4752, "Trading by Expert Advisors prohibited") | Check `TerminalInfoInteger(TERMINAL_TRADE_ALLOWED)` and `MQLInfoInteger(MQL_TRADE_ALLOWED)` before trading |
| `ERR_TRADE_POSITION_NOT_FOUND` (4753, "Position not found") | The position you selected is gone — re-select by ticket (`PositionSelectByTicket`) and handle the "already closed" path instead of assuming the modify worked |
| `ERR_TRADE_SEND_FAILED` (4756, "Trade request sending failed") | The request did not go out: log `GetLastError()` and the result's retcode, and re-read positions and pending orders before retrying rather than assuming nothing was placed |
| `ERR_FUNCTION_NOT_ALLOWED` (4014, "Function is not allowed for call") | The economic-calendar functions (`CalendarValueHistory`, `CalendarValueLast`, ...) cannot be used in the tester and fail with this error. An EA that reads the failure as "no news" backtests with no news filter at all; one that reads it as "news now" never trades. Record the calendar to a file from an online chart and read the file in the tester (see the cheatsheet) |
| `ERR_MARKET_NOT_SELECTED` (4302, "Symbol is not selected in MarketWatch") | `SymbolSelect(symbol, true)` before reading quotes or placing orders on a symbol other than the chart's |
| `ERR_INDICATOR_DATA_NOT_FOUND` (4806, "Requested data not found") | Check `BarsCalculated(handle)` before `CopyBuffer`; the data may simply not exist yet, so skip the tick instead of treating it as fatal |
| Positions vanishing mid-loop | Iterate `for(int i = PositionsTotal() - 1; i >= 0; i--)` and re-read `PositionGetTicket(i)` each pass |
| Wrong position touched | Always compare `PositionGetString(POSITION_SYMBOL)` **and** `PositionGetInteger(POSITION_MAGIC)` |
| Indicator handle invalid after symbol/timeframe change | Recreate handles in `OnInit`, never lazily inside `OnTick` |
| Tester vs live divergence | In the tester `TimeLocal()` and `TimeGMT()` are both just the simulated server time, so a GMT session filter silently becomes a server-time one (see "Which clock" in the cheatsheet). Fills, spread, chart objects and global variables also differ ("What else the Strategy Tester changes"). Do not call the economic-calendar functions in a backtest: they fail there with 4014 (next table) |
