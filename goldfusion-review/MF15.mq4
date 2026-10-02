//+------------------------------------------------------------------+
//|                                                       MF15.mq4   |
//|        GoldFusion v6.3 — نسخه خودکفا (All-in-One)                |
//|                                                                  |
//|  ⚠ این فایل کاملاً مستقل است — به هیچ فایل include نیازی ندارد.    |
//|  ⚠ هیچ معامله‌ای باز نمی‌کند. فقط تشخیص و پایش است.                 |
//|                                                                  |
//|  چرا این فایل:                                                    |
//|    خطای "event handling function not found" یعنی فایل .mq4 شما    |
//|    تابع OnTick() ندارد. این فایل هر چهار تابع رویداد را دارد و     |
//|    کد ماژول اصلاحی هم داخلش قرار گرفته است.                       |
//|                                                                  |
//|  نصب — فقط سه گام:                                                |
//|    ۱. کل محتوای این فایل را کپی کنید                              |
//|    ۲. در MetaEditor فایل MF15.mq4 را باز کنید و کل محتوایش را     |
//|       با این جایگزین کنید (Ctrl+A بعد Ctrl+V)                     |
//|    ۳. F7 برای کامپایل؛ سپس روی چارت XAUUSD M15 بیندازید            |
//|                                                                  |
//|  اگر خطا گرفتید، متن دقیق خطا و شماره خط را بفرستید.               |
//+------------------------------------------------------------------+
#property copyright "GoldFusion Review"
#property version   "1.00"
#property strict

#include <stdlib.mqh>      // برای ErrorDescription() — استاندارد MQL4

//==================================================================
//   بخش ۱ — ماژول اصلاحی (قبلاً GF63_Fixes.mqh)
//==================================================================
//+------------------------------------------------------------------+
//|                                                   GF63_Fixes.mqh |
//|          GoldFusion v6.3 — ماژول اصلاحی هسته معاملاتی            |
//|                                                                  |
//|  این ماژول چهار دسته باگ شناسایی‌شده را اصلاح می‌کند:              |
//|                                                                  |
//|   ۱. واحدها  — قاطی شدن Point / قیمت / دلار  (ریشه اصلی)          |
//|   ۲. کف حد ضرر — MathMin به‌جای MathMax، و ضرب دوباره در Point     |
//|   ۳. عملیات سفارش — OrderSend/Close/Modify بدون بررسی خطا          |
//|   ۴. حجم — محاسبه بر پایه TickValue بدون فرض Point                |
//|                                                                  |
//|  نصب:                                                             |
//|   ۱. این فایل را در  MQL4/Include/  کپی کنید                       |
//|   ۲. در فایل اکسپرت اضافه کنید:  #include <GF63_Fixes.mqh>         |
//|   ۳. توابع قدیمی را با نسخه‌های GF_ جایگزین کنید (راهنما:          |
//|      GF63_Integration_Guide.md)                                  |
//|                                                                  |
//|  ⚠ پیش‌نیاز: MQL4 build 600 یا بالاتر.                             |
//+------------------------------------------------------------------+





//==================================================================
//                        تنظیمات ایمنی
//==================================================================
input string GF_Sep1               = "=== GF63 Safety ===";  // ── ایمنی ──
input double GF_SpreadMult         = 5.0;    // کف حد ضرر = چند برابر اسپرد
input double GF_StopBufferPoints   = 10.0;   // بافر اضافه روی کف (پوینت)
input int    GF_MaxSlippagePoints  = 30;     // حداکثر لغزش مجاز (پوینت)
input int    GF_MaxRetries         = 3;      // تعداد تلاش OrderSend
input int    GF_RetryDelayMs       = 300;    // فاصله تلاش‌ها (میلی‌ثانیه)
input bool   GF_VerboseLog         = true;   // لاگ تفصیلی در تب Experts

//==================================================================
//                     بخش ۱ — توابع کمکی پایه
//==================================================================

//+------------------------------------------------------------------+
//| تعداد رقم اعشار گام حجم                                          |
//+------------------------------------------------------------------+
int GF_LotDigits(double step)
{
   for(int d = 0; d <= 8; d++)
      if(MathAbs(step - NormalizeDouble(step, d)) < 1e-9)
         return d;
   return 2;
}

//+------------------------------------------------------------------+
//| اسپرد لحظه‌ای بر حسب قیمت (نه پوینت)                              |
//|                                                                  |
//| نکته: MODE_SPREAD در MQL4 بر حسب پوینت است، پس در _Point ضرب      |
//|       می‌شود. مقدار زنده (Ask-Bid) از قبل بر حسب قیمت است و       |
//|       نباید ضرب شود. بزرگ‌ترین این دو گرفته می‌شود.                |
//+------------------------------------------------------------------+
double GF_SpreadPrice()
{
   double live   = Ask - Bid;                              // قیمت
   double report = MarketInfo(Symbol(), MODE_SPREAD) * _Point;  // پوینت → قیمت

   if(live   < 0) live   = 0;
   if(report < 0) report = 0;

   return MathMax(live, report);
}

