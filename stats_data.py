"""Read local agent logs into usage events and searchable conversation turns."""
import json
import re
import shutil
import sqlite3
import subprocess
from collections import defaultdict
from datetime import datetime
from pathlib import Path

TOOLS = ('claude', 'codex', 'pi', 'opencode', 'grok', 'dsh')
ZSTD = shutil.which('zstd')
DSH_VERSION_RE = re.compile(r'\.v(\d+)\.jsonl\.zstd$')
FIELDS = ('new', 'cc', 'cr', 'out', 'msgs')
GENERATED = ('environment_context', 'recommended_plugins', 'codex_delegation', 'turn_aborted', 'skill',
             'user_instructions', 'system-reminder', 'local-command-stdout', 'bash-stdout', 'bash-stderr',
             'task-notification', 'codex_internal_context', 'in-app-browser-context')
GENERATED_RE = re.compile(r'\s*<(?:' + '|'.join(GENERATED) + r')(?:\s[^>]*)?>'
                          r'|\s*#\s*AGENTS\.md instructions for '
                          r'|\s*## Referenced chats with Codex:'
                          r'|\s*## Code review guidelines:'
                          r'|\s*\[idle-notify:')
COMMENT_RE = re.compile(r'\s*<!--.*?-->', re.S)
COMMAND_NAME_RE = re.compile(r'<command-name>(.*?)</command-name>', re.S)
COMMAND_MESSAGE_RE = re.compile(r'<command-message>(.*?)</command-message>', re.S)
COMMAND_ARGS_RE = re.compile(r'<command-args>(.*?)</command-args>', re.S)


def command_input(value):
    if not value.lstrip().startswith('<command'):
        return None
    name = COMMAND_NAME_RE.search(value) or COMMAND_MESSAGE_RE.search(value)
    args = COMMAND_ARGS_RE.search(value)
    label = name.group(1).strip() if name else ''
    argument = args.group(1).strip() if args else ''
    return ' '.join(part for part in (label, argument) if part) or None


def stamp(value):
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000 if value > 1e11 else value).astimezone().isoformat()
        return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone().isoformat()
    except (ValueError, TypeError, AttributeError, OSError):
        return ''


def zstd_lines(path):
    if not ZSTD:
        raise OSError(f'{path.name}: 未找到 zstd 命令，无法读取 dsh 会话')
    process = subprocess.Popen([ZSTD, '-dc', '--quiet', str(path)], stdout=subprocess.PIPE,
                               text=True, encoding='utf-8', errors='replace')
    try:
        yield from process.stdout
    finally:
        process.stdout.close()
        process.wait()
    if process.returncode != 0:
        raise ValueError(f'{path.name}: zstd 解压失败')


def read_jsonl(path, warnings):
    skipped = 0
    stream = zstd_lines(path) if path.suffix == '.zstd' else path.open(encoding='utf-8', errors='replace')
    try:
        for number, line in enumerate(stream, 1):
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    yield value
                else:
                    skipped += 1
            except ValueError:
                warnings.append(f'{path.name}:{number} 无法解析，已跳过')
    finally:
        if hasattr(stream, 'close'):
            stream.close()
    if skipped:
        warnings.append(f'{path.name}: {skipped} 条非对象记录已跳过')


def clean_input(value):
    comment = COMMENT_RE.match(value)
    return value[comment.end():].strip() if comment else value


def machine_input(value):
    return bool(GENERATED_RE.match(value))


