#!/usr/bin/env python3
"""模拟测试：10100/01011 同向下注逻辑"""

FIXED_PATTERNS = {
    "010101": {"follow": "reverse", "label": "交替循环反转"},
    "101010": {"follow": "reverse", "label": "交替循环反转"},
    "111111": {"follow": "1", "label": "大龙延续"},
    "000000": {"follow": "0", "label": "小龙延续"},
    "00101": {"follow": "reverse", "label": "00101反向下注"},
    "11010": {"follow": "reverse", "label": "11010反向下注"},
    "001010": {"follow": "same", "label": "001010同向下注"},
    "110101": {"follow": "same", "label": "110101同向下注"},
    "10100": {"follow": "same", "duration": 2, "label": "10100 后续 2 次同向"},
    "01011": {"follow": "same", "duration": 2, "label": "01011 后续 2 次同向"},
}


def _detect_fixed_pattern_signal(history):
    if not isinstance(history, list) or len(history) < 5:
        return {"active": False}
    history_str = "".join(str(x) for x in history)
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
    forced_remaining = rt.get("forced_bet_remaining", 0)
    forced_direction = rt.get("forced_bet_direction", 0)
    
    if forced_remaining > 0:
        rt["forced_bet_remaining"] = forced_remaining - 1
        return forced_direction, "强制延续 (剩{})".format(rt["forced_bet_remaining"]), "forced"

    fixed = _detect_fixed_pattern_signal(history)
    if fixed.get("active"):
        duration = fixed.get("duration", 1)
        if duration > 1:
            rt["forced_bet_remaining"] = duration - 1
            rt["forced_bet_direction"] = fixed["prediction"]
            return fixed["prediction"], fixed["label"], "fixed_forced"
        else:
            return fixed["prediction"], fixed["label"], "fixed"
    
    if len(history) >= 5:
        last_5 = "".join(str(x) for x in history[-5:])
        if last_5 in ("10101", "01010"):
            pred = 1 - history[-1]
            return pred, f"5 位交替{last_5}反向", "alternation"
    
    if history:
        return history[-1], "跟随上一手", "follow"
    
    return 1, "无历史默认大", "default"


def simulate(history_sequence, description=""):
    print(f"\n{'='*70}")
    print(f"测试: {description}")
    print(f"序列: {' '.join(str(x) for x in history_sequence)}\n")
    
    rt = {"forced_bet_remaining": 0, "forced_bet_direction": 0}
    
    for i in range(len(history_sequence)):
        hist = history_sequence[:i]
        actual = history_sequence[i]
        
        prev_forced = rt.get("forced_bet_remaining", 0)
        pred, label, source = get_prediction(hist, rt)
        
        match = pred == actual
        m = "✓" if match else "✗"
        pt = "大" if pred == 1 else "小"
        at = "大" if actual == 1 else "小"
        
        forced_status = f" [强: {rt.get('forced_bet_remaining', 0)}]" if rt.get("forced_bet_remaining", 0) > 0 or prev_forced > 0 else ""
        
        print(f"  第{i+1:2d}手: {pt} -> {at} {m} [{label}]{forced_status}")


print("10100/01011 同向逻辑测试")
print("="*70)

# Case 1: 10100 (Ends 0) -> Predict 0 (Same). Forced 1 more.
# Sequence: 1 0 1 0 0 -> Pred 0.
simulate([1, 0, 1, 0, 0, 0, 0], "10100 触发：后续 2 次同向（即下注 0）")

# Case 2: 01011 (Ends 1) -> Predict 1 (Same). Forced 1 more.
# Sequence: 0 1 0 1 1 -> Pred 1.
simulate([0, 1, 0, 1, 1, 1, 1], "01011 触发：后续 2 次同向（即下注 1）")

# Case 3: 10100 -> Forced Same (0).
# Next comes 01011 (Ends 1).
# Logic: 10100 sets forced_same=0 (rem 1).
# Next bet is forced 0. Actual is 0.
# History now: ... 0 0 0.
# If next history is 0 0 0 1 0 1 1 -> 01011 detected?
# Yes, if forced status clears or if priority handles it.
# Since forced_remaining is handled in Priority 0, it runs FIRST.
simulate([1, 0, 1, 0, 0, 0, 1, 1, 1], "10100 后接 01011，强制同向优先级测试")
