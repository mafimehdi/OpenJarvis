//+------------------------------------------------------------------+
//|                                                   GF63_Test.mq4  |
//|        GoldFusion v6.3 — اکسپرت تشخیصی و تست ماژول اصلاحی         |
//|                                                                  |
//|  ⚠ این اکسپرت هیچ معامله‌ای باز نمی‌کند. فقط تشخیص و پایش است.      |
//|                                                                  |
//|  چرا این فایل لازم است:                                           |
//|    GF63_Fixes.mqh یک فایل include است و به‌تنهایی کامپایل نمی‌شود.  |
//|    کامپایلر MQL4 برای فایل .mq4 حداقل یک تابع رویداد (OnTick)     |
//|    الزامی می‌داند. این فایل آن پوسته را فراهم می‌کند تا بتوانید    |
//|    ماژول را جدا از اکسپرت اصلی تست کنید.                          |
//|                                                                  |
//|  نصب:                                                             |
//|    ۱. GF63_Fixes.mqh  را در  MQL4/Include/  بگذارید               |
//|    ۲. این فایل را در  MQL4/Experts/  بگذارید                      |
//|    ۳. F7 برای کامپایل — باید بدون خطا کامپایل شود                  |
//|    ۴. روی چارت XAUUSD M15 بیندازید                                |
//|    ۵. تب Experts (Ctrl+T) و گوشه چارت را ببینید                    |
//+------------------------------------------------------------------+
#property copyright "GoldFusion Review"
#property version   "1.00"
#property strict

#include <GF63_Fixes.mqh>

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
