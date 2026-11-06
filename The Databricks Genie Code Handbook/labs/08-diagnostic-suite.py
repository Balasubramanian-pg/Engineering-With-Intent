# labs/08-diagnostic-suite.py
# Diagnostic test harness replicating production symptom failures and verifying fixes.
def diagnose_symptom(symptom_code: str):
    fixes = {
        'NULL_CAST': 'Enforce 3-turn reset and stateless prompt entity bindings.',
        'CARTESIAN_EXPLOSION': 'Declare informational FOREIGN KEY constraints with RELY.',
        'PROMPT_DILUTION': 'Prune instruction file below 2,000 characters and move recipes to skills.',
        'BRONZE_SCAN': 'Annotate partition keys and restrict space to curated gold views.'
    }
    return fixes.get(symptom_code, 'Unknown symptom.')

if __name__ == '__main__':
    print("Diagnostic Engine Ready.")
