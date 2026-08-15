#!/usr/bin/env python3
from __future__ import annotations
import argparse,hashlib,json,shutil,sys,time
from datetime import date as Date,datetime
from pathlib import Path
from typing import Any
from backup_openclaw import create as create_backup
from build_daily_memory import MemoryGenerationError,generate as generate_memory,render as render_memory
from common import StepError, atomic_write_bytes, atomic_write_json, atomic_write_text, file_lock, load_env_file, load_json, make_at_env, make_tz, now_iso, sha256_file
from mirror_openclaw_memory import publish as publish_memory
from memory_quality import QUALITY_GATE_VERSION
from normalize_chatlog import normalize,transaction_outcomes
from providers.gateway_history import collect
from rollover_artifacts import load_carryover,next_rollover_seals,rollover_receipt_path
from raw_backup import create as create_raw_backup,verify as verify_raw_backup
from reconcile_daily_state import reconcile
from render_chatlog import render as render_chatlog
from schema_tools import validate

def transactions(root:Path,date:str)->list[dict[str,Any]]:
    result=[]; base=root/date
    if not base.is_dir(): return result
    for path in sorted(base.glob('*/transaction.json')):
        # A corrupt transaction is evidence corruption, not an absent event.
        # Silently skipping it can turn a real delivery into a false omission.
        value=load_json(path)
        value['_path']=str(path); result.append(value)
    return result
def journal_records(path:Path)->list[dict[str,Any]]:
    if not path.is_file(): return []
    rows=[]
    for number,line in enumerate(path.read_text(encoding='utf-8').splitlines(),1):
        try: value=json.loads(line)
        except json.JSONDecodeError as exc: raise StepError(f'invalid journal line {number}') from exc
        if not isinstance(value,dict) or value.get('sequence')!=number or not isinstance(value.get('event_id'),str) or not isinstance(value.get('to_phase'),str): raise StepError(f'invalid journal record {number}')
        rows.append(value)
    return rows
def merge_journal(outcomes:list[dict[str,Any]],records:list[dict[str,Any]])->list[dict[str,Any]]:
    index={x.get('execution_id') or x.get('event_id'):x for x in outcomes}; result=list(outcomes)
    for row in records:
        key=row.get('execution_id') or row['event_id']; compact={'sequence':row['sequence'],'timestamp':row.get('timestamp'),'from_phase':row.get('from_phase'),'to_phase':row['to_phase'],'reason':row.get('reason'),'error_code':row.get('error_code')}
        if key in index:
            index[key].setdefault('journal_records',[]).append(compact)
        else:
            evidence='ev-'+hashlib.sha256(('journal|'+str(key)).encode()).hexdigest()[:24]; item={'evidence_id':evidence,'event_id':row['event_id'],'execution_id':row.get('execution_id'),'phase':row['to_phase'],'decision':None,'decision_reason':row.get('reason'),'telegram_sent':row.get('provider_message_id') is not None,'session_injected':row['to_phase'] in {'injected','completed'},'error':row.get('error_code'),'journal_records':[compact]}; result.append(item); index[key]=item
    return result
def daily_outcomes(daily:dict[str,Any],existing:list[dict[str,Any]])->list[dict[str,Any]]:
    used={x.get('event_id') for x in existing}; rows=[]
    for state in daily['runtime']['event_states']:
        if state['event_id'] in used: continue
        evidence='ev-'+hashlib.sha256(('state|'+state['event_id']).encode()).hexdigest()[:24]
        rows.append({'evidence_id':evidence,'event_id':state['event_id'],'execution_id':None,'phase':state['status'],'decision':state.get('decision'),'decision_reason':state.get('decision_reason'),'telegram_sent':state.get('telegram_sent',False),'session_injected':state.get('session_injected',False),'error':state.get('error')})
    return rows
def carryover_rows(value:dict[str,Any]|None)->list[dict[str,Any]]:
    if value is None: return []
    rows=[]
    for item in value['messages']:
        meta={}
        if item.get('id') is not None: meta['id']=item['id']
        if item.get('openclaw_seq') is not None: meta['seq']=item['openclaw_seq']
        if item.get('kind') is not None: meta['kind']=item['kind']
        rows.append({'role':item['role'],'content':item['content'],'timestamp':item['timestamp'],'__openclaw':meta})
    return rows

