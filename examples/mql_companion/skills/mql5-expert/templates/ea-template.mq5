//+------------------------------------------------------------------+
//|  ea-template.mq5 — OpenJarvis MQL5 Expert Advisor skeleton        |
//|                                                                  |
//|  A deliberately boring, correct EA: EMA cross on the closed bar, |
//|  fixed-fractional risk sizing, spread filter, magic-number       |
//|  filtering, margin and stops-level checks, CTrade order flow.    |
//|  Replace the signal in OnTick(); keep everything else.           |
//+------------------------------------------------------------------+
#property copyright   "OpenJarvis contributors"
#property link        "https://open-jarvis.github.io/OpenJarvis/"
#property version     "1.00"
#property description "Template EA: bar-based EMA cross with fixed-fractional risk."

#include <Trade\Trade.mqh>

//--- inputs ---------------------------------------------------------
input group "=== Strategy ==="
input int             InpFastPeriod      = 12;            // Fast EMA period
input int             InpSlowPeriod      = 48;            // Slow EMA period
input ENUM_TIMEFRAMES InpTimeframe       = PERIOD_CURRENT;// Signal timeframe

input group "=== Risk ==="
input double          InpRiskPercent     = 1.0;           // Risk per trade, % of equity
input int             InpStopLossPoints  = 300;           // Stop loss in points (0 disables)
input int             InpTakeProfitPoints= 600;           // Take profit in points (0 disables)
input int             InpMaxSpreadPoints = 30;            // Skip entries above this spread
input int             InpSlippagePoints  = 10;            // Deviation in points
input ulong           InpMagic           = 20260929;      // Magic number

//--- globals --------------------------------------------------------
CTrade   g_trade;
int      g_fast_handle = INVALID_HANDLE;
int      g_slow_handle = INVALID_HANDLE;
datetime g_last_bar    = 0;
int      g_volume_digits = 2;

//+------------------------------------------------------------------+
//| Helpers                                                          |
//+------------------------------------------------------------------+
//--- decimals needed to write the lot step: 0.01 -> 2, 0.25 -> 2, 2.5 -> 1, 10 -> 0.
//--- Stop when the scaled step is a whole number, not when it reaches 1: that
//--- would give 0.25 -> 1 digit and round a 0.75 lot to 0.8, off the step grid.
int VolumeDigits(const double step)
  {
   if(step <= 0.0)
      return(2);
   int digits = 0;
   double s = step;
   while(MathAbs(s - MathRound(s)) > 1e-8 && digits < 8)
     {
      s *= 10.0;
      digits++;
     }
   return(digits);
  }

double MinStopDistance()
  {
   const long  stops = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL);
   const double point = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   return(stops > 0 ? (double)stops * point : 0.0);
  }

//--- Rounds DOWN to the lot step. Returns 0.0 when the result is below the
//--- minimum lot: bumping it up to SYMBOL_VOLUME_MIN would silently risk more
//--- than InpRiskPercent (a $100 account at 1% risk, a 300-point stop and about
//--- $1 per point per lot wants 0.003 lots; the minimum 0.01 risks 3%).
double NormalizeVolume(double volume)
  {
   const double min_lot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   const double max_lot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double       lot_step = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   if(lot_step <= 0.0)
      lot_step = 0.01;
   //--- the epsilon matters: 0.3 / 0.1 is 2.9999999999999996 in a double, and a
   //--- bare MathFloor would silently size one step down (0.2 instead of 0.3)
   double lots = MathFloor(volume / lot_step + 1e-8) * lot_step;
   lots = NormalizeDouble(lots, g_volume_digits);
   if(lots < min_lot)
      return(0.0);
   if(max_lot > 0.0 && lots > max_lot)
      lots = max_lot;
   return(lots);
  }

