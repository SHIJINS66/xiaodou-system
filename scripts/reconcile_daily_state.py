#!/usr/bin/env python3
from __future__ import annotations
from datetime import datetime
from typing import Any

from common import TERMINAL, make_tz, now_iso, parse_iso

TZ = make_tz("Asia/Shanghai")


def reconcile(daily: dict[str, Any], now: datetime | None = None, apply: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    current = (now or datetime.now(TZ)).astimezone(TZ); states = daily['runtime']['event_states']; anomalies = []; changed = False
    for state in states:
        status = state['status']
        if status in {'running', 'cancelling'}:
            anomalies.append(f"running_event:{state['event_id']}")
        elif status not in TERMINAL:
            scheduled = state.get('scheduled_for')
            expired = scheduled is None or parse_iso(scheduled) < current
            if expired and state.get('at_job_id') is None and apply:
                old = status; state.update(status='skipped', decision='cancel', decision_reason='finalize_cutoff', completed_at=now_iso(), error=None)
                state['history'].append({'at': now_iso(), 'from_status': old, 'to_status': 'skipped', 'reason': 'finalize_cutoff'}); changed = True
            elif expired:
                anomalies.append(f"residual_at_job:{state['event_id']}:{state.get('at_job_id')}")
    counts = {key: sum(1 for x in states if x['status'] == key) for key in ('completed', 'cancelled', 'failed', 'skipped')}; nonterminal = [x['event_id'] for x in states if x['status'] not in TERMINAL]
    status = 'deferred' if any(x.startswith('running_event:') for x in anomalies) else 'partial' if anomalies or nonterminal or counts['failed'] else 'completed'
    if apply and status != 'deferred':
        daily['plan_status'] = 'failed' if status == 'partial' and counts['failed'] else 'completed'; daily['file_revision'] += 1; daily['updated_at'] = now_iso(); changed = True
    report = {'status': status, 'planned_count': len(states), **{f'{k}_count': v for k, v in counts.items()}, 'nonterminal_event_ids': nonterminal, 'anomalies': anomalies, 'daily_changed': changed}
    return daily, report
