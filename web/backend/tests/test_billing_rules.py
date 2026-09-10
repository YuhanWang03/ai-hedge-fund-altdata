import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

import pytest
from v2.data import usage_ledger as ledger, billing_rules as rules


def rate():
    return ledger.add_price(dict(provider='DeepSeek', model='deepseek-v4-flash', currency='CNY',
        effective_at='2026-01-01T00:00:00+00:00', review_after='2099-01-01T00:00:00+00:00',
        source='test', rates={'input': 3, 'cached_input': .1, 'output': 9}))


def test_requested_model_and_breakdown():
    rate()
    ledger.record_llm({'model': 'deepseek-flash', 'usage': {'prompt_tokens': 1000, 'prompt_cache_hit_tokens': 400, 'completion_tokens': 200}}, 'deepseek-v4-flash')
    event = ledger.report()['recent'][0]
    assert event['model'] == 'deepseek-flash'
    assert event['requested_model'] == event['pricing_model'] == 'deepseek-v4-flash'
    assert event['breakdown']['input']['tokens'] == 600
    assert event['amount'] == pytest.approx(.00364)
    assert sum(v['amount'] for v in event['breakdown'].values()) == event['amount']


def test_agent_default_matches_owner_model(monkeypatch):
    from v2.agent.llm import OpenAICompatLLM
    monkeypatch.delenv('AGENT_LLM_MODEL', raising=False)
    assert OpenAICompatLLM().model == 'deepseek-v4-flash'
    monkeypatch.setenv('AGENT_LLM_MODEL', 'explicit-model')
    assert OpenAICompatLLM().model == 'explicit-model'
    assert OpenAICompatLLM(model='argument-model').model == 'argument-model'


def test_alias_backfill_is_explicit_audited_idempotent():
    rate()
    ledger.record('llm', 'DeepSeek', 'deepseek-flash', {'input_tokens': 100, 'cached_tokens': 0, 'output_tokens': 10}, occurred_at='2026-09-09T00:00:00+00:00')
    assert ledger.reconcile_pending()['updated'] == 0
    rules.configure_alias(dict(model='deepseek-flash', target='deepseek-v4-flash', source='user verified', confirmed=True,
                               effective_at='2026-09-01T00:00:00+00:00', review_after='2026-10-01T00:00:00+00:00'))
    assert ledger.reconcile_pending()['updated'] == 1
    assert ledger.reconcile_pending()['updated'] == 0
    with ledger._conn() as c:
        audit = json.loads(c.execute('SELECT payload FROM billing_audit').fetchone()[0])
        assert audit['before']['status'] == 'pending'
    assert ledger.report()['total_requests'] == 1


def test_future_price_not_applied_to_history():
    p = rate()
    ledger.record_llm({'usage': {'prompt_tokens': 100, 'completion_tokens': 10}}, p['model'])
    assert ledger.reconcile_pending()['updated'] == 0  # missing cache classification
    ledger.record('llm', 'DeepSeek', p['model'], {'input_tokens': 100, 'output_tokens': 10, 'cached_tokens': 0}, occurred_at='2025-01-01T00:00:00+00:00')
    assert ledger.reconcile_pending()['updated'] == 0


def test_historical_alias_does_not_override_different_request():
    rate()
    rules.configure_alias(dict(model='deepseek-flash', target='deepseek-v4-flash', source='user verified history', confirmed=True,
        effective_at='2026-01-01T00:00:00+00:00', review_after='2099-01-01T00:00:00+00:00'))
    ledger.record_llm({'model': 'deepseek-flash', 'usage': {'prompt_tokens': 100, 'prompt_cache_hit_tokens': 0, 'completion_tokens': 10}}, 'different-model')
    assert ledger.report()['recent'][0]['status'] == 'pending'


def test_mapping_endpoint_reconciles_immediately(monkeypatch):
    import asyncio
    from app.routers.workspace import add_model_mapping
    rate()
    ledger.record('llm', 'DeepSeek', 'deepseek-flash', {'input_tokens': 100, 'cached_tokens': 0, 'output_tokens': 10}, occurred_at='2026-09-09T00:00:00+00:00')
    result = asyncio.run(add_model_mapping(dict(model='deepseek-flash', target='deepseek-v4-flash', source='user verified', confirmed=True,
        effective_at='2026-09-01T00:00:00+00:00', review_after='2026-10-01T00:00:00+00:00')))
    assert result['updated'] == 1
    event = ledger.report()['recent'][0]
    assert event['model'] == 'deepseek-flash'
    assert event['pricing_model'] == 'deepseek-v4-flash'
    assert ledger.reconcile_pending()['updated'] == 0