def text_of(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return '\n'.join(filter(None, (text_of(v) for v in value)))
    if isinstance(value, dict):
        for key in ('text', 'content', 'thinking'):
            if key in value:
                return text_of(value[key])
        if value.get('type') in ('image', 'input_image', 'image_url'):
            return '[图片]'
    return ''


def blocks(content, role='assistant', channel=None):
    if isinstance(content, str):
        return [{'kind': '工具结果' if role in ('tool', 'toolResult') else '正文', 'text': content}] if content else []
    result = []
    for part in content or []:
        if not isinstance(part, dict):
            continue
        typ = part.get('type', '')
        if role in ('tool', 'toolResult') or typ == 'tool_result':
            kind, value = '工具结果', text_of(part.get('content', part))
        elif typ == 'tool':
            kind = '工具调用'
            value = part.get('name', part.get('tool', '')) + '\n' + json.dumps((part.get('state') or {}).get('input', {}), ensure_ascii=False, indent=2)
        elif typ in ('tool_use', 'toolCall', 'tool-call'):
            kind = '工具调用'
            arguments = part.get('input', part.get('arguments', {}))
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    pass
            value = part.get('name', part.get('tool', '')) + '\n' + json.dumps(arguments, ensure_ascii=False, indent=2)
        elif typ in ('thinking', 'reasoning'):
            kind, value = '思考', text_of(part)
        else:
            kind, value = ('过程' if channel == 'commentary' else '正文'), text_of(part)
        if value:
            result.append({'kind': kind, 'text': value})
    return result


def opencode_blocks(content):
    """OpenCode V2 的 content：工具部分紧接调用块给出结果，其余复用 blocks()。"""
    result = []
    for part in content or []:
        if not isinstance(part, dict):
            continue
        result.extend(blocks([part]))
        if part.get('type') == 'tool':
            state = part.get('state') or {}
            value = text_of(state.get('content')) or text_of(state.get('output'))
            if value:
                result.append({'kind': '工具结果', 'text': value})
    return result


def usage_values(agent, u):
    if agent == 'claude':
        return [u.get(k, 0) or 0 for k in ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens', 'output_tokens')] + [1]
    if agent == 'pi':
        return [u.get('input', 0), u.get('cacheWrite', 0), u.get('cacheRead', 0), u.get('output', 0) + u.get('reasoning', 0), 1]
    if agent == 'opencode':
        cache = u.get('cache') or {}
        return [u.get('input', 0), cache.get('write', 0), cache.get('read', 0), u.get('output', 0) + u.get('reasoning', 0), 1]
    if agent == 'grok':
        cr, cc = u.get('cachedReadTokens', 0), u.get('cacheCreationTokens', 0)
        return [max(u.get('inputTokens', 0) - cr - cc, 0), cc, cr, u.get('outputTokens', 0), u.get('modelCalls', 1)]
    if agent == 'dsh':
        # dsh 的 inputTokens 不含缓存读取，outputTokens 已含思考（totalTokens 可验证）。
        return [u.get('inputTokens', 0), 0, u.get('cacheReadTokens', 0), u.get('outputTokens', 0), 1]
    cr, cc = u.get('cached_input_tokens', 0), u.get('cache_write_input_tokens', 0)
    # Codex output_tokens includes reasoning; total_tokens confirms that boundary.
    return [max(u.get('input_tokens', 0) - cr - cc, 0), cc, cr, u.get('output_tokens', 0), 1]


class Dataset:
    def __init__(self):
        self.usage, self.turns, self.warnings = [], [], []
        self.seen = {}
        self.sources = defaultdict(int)

    def add_usage(self, agent, session, model, ts, usage, key):
        identity = (agent, key)
        if not usage:
            return
        if identity in self.seen:
            existing = self.seen[identity]
            for field, value in zip(FIELDS, usage_values(agent, usage)):
                existing[field] = max(existing[field], value)
            return
        ts = stamp(ts)
        if not ts:
            self.warnings.append(f'{agent}/{session} 用量缺少有效时间，已跳过')
            return
        values = usage_values(agent, usage)
        event = dict(agent=agent, session=session, model=model or 'unknown', day=ts[:10],
                     key=key if isinstance(key, str) else json.dumps(key, ensure_ascii=False),
                     **dict(zip(FIELDS, values)))
        self.usage.append(event)
        self.seen[identity] = event

    def read_file(self, agent, path):
        records = list(read_jsonl(path, self.warnings))
        self.sources[agent] += 1
        session = path.parent.name if agent in ('grok', 'dsh') else path.stem
        generated = path.parent.name == 'subagents'
        current = None
        model = 'unknown'
        previous_total = None
        seen_messages = set()
        precise_turns = {(d.get('payload') or {}).get('turn_id') for d in records if d.get('type') == 'token_usage_record'}
        turn_id = None
        has_response_user = any(d.get('type') == 'response_item' and (d.get('payload') or {}).get('role') == 'user' for d in records)

        def user(value, ts, identity, append=False, words=True):
            nonlocal current
            value = clean_input(value or '')
            if not value:
                return
            command = command_input(value)
            if command is not None:
                value = command
            if append and current:
                current['input'] += value
                return
            current = dict(agent=agent, session=session, model=model, timestamp=stamp(ts), input=value, output=[], source=str(path), key=str(identity))
            if command is not None:
                current['command'] = True
                current['words'] = False
            elif not words or machine_input(value):
                current['words'] = False
            self.turns.append(current)

        def output(parts, join=False):
            if current:
                for part in parts:
                    if join and current['output'] and current['output'][-1]['kind'] == part['kind']:
                        current['output'][-1]['text'] += part['text']
                    else:
                        current['output'].append(part)
                if model != 'unknown':
                    models = set(current['model'].split(' · ')) - {'unknown'}
                    models.add(model)
                    current['model'] = ' · '.join(sorted(models))

        prompt_index = None
        for index, d in enumerate(records):
            typ, ts = d.get('type'), d.get('timestamp')
            if agent in ('claude', 'pi'):
                m = d.get('message') or {}
                if typ == 'model_change':
                    model = d.get('modelId') or model
                if m.get('model'):
                    model = m['model']
                if not m:
                    continue
                identity = d.get('uuid') or d.get('id') or index
                if identity in seen_messages:
                    continue
                seen_messages.add(identity)
                role, content = m.get('role'), m.get('content')
                if role == 'user':
                    if isinstance(content, list):
                        tool_parts = [p for p in content if p.get('type') == 'tool_result']
                        output(blocks(tool_parts))
                        content = [p for p in content if p.get('type') != 'tool_result']
                    if not d.get('isMeta'):
                        user(text_of(content), ts, identity,
                             words=not (generated or d.get('isCompactSummary') or d.get('isSidechain')))
                elif role in ('assistant', 'toolResult'):
                    output(blocks(content, role))
                if role == 'assistant' and m.get('usage'):
                    # Pi IDs are short and scoped to the session lineage.
                    key = m.get('id') if agent == 'claude' else (identity, ts, model)
                    self.add_usage(agent, session, model, ts, m['usage'], key or (str(path), index))
            elif agent == 'codex':
                p = d.get('payload') or {}
                if typ == 'session_meta':
                    session = p.get('id') or session
                    generated = generated or (isinstance(p.get('source'), dict) and 'subagent' in p['source'])
                if typ == 'turn_context':
                    model = p.get('model') or model
                    turn_id = p.get('turn_id')
                if typ == 'world_state':
                    model = (p.get('state') or {}).get('model') or model
                if typ == 'token_usage_record':
                    self.add_usage(agent, session, model, ts, p.get('usage'), p.get('response_id') or (session, d.get('ordinal', index)))
                if typ == 'event_msg' and p.get('type') == 'token_count' and turn_id not in precise_turns:
                    info = p.get('info') or {}
                    total = info.get('total_token_usage')
                    if total and total == previous_total:
                        continue
                    previous_total = total
                    self.add_usage(agent, session, model, ts, info.get('last_token_usage'), (session, d.get('ordinal', index)))
                if typ == 'event_msg' and p.get('type') == 'user_message' and not has_response_user:
                    user(p.get('message', ''), ts, index, words=not generated)
                if typ != 'response_item':
                    continue
                identity = p.get('id') or d.get('ordinal', index)
                if identity in seen_messages:
                    continue
                seen_messages.add(identity)
                pt = p.get('type', '')
                if pt == 'message' and p.get('role') == 'user':
                    content = p.get('content') or []
                    kinds = (p.get('internal_chat_message_metadata_passthrough') or {}).get('content_item_kinds')
                    if kinds and len(kinds) == len(content):
                        content = [item for item, kind in zip(content, kinds) if kind.startswith('user.')]
                    user(text_of(content), ts, identity, words=not generated)
                elif pt == 'message' and p.get('role') == 'assistant':
                    output(blocks(p.get('content'), channel=p.get('channel')))
                elif pt == 'reasoning':
                    output([{'kind': '思考', 'text': text_of(p.get('summary'))}] if text_of(p.get('summary')) else [])
                elif pt.endswith('_call_output'):
                    output([{'kind': '工具结果', 'text': text_of(p.get('output')) or json.dumps(p.get('output'), ensure_ascii=False)}])
                elif pt.endswith('_call'):
                    output([{'kind': '工具调用', 'text': p.get('name', pt) + '\n' + text_of(p.get('arguments', p.get('input', '')))}])
            elif agent == 'dsh':
                p = d.get('data') or {}
                if typ == 'session':
                    session = d.get('id') or session
                    generated = bool(d.get('delegationDepth'))
                elif typ == 'user/message':
                    identity = p.get('id') or index
                    if identity in seen_messages:
                        continue
                    seen_messages.add(identity)
                    human = (p.get('source') or {}).get('kind') == 'user'
                    text = text_of(p.get('content'))
                    if not text:
                        continue
                    if not human and current:
                        output([{'kind': '上下文', 'text': text}])
                    else:
                        user(text, d.get('time'), identity, words=human and not generated)
                elif typ == 'assistant/message':
                    message = p.get('message') or {}
                    identity = message.get('id') or index
                    if identity in seen_messages:
                        continue
                    seen_messages.add(identity)
                    model = (message.get('source') or {}).get('model') or model
                    output(blocks(message.get('content')))
                    if p.get('usage'):
                        self.add_usage(agent, session, model, d.get('time'), p['usage'], identity)
                elif typ == 'tool/result':
                    output(blocks((p.get('message') or {}).get('content'), role='tool'))
                elif typ == 'compaction/summary':
                    identity = p.get('compactionId') or index
                    summary = text_of(p.get('summary'))
                    if summary:
                        user(summary, d.get('time'), identity, words=False)
                    if p.get('usage'):
                        self.add_usage(agent, session, p.get('model') or model, d.get('time'), p['usage'], identity)
            elif agent == 'grok':
                p = d.get('params') or {}
                u, meta = p.get('update') or {}, p.get('_meta') or {}
                kind = u.get('sessionUpdate')
                session = p.get('sessionId') or session
                model = (u.get('_meta') or {}).get('modelId') or model
                ts = meta.get('agentTimestampMs') or ts
                identity = meta.get('eventId') or index
                if identity in seen_messages:
                    continue
                seen_messages.add(identity)
                if kind == 'user_message_chunk':
                    pi = (u.get('_meta') or {}).get('promptIndex')
                    user(text_of(u.get('content')), ts, identity, pi is not None and pi == prompt_index)
                    prompt_index = pi
                elif kind in ('agent_thought_chunk', 'agent_message_chunk'):
                    output([{'kind': '思考' if kind == 'agent_thought_chunk' else '正文', 'text': text_of(u.get('content'))}], join=True)
                elif kind == 'tool_call':
                    output([{'kind': '工具调用', 'text': u.get('title', '') + '\n' + json.dumps(u.get('rawInput'), ensure_ascii=False, indent=2)}])
                elif kind == 'tool_call_update' and ('rawOutput' in u or u.get('content')):
                    output([{'kind': '工具结果', 'text': text_of(u.get('content')) or json.dumps(u.get('rawOutput'), ensure_ascii=False, indent=2)}])
                elif kind == 'turn_completed':
                    usage = u.get('usage') or {}
                    for name, values in (usage.get('modelUsage') or {model: usage}).items():
                        self.add_usage(agent, session, name, ts, values, (identity, name))
                    prompt_index = None
        self.turns = [t for t in self.turns if not (t.get('command') and not t['output'])]

    def read_opencode(self, path):
        self.sources['opencode'] += 1
        con = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
        try:
            tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            # 旧版 message/part 与新版 session_message 可能并存（升级按会话迁移，未迁移的旧轮次仍在旧表），两套都读。
            if {'message', 'part'} <= tables or 'session_message' not in tables:
                self.read_opencode_v1(con, path)
            if 'session_message' in tables:
                self.read_opencode_v2(con, path, tables)
        finally:
            con.close()

    def read_opencode_v1(self, con, path):
        parts = defaultdict(list)
        for mid, raw in con.execute('select message_id,data from part order by time_created,id'):
            parts[mid].append(json.loads(raw))
        current = {}
        for mid, sid, ts, raw in con.execute('select id,session_id,time_created,data from message order by time_created,id'):
            m = json.loads(raw)
            role = m.get('role')
            model = m.get('modelID') or (m.get('model') or {}).get('modelID') or 'unknown'
            if role == 'user':
                value = clean_input(text_of([p for p in parts[mid] if not p.get('synthetic')]))
                if value:
                    turn = dict(agent='opencode', session=sid, model=model, timestamp=stamp(ts), input=value, output=[], source=str(path), key=mid)
                    if machine_input(value):
                        turn['words'] = False
                    self.turns.append(turn)
                    current[(sid, mid)] = turn
                    current[sid] = turn
            elif role == 'assistant':
                turn = current.get((sid, m.get('parentID'))) or current.get(sid)
                if turn:
                    turn['output'].extend(blocks(parts[mid]))
                    for p in parts[mid]:
                        if p.get('type') == 'tool' and (p.get('state') or {}).get('output'):
                            turn['output'].append({'kind': '工具结果', 'text': text_of(p['state']['output'])})
                    models = set(turn['model'].split(' · ')) - {'unknown'}
                    models.add(model)
                    turn['model'] = ' · '.join(sorted(models))
                self.add_usage('opencode', sid, model, ts, m.get('tokens'), mid)

    def read_opencode_v2(self, con, path, tables):
        session_models = {}
        if 'session_v2' in tables:
            for sid, raw in con.execute('select id,model from session_v2'):
                try:
                    session_models[sid] = (json.loads(raw) or {}).get('id') or 'unknown'
                except (ValueError, TypeError, AttributeError):
                    session_models[sid] = 'unknown'
        current = {}
        for mid, sid, typ, ts, raw in con.execute('select id,session_id,type,time_created,data '
                                                  'from session_message order by time_created,id'):
            if typ not in ('user', 'assistant'):
                continue
            try:
                m = json.loads(raw)
            except ValueError:
                self.warnings.append(f'{path.name}: {mid} 无法解析，已跳过')
                continue
            if typ == 'user':
                value = clean_input(text_of(m.get('text')))
                if value:
                    turn = dict(agent='opencode', session=sid, model=session_models.get(sid, 'unknown'),
                                timestamp=stamp(ts), input=value, output=[], source=str(path), key=mid)
                    if machine_input(value):
                        turn['words'] = False
                    self.turns.append(turn)
                    current[sid] = turn
            else:
                model = (m.get('model') or {}).get('id') or 'unknown'
                turn = current.get(sid)
                if turn:
                    turn['output'].extend(opencode_blocks(m.get('content')))
                    models = set(turn['model'].split(' · ')) - {'unknown'}
                    models.add(model)
                    turn['model'] = ' · '.join(sorted(models))
                self.add_usage('opencode', sid, model, ts, m.get('tokens'), mid)


def discover(home=None):
    root = Path(home or Path.home()).resolve()
    patterns = {'claude': '.claude/projects/**/*.jsonl', 'codex': '.codex/sessions/**/*.jsonl',
                'pi': '.pi/agent/sessions/**/*.jsonl', 'grok': '.grok/sessions/**/updates.jsonl',
                'dsh': '.dsh/sessions/*/session*/*.jsonl.zstd'}
    found = []
    for agent, pattern in patterns.items():
        paths = set(root.glob(pattern))
        if agent == 'codex':
            paths.update(root.glob('.codex/archived_sessions/**/*.jsonl'))
        if agent == 'dsh':
            versions = {}
            for path in paths:
                match = DSH_VERSION_RE.search(path.name)
                versions.setdefault(path.parent, []).append((int(match.group(1)) if match else 0, path))
            paths = {max(entries, key=lambda entry: (entry[0], entry[1].name))[1] for entries in versions.values()}
        found.extend((agent, path) for path in sorted(paths))
    db = root / '.local/share/opencode/opencode.db'
    if db.exists():
        found.append(('opencode', db))
    return found


def read_agent(data, agent, path):
    if agent == 'opencode':
        data.read_opencode(path)
    else:
        data.read_file(agent, path)


def collect(home=None):
    data = Dataset()
    for agent, path in discover(home):
        try:
            read_agent(data, agent, path)
        except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error) as error:
            data.warnings.append(f'{agent}/{path.name}: {error}')
    return data
