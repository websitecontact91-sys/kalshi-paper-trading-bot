"""Check the scheduled paper entry points and their local import dependencies."""
from __future__ import annotations

import ast
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PAPER_ROOTS = ('paper_runner', 'validate_universe', 'simulate', 'scripts.workflow_gate')
LIVE_MODULES = {'live_broker', 'live_execution', 'live_state', 'live_persistence', 'live_runner'}
ORDER_MARKERS = ('portfolio/orders', 'portfolio/events/orders', 'create_order', 'place_order', 'submit_limit_buy')


class BoundaryError(ValueError):
    pass


def audit(root=ROOT, entry_points=PAPER_ROOTS):
    root = Path(root)
    pending, visited = list(entry_points), set()
    while pending:
        module = pending.pop()
        if module in visited:
            continue
        if module.split('.')[0] in LIVE_MODULES:
            raise BoundaryError('Paper execution imports live execution module: ' + module)
        path = root / (module.replace('.', '/') + '.py')
        if not path.is_file():
            path = root / module.replace('.', '/') / '__init__.py'
        if not path.is_file():
            continue
        source = path.read_text(encoding='utf-8')
        if any(marker in source.lower() for marker in ORDER_MARKERS):
            raise BoundaryError('Order operation found in paper dependency: ' + module)
        tree = ast.parse(source, filename=str(path))
        visited.add(module)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    raise BoundaryError('Unreviewed relative import in paper dependency: ' + module)
                imports = [node.module] if node.module else []
            else:
                imports = []
            for imported in imports:
                if imported.split('.')[0] in LIVE_MODULES or imported == 'importlib':
                    raise BoundaryError('Forbidden import from paper dependency: ' + imported)
                pending.append(imported)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {'__import__', 'eval', 'exec'}:
                raise BoundaryError('Dynamic code loading in paper dependency: ' + module)
        if module == 'kalshi_client':
            calls = {node.func.attr for node in ast.walk(tree)
                     if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
            if calls.intersection({'post', 'put', 'patch', 'delete', 'request'}):
                raise BoundaryError('Paper HTTP client contains a non-GET transport')
    if not set(entry_points).issubset(visited):
        raise BoundaryError('A scheduled paper entry point is missing')
    return {'paper_boundary': 'PASS', 'modules': sorted(visited)}


def main():
    print(json.dumps(audit(), sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