def test_quota_boundary_and_clear_does_not_reset():
    at = ledger.now_iso()
    rules.save_quota(999, 'test manual', at)
    later = (datetime.fromisoformat(at)+timedelta(seconds=1)).isoformat()
    ledger.record('search', 'Tavily', 'search', {'units': 2}, occurred_at=later)
    event = ledger.report()['recent'][0]
    assert event['amount'] == .008
    assert event['quota']['free_credits'] == event['quota']['paid_credits'] == 1
    with ledger._conn() as c:
        c.execute('DELETE FROM usage_events')
    ledger.record('search', 'Tavily', 'search', {'units': 1}, occurred_at=later)
    assert ledger.report()['recent'][0]['amount'] == .008
    assert rules.quota_status()['used'] == 1002


def test_unknown_pre_snapshot_rollover_and_expired_quota():
    at = ledger.now_iso()
    rules.save_quota(0, 'test', at)
    before = (datetime.fromisoformat(at)-timedelta(seconds=1)).isoformat()
    ledger.record('search', 'Tavily', 'search', {'units': 1}, occurred_at=before)
    assert ledger.report()['recent'][0]['status'] == 'pending'
    later = (datetime.fromisoformat(at)+timedelta(days=32)).isoformat()
    ledger.record('search', 'Tavily', 'search', {'units': 1}, occurred_at=later)
    assert ledger.report()['recent'][0]['status'] == 'pending'
    assert rules.quota_status()['used'] == 0


def test_concurrent_quota_reservation():
    at = ledger.now_iso()
    rules.save_quota(999, 'test', at)
    later = (datetime.fromisoformat(at)+timedelta(seconds=1)).isoformat()
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: ledger.record('search', 'Tavily', 'search', {'units': 1}, occurred_at=later), range(8)))
    report = ledger.report()
    assert report['total_requests'] == 8
    assert report['total_cost_usd'] == pytest.approx(7*.008)
    assert report['by_provider'][0]['free_credits'] == 1


def test_sync_sanitized_account_not_key_usage(monkeypatch):
    monkeypatch.setenv('TAVILY_API_KEY', 'secret')
    def get(url, **kwargs):
        assert url == 'https://api.tavily.com/usage' and kwargs['allow_redirects'] is False
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: {'key': {'usage': 2}, 'account': {'current_plan': 'Researcher', 'plan_limit': 1000, 'plan_usage': 1000, 'paygo_usage': 534}, 'secret': 'secret'})
    monkeypatch.setattr(rules.requests, 'get', get)
    q = rules.sync_tavily()
    assert q['used'] == 1534 and q['free_remaining'] == 0
    assert 'secret' not in str(q)
    assert ledger.report()['total_requests'] == 0


@pytest.mark.parametrize('value', [-1, float('nan'), float('inf'), True])
def test_bad_calibration_rejected(value):
    with pytest.raises(ValueError):
        rules.save_quota(value, 'test', ledger.now_iso())


@pytest.mark.parametrize('plan_used,paid,expected', [(1658, 658, 1658), (1658, 700, None)])
def test_tavily_cumulative_plan_usage(monkeypatch, plan_used, paid, expected):
    monkeypatch.setenv('TAVILY_API_KEY', 'test-key')
    monkeypatch.setattr(rules.requests, 'get', lambda *args, **kwargs: SimpleNamespace(
        status_code=200, raise_for_status=lambda: None, json=lambda: {'account': {
            'current_plan': 'Researcher', 'plan_limit': 1000, 'plan_usage': plan_used, 'paygo_usage': paid}}))
    result = rules.sync_tavily()
    if expected is None:
        assert result['status'] == 'error'
    else:
        assert result['used'] == expected
        assert result['paid_credits_estimate'] == paid


def test_new_endpoints_require_owner(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app import auth
    monkeypatch.setattr(auth, 'SETTINGS', SimpleNamespace(owner_token='owner'))
    client = TestClient(app)
    for endpoint in ['tavily/sync', 'tavily/calibrate', 'model-mappings', 'reconcile']:
        assert client.post('/api/costs/'+endpoint, json={}).status_code == 401


def test_invalid_key_and_expired_quota_do_not_consume(monkeypatch):
    at = ledger.now_iso()
    rules.save_quota(50, 'test', at)
    monkeypatch.setenv('TAVILY_API_KEY', 'another-key')
    ledger.record('search', 'Tavily', 'search', {'units': 3})
    assert ledger.report()['recent'][0]['status'] == 'pending'
    assert rules.quota_status()['used'] == 50


def test_error_state_persisted_and_throttled(monkeypatch):
    monkeypatch.setenv('TAVILY_API_KEY', 'secret')
    calls = []
    def fail(*args, **kwargs):
        calls.append(1)
        raise RuntimeError('secret')
    monkeypatch.setattr(rules.requests, 'get', fail)
    assert rules.sync_tavily()['status'] == 'error'
    assert '同步失败' in rules.quota_status()['message']
    rules.sync_tavily()
    assert len(calls) == 1
    assert 'secret' not in str(ledger.report())
