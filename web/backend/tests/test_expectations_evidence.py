from copy import deepcopy

import pytest

from v2.research.expectations import classify_statement, extract_statements, prepare_expectations


@pytest.mark.parametrize('text', [
    'Actual future results may differ materially from expected revenue.',
    'We assume no obligation to update forward-looking statements about revenue.',
    'We believe our facilities are in good condition and suitable for our business.',
    'Statements reflect our beliefs and opinions about future demand.',
])
def test_disclaimers_are_not_guidance(text):
    assert classify_statement(text) is None


def test_growth_is_not_a_guidance_upgrade():
    row = classify_statement('We expect revenue to increase next year.')
    assert row['group'] == 'outlook'
    assert row['status'] == 'NOT_COMPARABLE'
    assert row['period_label'] == '下一年度'


def test_explicit_guidance_action_and_negation():
    row = classify_statement('We are raising our revenue guidance to $40 billion for next quarter.')
    assert row['group'] == 'guidance'
    assert row['status'] == 'RAISED'
    assert row['value'] is None  # No invented units or parsed financial values.
    assert classify_statement('We are not raising our revenue guidance for next quarter.')['status'] == 'NOT_COMPARABLE'
    assert classify_statement('We may raise our revenue guidance for next quarter.')['status'] == 'NOT_COMPARABLE'
    assert classify_statement('We reaffirm our revenue guidance for next quarter.')['status'] == 'REITERATED'


def test_customer_risk_is_separate():
    row = classify_statement('Customers may postpone purchases, affecting our revenue timing and supply expenses.')
    assert row['group'] == 'risk'
    assert row['status'] == 'RISK_CONTEXT'


def test_old_cache_reclassified_deduplicated_without_mutation():
    text = 'We expect revenue to increase next year.'
    source = {'modules': {'expectations': {'metrics': {'forward_eps': 0}, 'details': {'guidance': [
        {'evidence_text': text, 'status': 'RAISED', 'filing_date': '2026-06-01'},
        {'evidence_text': text, 'status': 'RAISED', 'filing_date': '2026-03-01'},
        {'evidence_text': 'We assume no obligation to update forward-looking statements about revenue.'},
    ]}}, 'earnings': {'details': {'history': [{'eps_surprise': 0}, {'eps_surprise': None}]}}}}
    original = deepcopy(source)
    details = prepare_expectations(source)['modules']['expectations']['details']
    assert source == original
    assert len(details['guidance']) == 1
    assert details['guidance'][0]['status'] == 'NOT_COMPARABLE'
    assert details['guidance'][0]['filing_date'] == '2026-06-01'
    assert details['guidance_quality']['filtered'] == 1
    assert details['guidance_quality']['duplicates'] == 1
    assert details['capability_matrix']['current_consensus']['status'] == 'PARTIAL_DATA'
    assert '1 个' in details['capability_matrix']['earnings_surprise_history']['reason']


def test_empty_results_do_not_advertise_data():
    details = prepare_expectations({'modules': {'expectations': {'details': {}}}})['modules']['expectations']['details']
    assert all(row['status'] == 'UNAVAILABLE' for row in details['capability_matrix'].values())
    assert details['guidance'] == []


def test_scan_past_introduction_and_keep_actual_guidance():
    boilerplate = 'These forward-looking statements concern revenue and are subject to risks. '
    text = boilerplate * 180 + 'We forecast revenue of $40 billion for next quarter.'
    rows = extract_statements(text, '2026-08-26', 'https://www.sec.gov/example')
    assert len(rows) == 1
    assert rows[0]['group'] == 'guidance'