//+------------------------------------------------------------------+
//| کمینه فاصله مجاز استاپ/تارگت از قیمت ورود (بر حسب قیمت)          |
//|                                                                  |
//| این تابع اصلاح مستقیم باگ «حد ضرر کوچک‌تر از اسپرد» است.          |
//+------------------------------------------------------------------+
double GF_MinStopDistance()
{
   double stopLevel   = MarketInfo(Symbol(), MODE_STOPLEVEL)   * _Point;
   double freezeLevel = MarketInfo(Symbol(), MODE_FREEZELEVEL) * _Point;
   double brokerMin   = MathMax(stopLevel, freezeLevel);

   double spreadFloor = GF_SpreadPrice() * GF_SpreadMult;   // کف مبتنی بر اسپرد
   double buffer      = GF_StopBufferPoints * _Point;

   return MathMax(brokerMin, spreadFloor) + buffer;
}

//+------------------------------------------------------------------+
//| نرمال‌سازی حجم به گام بروکر — جلوگیری از خطای ۱۳۱                |
//|                                                                  |
//| حجم به سمت پایین گرد می‌شود تا ریسک از مقدار قصدشده بیشتر نشود.   |
//| اگر کمتر از MinLot شود، به MinLot بالابرده می‌شود (با هشدار).     |
//+------------------------------------------------------------------+
double GF_NormalizeLots(double lots)
{
   double minLot  = MarketInfo(Symbol(), MODE_MINLOT);
   double maxLot  = MarketInfo(Symbol(), MODE_MAXLOT);
   double lotStep = MarketInfo(Symbol(), MODE_LOTSTEP);

   if(minLot  <= 0) minLot  = 0.01;
   if(maxLot  <= 0) maxLot  = 100.0;
   if(lotStep <= 0) lotStep = 0.01;

   int digits = GF_LotDigits(lotStep);

   lots = MathFloor(lots / lotStep + 1e-9) * lotStep;
   lots = NormalizeDouble(lots, digits);

   if(lots < minLot)
   {
      if(GF_VerboseLog)
         PrintFormat("GF_NormalizeLots: %.4f < minLot(%.2f) -> MinLot", lots, minLot);
      lots = minLot;
   }
   if(lots > maxLot) lots = maxLot;

   return lots;
}

//+------------------------------------------------------------------+
//| نرمال‌سازی قیمت به TickSize — جلوگیری از خطای ۱۲۹                 |
//|                                                                  |
//| روی طلا با ۲ رقم اعشار کار می‌کند و روی ۵ رقمی‌ها هم درست است.     |
//+------------------------------------------------------------------+
double GF_NormalizePrice(double price)
{
   double tick = MarketInfo(Symbol(), MODE_TICKSIZE);
   if(tick <= 0.0)
      return NormalizeDouble(price, _Digits);
   return NormalizeDouble(MathRound(price / tick) * tick, _Digits);
}

//+------------------------------------------------------------------+
//| ATR با محافظت از سرریز و مقدار صفر                               |
//+------------------------------------------------------------------+
double GF_Atr(int period = 14, int shift = 1, int tf = PERIOD_CURRENT)
{
   if(period < 1) period = 14;
   if(shift  < 0) shift  = 0;
   if(tf == PERIOD_CURRENT) tf = (int)Period();
   if(Bars < period + shift + 2) return 0.0;

   double a = iATR(Symbol(), tf, period, shift);

   if(a == 0.0 || a == EMPTY_VALUE) return 0.0;
   return a;
}

//+------------------------------------------------------------------+
//| تشخیص کندل جدید — برای استراتژی‌های کندل‌محور                     |
//|                                                                  |
//| برای چند تایم‌فریم جداگانه کار می‌کند.                             |
//+------------------------------------------------------------------+
bool GF_IsNewBar(int tf = PERIOD_CURRENT)
{
   static int      tfList[16];
   static datetime tfTime[16];
   static int      tfCount = 0;

   if(tf == PERIOD_CURRENT) tf = (int)Period();

   datetime t = iTime(Symbol(), tf, 0);
   if(t == 0) return false;

   for(int i = 0; i < tfCount; i++)
   {
      if(tfList[i] == tf)
      {
         if(tfTime[i] != t) { tfTime[i] = t; return true; }
         return false;
      }
   }

   if(tfCount < 16)
   {
      tfList[tfCount] = tf;
      tfTime[tfCount] = t;
      tfCount++;
   }
   return false;
}

//+------------------------------------------------------------------+
//| آماده‌سازی بستر معاملاتی: مجوز، ترد آزاد، نرخ تازه                |
//+------------------------------------------------------------------+
bool GF_TradeContextReady(bool logIt = true)
{
   if(!IsTradeAllowed())
   {
      if(logIt) Print("GF: معامله مجاز نیست (AutoTrading یا تنظیمات اکسپرت).");
      return false;
   }

   int guard = 0;
   while(IsTradeContextBusy() && guard++ < 40) Sleep(50);

   if(IsTradeContextBusy())
   {
      if(logIt) Print("GF: ترد معاملاتی مشغول است — تلاش بعدی.");
      return false;
   }

   RefreshRates();
   return true;
}

