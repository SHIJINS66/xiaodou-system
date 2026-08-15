#!/usr/bin/env python3
"""Small dependency-free validator for the exact Draft 2020-12 subset used here."""
from __future__ import annotations
import re
from datetime import date,datetime
from typing import Any

SUPPORTED={'$schema','$id','$defs','$ref','title','description','type','const','enum','required','properties','additionalProperties','items','minItems','maxItems','uniqueItems','minLength','maxLength','pattern','minimum','maximum','format','allOf','if','then','else','not'}

def _ptr(root:dict[str,Any],ref:str)->dict[str,Any]:
    if not ref.startswith('#/'): raise ValueError(f'只允许内部 $ref：{ref}')
    cur:Any=root
    for raw in ref[2:].split('/'):
        key=raw.replace('~1','/').replace('~0','~')
        if not isinstance(cur,dict) or key not in cur: raise ValueError(f'无法解析 $ref：{ref}')
        cur=cur[key]
    if not isinstance(cur,dict): raise ValueError(f'$ref 目标不是 schema：{ref}')
    return cur

def audit_schema(schema:dict[str,Any])->list[str]:
    errors=[]
    def walk(node:Any,path:str,root:dict[str,Any]):
        if isinstance(node,dict):
            for key,value in node.items():
                if key not in SUPPORTED and path not in {'#/properties','#/$defs'}: errors.append(f'{path}: 不支持关键字 {key}')
                if key=='$ref':
                    try: _ptr(root,value)
                    except (ValueError,TypeError) as exc: errors.append(str(exc))
                child_path=path+'/'+key
                if key in {'properties','$defs'} and isinstance(value,dict):
                    for n,child in value.items(): walk(child,child_path,root)
                elif key=='additionalProperties' and isinstance(value,dict): walk(value,child_path,root)
                elif key in {'items','if','then','else','not'}: walk(value,child_path,root)
                elif key=='allOf' and isinstance(value,list):
                    for child in value: walk(child,child_path,root)
    walk(schema,'#',schema); return errors

def _type_ok(value:Any,want:str)->bool:
    return {'object':isinstance(value,dict),'array':isinstance(value,list),'string':isinstance(value,str),'integer':isinstance(value,int) and not isinstance(value,bool),'number':isinstance(value,(int,float)) and not isinstance(value,bool),'boolean':isinstance(value,bool),'null':value is None}.get(want,False)

def validate(instance:Any,schema:dict[str,Any])->list[str]:
    errors=[]
    def check(value:Any,node:dict[str,Any],path:str):
        if '$ref' in node: check(value,_ptr(schema,node['$ref']),path)
        if 'const' in node and value!=node['const']: errors.append(f'{path}: 必须等于 {node["const"]!r}')
        if 'enum' in node and value not in node['enum']: errors.append(f'{path}: 不在允许值中')
        types=node.get('type'); types=[types] if isinstance(types,str) else types
        if types and not any(_type_ok(value,t) for t in types): errors.append(f'{path}: 类型必须为 {types}'); return
        if isinstance(value,dict):
            for key in node.get('required',[]):
                if key not in value: errors.append(f'{path}: 缺少字段 {key}')
            props=node.get('properties',{})
            for key,child in props.items():
                if key in value: check(value[key],child,f'{path}.{key}')
            extra=set(value)-set(props)
            ap=node.get('additionalProperties',True)
            if ap is False and extra: errors.append(f'{path}: 不允许额外字段 {sorted(extra)}')
            elif isinstance(ap,dict):
                for key in extra: check(value[key],ap,f'{path}.{key}')
        if isinstance(value,list):
            if len(value)<node.get('minItems',0): errors.append(f'{path}: 数量小于 {node["minItems"]}')
            if 'maxItems' in node and len(value)>node['maxItems']: errors.append(f'{path}: 数量大于 {node["maxItems"]}')
            if node.get('uniqueItems'):
                import json
                vals=[json.dumps(v,sort_keys=True,ensure_ascii=False) for v in value]
                if len(vals)!=len(set(vals)): errors.append(f'{path}: 元素必须唯一')
            if isinstance(node.get('items'),dict):
                for i,item in enumerate(value): check(item,node['items'],f'{path}[{i}]')
        if isinstance(value,str):
            if len(value)<node.get('minLength',0): errors.append(f'{path}: 字符串过短')
            if 'maxLength' in node and len(value)>node['maxLength']: errors.append(f'{path}: 字符串过长')
            if 'pattern' in node and re.search(node['pattern'],value) is None: errors.append(f'{path}: 格式不匹配 {node["pattern"]}')
            if node.get('format')=='date':
                try: date.fromisoformat(value)
                except ValueError: errors.append(f'{path}: 不是有效 date')
            if node.get('format')=='date-time':
                try:
                    dt=datetime.fromisoformat(value.replace('Z','+00:00'))
                    if dt.tzinfo is None: raise ValueError
                except ValueError: errors.append(f'{path}: 不是带时区的 date-time')
        if isinstance(value,(int,float)) and not isinstance(value,bool):
            if 'minimum' in node and value<node['minimum']: errors.append(f'{path}: 小于最小值')
            if 'maximum' in node and value>node['maximum']: errors.append(f'{path}: 大于最大值')
        for child in node.get('allOf',[]): check(value,child,path)
        if 'not' in node:
            before=len(errors); check(value,node['not'],path)
            if len(errors)==before: errors.append(f'{path}: 命中禁止规则')
            else: del errors[before:]
        if 'if' in node:
            before=len(errors); check(value,node['if'],path); matched=len(errors)==before; del errors[before:]
            branch=node.get('then' if matched else 'else')
            if branch: check(value,branch,path)
    check(instance,schema,'$'); return errors
