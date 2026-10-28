# ---- OpenCode message row → normalized Records ---------------------------
#
# A v2 ``session_message`` row expands into one record per content
# element (a tool element into a call AND a result record, the result a
# pending placeholder while the tool runs, so the ordinal layout of a
# message never changes as it streams); a v1 row is one ``part`` with its
# message envelope. ``_expand(gen, row)`` returns the records in order
# with ``key`` unset; the tail assigns the dense ordinals.

_CANON_BY_TOOL = {
    'bash': 'bash', 'shell': 'bash',
    'read': 'read',
    'edit': 'edit', 'patch': 'edit',
    'write': 'write',
    'grep': 'grep',
    'glob': 'glob', 'list': 'glob',
    'task': 'spawn',
    'question': 'question',
    'webfetch': 'webfetch',
    'websearch': 'websearch',
    'skill': 'skill',
}

# OpenCode input keys → the names the generic one-liners and call bodies read.
_INPUT_ALIASES = {
    'path': 'file_path', 'filePath': 'file_path',
    'oldString': 'old_string', 'newString': 'new_string',
    'replaceAll': 'replace_all',
}

_TURN_END_LEVEL = 4


def _tool_canon(name):
    return _CANON_BY_TOOL.get(name, 'other')


def _ms(v):
    return v / 1000 if isinstance(v, (int, float)) else None


def _first_line(text, n=5000):
    return _trim((text or '').strip().split('\n', 1)[0], n)


def _norm_input(inp):
    if not isinstance(inp, dict):
        return {} if inp is None else {'arguments': inp}
    out = dict(inp)
    for src, dst in _INPUT_ALIASES.items():
        if src in out and dst not in out:
            out[dst] = out[src]
    if isinstance(out.get('command'), list):
        out['command'] = ' '.join(str(c) for c in out['command'])
    if 'name' in out and 'skill' not in out:
        out['skill'] = out['name']
    return out


def _result_glance(name, md, output):
    """Row glance from the tool metadata, else the output."""
    if not isinstance(md, dict):
        md = {}
    if name in ('bash', 'shell') and md.get('exit') not in (None, 0):
        return f'exit {md["exit"]} · {_trim(output or "", 5000)}'
    if 'matches' in md:
        return f'{md["matches"]} matches'
    if 'count' in md:
        return f'{md["count"]} files'
    files = md.get('files')
    if isinstance(files, list) and files:
        return f'{len(files)} file(s): ' + ', '.join(
            str(f.get('file') or f.get('path') or '?') for f in files
            if isinstance(f, dict))
    return None


def _result_diff(md):
    """Unified diff of an edit result: v2 ``metadata.files[].patch``, v1
    ``metadata.diff``."""
    if not isinstance(md, dict):
        return None
    if isinstance(md.get('diff'), str) and md['diff']:
        return md['diff']
    patches = [f['patch'] for f in md.get('files') or []
               if isinstance(f, dict) and isinstance(f.get('patch'), str) and f['patch']]
    return '\n'.join(patches) + '\n' if patches else None


def _tool_records(name, call_id, state, raw, ts, meta):
    """``[tool_call, tool_result]`` for one tool element / part. The result
    is a pending placeholder (no output) until ``state.status`` settles."""
    state = state if isinstance(state, dict) else {}
    status = state.get('status') or 'pending'
    canon = _tool_canon(name)
    call = Record('tool_call', raw, tag='assistant', ts=ts, meta=dict(meta),
                  tools=[new_tool(canon=canon, name=name, call_id=call_id,
                                  input=_norm_input(state.get('input')))])
    if canon == 'spawn' and isinstance(state.get('input'), dict):
        call.tool['peer'] = state['input'].get('description') or None
    md = state.get('metadata')
    if isinstance(state.get('output'), str):
        output = state['output']
    else:
        output = '\n'.join(
            p.get('text') or '' for p in state.get('content') or []
            if isinstance(p, dict)) or None
    done = status in ('completed', 'error')
    if status == 'error':
        output = state.get('error') or output
    result = Record(
        'tool_result', raw, tag=name or 'tool_result',
        ts=_ms((state.get('time') or {}).get('end')) or ts,
        tools=[new_tool(
            canon=canon, name=name, call_id=call_id,
            output=output if done else None,
            glance=(_result_glance(name, md, output) if done else f'({status})'),
            diff=_result_diff(md) if done else None,
            is_error=status == 'error', metadata=md, status=status)])
    if canon in _EDIT_CANONS:
        result.min_level = 3
    return [call, result]


def _assistant_meta(d, v1=False):
    meta = {}
    model = _model_name({'providerID': d.get('providerID'), 'modelID': d.get('modelID')}
                        if v1 else d.get('model'))
    if model:
        meta['model'] = model
    if d.get('agent'):
        meta['agent'] = d['agent']
    return meta


def _stamp_usage(rec, d):
    """Cost and token total on the message's last record."""
    tokens = d.get('tokens') if isinstance(d.get('tokens'), dict) else {}
    total = tokens.get('total') or sum(
        v for k, v in tokens.items() if k != 'cache' and isinstance(v, (int, float)))
    if total:
        rec.meta['tokens'] = int(total)
    if d.get('cost'):
        rec.meta['cost'] = f'${d["cost"]:.4f}'


def _text_record(kind, text, raw, ts, meta, tag):
    return Record(kind, raw, tag=tag, ts=ts, text=text or '',
                  is_voice=bool((text or '').strip()), meta=dict(meta))


# ---- v2: session_message rows --------------------------------------------


