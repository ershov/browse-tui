# ---- Codex extras: command status line -----------------------------------
#
# The generic renderers carry Codex: the only addition is a status line
# (exit code, duration, cwd, parsed_cmd kinds) on completed
# ``exec_command`` results, which the common bash umbrella does not know.


def _exec_status_line(result):
    """``exit 0 · 1.2s · cwd /x · read, search`` for a CommandExecution
    result, or ``''`` for a pending / raw result."""
    if result is None or 'exit_code' not in result:
        return ''
    bits = []
    code = result.get('exit_code')
    if code is not None:
        color = GREEN if code == 0 else RED
        bits.append(f'{color}exit {code}{RESET}')
    elif result.get('status'):
        bits.append(str(result['status']))
    dur = result.get('duration')
    if isinstance(dur, dict) and dur.get('secs') is not None:
        secs = dur['secs'] + (dur.get('nanos') or 0) / 1e9
        bits.append(f'{secs:.1f}s')
    if result.get('cwd'):
        bits.append(f'cwd {result["cwd"]}')
    kinds = sorted({pc.get('type') for pc in result.get('parsed_cmd') or []
                    if isinstance(pc, dict) and pc.get('type')})
    if kinds:
        bits.append(', '.join(kinds))
    return f'{MUTE}{" · ".join(bits)}{RESET}' if bits else ''


def _fmt_umbrella_exec(call, result):
    body = _fmt_umbrella_bash(call, result)
    status = _exec_status_line(result)
    return f'{body}\n{status}' if status else body


def _fmt_result_exec(call, result):
    status = _exec_status_line(result)
    body = _result_text(result)
    return f'{status}\n{body}' if status and body else status or body


_TOOL_FORMATTERS['exec_command'] = {
    'call': _TOOL_FORMATTERS['bash']['call'],
    'umbrella': _fmt_umbrella_exec,
    'result': _fmt_result_exec,
}
