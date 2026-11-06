# labs/02-stateless-prompts.py
# Compares conversational relative prompts vs explicit stateless prompt resolution.
def test_prompt_resolution(prompt: str):
    has_relative = any(w in prompt.lower().split() for w in ['it', 'that', 'this', 'earlier'])
    has_entity = '@' in prompt or any(w in prompt for w in ['gold_', 'dim_', 'fact_', 'orders', 'customers'])
    return 'STATELESS (PASS)' if (has_entity and not has_relative) else 'CONVERSATIONAL (WARN)'

if __name__ == '__main__':
    print(test_prompt_resolution("Now join it with orders"))
    print(test_prompt_resolution("Join @retail_prod.gold.customers with @retail_prod.gold.orders on customer_id"))
