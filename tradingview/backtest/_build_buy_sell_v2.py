"""
Build buy_sell_v2.pine — a clean clone of MyStrategyPositiveBuyOnlyAlert
with a significantly enhanced sell section.

Key design principle:
  - BUY triggers first (same as original)
  - canTriggerSell gate: sell can only fire AFTER a buy — no noise
  - SELL is now multi-condition (ATR stop, MA break, MACD cross, RSI decay, S-zone break)
  - Entry price tracked at buy time for ATR-stop calculation
"""
import json, os

_ROOT = os.path.dirname(os.path.dirname(__file__))
SRC_JSON = os.path.join(_ROOT, "pine_sources.json")
OUT_FILE = os.path.join(_ROOT, "pine", "buy_sell_v2.pine")

# ---------------------------------------------------------------------------
# Load original source
# ---------------------------------------------------------------------------
with open(SRC_JSON, encoding="utf-8") as f:
    sources = json.load(f)

base_src: str = sources["WIN_SR_SM_COMBINED"]["source"]  # MyStrategyPositiveBuyOnlyAlert

# Normalise Windows line endings
base_src = base_src.replace("\r\n", "\n")

# ---------------------------------------------------------------------------
# Locate the splice point — everything up to and including the two var bools
# (canTriggerBuy / canTriggerSell declarations) is kept as-is.
# We then replace the rest (sell logic + alerts) with our enhanced version.
# ---------------------------------------------------------------------------
SPLICE_MARKER = "var bool canTriggerSell = false"
idx = base_src.find(SPLICE_MARKER)
if idx == -1:
    raise RuntimeError("Splice marker not found — source may have changed.")

# Keep header up through the canTriggerSell declaration
kept = base_src[: idx + len(SPLICE_MARKER)]

# Change strategy name & ID so TradingView treats it as a new script
kept = kept.replace(
    'strategy("SELL strategy", \'MyStrategySellOnlyAlert\'',
    'strategy("BuySell Enhanced v2", \'MyStrategyBuySellV2\'',
)
kept = kept.replace(
    "strategy(\"MyStrategyPositiveBuyOnlyAlert\", 'MyStrategyPositiveBuyOnlyAlert'",
    "strategy(\"BuySell Enhanced v2\", 'MyStrategyBuySellV2'",
)
# The actual script title varies — just patch any strategy(" call that still has the old name
import re
kept = re.sub(
    r'strategy\(".*?",\s*\'MyStrategyPositiveBuyOnlyAlert\'',
    "strategy(\"BuySell Enhanced v2\", 'MyStrategyBuySellV2'",
    kept,
)