def _records_v2(row):
    typ, d = row['type'], row['data']
    ts = _ms((d.get('time') or {}).get('created')) or _ms(row['created'])
    rec_id = row['id']
    if typ == 'user':
        rec = _text_record('user_prompt', d.get('text'), d, ts, {}, 'user')
        rec.id = rec_id
        return [rec]
    if typ == 'assistant':
        return _assistant_v2(row, d, ts)
    if typ == 'idle':
        rec = Record('turn_end', d, tag='idle', ts=ts, id=rec_id,
                     min_level=_TURN_END_LEVEL,
                     summary='idle' + (f' · {d["outcome"]}' if d.get('outcome') else ''))
        return [rec]
    if typ in ('system', 'synthetic', 'skill', 'shell'):
        text = d.get('text') or d.get('command') or ''
        rec = Record('system', d, tag=typ, ts=ts, id=rec_id, text=text,
                     is_meta=typ == 'synthetic',
                     summary=f'{typ}: {_first_line(text)}' if text else typ)
        rec.meta['span_label'] = typ
        return [rec]
    if typ == 'compaction':
        return [Record('compaction', d, tag=typ, ts=ts, id=rec_id,
                       text=d.get('text') or d.get('summary') or '')]
    if typ in ('agent_selected', 'model_selected', 'location_switched'):
        what = d.get('agent') or _model_name(d.get('model')) or d.get('directory') or ''
        return [Record('meta', d, tag=typ, ts=ts, id=rec_id, min_level=5,
                       summary=f'{typ}: {what}' if what else typ)]
    return [Record('unknown', d, tag=typ or 'unknown', ts=ts, id=rec_id)]


def _assistant_v2(row, d, ts):
    meta = _assistant_meta(d)
    elements = [e for e in d.get('content') or [] if isinstance(e, dict)]
    out = []
    last_text = max((i for i, e in enumerate(elements) if e.get('type') == 'text'),
                    default=None)
    for i, el in enumerate(elements):
        et = el.get('type')
        el_ts = _ms((el.get('time') or {}).get('created')) or ts
        ts = max(ts or 0, el_ts or 0) or None
        if et == 'text':
            rec = _text_record('assistant_text', el.get('text'), el, ts, meta, 'assistant')
            if i == last_text and d.get('finish') == 'stop':
                rec.end_turn = True
                rec.min_level = 1
            out.append(rec)
        elif et == 'reasoning':
            out.append(_text_record('thinking', el.get('text'), el, ts, meta, 'reasoning'))
        elif et == 'tool':
            out.extend(_tool_records(el.get('name'), el.get('id'), el.get('state'),
                                     el, ts, meta))
        else:
            out.append(Record('unknown', el, tag=f'content:{et}', ts=ts))
    if not out:
        err = d.get('error')
        if err:
            msg = err.get('data', {}).get('message') if isinstance(err, dict) else err
            msg = msg or (err.get('name') if isinstance(err, dict) else '') or 'error'
            out.append(Record('system', d, tag='error', ts=ts, id=row['id'], text=str(msg),
                              min_level=4, summary=f'error: {_first_line(str(msg))}',
                              tag_style='red', meta=dict(meta)))
        else:
            out.append(Record('meta', d, tag='assistant', ts=ts, id=row['id'], min_level=6,
                              summary=f'assistant · {d.get("finish") or "no content"}',
                              meta=dict(meta)))
    _stamp_usage(out[-1], d)
    return out


# ---- v1: parts with their message envelope -------------------------------


def _records_v1(row):
    msg, part = row['message'], row['data']
    role = msg.get('role')
    pt = part.get('type')
    ts = (_ms((part.get('time') or {}).get('start'))
          or _ms((msg.get('time') or {}).get('created')) or _ms(row['created']))
    meta = _assistant_meta(msg, v1=True) if role == 'assistant' else {}
    raw = {'message_id': row['mid'], 'part': part}
    if pt == 'text':
        if role == 'user':
            return [_text_record('user_prompt', part.get('text'), raw, ts, {}, 'user')]
        rec = _text_record('assistant_text', part.get('text'), raw, ts, meta, 'assistant')
        if row.get('last_text') and msg.get('finish') == 'stop':
            rec.end_turn = True
            rec.min_level = 1
        return [rec]
    if pt == 'reasoning':
        return [_text_record('thinking', part.get('text'), raw, ts, meta, 'reasoning')]
    if pt == 'tool':
        return _tool_records(part.get('tool'), part.get('callID'), part.get('state'),
                             raw, ts, meta)
    if pt == 'step-start':
        return [Record('turn_start', raw, tag='step', ts=ts, summary='step start',
                       meta=dict(meta))]
    if pt == 'step-finish':
        rec = Record('turn_end', raw, tag='step', ts=ts, min_level=_TURN_END_LEVEL,
                     summary=f'step finish · {part.get("reason") or "?"}', meta=dict(meta))
        _stamp_usage(rec, part)
        return [rec]
    if pt == 'compaction':
        return [Record('compaction', raw, tag=pt, ts=ts, text=part.get('text') or '')]
    if pt == 'retry':
        return [Record('meta', raw, tag=pt, ts=ts, min_level=5,
                       summary=f'retry: {_first_line(str(part.get("error") or ""))}' or pt)]
    if pt in ('snapshot', 'patch', 'file', 'agent', 'subtask'):
        return [Record('meta', raw, tag=pt, ts=ts, min_level=6, summary=pt)]
    return [Record('unknown', raw, tag=pt or 'unknown', ts=ts)]


def _expand(gen, row):
    recs = _records_v2(row) if gen == 'v2' else _records_v1(row)
    for i, rec in enumerate(recs):
        if rec.id is None:
            rec.id = f'{row["id"]}#{i}'
    return recs
