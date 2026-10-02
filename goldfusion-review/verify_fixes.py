#!/usr/bin/env python3
"""
شبیه‌سازی عددی اصلاحات GF63_Fixes.mqh

این اسکریپت همان محاسبات MQL4 را در پایتون بازتولید می‌کند تا اعداد
گزارش قابل بازبینی و بازتولید باشند. با اعداد واقعی اسکرین‌شات اجرا می‌شود.

اجرا:  python3 verify_fixes.py
"""
import math

# ── پارامترهای XAUUSD از اسکرین‌شات ──
DIGITS      = 2
POINT       = 0.01
TICKSIZE    = 0.01
TICKVALUE   = 1.0        # ارزش یک تیک برای ۱ لات
BID         = 4171.44
ASK         = 4171.79
SPREAD_PTS  = 35         # اسپرد اعلامی پنل
MAX_SPREAD  = 160        # بدترین اسپرد دیده‌شده در پنل (1.60)
ATR_M15     = 5.0        # ATR(14) تقریبی طلا روی M15
TP_PRICE    = 56.18      # از جدول سفارش‌ها (4171.05 -> 4114.87)
BROKEN_SL   = 0.20       # حد ضرر مشاهدهشده در جدول
EQUITY      = 10_000.0


def spread_price(bid=BID, ask=ASK, reported_pts=SPREAD_PTS):
    """GF_SpreadPrice — بزرگ‌ترین مقدار بین اسپرد زنده و اعلامی"""
    live = max(0.0, ask - bid)
    report = max(0.0, reported_pts * POINT)
    return max(live, report)


def min_stop_distance(mult=5.0, buffer_pts=10.0, stoplevel_pts=0.0):
    """GF_MinStopDistance — کف ایمنی"""
    broker = max(stoplevel_pts * POINT, 0.0)
    return max(broker, spread_price() * mult) + buffer_pts * POINT


def sl_floor(sl_distance_from_usd, atr=0.0, atr_mult=0.0, fixed_min=None):
    """GF_SLFloor — اصلاح MathMin→MathMax و باگ واحد"""
    sl = max(0.0, sl_distance_from_usd)
    if atr > 0.0 and atr_mult > 0.0:
        sl = max(sl, atr * atr_mult)          # بدون ضرب در POINT
    if fixed_min is None:
        fixed_min = min_stop_distance()
    return max(sl, fixed_min)


def normalize_lots(lots, minlot=0.01, maxlot=100.0, step=0.01):
    """GF_NormalizeLots — گرد شدن به گام بروکر"""
    lots = math.floor(lots / step + 1e-9) * step
    digits = 0
    for d in range(9):
        if abs(step - round(step, d)) < 1e-9:
            digits = d
            break
    lots = round(lots, digits)
    return max(minlot, min(lots, maxlot))


def lots_for_risk(risk_pct, sl_price, equity=EQUITY):
    """GF_LotsForRisk — بر پایه TickValue، بدون فرض Point"""
    risk_money = equity * risk_pct / 100.0
    loss_per_lot = (sl_price / TICKSIZE) * TICKVALUE
    if loss_per_lot <= 0:
        return 0.0
    return normalize_lots(risk_money / loss_per_lot)


def break_even_distance():
    """GF_BreakEvenDistance"""
    return max(spread_price() + 20 * POINT, min_stop_distance())


def h(title):
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


h("۱) باگ اصلی — کف حد ضرر با ATR")

atr_broken = ATR_M15 * 4 * POINT      # ضرب اشتباه در Point
atr_fixed = ATR_M15 * 4               # مقدار درست

print(f"  ATR(14) طلا M15        = {ATR_M15:.2f} قیمت")
print(f"  ضریب floor             = 4.0")
print(f"  ─────────────────────────────────────────────")
print(f"  محاسبه معیوب (× Point) = {atr_broken:.2f}  ({atr_broken/POINT:.0f} پوینت)   ❌")
print(f"  محاسبه درست            = {atr_fixed:.2f}  ({atr_fixed/POINT:.0f} پوینت)   ✅")
print(f"  نسبت                   = {atr_fixed/atr_broken:.0f} برابر")
print()
print(f"  MathMin(0.20, {atr_fixed:.2f}) = {min(0.20, atr_fixed):>6.2f}   ← کف به سقف تبدیل می‌شود ❌")
print(f"  MathMax(0.20, {atr_fixed:.2f}) = {max(0.20, atr_fixed):>6.2f}   ← کف واقعی ✅")