//==================================================================
//          بخش ۲ — اصلاح باگ واحدها و کف حد ضرر
//==================================================================

//+------------------------------------------------------------------+
//| کف حد ضرر — اصلاح دو باگ هم‌زمان                                 |
//|                                                                  |
//| باگ الف) MathMin به‌جای MathMax:                                 |
//|    MathMin(sl, atrFloor)  →  کف به سقف تبدیل می‌شود              |
//|    MathMax(sl, atrFloor)  →  کف واقعی                             |
//|                                                                  |
//| باگ ب) واحدها:                                                   |
//|    ATR خروجی iATR از قبل بر حسب «قیمت» است.                       |
//|    ضرب دوباره آن در _Point مقدار را ۱۰۰ برابر کوچک می‌کند.        |
//|                                                                  |
//| پارامترها:                                                        |
//|   slDistanceFromUsd — فاصله حد ضرر بر حسب قیمت (نه پوینت)         |
//|   atrValue          — خروجی خام iATR (قیمت). صفر = بدون ATR       |
//|   atrMultiplier     — ضریب ATR                                    |
//|                                                                  |
//| خروجی: فاصله نهایی حد ضرر بر حسب قیمت                             |
//+------------------------------------------------------------------+
double GF_SLFloor(double slDistanceFromUsd,
                  double atrValue = 0.0,
                  double atrMultiplier = 0.0)
{
   double sl = slDistanceFromUsd;

   if(sl < 0) sl = 0;

   // --- کف ATR ---
   if(atrValue > 0.0 && atrMultiplier > 0.0)
   {
      double atrFloor = atrValue * atrMultiplier;   // بدون ضرب در _Point
      sl = MathMax(sl, atrFloor);                    // کف، نه سقف
   }

   // --- کف ایمنی اسپرد و بروکر ---
   double minDist = GF_MinStopDistance();
   if(sl < minDist)
   {
      if(GF_VerboseLog)
         PrintFormat("GF_SLFloor: %.2f -> %.2f (کف ایمنی %.0f پوینت)",
                     sl, minDist, minDist / _Point);
      sl = minDist;
   }

   return sl;
}

//+------------------------------------------------------------------+
//| محاسبه حجم بر پایه درصد ریسک — بدون فرض Point                     |
//|                                                                  |
//| از TickValue استفاده می‌کند، پس روی طلا، فارکس و شاخص‌ها درست     |
//| کار می‌کند. پارامتر slDistancePrice بر حسب «قیمت» است.            |
//+------------------------------------------------------------------+
double GF_LotsForRisk(double riskPercent, double slDistancePrice)
{
   if(riskPercent <= 0.0 || slDistancePrice <= 0.0) return 0.0;

   double tickValue = MarketInfo(Symbol(), MODE_TICKVALUE);
   double tickSize  = MarketInfo(Symbol(), MODE_TICKSIZE);

   if(tickValue <= 0.0 || tickSize <= 0.0)
   {
      Print("GF_LotsForRisk: TickValue/TickSize نامعتبر — محاسبه ممکن نیست.");
      return 0.0;
   }

   double riskMoney  = AccountEquity() * riskPercent / 100.0;
   double lossPerLot = (slDistancePrice / tickSize) * tickValue;

   if(lossPerLot <= 0.0) return 0.0;

   return GF_NormalizeLots(riskMoney / lossPerLot);
}

//+------------------------------------------------------------------+
//| فاصله سربه‌سر — باید از اسپرد بزرگ‌تر باشد                        |
//|                                                                  |
//| BE روی سود صفر یعنی پوزیشن بلافاصله با هزینه اسپرد بسته می‌شود.   |
//+------------------------------------------------------------------+
double GF_BreakEvenDistance()
{
   double spreadBased = GF_SpreadPrice() + 20.0 * _Point;
   return MathMax(spreadBased, GF_MinStopDistance());
}

//==================================================================
//        بخش ۳ — ارسال، بستن و اصلاح سفارش با بررسی خطا
//==================================================================

//+------------------------------------------------------------------+
//| خطاهای غیرقابل‌تکرار — تلاش مجدد بی‌فایده است                     |
//+------------------------------------------------------------------+
bool GF_IsFatalError(int err)
{
   return (err == 130  ||   // Invalid stops
           err == 131  ||   // Invalid trade volume
           err == 133  ||   // Trade disabled
           err == 134  ||   // Not enough money
           err == 4051 ||   // Invalid function parameter
           err == 4060 ||   // EA not allowed to trade
           err == 4109);    // Trade not allowed
}

