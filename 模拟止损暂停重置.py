"""
模拟连输止损后暂停 10 局重置功能
"""

def calculate_bet_amount(win_count, lose_count, initial_amount, lose_stop, 
                         lose_once=3.0, lose_twice=2.5, lose_three=2.2, lose_four=2.1):
    if win_count >= 0 and lose_count == 0:
        return initial_amount
    if (lose_count + 1) > lose_stop:
        return 0
    base_amount = initial_amount
    if lose_count == 1:
        target = base_amount * lose_once
    elif lose_count == 2:
        target = base_amount * lose_twice
    elif lose_count == 3:
        target = base_amount * lose_three
    else:
        target = base_amount * lose_four
    return round(target * 1.01 / 500) * 500


def simulate_with_pause(initial_amount=20000, lose_stop=6, start_balance=500000):
    balance = start_balance
    win_count = 0
    lose_count = 0
    total_round = 0
    pause_countdown = 0
    is_paused = False
    reset_count = 0
    
    print("=" * 100)
    print(f"连输止损暂停重置模拟 - 首注={initial_amount:,}, 止损={lose_stop}手")
    print("=" * 100)
    print(f"{'局数':<6} {'状态':<8} {'连输':<6} {'下注':<12} {'开奖':<6} {'结果':<10} {'变动':<12} {'余额':<12} {'说明'}")
    print("-" * 100)
    
    while total_round < 30:
        total_round += 1
        
        if is_paused:
            pause_countdown -= 1
            if pause_countdown <= 0:
                is_paused = False
                win_count = 0
                lose_count = 0
                reset_count += 1
                print(f"{total_round:<6} {'恢复':<8} {'0':<6} {'-':<12} {'-':<6} {'-':<10} {'0':<12} {balance:>10,.0f} 重置首注")
            else:
                print(f"{total_round:<6} {'暂停':<8} {'-':<6} {'-':<12} {'-':<6} {'-':<10} {f'剩余{pause_countdown}局':<12} {'等待中':<12}")
            continue
        
        bet_amount = calculate_bet_amount(win_count, lose_count, initial_amount, lose_stop)
        
        if bet_amount == 0:
            is_paused = True
            pause_countdown = 10
            print(f"{total_round:<6} {'止损':<8} {lose_count:<6} {bet_amount:<12} {'-':<6} {'触发暂停':<10} {'0':<12} {balance:>10,.0f} 暂停 10 局")
            continue
        
        # 模拟开奖：前 6 局全输，之后全赢
        if total_round <= 6:
            win = False
            result_side = "小"
        else:
            win = True
            result_side = "大"
        
        if win:
            profit = bet_amount * 0.98
            balance += profit
            win_count += 1
            lose_count = 0
            result_text = f"赢 +{profit:,.0f}"
            change = f"+{profit:,.0f}"
            note = "回本" if lose_count == 0 else "重置后首赢"
        else:
            balance -= bet_amount
            lose_count += 1
            win_count = 0
            result_text = f"输 -{bet_amount:,}"
            change = f"-{bet_amount:,}"
            note = "连输" if lose_count > 1 else "首输"
        
        print(f"{total_round:<6} {'下注':<8} {lose_count:<6} {bet_amount:<12} {result_side:<6} {result_text:<10} {change:<12} {balance:>10,.0f} {note}")
    
    print("-" * 100)
    print(f"总 rounds={total_round}, 重置={reset_count}次，余额={balance:,.0f}, 净盈利={balance-start_balance:,.0f}")
    print()
    return balance - start_balance


# 运行模拟
print("\n模拟场景：连输 6 局 → 暂停 10 局 → 重置首注\n")
profit = simulate_with_pause(initial_amount=20000, lose_stop=6, start_balance=500000)

if profit > 0:
    print(f"✅ 测试通过：暂停重置后仍盈利 {profit:,.0f}")
else:
    print(f"⚠️ 净亏损 {profit:,.0f}")

