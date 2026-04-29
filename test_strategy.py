"""
测试简单跟随策略和止损暂停重置功能
"""

import sys
sys.path.insert(0, '/workspace')

# 模拟测试场景
def test_follow_strategy():
    """测试跟随策略逻辑"""
    print("=" * 60)
    print("测试 1: 跟随策略")
    print("=" * 60)
    
    # 模拟历史数据
    test_cases = [
        ([1, 0, 1, 0, 1], "10101", 0, "6 位纯交替，反向打破"),
        ([0, 1, 0, 1, 0], "01010", 1, "6 位纯交替，反向打破"),
        ([1, 1, 0, 1, 1], "11011", 1, "跟随上一手"),
        ([0, 0, 1, 0, 0], "00100", 0, "跟随上一手"),
        ([1], "1", 1, "历史不足 5 手，跟随"),
        ([], "无", 1, "无历史，默认下大"),
    ]
    
    for history, expected_pattern, expected_bet, description in test_cases:
        if len(history) >= 5:
            last_5 = "".join(str(x) for x in history[-5:])
            if last_5 in ("10101", "01010"):
                prediction = 1 - history[-1]
            else:
                prediction = history[-1]
        elif len(history) > 0:
            prediction = history[-1]
        else:
            prediction = 1
        
        status = "✅" if prediction == expected_bet else "❌"
        print(f"{status} 历史={history} → 下注={'大' if prediction==1 else '小'} ({description})")
    
    print()


def test_pause_reset():
    """测试止损暂停重置逻辑"""
    print("=" * 60)
    print("测试 2: 止损暂停重置")
    print("=" * 60)
    
    initial_amount = 20000
    lose_stop = 6
    lose_count = 0
    bet_amount = initial_amount
    round_num = 0
    
    # 模拟连输 6 局
    while round_num < 15:
        round_num += 1
        
        if bet_amount <= 0:
            # 触发暂停
            print(f"第{round_num}局：触发暂停，等待 10 局后重置")
            for pause_round in range(10):
                print(f"  暂停第{pause_round+1}局...")
            
            # 重置
            lose_count = 0
            bet_amount = initial_amount
            print(f"  重置：lose_count=0, bet_amount={bet_amount:,}")
            continue
        
        # 模拟下注
        if round_num <= 6:
            # 前 6 局输
            lose_count += 1
            bet_amount = 0 if (lose_count + 1) > lose_stop else bet_amount * 1.5
            result = "输"
        else:
            # 第 7 局开始赢
            lose_count = 0
            bet_amount = initial_amount
            result = "赢"
        
        status = "❌" if result == "输" else "✅"
        print(f"{status} 第{round_num}局：下注={bet_amount:,.0f}, 连输={lose_count}, 结果={result}")
    
    print()


if __name__ == "__main__":
    test_follow_strategy()
    test_pause_reset()
    print("所有测试完成!")
