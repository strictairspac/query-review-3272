"""撤下过滤纯函数口径测试：到期行剔除、差异样本同步撤下、原结论/原行数保留。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from src.services import withdrawal_filter as wf


def _detail_result(rows_left, rows_right, equivalent=True):
    cols = ['invoice_id', 'customer', 'route', 'amount']
    return {
        'equivalent': equivalent, 'comparison': 'cmp', 'note': 'n',
        'original': {'columns': cols, 'rows': rows_left, 'row_count': len(rows_left),
                     'elapsed_ms': 1.0, 'plan': 'p-left'},
        'candidate': {'columns': cols, 'rows': rows_right, 'row_count': len(rows_right),
                      'elapsed_ms': 2.0, 'plan': 'p-right'},
        'original_checksum': 'old-left', 'candidate_checksum': 'old-right',
        'missing_rows': [], 'extra_rows': [],
    }


def test_is_due():
    due = {7: '2026-10-31', 8: '2026-12-31'}
    assert wf.is_due(7, due) is True
    assert wf.is_due(8, due) is False       # 未到期
    assert wf.is_due(999, due) is False     # 查不到按未到期


def test_bilateral_intersection():
    result = _detail_result(
        [[7, 'a', '东线', '10'], [8, 'b', '西线', '20']],
        [[7, 'a', '东线', '10'], [9, 'c', '东线', '30']])
    assert wf.bilateral_invoice_ids(result) == {7}


def test_eligible_requires_due_and_bilateral():
    result = _detail_result(
        [[7, 'a', 'x', '1'], [8, 'b', 'x', '2']],
        [[7, 'a', 'x', '1'], [8, 'b', 'x', '2']])
    due = {7: '2026-10-31', 8: '2026-12-31'}
    assert wf.eligible_invoice_ids(result, due) == {7}


def test_apply_removes_due_rows_and_keeps_rest():
    rows = [[7, 'a', 'x', '1'], [8, 'b', 'x', '2'], [14, 'c', 'x', '3']]
    result = _detail_result(rows, [list(r) for r in rows])
    view = wf.apply_withdrawals(result, {7, 14})
    # 到期行被剔除，未到期行保留
    assert [r[0] for r in view['original']['rows']] == [8]
    assert [r[0] for r in view['candidate']['rows']] == [8]
    # 当前行数下调，原行数留档
    assert view['original']['row_count'] == 1
    assert view['original_row_count_original'] == 3
    # 结论、列、计划不变
    assert view['equivalent'] is True
    assert view['original']['columns'] == ['invoice_id', 'customer', 'route', 'amount']
    assert view['original']['plan'] == 'p-left'
    assert view['withdrawn_invoice_ids'] == [7, 14]


def test_aggregate_without_invoice_col_is_untouched():
    result = {
        'equivalent': True, 'comparison': 'c', 'note': 'n',
        'original': {'columns': ['customer', 'c'], 'rows': [['a', '10']], 'row_count': 1},
        'candidate': {'columns': ['customer', 'c'], 'rows': [['a', '10']], 'row_count': 1},
        'original_checksum': 'x', 'candidate_checksum': 'y',
        'missing_rows': [], 'extra_rows': [],
    }
    view = wf.apply_withdrawals(result, {7})
    assert view['original']['rows'] == [['a', '10']]
    assert 'withdrawn_invoice_ids' not in view


def test_diff_samples_also_withdrawn():
    # 候选缺了 7、8 两行 -> 都进 missing_rows
    left = [[7, 'a', 'x', '1'], [8, 'b', 'x', '2'], [9, 'c', 'x', '3']]
    right = [[9, 'c', 'x', '3']]
    result = _detail_result(left, right, equivalent=False)
    import json
    result['missing_rows'] = [json.dumps([7, 'a', 'x', '1'], ensure_ascii=False),
                              json.dumps([8, 'b', 'x', '2'], ensure_ascii=False)]
    view = wf.apply_withdrawals(result, {7})
    # 命中到期账单 7 的差异样本被撤下，8 的样本保留
    assert all('7' != json.loads(s)[0] for s in view['missing_rows'])
    assert any(json.loads(s)[0] == 8 for s in view['missing_rows'])
    assert view['equivalent'] is False   # 原结论不变


def test_no_removed_returns_original_object():
    result = _detail_result([[1, 'a', 'x', '1']], [[1, 'a', 'x', '1']])
    assert wf.apply_withdrawals(result, set()) is result
