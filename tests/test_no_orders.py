"""Production source and runtime transport must remain market-data-only."""
import ast
from pathlib import Path
import pytest
from kalshi_client import Kalshi

ROOT = Path(__file__).resolve().parents[1]

def test_no_trade_submission_symbols_in_active_python():
    prohibited = ('create_'+'order', 'portfolio/'+'orders', 'place_'+'order')
    for path in ROOT.rglob('*.py'):
        if any(p in {'.git','.venv','__pycache__'} for p in path.parts):
            continue
        source = path.read_text().lower()
        assert not any(word in source for word in prohibited), str(path)

def test_active_http_transport_only_uses_get():
    tree = ast.parse((ROOT/'kalshi_client.py').read_text())
    called = {node.func.attr for node in ast.walk(tree) if isinstance(node,ast.Call)
              and isinstance(node.func,ast.Attribute)}
    assert not called.intersection({'post','put','patch','delete','request'})

@pytest.mark.parametrize('path', ['/portfolio/'+'orders','/account/balance','/rfqs','/quotes'])
def test_non_market_endpoint_refused_before_network(path):
    class NeverNetwork:
        def get(self,*a,**kw): raise AssertionError('forbidden endpoint reached transport')
    with pytest.raises(ValueError): Kalshi(session=NeverNetwork()).get(path)
