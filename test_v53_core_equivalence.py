import ast
from pathlib import Path

CORE = Path('gtc_core_engine.py')
WEB = Path('main.py')

core_src = CORE.read_text(encoding='utf-8')
web_src = WEB.read_text(encoding='utf-8')
core_tree = ast.parse(core_src)
web_tree = ast.parse(web_src)

classes = [n.name for n in core_tree.body if isinstance(n, ast.ClassDef)]
funcs = [n.name for n in core_tree.body if isinstance(n, ast.FunctionDef)]
assert 'GTCProApp' not in classes, 'Core must not contain Tkinter UI class GTCProApp'
assert 'main' not in funcs, 'Core must not contain Tkinter startup main()'
for banned in ['import tkinter', 'from tkinter', 'messagebox', 'filedialog', 'tk.']:
    assert banned not in core_src, f'Core contains UI dependency: {banned}'
for required in ['analyze_symbol', 'load_battle_plan_excel', 'build_control_status', 'build_market_overview', 'empty_battle_fields']:
    assert required in funcs, f'Missing core function: {required}'
assert 'import gtc_core_engine as core' in web_src, 'Web must import gtc_core_engine'
assert 'gtc_v525_core' not in web_src, 'Web must not import legacy gtc_v525_core'
assert 'make_pdf_download' in web_src, 'Web PDF export missing'
assert 'auto_refresh_enabled' in web_src, 'Web auto refresh control missing'
print('PASS: v5.3.1 new GitHub project core/web structure validation')