//+------------------------------------------------------------------+
//| ارسال سفارش باز با بررسی کامل خطا و تلاش مجدد                     |
//|                                                                  |
//| حد ضرر و تارگت به‌صورت «فاصله بر حسب قیمت» گرفته می‌شوند و خود    |
//| تابع سمت درست را رعایت و کف ایمنی را اعمال می‌کند.                 |
//|                                                                  |
//| خروجی: شماره سفارش، یا ۱- در صورت شکست                             |
//+------------------------------------------------------------------+
int GF_SafeOrderSend(int    cmd,
                     double lots,
                     double slDistance,
                     double tpDistance,
                     int    magic,
                     string comment = "GF63")
{
   if(!GF_TradeContextReady()) return -1;

   lots = GF_NormalizeLots(lots);
   if(lots <= 0.0)
   {
      Print("GF_SafeOrderSend: حجم نامعتبر — ارسال لغو شد.");
      return -1;
   }

   double minDist = GF_MinStopDistance();

   if(slDistance > 0.0 && slDistance < minDist)
   {
      if(GF_VerboseLog)
         PrintFormat("GF: SL %.0fpt < کف %.0fpt -> اصلاح شد.",
                     slDistance / _Point, minDist / _Point);
      slDistance = minDist;
   }
   if(tpDistance > 0.0 && tpDistance < minDist)
   {
      if(GF_VerboseLog)
         PrintFormat("GF: TP %.0fpt < کف %.0fpt -> اصلاح شد.",
                     tpDistance / _Point, minDist / _Point);
      tpDistance = minDist;
   }

   for(int attempt = 1; attempt <= GF_MaxRetries; attempt++)
   {
      if(!GF_TradeContextReady(attempt == 1)) return -1;

      double price = (cmd == OP_BUY) ? Ask : Bid;
      double sl = 0.0, tp = 0.0;

      if(cmd == OP_BUY)
      {
         if(slDistance > 0.0) sl = GF_NormalizePrice(price - slDistance);
         if(tpDistance > 0.0) tp = GF_NormalizePrice(price + tpDistance);
      }
      else
      {
         if(slDistance > 0.0) sl = GF_NormalizePrice(price + slDistance);
         if(tpDistance > 0.0) tp = GF_NormalizePrice(price - tpDistance);
      }

      int ticket = OrderSend(Symbol(), cmd, lots, price, GF_MaxSlippagePoints,
                             sl, tp, comment, magic, 0, clrNONE);

      if(ticket > 0)
      {
         if(GF_VerboseLog)
            PrintFormat("GF: #%d باز شد | %s %.2f @ %s | SL=%s TP=%s",
                        ticket,
                        (cmd == OP_BUY ? "BUY" : "SELL"),
                        lots,
                        DoubleToString(price, _Digits),
                        DoubleToString(sl, _Digits),
                        DoubleToString(tp, _Digits));
         return ticket;
      }

      int err = GetLastError();
      PrintFormat("GF: OrderSend تلاش %d/%d ناموفق — خطا %d (%s)",
                  attempt, GF_MaxRetries, err, ErrorDescription(err));

      if(GF_IsFatalError(err))
      {
         Print("GF: خطای غیرقابل‌تکرار — ارسال لغو شد.");
         return -1;
      }

      Sleep(GF_RetryDelayMs);
   }

   Print("GF: تمام تلاش‌های OrderSend شکست خورد.");
   return -1;
}

//+------------------------------------------------------------------+
//| بستن یک پوزیشن با بررسی خطا                                     |
//+------------------------------------------------------------------+
bool GF_ClosePosition(int ticket)
{
   if(!GF_TradeContextReady()) return false;

   if(!OrderSelect(ticket, SELECT_BY_TICKET))
   {
      PrintFormat("GF: OrderSelect #%d ناموفق — خطا %d (%s)",
                  ticket, GetLastError(), ErrorDescription(GetLastError()));
      return false;
   }

   if(OrderCloseTime() != 0) return true;          // از قبل بسته شده
   if(OrderSymbol() != Symbol()) return false;
   if(OrderType()   >  OP_SELL)  return false;     // فقط پوزیشن باز

   double price = (OrderType() == OP_BUY) ? Bid : Ask;

   if(OrderClose(ticket, OrderLots(), price, GF_MaxSlippagePoints, clrRed))
   {
      if(GF_VerboseLog) PrintFormat("GF: #%d بسته شد @ %s",
                                    ticket, DoubleToString(price, _Digits));
      return true;
   }

   int err = GetLastError();
   PrintFormat("GF: OrderClose #%d ناموفق — خطا %d (%s)",
               ticket, err, ErrorDescription(err));
   return false;
}

//+------------------------------------------------------------------+
//| بستن همه پوزیشن‌ها — با ایندکس نزولی                             |
//|                                                                  |
//| این تابع باگ «از قلم افتادن سفارش» را اصلاح می‌کند:              |
//|   ❌ for(int i = 0; i < OrdersTotal(); i++)  ← ایندکس‌ها جابه‌جا    |
//|   ✅ for(int i = OrdersTotal()-1; i >= 0; i--)  ← درست             |
//|                                                                  |
//| خروجی: تعداد سفارش‌های بسته‌شده                                    |
//+------------------------------------------------------------------+
int GF_CloseAllReverse(int magic = 0, int onlyType = -1)
{
   if(!GF_TradeContextReady()) return 0;

   int closed = 0;

   for(int i = OrdersTotal() - 1; i >= 0; i--)
   {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES)) continue;
      if(OrderSymbol() != Symbol())                   continue;
      if(magic > 0 && OrderMagicNumber() != magic)    continue;
      if(OrderType()   >  OP_SELL)                    continue;
      if(onlyType >= 0 && OrderType() != onlyType)    continue;

      if(GF_ClosePosition(OrderTicket())) closed++;
   }

   return closed;
}

