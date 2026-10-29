import pytest
from timing import in_entry_window, observation_fresh, round_key, seconds_to_close, timestamp

@pytest.mark.parametrize('remaining,allowed', [(61,False),(60,True),(52.9,True),(1,True),(0,False),(-1,False)])
def test_entry_boundaries(remaining, allowed):
    assert in_entry_window(1800000000, 1800000000-remaining) is allowed
    assert seconds_to_close(1800000000, 1800000000-remaining) == pytest.approx(remaining)

def test_equivalent_timezones_share_one_round():
    assert round_key('2026-10-29T17:30:00Z') == round_key('2026-10-29T13:30:00-04:00')
    with pytest.raises(ValueError): timestamp('2026-10-29T17:30:00')

@pytest.mark.parametrize('age,allowed', [(15,True),(15.01,False),(-5,True),(-5.01,False),(0,True)])
def test_reference_freshness(age, allowed):
    assert observation_fresh(100-age,100) is allowed

def test_bad_times_fail_closed():
    for bad in (None,float('nan'),float('inf'),'bad'):
        assert not observation_fresh(bad,100)
