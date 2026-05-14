#!/usr/bin/env python3
"""完整下注逻辑模拟 - 冲突检测版"""

FIXED_PATTERNS = {
    "010101": {"follow": "reverse", "label": "交替循环反转"},
    "101010": {"follow": "reverse", "label": "交替循环反转"},
    "111111": {"follow": "1", "label": "大龙延续"},
    "000000": {"follow": "0", "label": "小龙延续"},
    "00101": {"follow": "reverse", "label": "00101反向下注"},
    "11010": {"follow": "reverse", "label": "11010反向下注"},
    "001010": {"follow": "same", "label": "001010同向下注"},
    "110101": {"follow": "same", "label": "110101同向下注"},
    "10100": {"follow": "reverse", "duration": 2, "label": "10100 后续 2 次反向"},
    "01011": {"follow": "reverse", "duration": 2, "label": "01011 后续 2 次反向"},
}


def _detect_fixed_pattern_signal(history):
    if not isinstance(history, list) or len(history) < 5:
        return {"active": False}
    history_str = "".join(str(x) for x in history)
    # Iterate patterns
    for pattern, config in FIXED_PATTERNS.items():
        pattern_len = len(pattern)
        if len(history) < pattern_len:
            continue
        recent_seq = history_str[-pattern_len:]
        if recent_seq == pattern:
            follow = config["follow"]
            latest = int(history[-1])
            if follow == "reverse":
                pred = 1 - latest
            elif follow == "same":
                pred = latest
            elif len(follow) == 1:
                pred = int(follow)
            else:
                pred = latest
            return {"active": True, "detected_seq": recent_seq,
                    "follow_pattern": follow, "label": config["label"],
                    "prediction": pred, "duration": config.get("duration", 1)}
    return {"active": False}


def get_prediction(history, rt):
    # Priority 0: Forced continuation
    forced_remaining = rt.get("forced_bet_remaining", 0)
    forced_direction = rt.get("forced_bet_direction", 0)
    
    if forced_remaining > 0:
        rt["forced_bet_remaining"] = forced_remaining - 1
        return forced_direction, "强制延续 (剩{})".format(rt["forced_bet_remaining"]), "forced"

    # Priority 1: Fixed patterns
    fixed = _detect_fixed_pattern_signal(history)
    if fixed.get("active"):
        duration = fixed.get("duration", 1)
        if duration > 1:
            # Set forced state for NEXT bets (current bet is handled here)
            rt["forced_bet_remaining"] = duration - 1
            rt["forced_bet_direction"] = fixed["prediction"]
            return fixed["prediction"], fixed["label"], "fixed_forced"
        else:
            return fixed["prediction"], fixed["label"], "fixed"
    
    # Priority 2: 5-bit Alternation break
    if len(history) >= 5:
        last_5 = "".join(str(x) for x in history[-5:])
        if last_5 in ("10101", "01010"):
            pred = 1 - history[-1]
            return pred, f"5 位交替{last_5}反向", "alternation"
    
    # Priority 3: Follow last
    if history:
        return history[-1], "跟随上一手", "follow"
    
    return 1, "无历史默认大", "default"


def simulate(history_sequence, description="", initial_amount=500):
    print(f"\n{'='*70}")
    print(f"测试: {description}")
    print(f"序列: {' '.join(str(x) for x in history_sequence)}\n")
    
    rt = {"forced_bet_remaining": 0, "forced_bet_direction": 0}
    
    for i in range(len(history_sequence)):
        hist = history_sequence[:i]
        actual = history_sequence[i]
        
        pred, label, source = get_prediction(hist, rt.copy()) # Copy rt to avoid mutation in check? No, need mutation
        # Re-call to ensure state changes are reflected?
        # Actually get_prediction modifies rt.
        # Let's use a fresh rt for logic check if we want to debug, 
        # but for simulation we must mutate rt.
        # But wait, passing rt.copy() above prevents mutation for the NEXT step.
        # I need to pass rt directly.
    
    # Redo simulation with correct state mutation
    rt = {"forced_bet_remaining": 0, "forced_bet_direction": 0}
    
    for i in range(len(history_sequence)):
        hist = history_sequence[:i]
        actual = history_sequence[i]
        
        # We need to see what state rt was BEFORE prediction
        prev_forced = rt.get("forced_bet_remaining", 0)
        
        pred, label, source = get_prediction(hist, rt)
        
        match = pred == actual
        m = "✓" if match else "✗"
        pt = "大" if pred == 1 else "小"
        at = "大" if actual == 1 else "小"
        
        forced_status = f" [强: {rt.get('forced_bet_remaining', 0)}]" if rt.get("forced_bet_remaining", 0) > 0 or prev_forced > 0 else ""
        
        print(f"  第{i+1:2d}手: {pt} -> {at} {m} [{label}]{forced_status}")


# Test Cases
print("冲突与逻辑检测模拟")
print("="*70)

# Case 1: 10100 trigger -> 2 bets forced reverse
# Sequence: 1 0 1 0 0 -> Predict 1 (reverse of 0)
simulate([1, 0, 1, 0, 0, 1, 0], "10100 触发：后续 2 次反向")
# Logic:
# 1 0 1 0 0 (ends 0) -> Detect 10100. Pred=1. Duration=2. Set forced=1.
# Next (1): Pred=1 (forced). Actual 1. Match. Forced becomes 0.
# Next (0): Pred=0 (Follow). Actual 0. Match.

# Case 2: 01011 trigger
simulate([0, 1, 0, 1, 1, 0, 1], "01011 触发：后续 2 次反向")

# Case 3: Conflict with 10101 (Alternation)
# Sequence: 1 0 1 0 1 -> Alternation break predicts 0 (Reverse)
# If this was preceded by 10100?
# 1 0 1 0 0 -> Predict 1 (Forced).
# Next: 0 1 0 1 0 1 -> 10101 break?
# History: 1 0 1 0 0 0 1 0 1 0 1
simulate([1, 0, 1, 0, 0, 0, 1, 0, 1, 0, 1], "10100 后出现 10101 交替")

# Case 4: Conflict with 00101 (Fixed Pattern)
# 0 0 1 0 1 -> Predict 0 (Reverse).
# Preceded by 10100?
# 1 0 1 0 0 -> Predict 1 (Forced).
# History: 1 0 1 0 0 0 0 1 0 1
simulate([1, 0, 1, 0, 0, 0, 0, 1, 0, 1], "10100 后出现 00101")

# Case 5: 10100 followed by 111111 (Dragon)
# 1 0 1 0 0 -> Predict 1.
# Actuals: 1 1 1 1 1 1 -> 6 Dragon.
# History: 1 0 1 0 0 1 1 1 1 1 1
simulate([1, 0, 1, 0, 0, 1, 1, 1, 1, 1, 1], "10100 后接 111111 长龙")
