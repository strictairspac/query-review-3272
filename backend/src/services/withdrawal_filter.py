"""到期账单判定与复核结果行撤下的共享口径。

复核证据是一次查询留档：原始侧与候选侧各自保存 columns/rows。
撤下只针对“两侧都带账单号、且账单已到期”的行，原始结论、行数和其余账单保持可查。
本模块不访问数据库，纯函数便于在 API、worker 与导出中复用同一口径。
"""
import json
from datetime import date
from collections import Counter

# 留档口径固定的“今天”，与演示数据到期日配套；上线后由账单到期字段驱动
TODAY = date(2026, 11, 19)


def is_due(invoice_id, due_dates):
    """账单是否到期。due_dates: {invoice_id: 'YYYY-MM-DD'}；查不到视为未到期。"""
    due = due_dates.get(int(invoice_id))
    if not due:
        return False
    if isinstance(due, date):
        return due <= TODAY
    return date.fromisoformat(str(due)[:10]) <= TODAY


def _invoice_col_index(columns):
    """找到结果列中的账单号列（不区分大小写、容忍别名），没有则返回 None。"""
    for idx, name in enumerate(columns or []):
        if str(name).lower() == 'invoice_id':
            return idx
    return None


def _row_invoice_id(row, idx):
    if idx is None or idx >= len(row) or row[idx] is None:
        return None
    try:
        return int(str(row[idx]))
    except (TypeError, ValueError):
        return None


def collect_invoice_ids(side):
    """收集一侧结果里出现过的全部账单号。"""
    idx = _invoice_col_index(side.get('columns'))
    if idx is None:
        return set()
    out = set()
    for row in side.get('rows', []):
        inv = _row_invoice_id(row, idx)
        if inv is not None:
            out.add(inv)
    return out


def bilateral_invoice_ids(result):
    """两侧都带账单号的账单（交集），撤下登记只允许这些。"""
    if result.get('error'):
        return set()
    left = collect_invoice_ids(result.get('original', {}))
    right = collect_invoice_ids(result.get('candidate', {}))
    return left & right


def eligible_invoice_ids(result, due_dates):
    """可在页面登记撤下的账单：两侧都带账单号且已到期。"""
    return {inv for inv in bilateral_invoice_ids(result) if is_due(inv, due_dates)}


def _canon(row):
    return json.dumps(row, ensure_ascii=False)


def filter_side(side, removed_ids):
    """把一侧结果中账单号命中撤下集合的行剔除，返回新的 side（不修改入参）。

    聚合结果没有 invoice_id 列时原样返回——撤下的账单不会出现在聚合行里。
    返回值附带 removed_rows 便于审计“撤下了哪些到期行”。
    """
    idx = _invoice_col_index(side.get('columns'))
    if idx is None:
        return {**side, 'removed_rows': []}, []
    kept, dropped = [], []
    for row in side.get('rows', []):
        inv = _row_invoice_id(row, idx)
        if inv is not None and inv in removed_ids:
            dropped.append(row)
        else:
            kept.append(row)
    new_side = {**side, 'rows': kept, 'row_count': len(kept), 'removed_rows': dropped}
    return new_side, dropped


def _counter(side):
    return Counter(_canon(row) for row in side['rows'])


def apply_withdrawals(result, removed_ids):
    """在一份 evidence.result 上应用（全局生效的）撤下，返回页面/下载可见结果。

    removed_ids 是全库已生效撤下的账单号集合：一张账单撤下后，旧档与并发发布的
    复核只要含该账单行都会被剔除。本证据没有的账单自然不影响它，报告里只列本证据
    实际剔除的账单。

    - 原始结论(equivalent/error)、两侧列、查询计划、耗时保持不变；
    - 两侧结果行剔除到期账单行，行数随之下调（原行数字段保留为 *_row_count_original）；
    - missing/extra 差异样本同步撤下到期账单，并重算 SHA-256 摘要。
    """
    removed_ids = {int(x) for x in removed_ids}
    if result.get('error') or not removed_ids:
        return result
    # 两侧都没有账单号列（如纯聚合结果）时，撤下的账单不会出现在任何行里，原样返回
    if _invoice_col_index(result.get('original', {}).get('columns')) is None and \
            _invoice_col_index(result.get('candidate', {}).get('columns')) is None:
        return result
    original, dropped_left = filter_side(result['original'], removed_ids)
    candidate, dropped_right = filter_side(result['candidate'], removed_ids)
    # 本证据实际被剔除的账单号（全局集合与本证据账单号的交集）
    present = collect_invoice_ids(result.get('original', {})) | \
        collect_invoice_ids(result.get('candidate', {}))
    applied = sorted(removed_ids & present)
    if not applied:  # 全局撤下集合里没有任何账单出现在本证据，原样返回
        return result

    # 差异样本是规范行 JSON；按两侧账单号列位置撤下命中账单的样本
    def filter_samples(samples):
        return [s for s in (samples or []) if _sample_kept(s, result, removed_ids)]

    missing = filter_samples(result.get('missing_rows'))
    extra = filter_samples(result.get('extra_rows'))

    def digest(side):
        import hashlib
        return hashlib.sha256(
            json.dumps(sorted(_counter(side).items()), ensure_ascii=False).encode()).hexdigest()

    return {
        **result,
        'original': {k: v for k, v in original.items() if k != 'removed_rows'},
        'candidate': {k: v for k, v in candidate.items() if k != 'removed_rows'},
        'original_row_count_original': result['original'].get('row_count'),
        'candidate_row_count_original': result['candidate'].get('row_count'),
        'missing_rows': missing,
        'extra_rows': extra,
        'original_checksum': digest(original),
        'candidate_checksum': digest(candidate),
        'withdrawn_invoice_ids': applied,
        'withdrawn_note': '已撤下到期账单 %d 张；原结论、列结构与执行计划不变，行数与差异样本已同步剔除' % len(applied),
    }


def _sample_kept(line, result, removed_ids):
    """差异样本是否保留。样本是一行规范 JSON，按结果账单号列位置取账单号判断。"""
    try:
        row = json.loads(line)
    except (TypeError, ValueError):
        return True
    for side_name in ('original', 'candidate'):
        idx = _invoice_col_index(result.get(side_name, {}).get('columns'))
        if idx is not None and idx < len(row):
            inv = _row_invoice_id(row, idx)
            if inv is not None and inv in removed_ids:
                return False
    return True
