# Phase 34: BOOM Spike Analysis & Strategy

**Date**: 2026-05-21  
**Commit**: `9c87ef6`  
**Data basis**: 1937 trades total · 139 spike events  
**Status**: DEPLOYED ✅

---

## BOOM Trade Stats (all-time)

| Symbol   | n   | WR    | total_pnl | avg_dur | spike_tp | timeout | key exits |
|----------|-----|-------|-----------|---------|----------|---------|-----------|
| BOOM600  | 174 | 30.4% | -3.77     | 266.5s  | 44       | 105     | manual:22, spike_timeout:1 |
| BOOM900  | 169 | 21.2% | -12.59    | 298.7s  | 26       | 114     | manual:23, spike_timeout:2 |
| BOOM1000 | 75  | 33.3% | -1.24     | 256.9s  | 15       | 11      | spike_timeout:38, manual:8 |
| BOOM500  | 55  | 14.5% | -11.33    | 0.0s    | 0        | 0       | spike_timeout:43 |

## CRASH Trade Stats (all-time)

| Symbol   | n   | WR    | total_pnl | avg_dur | spike_tp | timeout |
|----------|-----|-------|-----------|---------|----------|---------|
| CRASH600 | 154 | 21.2% | -5.88     | 203.9s  | 28       | 93      |
| CRASH900 | 284 | 17.1% | -12.33    | 159.6s  | 32       | 156     |
| CRASH500 | 276 | 19.7% | -51.00    | 130.8s  | 42       | 0       |
| CRASH1000| 218 | 13.3% | -25.84    | 155.8s  | 23       | 0       |

---

## Root Cause Found: BOOM900 DPM Too Short (Phase 34 Fix)

**Same pattern as Phase 33 (CRASH600/900):**

| Symbol  | DPM (before) | avg spike interval | timeout losses | verdict          |
|---------|-------------|-------------------|----------------|------------------|
| BOOM900 | 250s        | **297s**          | 114/169 (67%)  | CRITICAL → fixed |
| BOOM600 | 250s        | 154s              | 105/174 (60%)  | OK-SUFFICIENT    |
| CRASH600| 180s→450s   | 226s              | 14/14 (100%)   | Fixed Phase 33   |
| CRASH900| 150s→350s   | 172s              | 15/15 (100%)   | Fixed Phase 33   |

**Fix**: `BOOM900 max_duration_seg: 250 → 450s`

- timeout_avg was 338s — trades dying at 250s before avg spike arrives at 297s  
- spike_tp timing: avg=189s, max=628s → 250s was cutting off 33% of wins
- 26 spike_tp wins avg +0.42 each (total +10.15) vs 114 timeout losses

---

## BOOM Spike Events Analysis

**Spike counts**: 54 BOOM · 85 CRASH (139 total)

### BOOM600 (28 spikes)
- entered=3, blocked=3, null=22  
- Ratio avg=225x, min=14x, max=837x
- **3 SPIKE_CYCLE_GATE blocks**: 320x (23s), 322x (6s), 837x (24s)  
  — all cluster burst cases (spike within 24s of prior spike)
- DPM verdict: **OK-SUFFICIENT** (avg interval 154s < 250s DPM)

### BOOM900 (16 spikes)
- entered=3, blocked=2, null=11
- Ratio avg=563x, min=83x, max=2326x  
- **2 SPIKE_CYCLE_GATE blocks**: 234x (24s), 722x (24s)  
  — cluster burst cases right at 24s threshold  
- DPM verdict: **CRITICAL-TOO-SHORT** → **fixed to 450s**

### BOOM1000 (17 spikes)
- entered=0 (!!), blocked=5, null=12  
- Ratio avg=299x, max=1108x
- **5 SPIKE_CYCLE_GATE blocks** — all using 80s threshold (8% of 1000s)
- No DPM (None) — uses max_hold=900s
- Problem: 0 entered, all blocked or null → investigation needed