//+------------------------------------------------------------------+
//| اصلاح حد ضرر/تارگت با رعایت کف و بررسی خطا                       |
//+------------------------------------------------------------------+
bool GF_SafeOrderModify(int ticket, double newSL, double newTP)
{
   if(!GF_TradeContextReady()) return false;

   if(!OrderSelect(ticket, SELECT_BY_TICKET)) return false;
   if(OrderCloseTime() != 0)                  return false;
   if(OrderSymbol() != Symbol())              return false;

   int    type = OrderType();
   double minD = GF_MinStopDistance();

   if(newSL > 0.0)
   {
      if(type == OP_BUY) newSL = MathMin(newSL, Bid - minD);
      else               newSL = MathMax(newSL, Ask + minD);
      newSL = GF_NormalizePrice(newSL);
   }

   if(newTP > 0.0)
   {
      if(type == OP_BUY) newTP = MathMax(newTP, Bid + minD);
      else               newTP = MathMin(newTP, Ask - minD);
      newTP = GF_NormalizePrice(newTP);
   }

   // اگر تغییری لازم نیست، درخواست بی‌مورد نفرست
   if(MathAbs(OrderStopLoss()   - newSL) < _Point * 0.5 &&
      MathAbs(OrderTakeProfit() - newTP) < _Point * 0.5)
      return true;

   if(OrderModify(ticket, OrderOpenPrice(), newSL, newTP, 0, clrYellow))
   {
      if(GF_VerboseLog)
         PrintFormat("GF: #%d اصلاح شد | SL=%s TP=%s", ticket,
                     DoubleToString(newSL, _Digits),
                     DoubleToString(newTP, _Digits));
      return true;
   }

   int err = GetLastError();
   if(err == 1) return true;      // «تغییری وجود ندارد»

   PrintFormat("GF: OrderModify #%d ناموفق — خطا %d (%s)",
               ticket, err, ErrorDescription(err));
   return false;
}

//+------------------------------------------------------------------+
//| تریلینگ استاپ — با محدودیت نرخ و رعایت کف                        |
//|                                                                  |
//| حداکثر هر ۲ ثانیه اجرا می‌شود تا سرور با درخواست Modify بمباران    |
//| نشود. هر دو پارامتر بر حسب «قیمت» هستند.                          |
//+------------------------------------------------------------------+
void GF_TrailStop(int magic, double trailStartPrice, double trailDistancePrice)
{
   static datetime lastRun = 0;
   if(TimeCurrent() - lastRun < 2) return;
   lastRun = TimeCurrent();

   if(trailStartPrice    <= 0.0) return;
   if(trailDistancePrice <= 0.0) return;

   double minD = GF_MinStopDistance();
   if(trailDistancePrice < minD) trailDistancePrice = minD;

   for(int i = OrdersTotal() - 1; i >= 0; i--)
   {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES)) continue;
      if(OrderSymbol() != Symbol())                   continue;
      if(magic > 0 && OrderMagicNumber() != magic)    continue;

      int type = OrderType();
      if(type > OP_SELL) continue;

      double open = OrderOpenPrice();
      double sl   = OrderStopLoss();

      if(type == OP_BUY)
      {
         if(Bid - open < trailStartPrice) continue;
         double want = Bid - trailDistancePrice;
         if(sl > 0.0 && sl >= want) continue;
         GF_SafeOrderModify(OrderTicket(), want, OrderTakeProfit());
      }
      else
      {
         if(open - Ask < trailStartPrice) continue;
         double want = Ask + trailDistancePrice;
         if(sl > 0.0 && sl <= want) continue;
         GF_SafeOrderModify(OrderTicket(), want, OrderTakeProfit());
      }
   }
}