//--- SYMBOL_VOLUME_LIMIT caps the open volume PLUS the pending orders in ONE
//--- direction, per symbol - whichever EA or magic number holds them (MQL5
//--- Reference, "Symbol Properties"). Returns the volume still allowed in the
//--- direction of `type`; DBL_MAX when the server sets no limit (value 0).
double VolumeRoomFor(const ENUM_ORDER_TYPE type)
  {
   const double limit = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_LIMIT);
   if(limit <= 0.0)
      return(DBL_MAX);
   const bool want_buy = (type == ORDER_TYPE_BUY);
   double used = 0.0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      if(PositionGetTicket(i) == 0)
         continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol)
         continue;
      const bool is_buy = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY);
      if(is_buy == want_buy)
         used += PositionGetDouble(POSITION_VOLUME);
     }
   for(int i = OrdersTotal() - 1; i >= 0; i--)
     {
      if(OrderGetTicket(i) == 0)
         continue;
      if(OrderGetString(ORDER_SYMBOL) != _Symbol)
         continue;
      const long kind = OrderGetInteger(ORDER_TYPE);
      const bool pending_buy  = (kind == ORDER_TYPE_BUY_LIMIT ||
                                 kind == ORDER_TYPE_BUY_STOP ||
                                 kind == ORDER_TYPE_BUY_STOP_LIMIT);
      const bool pending_sell = (kind == ORDER_TYPE_SELL_LIMIT ||
                                 kind == ORDER_TYPE_SELL_STOP ||
                                 kind == ORDER_TYPE_SELL_STOP_LIMIT);
      if(want_buy ? pending_buy : pending_sell)
         used += OrderGetDouble(ORDER_VOLUME_CURRENT);
     }
   return(MathMax(0.0, limit - used));
  }

//--- fixed-fractional sizing: risk InpRiskPercent of equity over sl_points
double RiskVolume(const double sl_points)
  {
   const double min_lot    = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   const double equity     = AccountInfoDouble(ACCOUNT_EQUITY);
   //--- SYMBOL_TRADE_TICK_VALUE is the value for a *profitable* tick; a stop-loss
   //--- is a losing one, which has its own property. Some servers leave it 0.
   double tick_value = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE_LOSS);
   if(tick_value <= 0.0)
      tick_value = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
   const double tick_size  = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   const double point      = SymbolInfoDouble(_Symbol, SYMBOL_POINT);

   if(sl_points <= 0.0 || tick_value <= 0.0 || tick_size <= 0.0 || point <= 0.0)
      return(min_lot);

   const double risk_money   = equity * InpRiskPercent / 100.0;
   const double loss_per_lot = sl_points * point / tick_size * tick_value;
   if(risk_money <= 0.0 || loss_per_lot <= 0.0)
      return(min_lot);

   return(NormalizeVolume(risk_money / loss_per_lot));
  }

//--- A bar gets ONE decision, but a bar only counts as decided once the
//--- transient filters have let the signal be evaluated. Marking it on sight
//--- (the usual IsNewBar() that flips the flag) would let a wide spread on the
//--- first tick - the norm around rollover - discard the bar's signal for good.
bool HasUnhandledBar()
  {
   const datetime bar_time = iTime(_Symbol, InpTimeframe, 0);
   return(bar_time != 0 && bar_time != g_last_bar);
  }

void MarkBarHandled()
  {
   g_last_bar = iTime(_Symbol, InpTimeframe, 0);
  }

bool SpreadIsAcceptable()
  {
   if(InpMaxSpreadPoints <= 0)
      return(true);
   const long spread = SymbolInfoInteger(_Symbol, SYMBOL_SPREAD);
   return(spread <= InpMaxSpreadPoints);
  }

bool TradingIsAllowed()
  {
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED))
      return(false);
   if(!MQLInfoInteger(MQL_TRADE_ALLOWED))
      return(false);
   if(!AccountInfoInteger(ACCOUNT_TRADE_ALLOWED))
      return(false);
   return((bool)AccountInfoInteger(ACCOUNT_TRADE_EXPERT));
  }

//--- SYMBOL_TRADE_MODE can refuse a new position outright (DISABLED, CLOSEONLY)
//--- or in one direction only (LONGONLY, SHORTONLY); TERMINAL_/ACCOUNT_ flags do
//--- not cover it. Anything but FULL or the matching one-way mode is a refusal.
bool SymbolAllowsEntry(const ENUM_ORDER_TYPE type)
  {
   const long mode = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_MODE);
   if(mode == SYMBOL_TRADE_MODE_FULL)
      return(true);
   if(mode == SYMBOL_TRADE_MODE_LONGONLY)
      return(type == ORDER_TYPE_BUY);
   if(mode == SYMBOL_TRADE_MODE_SHORTONLY)
      return(type == ORDER_TYPE_SELL);
   return(false);
  }

bool HasOwnPosition()
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      const ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol)
         continue;
      if(PositionGetInteger(POSITION_MAGIC) != (long)InpMagic)
         continue;
      return(true);
     }
   return(false);
  }

