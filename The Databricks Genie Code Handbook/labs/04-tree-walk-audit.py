# labs/04-tree-walk-audit.py
# Traces directory tree-walking resolution for AGENTS.md discovery.
import pathlib

def find_nearest_agents_md(start_path: pathlib.Path, repo_root: pathlib.Path):
    curr = start_path.resolve()
    root = repo_root.resolve()
    while True:
        target = curr / 'AGENTS.md'
        if target.exists():
            return target
        if curr == root or curr.parent == curr:
            break
        curr = curr.parent
    return None

if __name__ == '__main__':
    print('Tree walking resolution simulation initialized.')