//+------------------------------------------------------------------+
//| ارسال سفارش معلق — راه‌حل «دیر وارد شدن معامله»                   |
//|                                                                  |
//| به‌جای تعقیب قیمت بعد از بسته شدن کندل، سطح سیگنال را از پیش      |
//| ثبت کنید تا وقتی قیمت به آن رسید پر شود.                          |
//|                                                                  |
//| ⚠ بعضی بروکرها پارامتر expiry را پشتیبانی نمی‌کنند.               |
//|   expiryMinutes = 0 یعنی بدون انقضا.                              |
//+------------------------------------------------------------------+
int GF_SendStopOrder(int    cmd,
                     double lots,
                     double entryPrice,
                     double slDistance,
                     double tpDistance,
                     int    magic,
                     int    expiryMinutes = 0,
                     string comment = "GF63")
{
   if(cmd != OP_BUYSTOP && cmd != OP_SELLSTOP)
   {
      Print("GF_SendStopOrder: فقط OP_BUYSTOP و OP_SELLSTOP پذیرفته می‌شود.");
      return -1;
   }

   if(!GF_TradeContextReady()) return -1;

   lots = GF_NormalizeLots(lots);
   if(lots <= 0.0) return -1;

   double minD = GF_MinStopDistance();
   double sl = 0.0, tp = 0.0;

   if(cmd == OP_BUYSTOP)
   {
      if(slDistance > 0.0) sl = GF_NormalizePrice(entryPrice - MathMax(slDistance, minD));
      if(tpDistance > 0.0) tp = GF_NormalizePrice(entryPrice + MathMax(tpDistance, minD));
   }
   else
   {
      if(slDistance > 0.0) sl = GF_NormalizePrice(entryPrice + MathMax(slDistance, minD));
      if(tpDistance > 0.0) tp = GF_NormalizePrice(entryPrice - MathMax(tpDistance, minD));
   }

   entryPrice = GF_NormalizePrice(entryPrice);
   datetime expiry = (expiryMinutes > 0) ? TimeCurrent() + expiryMinutes * 60 : 0;

   for(int attempt = 1; attempt <= GF_MaxRetries; attempt++)
   {
      if(!GF_TradeContextReady(attempt == 1)) return -1;

      int ticket = OrderSend(Symbol(), cmd, lots, entryPrice, GF_MaxSlippagePoints,
                             sl, tp, comment, magic, expiry, clrBlue);

      if(ticket > 0)
      {
         if(GF_VerboseLog)
            PrintFormat("GF: سفارش معلق #%d ثبت شد | %s %.2f @ %s | SL=%s TP=%s",
                        ticket,
                        (cmd == OP_BUYSTOP ? "BUYSTOP" : "SELLSTOP"),
                        lots,
                        DoubleToString(entryPrice, _Digits),
                        DoubleToString(sl, _Digits),
                        DoubleToString(tp, _Digits));
         return ticket;
      }

      int err = GetLastError();
      PrintFormat("GF: OrderSend(معلق) تلاش %d/%d ناموفق — خطا %d (%s)",
                  attempt, GF_MaxRetries, err, ErrorDescription(err));

      if(GF_IsFatalError(err)) return -1;

      Sleep(GF_RetryDelayMs);
   }

   return -1;
}

//==================================================================
//                  بخش ۴ — تشخیص و ممیزی
//==================================================================

//+------------------------------------------------------------------+
//| ممیزی پوزیشن‌های باز — باگ‌های باقی‌مانده را پیدا می‌کند           |
//+------------------------------------------------------------------+
void GF_AuditOpenPositions(int magic = 0)
{
   int    count      = 0;
   int    badSL      = 0;
   int    wrongSide  = 0;
   int    belowSpread= 0;
   double worstRR    = 0.0;
   double spreadPts  = GF_SpreadPrice() / _Point;
   double stopLvl    = MarketInfo(Symbol(), MODE_STOPLEVEL);

   for(int i = OrdersTotal() - 1; i >= 0; i--)
   {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES)) continue;
      if(OrderSymbol() != Symbol())                   continue;
      if(magic > 0 && OrderMagicNumber() != magic)    continue;
      if(OrderType()   >  OP_SELL)                    continue;

      count++;

      double open   = OrderOpenPrice();
      double sl     = OrderStopLoss();
      double tp     = OrderTakeProfit();
      double slDist = (sl > 0.0) ? MathAbs(open - sl) / _Point : 0.0;
      double tpDist = (tp > 0.0) ? MathAbs(tp - open) / _Point : 0.0;

      bool ws = (OrderType() == OP_BUY  && sl > 0.0 && sl > open) ||
                (OrderType() == OP_SELL && sl > 0.0 && sl < open);

      if(sl > 0.0 && slDist < stopLvl)          badSL++;
      if(ws)                                     wrongSide++;
      if(slDist > 0.0 && slDist < spreadPts)     belowSpread++;
      if(slDist > 0.0 && tpDist > 0.0 && (tpDist / slDist) > worstRR)
         worstRR = tpDist / slDist;

      PrintFormat("GF_Audit #%d %s open=%s SL=%s TP=%s | SLdist=%.0fpt TPdist=%.0fpt%s",
                  OrderTicket(),
                  (OrderType() == OP_BUY ? "BUY " : "SELL"),
                  DoubleToString(open, _Digits),
                  DoubleToString(sl,   _Digits),
                  DoubleToString(tp,   _Digits),
                  slDist, tpDist,
                  (ws ? "   <<< حد ضرر سمت اشتباه!" : ""));
   }

   PrintFormat("GF_Audit: %d پوزیشن | اسپرد=%.0fپوینت | SL زیر کف بروکر=%d | سمت اشتباه=%d | SL کوچک‌تر از اسپرد=%d | بدترین R:R=1:%.0f",
               count, spreadPts, badSL, wrongSide, belowSpread, worstRR);

   if(belowSpread > 0)
      Print("GF_Audit *** خطر: حد ضرر کوچک‌تر از اسپرد است — پوزیشن از لحظه ورود در ضرر. مقدار GF_SpreadMult را چک کنید.");
   if(wrongSide > 0)
      Print("GF_Audit *** خطر: حد ضرر در سمت اشتباه قیمت — این سفارش‌ها باید با خطای ۱۳۰ رد می‌شدند.");
}