//--- SL/TP distances are measured from the price the position is CLOSED at:
//--- Bid for a buy, Ask for a sell (mql5.com/en/articles/2555) - the opposite
//--- side of the quote from the entry. An SL typed `n` points from the entry is
//--- therefore only `n - spread` from that price, for a buy and a sell alike.
//--- 0 = not set.
bool StopsAreValid(const ENUM_ORDER_TYPE type, const double sl, const double tp)
  {
   const double bid      = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   const double ask      = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   const double min_dist = MinStopDistance();
   const double slack    = SymbolInfoDouble(_Symbol, SYMBOL_POINT) / 2.0;

   if(type == ORDER_TYPE_BUY)
     {
      if(sl > 0.0 && bid - sl < min_dist - slack)
         return(false);
      if(tp > 0.0 && tp - bid < min_dist - slack)
         return(false);
     }
   else
     {
      if(sl > 0.0 && sl - ask < min_dist - slack)
         return(false);
      if(tp > 0.0 && ask - tp < min_dist - slack)
         return(false);
     }
   return(true);
  }

//--- CTrade returns true when the request STRUCTURE checked out, not when the
//--- server accepted it (mql5.com/en/docs/standardlibrary/tradeclasses/ctrade/
//--- ctradepositionopen) - the server's answer is ResultRetcode().
bool RetcodeIsSuccess(const uint retcode)
  {
   return(retcode == (uint)TRADE_RETCODE_PLACED ||
          retcode == (uint)TRADE_RETCODE_DONE ||
          retcode == (uint)TRADE_RETCODE_DONE_PARTIAL);
  }

bool MarginIsSufficient(const ENUM_ORDER_TYPE type, const double lots, const double price)
  {
   double margin = 0.0;
   if(!OrderCalcMargin(type, _Symbol, lots, price, margin))
      return(false);
   return(margin <= AccountInfoDouble(ACCOUNT_MARGIN_FREE));
  }

void OpenPosition(const ENUM_ORDER_TYPE type)
  {
   if(!SymbolAllowsEntry(type))
     {
      Print("SYMBOL_TRADE_MODE does not allow this entry - skipping");
      return;
     }
   const double point = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   const int    digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
   const double sl_points = (double)InpStopLossPoints;

   double price = (type == ORDER_TYPE_BUY)
                  ? SymbolInfoDouble(_Symbol, SYMBOL_ASK)
                  : SymbolInfoDouble(_Symbol, SYMBOL_BID);

   double lots = RiskVolume(sl_points);
   const double room = VolumeRoomFor(type);
   if(room < SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN))
     {
      Print("SYMBOL_VOLUME_LIMIT reached in this direction - skipping entry");
      return;
     }
   if(lots > room)
      lots = NormalizeVolume(room);
   if(lots <= 0.0)
     {
      Print("Volume below the minimum lot at this risk - skipping entry");
      return;
     }
   double sl = 0.0;
   double tp = 0.0;
   if(type == ORDER_TYPE_BUY)
     {
      sl = (InpStopLossPoints   > 0) ? price - sl_points * point : 0.0;
      tp = (InpTakeProfitPoints > 0) ? price + (double)InpTakeProfitPoints * point : 0.0;
     }
   else
     {
      sl = (InpStopLossPoints   > 0) ? price + sl_points * point : 0.0;
      tp = (InpTakeProfitPoints > 0) ? price - (double)InpTakeProfitPoints * point : 0.0;
     }

   if(!StopsAreValid(type, sl, tp))
     {
      PrintFormat("SL/TP too close for stops level %d points (spread %d) - skipping entry",
                  (int)SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL),
                  (int)SymbolInfoInteger(_Symbol, SYMBOL_SPREAD));
      return;
     }

   if(!MarginIsSufficient(type, lots, price))
     {
      PrintFormat("Insufficient margin for %.2f lots - skipping entry", lots);
      return;
     }

   const bool sent = g_trade.PositionOpen(_Symbol, type, lots,
                                          NormalizeDouble(price, digits),
                                          NormalizeDouble(sl, digits),
                                          NormalizeDouble(tp, digits),
                                          "openjarvis-template");
   const uint retcode = g_trade.ResultRetcode();
   if(!sent || !RetcodeIsSuccess(retcode))
      PrintFormat("PositionOpen not accepted: retcode=%u %s",
                  retcode, g_trade.ResultRetcodeDescription());
  }

