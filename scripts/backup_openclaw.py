#!/usr/bin/env python3
from __future__ import annotations
import json,subprocess
from pathlib import Path
from typing import Any
from common import StepError, make_at_env, sha256_file
def create(config: dict[str, Any], runner=subprocess.run) -> dict[str, Any]:
    command = [config['openclaw_bin'], 'backup', 'create', '--output', config['backup_root'], '--verify', '--json']
    env = make_at_env(config['_settings'], home=config.get('openclaw_home'))
    try: result=runner(command,text=True,capture_output=True,timeout=900,check=False,env=env)
    except subprocess.TimeoutExpired as exc: raise StepError('backup_unknown: timeout') from exc
    if result.returncode!=0: raise StepError(f'backup_failed: code={result.returncode}')
    try: value=json.loads(result.stdout)
    except json.JSONDecodeError as exc: raise StepError('backup returned invalid JSON') from exc
    def strings(node):
        if isinstance(node,dict):
            for item in node.values(): yield from strings(item)
        elif isinstance(node,list):
            for item in node: yield from strings(item)
        elif isinstance(node,str): yield node
    def verification_passed(node):
        # Do not accept an unrelated nested {status: "passed"} as archive
        # verification.  Support the documented/root form and the CLI's
        # explicit verification object, optionally wrapped in result.
        if not isinstance(node,dict): return False
        roots=[node]
        if isinstance(node.get('result'),dict): roots.append(node['result'])
        for root_node in roots:
            if root_node.get('verified') is True: return True
            verification=root_node.get('verification')
            if isinstance(verification,dict) and (
                verification.get('verified') is True
                or verification.get('passed') is True
                or verification.get('status') in {'passed','verified'}
            ): return True
        return False
    root=Path(config['backup_root']).resolve(); candidates=[Path(x).resolve() for x in strings(value) if x.endswith(('.tar.gz','.tgz'))]; path=next((x for x in candidates if root in x.parents and x.is_file()),None)
    verified=verification_passed(value)
    if path is None or root not in path.parents or not path.is_file() or not verified: raise StepError('backup result is not verified archive')
    path.chmod(0o600); return {'archive':str(path),'verified':True,'size':path.stat().st_size,'sha256':sha256_file(path)}