# ---------------------------------------------------------------------------
# Enhanced sell section (appended after the splice point)
# ---------------------------------------------------------------------------
ENHANCED = r"""

// =============================================================================
// SELL SETTINGS  ➞ Enhanced multi-condition exit (gated by canTriggerSell)
// NOTE: triggerBuy is already computed above in the base code (kept as-is).
//       cond_down  is already computed above in the base code (kept as-is).
// =============================================================================
string SELL_GRP = "➞ Enhanced Sell Settings 🔻"

float atrStopMult  = input.float(2.0,  "ATR Hard Stop (× ATR)",     minval=0.5, maxval=5.0, step=0.1, group=SELL_GRP,
     tooltip="Sell immediately if price falls this many ATRs below entry price.")
float atrTrailMult = input.float(1.5,  "ATR Trailing Stop (× ATR)", minval=0.5, maxval=5.0, step=0.1, group=SELL_GRP,
     tooltip="Trailing stop: sell if close drops this many ATRs below the highest close since entry.")
int   rsiExitLevel = input.int(45,     "RSI Exit Level",            minval=20,  maxval=65, group=SELL_GRP,
     tooltip="Sell when RSI falls below this level AND is declining.")
bool  useMaBreak   = input.bool(true,  "MA Breakdown Sell",         group=SELL_GRP,
     tooltip="cond_down: MA turning down, price closed below MA open & close.")
bool  useMacdCross = input.bool(true,  "MACD Bearish Cross Sell",   group=SELL_GRP,
     tooltip="Sell when MACD line crosses below signal line.")
bool  useRsiDecay  = input.bool(true,  "RSI Decay Sell",            group=SELL_GRP)
bool  useAtrStop   = input.bool(true,  "ATR Hard Stop Sell",        group=SELL_GRP)
bool  useAtrTrail  = input.bool(true,  "ATR Trailing Stop Sell",    group=SELL_GRP)
bool  useSuppBreak = input.bool(false, "Support Zone Break Sell",   group=SELL_GRP,
     tooltip="Sell when price breaks below current S-zone. Keep OFF in choppy markets.")

// =============================================================================
// Entry tracking  (updated when the BUY alert fires each cycle)
// =============================================================================
var float entryPriceRef = na    // close price when last BUY fired
var float hwmSinceEntry = na    // highest close since entry (trailing HWM)

// =============================================================================
// ATR for stop calculations
// =============================================================================
float atr14 = ta.atr(14)

// =============================================================================
// Sell sub-conditions  (all false when canTriggerSell = false, so no noise)
// =============================================================================

// 1. MA breakdown — original cond_down logic (already computed above in base)
sell_ma_breakdown = useMaBreak and cond_down

// 2. MACD bearish crossunder
sell_macd_cross = useMacdCross and ta.crossunder(macdVal, signalVal)

// 3. RSI decay — RSI falling below exit level
sell_rsi_decay = useRsiDecay and rsi < rsiExitLevel and rsi < rsi[3]

// 4. ATR hard stop from entry price
sell_atr_stop  = useAtrStop and (not na(entryPriceRef)) and close < entryPriceRef - atrStopMult * atr14

// 5. ATR trailing stop from high-water-mark
sell_atr_trail = useAtrTrail and (not na(hwmSinceEntry)) and close < hwmSinceEntry - atrTrailMult * atr14

// 6. Support zone break (optional, OFF by default)
float _s_bot = S.size() > 0 ? S.get(0).bx.get_bottom() : na
float _s_mrg = S.size() > 0 ? S.get(0).m : 0.02
sell_supp_break = useSuppBreak and (not na(_s_bot)) and close < _s_bot * (1.0 - _s_mrg * 0.17) and close < close[1]

// Combined: any sub-condition triggers sell (order matters for label priority)
enhanced_sell = sell_atr_stop or sell_atr_trail or sell_ma_breakdown or sell_macd_cross or sell_rsi_decay or sell_supp_break

// Human-readable reason label (highest-priority first)
string sell_reason =
     sell_atr_stop    ? "🛑 ATR Hard Stop"     :
     sell_atr_trail   ? "📉 ATR Trail Stop"    :
     sell_ma_breakdown? "📊 MA Breakdown"      :
     sell_macd_cross  ? "⚡ MACD Cross"        :
     sell_rsi_decay   ? "📛 RSI Decay"         :
     sell_supp_break  ? "🔻 Support Break"     :
                        "SELL"

// =============================================================================
// State machine  (buy-first, then sell enabled — identical gate to original)
// =============================================================================
plot_buy  = false   // reset every bar
plot_sell = false   // reset every bar

// BUY — triggerBuy is computed above in base code, gate is canTriggerBuy
if triggerBuy and not triggerBuy[1] and canTriggerBuy
    plot_buy       := true
    canTriggerBuy  := false
    canTriggerSell := true      // unlock sell for next trigger
    entryPriceRef  := close     // record entry for ATR-stop
    hwmSinceEntry  := close     // reset trailing HWM
    srBuySignal    := false
    log.info("BUY fired: " + syminfo.ticker +
             " price=" + str.tostring(close, format.mintick) +
             " rsi="   + str.tostring(rsi, "#.0") +
             " macd="  + str.tostring(macdAboveSignal))

// Keep HWM updated while we're in a trade (canTriggerSell = true)
if canTriggerSell and not na(hwmSinceEntry)
    hwmSinceEntry := math.max(hwmSinceEntry, close)

// SELL — enhanced_sell, gated by canTriggerSell
if enhanced_sell and not enhanced_sell[1] and canTriggerSell
    plot_sell      := true
    canTriggerSell := false     // lock sell until next buy
    canTriggerBuy  := true      // allow next buy
    entryPriceRef  := na        // reset entry tracking
    hwmSinceEntry  := na
    log.info("SELL fired [" + sell_reason + "]: " + syminfo.ticker +
             " price=" + str.tostring(close, format.mintick) +
             " rsi="   + str.tostring(rsi, "#.0"))

// =============================================================================
// Visual shapes
// =============================================================================
plotshape(plot_buy,
     title    = "BUY Signal",
     location = location.belowbar,
     color    = color.rgb(0, 200, 100),
     style    = shape.labelup,
     size     = size.small,
     text     = "BUY",
     textcolor= color.white)

plotshape(plot_sell,
     title    = "SELL Signal",
     location = location.abovebar,
     color    = color.rgb(240, 40, 40),
     style    = shape.labeldown,
     size     = size.small,
     text     = "SELL",
     textcolor= color.white)

// Sell reason label (small, appears above bar)
if plot_sell
    label.new(bar_index, high * 1.003, sell_reason,
         style    = label.style_label_down,
         color    = color.new(color.red, 60),
         textcolor= color.white,
         size     = size.tiny)

// Draw ATR stop levels while in trade (visualise where stops sit)
plot(canTriggerSell and useAtrStop and not na(entryPriceRef) ?
         entryPriceRef - atrStopMult * atr14 : na,
     title="ATR Hard Stop", color=color.new(color.red, 30),
     linewidth=1, style=plot.style_linebr)

plot(canTriggerSell and useAtrTrail and not na(hwmSinceEntry) ?
         hwmSinceEntry - atrTrailMult * atr14 : na,
     title="ATR Trail Stop", color=color.new(color.orange, 30),
     linewidth=1, style=plot.style_linebr)

// =============================================================================
// Date filter & alerts
// =============================================================================
oneYearAgo  = timenow - 365 * 24 * 60 * 60 * 1000
isInLastYear= time >= oneYearAgo and time <= timenow

if plot_buy and isInLastYear
    alert("🚀 BUY: "  + syminfo.ticker +
          " | tf="    + timeframe.period +
          " | price=" + str.tostring(close, format.mintick) +
          " | rsi="   + str.tostring(rsi, "#.0") +
          " | sqzOff="+ str.tostring(sqzOff))

if plot_sell and isInLastYear
    alert("🔴 SELL [" + sell_reason + "]: " + syminfo.ticker +
          " | tf="     + timeframe.period +
          " | price="  + str.tostring(close, format.mintick) +
          " | rsi="    + str.tostring(rsi, "#.0") +
          " | entry="  + str.tostring(nz(entryPriceRef, close), format.mintick))

// alertcondition — set once in TradingView, fires per trigger
alertcondition(plot_buy,                       "BUY Signal",           "🚀 BUY {{ticker}} {{interval}} price={{close}}")
alertcondition(plot_sell,                      "SELL Signal",          "🔴 SELL {{ticker}} {{interval}} price={{close}}")
alertcondition(plot_sell and sell_atr_stop,    "SELL – ATR Hard Stop", "🛑 ATR Stop: {{ticker}} price={{close}}")
alertcondition(plot_sell and sell_atr_trail,   "SELL – ATR Trailing",  "📉 Trail Stop: {{ticker}} price={{close}}")
alertcondition(plot_sell and sell_ma_breakdown,"SELL – MA Breakdown",  "📊 MA Break: {{ticker}} price={{close}}")
alertcondition(plot_sell and sell_macd_cross,  "SELL – MACD Cross",    "⚡ MACD Cross: {{ticker}} price={{close}}")
alertcondition(plot_sell and sell_rsi_decay,   "SELL – RSI Decay",     "📛 RSI Decay: {{ticker}} price={{close}}")
"""