def input_fingerprints(daily_path:Path,journal_path:Path,txns:list[dict[str,Any]],history:dict[str,Any],carryover:dict[str,Any]|None,config:dict[str,Any],target:Date)->dict[str,Any]:
    history_bytes=json.dumps(history['messages'],ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
    carry_path=Path(config.get('carryover_root') or (Path(config['state_root'])/'carryover'))/f'{target.isoformat()}.json'
    receipt_path=rollover_receipt_path(config,target.isoformat())
    return {
        'daily_sha256':sha256_file(daily_path),
        'journal_sha256':sha256_file(journal_path) if journal_path.is_file() else 'missing',
        'transaction_sha256':[sha256_file(Path(x['_path'])) for x in txns],
        'history_sha256':hashlib.sha256(history_bytes).hexdigest(),
        'carryover_sha256':sha256_file(carry_path) if carryover is not None else 'missing',
        'rollover_receipt_sha256':sha256_file(receipt_path) if receipt_path.is_file() else 'missing',
    }
def fingerprint(inputs:dict[str,Any])->str: return hashlib.sha256(json.dumps(inputs,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def state_write(path:Path,state:dict[str,Any],phase:str,**updates:Any)->dict[str,Any]:
    state.update(phase=phase,updated_at=now_iso(),**updates); state.setdefault('history',[]).append({'at':state['updated_at'],'phase':phase}); validate(state,Path(__file__).resolve().parents[1]/'schemas'/'finalization_record_v1.schema.json'); atomic_write_json(path,state); return state
def publish_text(path:Path,payload:str,apply:bool,expected_existing_sha256:str|None=None)->dict[str,Any]:
    raw=payload.encode(); digest=hashlib.sha256(raw).hexdigest()
    if path.exists() and path.read_bytes()!=raw:
        if expected_existing_sha256 is None or sha256_file(path)!=expected_existing_sha256: raise StepError(f'refusing to overwrite unknown output: {path}')
    if apply: atomic_write_text(path,payload)
    return {'path':str(path),'sha256':digest,'written':apply}
def completed_noop(state:dict[str,Any])->bool:
    if state.get('phase')!='completed': return False
    for item in ('chatlog','daily_memory','memory_mirror'):
        value=state.get(item,{}); path=Path(value.get('path',''))
        if not path.is_file() or sha256_file(path)!=value.get('sha256'): raise StepError(f'completed output drift: {item}')
    backup=state.get('backup',{}); archive=Path(backup.get('archive',''))
    if backup.get('verified') is not True or not archive.is_file() or sha256_file(archive)!=backup.get('sha256'):
        raise StepError('completed final backup drift')
    if isinstance(state.get('raw_backup'),dict): verify_raw_backup(state['raw_backup'])
    return True

def _raw_backup_files(state_path:Path,state:dict[str,Any],daily_path:Path,journal_path:Path)->list[tuple[str,Path]]:
    files=[
        ('state/finalization.json',state_path),
        ('workspace/chatlog.md',Path(state['chatlog']['path'])),
        ('workspace/daily.json',daily_path),
        ('run/evidence.json',Path(state['evidence_path'])),
    ]
    if journal_path.is_file(): files.append(('journal/events.jsonl',journal_path))
    for index,value in enumerate(state.get('transaction_paths',[]),1):
        path=Path(value)
        if path.is_file(): files.append((f'transactions/{index:04d}-transaction.json',path))
    carry=state.get('carryover_path')
    if isinstance(carry,str) and Path(carry).is_file(): files.append(('rollover/carryover.json',Path(carry)))
    receipt=state.get('rollover_receipt_path')
    if isinstance(receipt,str) and Path(receipt).is_file(): files.append(('rollover/receipt.json',Path(receipt)))
    return files

def ensure_raw_backup(config:dict[str,Any],state_path:Path,state:dict[str,Any],daily_path:Path,journal_path:Path)->dict[str,Any]:
    if not state.get('transaction_paths'):
        state['transaction_paths']=[str(Path(x['_path'])) for x in transactions(Path(config['transaction_root']),state['date'])]
    if not state.get('carryover_path'):
        carry=Path(config.get('carryover_root') or (Path(config['state_root'])/'carryover'))/f"{state['date']}.json"
        state['carryover_path']=str(carry) if carry.is_file() else None
    if not state.get('rollover_receipt_path'):
        receipt=rollover_receipt_path(config,state['date'])
        state['rollover_receipt_path']=str(receipt) if receipt.is_file() else None
    prior=state.get('raw_backup')
    if isinstance(prior,dict) and prior.get('verified') is True:
        return verify_raw_backup(prior)
    state_write(state_path,state,'raw_backup_intent',memory_status='pending',error=None)
    try:
        backup=create_raw_backup(
            config,
            date=state['date'],
            source_fingerprint=state['source_fingerprint'],
            files=_raw_backup_files(state_path,state,daily_path,journal_path),
        )
    except StepError as exc:
        state_write(state_path,state,'raw_backup_failed',memory_status='pending',error={'code':'raw_backup_failed','message':type(exc).__name__})
        raise
    state_write(state_path,state,'raw_backup_ready',raw_backup=backup,memory_status='pending',error=None)
    return backup
def finish_backup(config:dict[str,Any],state_path:Path,state:dict[str,Any])->dict[str,Any]:
    state_write(state_path,state,'backup_intent')
    try: backup=create_backup(config)
    except StepError as exc:
        phase='backup_unknown' if 'backup_unknown' in str(exc) else 'backup_failed'; state_write(state_path,state,phase,error={'code':phase,'message':type(exc).__name__}); raise
    state_write(state_path,state,'completed',backup=backup,completed_at=now_iso(),memory_status='completed',error=None); return backup
def finish_memory(config:dict[str,Any],state_path:Path,state:dict[str,Any],daily_path:Path,workspace:Path,evidence:dict[str,Any],recovery_mode:bool=False)->dict[str,Any]:
    prior_usage=state.get('model_usage',{}) if isinstance(state.get('model_usage'),dict) else {}
    quality=prior_usage.get('quality_report') if isinstance(prior_usage,dict) else None
    reusable_pending=(
        state.get('phase')=='memory_ready'
        and prior_usage.get('quality_gate_version')==QUALITY_GATE_VERSION
        and isinstance(quality,dict)
        and quality.get('passed') is True
    )
    if reusable_pending:
        pending=Path(state.get('pending_memory',{}).get('path',''))
        expected_digest=state.get('pending_memory',{}).get('sha256')
        if not pending.is_file() or sha256_file(pending)!=expected_digest: raise StepError('pending memory drift')
        memory_payload=pending.read_bytes(); usage=prior_usage
    else:
        env=load_env_file(Path(config["env_file"]))
        memory_config=dict(config)
        if recovery_mode:
            if state.get('recovery_attempted') is True:
                raise StepError('memory recovery strategy already attempted; manual review required')
            memory_config['memory_recovery_mode']=True
        if evidence['messages'] or evidence['event_outcomes']:
            try:
                settings = config['_settings']
                memory_value, usage = generate_memory(settings, memory_config, env.get('DEEPSEEK_API_KEY', ''), evidence)
            except StepError as exc:
                attempts=exc.attempts if isinstance(exc,MemoryGenerationError) else []
                state_write(
                    state_path,state,'memory_pending',memory_status='pending',
                    model_attempts=attempts,
                    recovery_attempted=True if recovery_mode else bool(state.get('recovery_attempted',False)),
                    error={'code':'memory_generation_exhausted','message':type(exc).__name__},
                )
                raise
        else:
            memory_value={key:[] for key in ('actual_life','proactive_shares','companion_responses','important_conversations','emotional_and_relationship_notes','unresolved_items','tomorrow_implications','long_term_memory_candidates')}; usage={'skipped_empty_evidence':True}
        memory_payload = render_memory(state['date'], memory_value, config['_settings']).encode()
        pending=Path(state['evidence_path']).with_name('daily_memory.pending.md')
        atomic_write_bytes(pending,memory_payload)
        state_write(state_path,state,'memory_ready',pending_memory={'path':str(pending),'sha256':hashlib.sha256(memory_payload).hexdigest()},model_usage=usage,memory_status='generated',error=None)
    daily_md=workspace/'daily'/f"{state['date']}.md"; memory_md=workspace/'memory'/f"{state['date']}.md"; expected=state.get('previous_output_hashes',{}).get('daily_memory'); mirror=publish_memory(daily_md,memory_md,memory_payload,True,expected); state_write(state_path,state,'mirrored',daily_memory={'path':str(daily_md),'sha256':mirror['sha256']},memory_mirror={'path':str(memory_md),'sha256':mirror['sha256']},model_usage=usage,memory_status='published',error=None)
    latest=load_json(daily_path)
    if latest['runtime']['daily_memory'].get('status')!='generated' or latest['runtime']['daily_memory'].get('path')!=str(daily_md):
        latest['runtime']['daily_memory'].update(status='generated',generated_at=now_iso(),path=str(daily_md),error=None); latest['file_revision']+=1; latest['updated_at']=now_iso(); validate(latest,Path(config['daily_schema_path'])); atomic_write_json(daily_path,latest)
    final_inputs=dict(state['input_fingerprints']); final_inputs['daily_sha256']=sha256_file(daily_path); state['final_inputs']=final_inputs; atomic_write_json(state_path,state)
    backup=finish_backup(config,state_path,state); return {'raw_backup':state.get('raw_backup'),'backup':backup,'daily_memory':state['daily_memory'],'memory_mirror':state['memory_mirror']}
def run(args: argparse.Namespace) -> dict[str, Any]:
    from step04_config import load_step04_config
    if getattr(args, "settings", None):
        config = load_step04_config(args.settings)
    elif getattr(args, "config", None):
        config = load_json(Path(args.config))
    else:
        raise StepError('finalize_day 需要 --settings 或 --config')
    _schema_view = {k: v for k, v in config.items() if k != '_settings'}  # 运行期注入对象不参与 schema 校验
    validate(_schema_view, Path(config['config_schema_path']))
    target = Date.fromisoformat(args.date); workspace = Path(config['workspace']).resolve(); daily_path = Path(config['daily_json_root']).resolve() / f'{args.date}.json'; journal_path = Path(config['journal_root']).resolve() / f'{args.date}.jsonl'; state_root = Path(config['state_root']).resolve(); state_path = state_root / 'state' / f'{args.date}.json'
    enabled = Path(config.get('finalize_enabled_gate') or (state_root.parent / 'step04.enabled'))
    if args.apply and (args.ack != 'FINALIZE_DAY' or not enabled.is_file()): raise StepError('apply requires enabled gate and --ack FINALIZE_DAY')
    with file_lock(Path(config['finalize_internal_lock_file']), blocking=False):
        daily = load_json(daily_path); validate(daily, Path(config['daily_schema_path']))
        if daily['date']!=args.date: raise StepError('daily date mismatch')
        prior=load_json(state_path) if state_path.is_file() else {}
        if args.apply and prior.get('phase')=='backup_unknown': raise StepError('backup_unknown requires archive reconciliation before retry')
        if args.apply and prior.get('phase') in {'mirrored','backup_intent','backup_failed'}:
            for key in ('daily_memory','memory_mirror'):
                item=prior.get(key,{}); path=Path(item.get('path',''))
                if not path.is_file() or sha256_file(path)!=item.get('sha256'): raise StepError(f'resume output drift: {key}')
            backup=finish_backup(config,state_path,prior); return {'mode':'apply','status':'completed','date':args.date,'state':str(state_path),'backup':backup,'resumed_from':'mirrored'}
        if args.apply and prior.get('phase') in {'chatlog_written','raw_backup_intent','raw_backup_failed','raw_backup_ready','memory_pending','memory_ready'}:
            evidence_path=Path(prior.get('evidence_path',''))
            if not evidence_path.is_file(): raise StepError('resume evidence missing')
            resumed_from=prior['phase']
            recovery_mode=resumed_from=='memory_pending'
            ensure_raw_backup(config,state_path,prior,daily_path,journal_path)
            result=finish_memory(config,state_path,prior,daily_path,workspace,load_json(evidence_path),recovery_mode=recovery_mode)
            return {'mode':'apply','status':'completed','date':args.date,'state':str(state_path),**result,'resumed_from':resumed_from}
        active_state=None
        if args.apply and prior.get('phase')!='completed':
            if prior:
                active_state=prior
            else:
                preliminary=hashlib.sha256((sha256_file(daily_path)+(sha256_file(journal_path) if journal_path.is_file() else 'missing')).encode()).hexdigest(); active_state={'schema_version':'1.0','date':args.date,'run_id':f'{args.date}-pending-{preliminary[:8]}','phase':'new','source_fingerprint':preliminary,'created_at':now_iso(),'updated_at':now_iso(),'history':[]}
            preliminary_dir=state_root/'runs'/args.date/active_state['run_id']; preliminary_dir.mkdir(parents=True,exist_ok=True); preliminary_dir.chmod(0o700); state_path.parent.mkdir(parents=True,exist_ok=True); state_path.parent.chmod(0o700); state_write(state_path,active_state,'acquired')
        deadline=time.monotonic()+config['running_wait_seconds']
        while any(x['status'] in {'running','cancelling'} for x in daily['runtime']['event_states']) and args.apply and time.monotonic()<deadline:
            time.sleep(min(5,max(0,deadline-time.monotonic()))); daily=load_json(daily_path); validate(daily,Path(config['daily_schema_path']))
        if any(x['status'] in {'running','cancelling'} for x in daily['runtime']['event_states']):
            if args.apply:
                if active_state is None: raise StepError('completed finalization conflicts with new running event')
                state_write(state_path,active_state,'deferred_running_event')
            return {'mode':'apply' if args.apply else 'dry_run','status':'deferred','date':args.date,'reason':'running_event','external_calls':[]}
        # A running event may have completed while we waited.  Read its
        # transaction and journal only after the wait so the archive cannot
        # use a stale pre-wait evidence snapshot.
        txns=transactions(Path(config['transaction_root']),args.date); journals=journal_records(journal_path)
        if args.apply and prior.get('phase')=='completed':
            completed_noop(prior)
            seal=next_rollover_seals(config,target)
            if seal is not None:
                return {'mode':'apply','status':'sealed_verified_noop','date':args.date,'state':str(state_path),'sealed_by_rollover_date':seal['date'],'external_calls':[]}
        carryover=load_carryover(config,target)
        history=collect(config,target)
        current_messages=list(history['messages'])
        history['messages']=carryover_rows(carryover)+current_messages
        history['carryover_message_count']=len(carryover_rows(carryover))
        history['current_session_message_count']=len(current_messages)
        inputs=input_fingerprints(daily_path,journal_path,txns,history,carryover,config,target); source_fp=fingerprint(inputs)
        if args.apply and prior.get('phase')=='completed':
            if prior.get('final_inputs')==inputs: return {'mode':'apply','status':'verified_noop','date':args.date,'state':str(state_path),'external_calls':['sessions.list(read_only)','chat.history(read_only)']}
        run_id=f"{args.date}-{source_fp[:12]}"; run_dir=state_root/'runs'/args.date/run_id; previous_hashes={}
        if args.apply and prior.get('phase')=='completed':
            archive_dir=run_dir/'previous_outputs'; archive_dir.mkdir(parents=True,exist_ok=True); archive_dir.chmod(0o700)
            for key in ('chatlog','daily_memory','memory_mirror'):
                item=prior[key]; source=Path(item['path']); shutil.copy2(source,archive_dir/f'{key}.previous'); (archive_dir/f'{key}.previous').chmod(0o600); previous_hashes[key]=item['sha256']
        state={'schema_version':'1.0','date':args.date,'run_id':run_id,'phase':'new','source_fingerprint':source_fp,'input_fingerprints':inputs,'previous_output_hashes':previous_hashes,'created_at':now_iso(),'updated_at':now_iso(),'history':[]}
        if args.apply: run_dir.mkdir(parents=True,exist_ok=True); run_dir.chmod(0o700); state_write(state_path,state,'acquired')
        normalized=normalize(history['messages'],txns); outcomes=merge_journal(transaction_outcomes(txns),journals); outcomes+=daily_outcomes(daily,outcomes)
        reconciled,rec=reconcile(daily,apply=args.apply)
        if not journal_path.is_file(): rec['anomalies'].append('event_journal_missing'); rec['status']='partial' if rec['status']=='completed' else rec['status']
        if history.get('untimed_count',0): rec['anomalies'].append(f"history_messages_without_timestamp:{history['untimed_count']}"); rec['status']='partial' if rec['status']=='completed' else rec['status']
        evidence={'schema_version':'1.0','date':args.date,'messages':normalized,'event_outcomes':outcomes,'reconciliation':rec}
        validate(rec,Path(__file__).resolve().parents[1]/'schemas'/'reconciliation_v1.schema.json'); validate(evidence,Path(__file__).resolve().parents[1]/'schemas'/'evidence_bundle_v1.schema.json')
        provenance={'page_count':len(history['pages']),'source_message_count':history['source_message_count'],'carryover_message_count':history.get('carryover_message_count',0),'current_session_message_count':history.get('current_session_message_count',0),'daily_revision':daily['file_revision']}
        _s = config['_settings']
        _cname = (_s.get('character') or {}).get('name') or 'assistant'
        _cname2 = (_s.get('companion') or {})
        _pname = ((_cname2.get('names') or ['user'])[0] if isinstance(_cname2.get('names'), list) and _cname2.get('names') else 'user')
        chatlog = render_chatlog(args.date, normalized, outcomes, rec, provenance, character_name=_cname, companion_name=_pname); chatlog_path = workspace / 'chatlog' / f'{args.date}.md'; chatlog_result = publish_text(chatlog_path, chatlog, args.apply, previous_hashes.get('chatlog'))
        if not args.apply:
            return {'mode':'dry_run','date':args.date,'source_fingerprint':source_fp,'history_pages':len(history['pages']),'messages':len(normalized),'event_outcomes':len(outcomes),'reconciliation':rec,'planned_outputs':[str(chatlog_path),str(workspace/'daily'/f'{args.date}.md'),str(workspace/'memory'/f'{args.date}.md')],'external_calls':['sessions.list(read_only)','chat.history(read_only)']}
        atomic_write_json(run_dir/'evidence.json',evidence); atomic_write_json(daily_path,reconciled)
        carry_path=Path(config.get('carryover_root') or (Path(config['state_root'])/'carryover'))/f'{args.date}.json'
        receipt_path=rollover_receipt_path(config,args.date)
        state_write(
            state_path,state,'chatlog_written',chatlog=chatlog_result,reconciliation=rec,
            evidence_path=str(run_dir/'evidence.json'),
            transaction_paths=[str(Path(x['_path'])) for x in txns],
            carryover_path=str(carry_path) if carry_path.is_file() else None,
            rollover_receipt_path=str(receipt_path) if receipt_path.is_file() else None,
            memory_status='pending',
        )
        ensure_raw_backup(config,state_path,state,daily_path,journal_path)
        result=finish_memory(config,state_path,state,daily_path,workspace,evidence)
        return {'mode':'apply','status':'completed','date':args.date,'state':str(state_path),'chatlog':chatlog_result,**result}
def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument('--date', required=True); parser.add_argument('--settings', help='framework settings YAML（从它构造 step04 config）'); parser.add_argument('--config', help='旧式 step04 config JSON（与 --settings 二选一）'); parser.add_argument('--apply', action='store_true'); parser.add_argument('--ack'); args = parser.parse_args()
    try: print(json.dumps(run(args),ensure_ascii=False,indent=2)); return 0
    except (StepError,KeyError,OSError,ValueError) as exc: print(f'FATAL: {exc}',file=sys.stderr); return 2
if __name__=='__main__': raise SystemExit(main())