---

## SPIKE_CYCLE_GATE Analysis

**Config bug**: BOOM600 and BOOM900 use `300s` cycle period (should be 600s/900s).  
Current gate: `8% × 300s = 24s` min cooldown.  
Correct gate would be: `8% × 600s = 48s` / `8% × 900s = 72s` (more restrictive).  

**Decision**: Leave as-is — shorter gate is more permissive, helping spike capture.  
If we fixed the interval, the gate would be 2-3x longer, blocking MORE entries.

Code in `deriv_risk.py`:
```python
_spike_gate_interval = (
    1000 if "1000" in _su_sg
    else (500 if "500" in _su_sg else 300)  # BUG: 600/900 fall to 300
)
```

---

## Score Breakdown WIN vs LOSS (BOOM symbols)

### BOOM600 (W=52, L=122)
- score_raw: WIN=6.02 vs LOSS=6.45 — LOSSES have HIGHER scores (paradox)
- WIN strategies: SMC_Liquidity_Inbound:6, others:46
- LOSS strategies: SMC_Liquidity_Inbound:18, others:104

### BOOM900 (W=37, L=132)  
- score_raw: WIN=5.92 vs LOSS=5.77 — almost identical
- WIN strategies: only `?` (37/37) — no SMC_Liquidity_Inbound wins
- LOSS strategies: SMC_Liquidity_Inbound:11, others:121

### Key insight: BOOM losses have equal or higher scores than wins
- Score is NOT differentiating win vs loss for BOOM — timing (DPM) is the issue
- Fixing DPM (holding longer for spike) is more impactful than score tuning

---

## DPM Sufficiency Table (Phase 34 state)

| Symbol   | DPM  | avg_spike_int | verdict        |
|----------|------|---------------|----------------|
| BOOM600  | 250s | 154s          | OK-SUFFICIENT  |
| BOOM900  | **450s** | 297s      | **FIXED** ✅   |
| BOOM1000 | None | 253s          | N/A (max_hold) |
| CRASH600 | 450s | 181s          | OK-SUFFICIENT  |
| CRASH900 | 350s | 188s          | OK-SUFFICIENT  |
| CRASH500 | 720s | 276s          | OK-SUFFICIENT  |
| CRASH1000| None | 137s          | N/A (max_hold) |

---

## spike_active_override: BOOM Compatible ✅

The override in `deriv_risk.py` is direction-agnostic:
- Checks: `_is_spike AND _spike_just_fired AND _bc_escape_active`
- No CRASH-only restriction — works for BOOM (direction="rise") too
- Already handles both MULTUP (BOOM) and MULTDOWN (CRASH) symmetrically

---

## Pending Issues (Lower Priority)

1. **BOOM1000: 0 entered** — all 17 spikes in spike_events never led to entry.  
   DPM=None (uses max_hold=900s). May need investigation of SPIKE_CYCLE_GATE  
   80s threshold and whether `_is_spike` correctly fires for BOOM1000.

2. **since_last_trade_s timestamp bug** — values like 1779388852s (~56 years).  
   ms vs s conversion bug in spike event writer. Affects logging only, not trades.

3. **Duplicate spike events** — same spike appears 2-3x with same ratio but different  
   `since_last` values. Cosmetic issue in spike_events.json logging.

4. **BOOM600 high timeout volume** — 105/174 (60%) timeout without spike.  
   These are genuinely no-spike windows, DPM extension would just extend losses.  
   Consider: tighter entry filters to reduce no-spike entries.

---

## Deployment

- **Commit**: `9c87ef6` pushed to `main`  
- **Build**: `docker build -f Dockerfile.deriv -t deriv_bot:9c87ef6 .` on server  
- **Container**: `o4w1ns4cceccmn2ozqt7sol2-9c87ef6`  
- **Confirmed**: `[DPM_CONFIG] BOOM900 dur=450s` ✅ in startup logs
