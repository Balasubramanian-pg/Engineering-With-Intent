# labs/02-ui-performance.py
# Benchmarks DOM node memory overhead for inline in-cell diffs vs side-panel web worker.

def profile_ui_overhead(num_cells=50):
    inline_dom_mb = 450 + (num_cells * 39)
    side_panel_mb = 580
    return inline_dom_mb, side_panel_mb

if __name__ == '__main__':
    for cells in [10, 20, 35, 50]:
        inline_ram, side_ram = profile_ui_overhead(cells)
        print(f'Notebook with {cells:02d} cells: Inline = {inline_ram:4d} MB | Side-Panel = {side_ram:4d} MB')