# ---------------------------------------------------------------------------
# Assemble and write
# ---------------------------------------------------------------------------
final = kept + "\n" + ENHANCED

with open(OUT_FILE, "w", encoding="utf-8") as f:
    f.write(final)

lines = final.count("\n") + 1
print(f"Written: {OUT_FILE}")
print(f"  Lines : {lines}")
print(f"  Chars : {len(final):,}")
print()
print("=== BUY condition (unchanged) ===")
print("  triggerBuy = macdAboveSignal AND rsi>=63 AND ma_up AND (BUY or LongTrade or srBuySignal) AND percent_valid")
print("  canTriggerBuy starts TRUE; flips to FALSE after buy fires (prevents duplicate buys)")
print()
print("=== SELL conditions (enhanced, gated by canTriggerSell) ===")
print("  1. ATR Hard Stop  : close < entryPrice - 2.0×ATR14")
print("  2. ATR Trail Stop : close < highWaterMark - 1.5×ATR14")
print("  3. MA Breakdown   : ma_down AND out1>close AND out1>open AND open>close  (original cond_down)")
print("  4. MACD Cross     : ta.crossunder(macdVal, signalVal)")
print("  5. RSI Decay      : rsi < 45 AND rsi < rsi[3]  (falling below exit level)")
print("  6. Support Break  : close < S-zone bottom  (optional, off by default)")
print()
print("  All gated: only fires when canTriggerSell=true (i.e., after a BUY has fired)")
print("  entryPriceRef resets when sell fires; HWM resets on new buy")
print()
print("=== 7 alertcondition()s ===")
print("  BUY Signal, SELL Signal, SELL-ATR Stop, SELL-ATR Trail, SELL-MA Break, SELL-MACD Cross, SELL-RSI Decay")
