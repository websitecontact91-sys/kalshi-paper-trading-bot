"""Scheduled paper execution stays isolated from the opt-in live broker."""
import ast
from pathlib import Path
import pytest
from kalshi_client import Kalshi
from scripts.audit_paper_boundary import audit, BoundaryError

ROOT = Path(__file__).resolve().parents[1]

def test_paper_dependency_graph_cannot_reach_live_submission():
    result = audit(ROOT)
    assert result['paper_boundary'] == 'PASS'
    assert 'kalshi_client' in result['modules']
    assert 'live_client' in result['modules']
    assert 'live_broker' not in result['modules']


def test_boundary_rejects_a_transitive_live_import(tmp_path):
    (tmp_path / 'paper.py').write_text('import shared\n')
    (tmp_path / 'shared.py').write_text('from live_broker import LiveBroker\n')
    with pytest.raises(BoundaryError, match='Forbidden import'):
        audit(tmp_path, ('paper',))


def test_boundary_rejects_hidden_submission_or_dynamic_import(tmp_path):
    for source in ('def run():\n    broker.submit_limit_buy()\n', '__import__("broker")\n'):
        (tmp_path / 'paper.py').write_text(source)
        with pytest.raises(BoundaryError):
            audit(tmp_path, ('paper',))

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
