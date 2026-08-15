#!/usr/bin/env python3
from __future__ import annotations
from pathlib import Path
from common import StepError,atomic_write_bytes,sha256_bytes
def publish(daily_path:Path,memory_path:Path,payload:bytes,apply:bool,expected_existing_sha256:str|None=None)->dict:
    digest=sha256_bytes(payload)
    for path in (daily_path,memory_path):
        if path.exists() and path.read_bytes()!=payload:
            existing=sha256_bytes(path.read_bytes())
            if expected_existing_sha256 is None or existing!=expected_existing_sha256: raise StepError(f'refusing to overwrite unknown output: {path}')
    if apply:
        atomic_write_bytes(daily_path,payload); atomic_write_bytes(memory_path,payload)
        if daily_path.read_bytes()!=memory_path.read_bytes(): raise StepError('daily/memory mirror mismatch')
    return {'daily_path':str(daily_path),'memory_path':str(memory_path),'sha256':digest,'identical':True,'written':apply}