fixed_sl = sl_floor(BROKEN_SL, ATR_M15, 4.0)
print()
print(f"  خروجی GF_SLFloor       = {fixed_sl:.2f}  ({fixed_sl/POINT:.0f} پوینت)")

h("۲) اسپرد در برابر حد ضرر")

sp = spread_price()
print(f"  اسپرد زنده             = {sp:.2f} قیمت = {sp/POINT:.0f} پوینت")
print(f"  بدترین اسپرد پنل       = {MAX_SPREAD} پوینت = {MAX_SPREAD*POINT:.2f} قیمت")
print()
print(f"  {'حد ضرر':<26}{'اندازه':<14}{'سهم اسپرد':<14}")
print(f"  {'─'*54}")
for slp, label in [(BROKEN_SL, "معیوب (فعلی)"),
                   (min_stop_distance(), "کف ایمنی GF"),
                   (fixed_sl, "اصلاح ATR")]:
    pct = sp / slp * 100
    flag = "❌" if pct > 100 else ("⚠" if pct > 20 else "✅")
    print(f"  {label:<26}{slp/POINT:>6.0f} پوینت   {pct:>6.0f}%{flag:>6}")

h("۳) نسبت ریسک به ریوارد")

for slp, label in [(BROKEN_SL, "معیوب"), (fixed_sl, "اصلاح‌شده")]:
    print(f"  {label:<12} SL={slp:>6.2f}  TP={TP_PRICE:>6.2f}   R:R = 1 : {TP_PRICE/slp:>6.0f}")

h("۴) حجم بر پایه ریسک (۱٪ از ۱۰٬۰۰۰ دلار)")

print(f"  {'حد ضرر':<26}{'حجم':<12}{'ریسک واقعی'}")
print(f"  {'─'*54}")
for slp, label in [(BROKEN_SL, "معیوب (۲۰ پوینت)"),
                   (min_stop_distance(), "کف ایمنی GF"),
                   (fixed_sl, "اصلاح ATR")]:
    lots = lots_for_risk(1.0, slp)
    actual = lots * slp / TICKSIZE * TICKVALUE
    print(f"  {label:<26}{lots:>6.2f} لات   ${actual:>9,.2f}")

print()
print(f"  ⚠ باگ حد ضرر، اهرم را {lots_for_risk(1.0, BROKEN_SL)/lots_for_risk(1.0, fixed_sl):.0f} برابر اشتباه محاسبه می‌کرد.")

h("۵) نرمال‌سازی حجم")

for v in (0.037, 0.0149999, 0.001, 250.0):
    print(f"  {v:>12} -> {normalize_lots(v):>7} لات")

h("۶) فاصله سربه‌سر")

print(f"  BE فعلی شما            = 0.00    ❌ سربه‌سر روی سود صفر")
print(f"  GF_BreakEvenDistance   = {break_even_distance():.2f} قیمت = {break_even_distance()/POINT:.0f} پوینت   ✅")
print()
print("  در اسکرین‌شات سودها +۰.۱۲ / +۰.۱۶ / +۰.۰۷ دلار بودند —")
print("  یعنی پوزیشن‌ها بعد از ۷ تا ۱۷ پوینت بسته شدند، نه ۵۶ پوینت.")
print("  این امضای دقیق BE روی صفر است.")

h("۷) تأخیر ورود")

entries = ["13:15:00", "13:15:01", "13:15:01",
           "13:45:01", "13:45:01", "13:45:01",
           "14:15:00", "14:15:00", "14:15:01"]
print("  زمان ورود هر ۹ سفارش:")
for e in entries:
    sec = int(e.split(":")[2])
    mark = "◄ مرز کندل" if sec <= 1 else ""
    print(f"    {e}   {mark}")
print()
print("  هر ۹ سفارش در ثانیه ۰ یا ۱ بعد از مرز M15 — تصادفی نیست.")
