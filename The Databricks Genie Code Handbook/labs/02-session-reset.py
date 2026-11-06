# labs/02-session-reset.py
# Demonstrates token buffer restoration when triggering an explicit session reset.

def simulate_reset(turn_history: list):
    print(f'Before Reset: Accumulated history = {sum(turn_history)} tokens across {len(turn_history)} turns.')
    turn_history.clear()
    restored_budget = 16000 - 2000
    print(f'After Reset: History tokens = 0. Restored dynamic schema budget = {restored_budget} tokens (100% capacity).')
    return True

if __name__ == '__main__':
    history = [1200, 1850, 2400, 3100]
    simulate_reset(history)