//+------------------------------------------------------------------+
//| خودآزمایی — در OnInit صدا بزنید                                   |
//+------------------------------------------------------------------+
void GF_SelfTest()
{
   double minD   = GF_MinStopDistance();
   double spread = GF_SpreadPrice();

   PrintFormat("=== GF63 SelfTest | %s ===", Symbol());
   PrintFormat("Digits=%d  Point=%s  اسپرد=%.0fپوینت (%.2f قیمت)",
               _Digits, DoubleToString(_Point, 5),
               spread / _Point, spread);

   PrintFormat("کف بروکر (STOPLEVEL)=%.0fپوینت | FREEZELEVEL=%.0fپوینت | کف ایمنی نهایی=%.0fپوینت (%.2f قیمت)",
               MarketInfo(Symbol(), MODE_STOPLEVEL),
               MarketInfo(Symbol(), MODE_FREEZELEVEL),
               minD / _Point, minD);

   PrintFormat("MinLot=%.2f  MaxLot=%.2f  LotStep=%.2f  TickValue=%.2f  TickSize=%s",
               MarketInfo(Symbol(), MODE_MINLOT),
               MarketInfo(Symbol(), MODE_MAXLOT),
               MarketInfo(Symbol(), MODE_LOTSTEP),
               MarketInfo(Symbol(), MODE_TICKVALUE),
               DoubleToString(MarketInfo(Symbol(), MODE_TICKSIZE), 5));

   // آزمون ۱ — کف حد ضرر با ATR
   double atr = GF_Atr(14, 1);
   double sl  = GF_SLFloor(0.20, atr, 4.0);
   PrintFormat("آزمون SL: ورودی ۰.۲۰ + ATR floor(%.2f × ۴) -> %.2f (%.0fپوینت)",
               atr, sl, sl / _Point);

   // آزمون ۲ — حجم بر پایه ریسک
   double lots = GF_LotsForRisk(1.0, minD);
   PrintFormat("آزمون حجم: ۱٪ ریسک با SL=%.0fپوینت -> %.2f لات", minD / _Point, lots);

   // آزمون ۳ — نرمال‌سازی حجم
   PrintFormat("آزمون نرمال‌سازی حجم: 0.037 -> %.2f", GF_NormalizeLots(0.037));

   // آزمون ۴ — فاصله سربه‌سر
   PrintFormat("آزمون BE: %.0fپوینت (اسپرد %.0f + بافر)", GF_BreakEvenDistance() / _Point, spread / _Point);

   if(minD <= spread)
      Print("GF_SelfTest *** هشدار: کف ایمنی از اسپرد بزرگ‌تر نیست — GF_SpreadMult را بالا ببرید.");

   Print("=== پایان SelfTest ===");
}


//+------------------------------------------------------------------+

//==================================================================
//   بخش ۲ — اکسپرت تشخیصی (تابع‌های رویداد)
//==================================================================
//==================================================================
//                        متغیرهای پایش
//==================================================================
int      g_tickCount     = 0;
int      g_auditCount    = 0;
datetime g_lastAudit     = 0;
datetime g_startTime     = 0;
int      g_maxPositions  = 0;
int      g_alertsRaised  = 0;

//==================================================================
//                          OnInit
//==================================================================
int OnInit()
{
   g_startTime = TimeCurrent();

   Print("╔══════════════════════════════════════════════════════════╗");
   Print("║   GF63 Test EA — اکسپرت تشخیصی (بدون معامله)             ║");
   Print("╚══════════════════════════════════════════════════════════╝");

   // ── اجرای خودآزمایی ماژول ──
   GF_SelfTest();

   // ── ممیزی پوزیشن‌های موجود ──
   GF_AuditOpenPositions();

   // ── هشدارهای اولیه ──
   double spread = GF_SpreadPrice();
   double minD   = GF_MinStopDistance();

   if(minD <= spread)
   {
      Print("GF63_Test *** هشدار: کف ایمنی از اسپرد بزرگ‌تر نیست.");
      g_alertsRaised++;
   }

   if(Period() != PERIOD_M15)
      PrintFormat("GF63_Test: توجه — این اکسپرت روی %s اجرا می‌شود، نه M15.",
                  EnumToString((ENUM_TIMEFRAMES)Period()));

   UpdatePanel();
   return(INIT_SUCCEEDED);
}

//==================================================================
//                          OnTick
//==================================================================
void OnTick()
{
   g_tickCount++;

   // ── ممیزی هر ۶۰ ثانیه (نه هر تیک، برای جلوگیری از پر شدن لاگ) ──
   if(TimeCurrent() - g_lastAudit >= 60)
   {
      g_lastAudit = TimeCurrent();
      g_auditCount++;

      int positions = CountPositions();
      if(positions > g_maxPositions) g_maxPositions = positions;

      // فقط اگر پوزیشن باز است ممیزی کن
      if(positions > 0)
      {
         GF_AuditOpenPositions();
      }
      else if(g_auditCount <= 2)
      {
         PrintFormat("GF63_Test: هیچ پوزیشن بازی نیست | اسپرد=%.0fپوینت | کف ایمنی=%.0fپوینت",
                     GF_SpreadPrice() / _Point, GF_MinStopDistance() / _Point);
      }
   }

   UpdatePanel();
}

