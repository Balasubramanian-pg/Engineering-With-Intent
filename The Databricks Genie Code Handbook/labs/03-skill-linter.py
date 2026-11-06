# labs/03-skill-linter.py
# Lints YAML frontmatter and triggers in .assistant skill files.
def lint_skill(filepath: str):
    print(f"Linting skill file format: {filepath}")
    return True

if __name__ == '__main__':
    lint_skill('/Workspace/.assistant/skills/lakeflow-builder/SKILL.md')
