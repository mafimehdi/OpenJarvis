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
int VolumeDigits(const double step)
  {
   if(step <= 0.0)
      return(2);
   int digits = 0;
   double s = step;
   while(s < 1.0 && digits < 8)
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

double NormalizeVolume(double volume)
  {
   const double min_lot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   const double max_lot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double       lot_step = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   if(lot_step <= 0.0)
      lot_step = 0.01;
   double lots = MathFloor(volume / lot_step) * lot_step;
   lots = NormalizeDouble(lots, g_volume_digits);
   if(lots < min_lot)
      lots = min_lot;
   if(max_lot > 0.0 && lots > max_lot)
      lots = max_lot;
   return(lots);
  }

//--- fixed-fractional sizing: risk InpRiskPercent of equity over sl_points
double RiskVolume(const double sl_points)
  {
   const double min_lot    = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   const double equity     = AccountInfoDouble(ACCOUNT_EQUITY);
   const double tick_value = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
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

bool IsNewBar()
  {
   const datetime bar_time = iTime(_Symbol, InpTimeframe, 0);
   if(bar_time == 0 || bar_time == g_last_bar)
      return(false);
   g_last_bar = bar_time;
   return(true);
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

bool MarginIsSufficient(const ENUM_ORDER_TYPE type, const double lots, const double price)
  {
   double margin = 0.0;
   if(!OrderCalcMargin(type, _Symbol, lots, price, margin))
      return(false);
   return(margin <= AccountInfoDouble(ACCOUNT_MARGIN_FREE));
  }

void OpenPosition(const ENUM_ORDER_TYPE type)
  {
   const double point = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   const int    digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
   const double sl_points = (double)InpStopLossPoints;

   double price = (type == ORDER_TYPE_BUY)
                  ? SymbolInfoDouble(_Symbol, SYMBOL_ASK)
                  : SymbolInfoDouble(_Symbol, SYMBOL_BID);

   if(InpStopLossPoints > 0)
     {
      const double min_distance = MinStopDistance();
      if(sl_points * point < min_distance)
        {
         PrintFormat("SL %d points below stops level (%d) - skipping entry",
                     InpStopLossPoints, (int)SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL));
         return;
        }
     }

   const double lots = RiskVolume(sl_points);
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
   if(!sent)
      PrintFormat("PositionOpen failed: retcode=%d %s",
                  g_trade.ResultRetcode(),
                  g_trade.ResultRetcodeDescription());
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
   //--- filters first: cheap checks before any indicator work
   if(!IsNewBar())            return;
   if(!TradingIsAllowed())    return;
   if(!SpreadIsAcceptable())  return;
   if(HasOwnPosition())       return;
   if(BarsCalculated(g_fast_handle) < InpSlowPeriod + 2) return;
   if(BarsCalculated(g_slow_handle) < InpSlowPeriod + 2) return;

   //--- read the two closed bars (shifts 1 and 2)
   double fast[];
   double slow[];
   if(CopyBuffer(g_fast_handle, 0, 1, 2, fast) != 2) return;
   if(CopyBuffer(g_slow_handle, 0, 1, 2, slow) != 2) return;
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