//==================================================================
//                          OnTimer
//==================================================================
void OnTimer()
{
   UpdatePanel();
}

//==================================================================
//                          OnDeinit
//==================================================================
void OnDeinit(const int reason)
{
   Comment("");

   Print("╔══════════════════════════════════════════════════════════╗");
   PrintFormat("║  GF63 Test EA — پایان | دلیل: %d", reason);
   PrintFormat("║  مدت اجرا      : %d ساعت و %d دقیقه",
                 (int)((TimeCurrent() - g_startTime) / 3600),
                 (int)(((TimeCurrent() - g_startTime) % 3600) / 60));
   PrintFormat("║  تیک‌های پردازش‌شده: %d", g_tickCount);
   PrintFormat("║  ممیزی‌ها       : %d", g_auditCount);
   PrintFormat("║  بیشترین پوزیشن : %d", g_maxPositions);
   PrintFormat("║  هشدارها       : %d", g_alertsRaised);
   Print("╚══════════════════════════════════════════════════════════╝");

   if(g_alertsRaised > 0)
      Print("GF63_Test: هشدار صادر شد — خروجی لاگ را بررسی کنید.");
   else
      Print("GF63_Test: هیچ مشکل ساختاری یافت نشد.");
}

//==================================================================
//                        توابع کمکی
//==================================================================

//+------------------------------------------------------------------+
//| شمارش پوزیشن‌های باز روی این نماد                                |
//+------------------------------------------------------------------+
int CountPositions(int magic = 0)
{
   int n = 0;
   for(int i = OrdersTotal() - 1; i >= 0; i--)
   {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES)) continue;
      if(OrderSymbol() != Symbol())                   continue;
      if(OrderType()   >  OP_SELL)                    continue;
      if(magic > 0 && OrderMagicNumber() != magic)    continue;
      n++;
   }
   return n;
}

//+------------------------------------------------------------------+
//| پنل روی چارت                                                     |
//+------------------------------------------------------------------+
void UpdatePanel()
{
   double spread    = GF_SpreadPrice();
   double minD      = GF_MinStopDistance();
   double beDist    = GF_BreakEvenDistance();
   double atr       = GF_Atr(14, 1);
   int    positions = CountPositions();

   double spreadShare = (minD > 0) ? (spread / minD * 100.0) : 0.0;

   string status = "✅ سالم";
   if(spreadShare > 50.0) status = "⚠ اسپرد سهم بالایی از بودجه حد ضرر دارد";
   if(spreadShare > 100.0) status = "❌ اسپرد بزرگ‌تر از حد ضرر";

   if(g_alertsRaised > 0) status = "❌ هشدار — لاگ را ببینید";

   string txt = "";
   txt += "═══ GF63 Test EA ═══\n";
   txt += StringFormat("%s  (%s)\n", Symbol(), EnumToString((ENUM_TIMEFRAMES)Period()));
   txt += "────────────────────\n";
   txt += StringFormat("Bid/Ask      : %s / %s\n",
                       DoubleToString(Bid, _Digits), DoubleToString(Ask, _Digits));
   txt += StringFormat("Digits/Point : %d / %s\n",
                       _Digits, DoubleToString(_Point, 5));
   txt += StringFormat("اسپرد         : %.0f پوینت (%.2f قیمت)\n",
                       spread / _Point, spread);
   txt += "────────────────────\n";
   txt += StringFormat("ATR(14)      : %.2f قیمت\n", atr);
   txt += StringFormat("کف ایمنی      : %.0f پوینت (%.2f قیمت)\n",
                       minD / _Point, minD);
   txt += StringFormat("فاصله BE      : %.0f پوینت\n", beDist / _Point);
   txt += StringFormat("سهم اسپرد      : %.0f%%\n", spreadShare);
   txt += "────────────────────\n";

   double stopLvl = MarketInfo(Symbol(), MODE_STOPLEVEL);
   txt += StringFormat("STOPLEVEL    : %.0f پوینت\n", stopLvl);
   txt += StringFormat("MinLot       : %.2f\n", MarketInfo(Symbol(), MODE_MINLOT));
   txt += StringFormat("LotStep      : %.2f\n", MarketInfo(Symbol(), MODE_LOTSTEP));
   txt += StringFormat("TickValue    : %.2f\n", MarketInfo(Symbol(), MODE_TICKVALUE));
   txt += "────────────────────\n";
   txt += StringFormat("پوزیشن‌های باز : %d\n", positions);
   txt += StringFormat("تیک‌ها        : %d\n", g_tickCount);
   txt += StringFormat("وضعیت         : %s\n", status);

   Comment(txt);
}
//+------------------------------------------------------------------+