//+------------------------------------------------------------------+
//| Expert initialization                                            |
//+------------------------------------------------------------------+
int OnInit()
  {
   if(InpFastPeriod < 1 || InpSlowPeriod < 1 || InpFastPeriod >= InpSlowPeriod)
     {
      Print("Invalid EMA periods: fast must be >= 1 and < slow");
      return(INIT_PARAMETERS_INCORRECT);
     }
   if(InpRiskPercent <= 0.0 || InpRiskPercent > 10.0)
     {
      Print("Risk percent must be in (0, 10]");
      return(INIT_PARAMETERS_INCORRECT);
     }

   g_volume_digits = VolumeDigits(SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP));

   g_trade.SetExpertMagicNumber(InpMagic);
   g_trade.SetDeviationInPoints(InpSlippagePoints);
   g_trade.SetTypeFillingBySymbol(_Symbol);
   g_trade.LogLevel(LOG_LEVEL_ERRORS);

   g_fast_handle = iMA(_Symbol, InpTimeframe, InpFastPeriod, 0, MODE_EMA, PRICE_CLOSE);
   g_slow_handle = iMA(_Symbol, InpTimeframe, InpSlowPeriod, 0, MODE_EMA, PRICE_CLOSE);
   if(g_fast_handle == INVALID_HANDLE || g_slow_handle == INVALID_HANDLE)
     {
      PrintFormat("Failed to create indicator handles (error %d)", GetLastError());
      return(INIT_FAILED);
     }

   PrintFormat("Initialized on %s %s | fast=%d slow=%d risk=%.2f%%",
               _Symbol, EnumToString(InpTimeframe),
               InpFastPeriod, InpSlowPeriod, InpRiskPercent);
   return(INIT_SUCCEEDED);
  }

//+------------------------------------------------------------------+
//| Expert deinitialization                                          |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   if(g_fast_handle != INVALID_HANDLE)
      IndicatorRelease(g_fast_handle);
   if(g_slow_handle != INVALID_HANDLE)
      IndicatorRelease(g_slow_handle);
   g_fast_handle = INVALID_HANDLE;
   g_slow_handle = INVALID_HANDLE;
  }

//+------------------------------------------------------------------+
//| Expert tick function                                             |
//+------------------------------------------------------------------+
void OnTick()
  {
   //--- filters first: cheap checks before any indicator work. A refusal that
   //--- can clear within the bar (trading off, spread, data not ready) returns
   //--- WITHOUT marking the bar, so the next tick retries; an own position is
   //--- final for this bar, so it marks it - closing mid-bar must not re-enter
   //--- on the same cross.
   if(!HasUnhandledBar())     return;
   if(HasOwnPosition())       { MarkBarHandled(); return; }
   if(!TradingIsAllowed())    return;
   if(!SpreadIsAcceptable())  return;
   if(BarsCalculated(g_fast_handle) < InpSlowPeriod + 2) return;
   if(BarsCalculated(g_slow_handle) < InpSlowPeriod + 2) return;

   //--- read the two closed bars (shifts 1 and 2)
   double fast[];
   double slow[];
   if(CopyBuffer(g_fast_handle, 0, 1, 2, fast) != 2) return;
   if(CopyBuffer(g_slow_handle, 0, 1, 2, slow) != 2) return;
   MarkBarHandled();               // every input is in hand: decide once, now
   ArraySetAsSeries(fast, true);   // [0] = shift 1 (last closed bar)
   ArraySetAsSeries(slow, true);   // [1] = shift 2 (bar before it)

   //--- signal: cross confirmed on the closed bar
   const bool crossed_up   = (fast[1] <= slow[1] && fast[0] >  slow[0]);
   const bool crossed_down = (fast[1] >= slow[1] && fast[0] <  slow[0]);

   if(crossed_up)
      OpenPosition(ORDER_TYPE_BUY);
   else if(crossed_down)
      OpenPosition(ORDER_TYPE_SELL);
  }

//+------------------------------------------------------------------+
//| Trade transaction handler — confirm fills instead of assuming    |
//+------------------------------------------------------------------+
void OnTradeTransaction(const MqlTradeTransaction &trans,
                        const MqlTradeRequest &request,
                        const MqlTradeResult &result)
  {
   if(trans.type == TRADE_TRANSACTION_DEAL_ADD)
     {
      if(HistoryDealSelect(trans.deal))
        {
         const long deal_magic  = HistoryDealGetInteger(trans.deal, DEAL_MAGIC);
         const long deal_entry  = HistoryDealGetInteger(trans.deal, DEAL_ENTRY);
         const double deal_vol  = HistoryDealGetDouble(trans.deal, DEAL_VOLUME);
         if(deal_magic == (long)InpMagic)
            PrintFormat("Deal %I64u magic=%I64d entry=%s volume=%.2f",
                        trans.deal, deal_magic,
                        EnumToString((ENUM_DEAL_ENTRY)deal_entry), deal_vol);
        }
     }
  }
//+------------------------------------------------------------------+
