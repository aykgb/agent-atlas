#!/usr/bin/env python3
"""Local usage dashboard and conversation index."""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import urllib.request
from collections import Counter, defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from stats_data import FIELDS, TOOLS, Dataset, discover, read_agent

ROOT = Path(__file__).resolve().parent
EXCLUDE_FILE = ROOT / 'words-exclude.txt'
REMOTES_FILE = ROOT / 'remotes.json'
LOCAL_FILE = ROOT / 'local.json'
SCHEMA_VERSION = 5
AUTO_REFRESH_SECONDS = 30 * 60
AUTO_REFRESH_STEP_SECONDS = 60
MAX_BODY_BYTES = 1024 * 1024
STOP = set('的 了 和 是 在 我 你 他 她 它 这 那 一个 一些 我们 你们 他们 可以 需要 使用 进行 以及 并且 如果 然后 这个 那个 不 有 就 都 也 到 与 为 对 中 将 请 把 被 要 吗 呢 啊 the a an and or is are to of in for on with this that it be as at by from you your i we'.split())
LOOPBACK = frozenset({'127.0.0.1', 'localhost', '::1'})
DEFAULT_ALLOWED = ('100.64.216.70',)

EXTRA_INDEXES = """
CREATE INDEX IF NOT EXISTS turn_session ON turns(agent,session,timestamp);
CREATE INDEX IF NOT EXISTS turn_time ON turns(timestamp DESC,id DESC);
"""

SCHEMA = """
CREATE TABLE usage(day TEXT,agent TEXT,model TEXT,new INTEGER,cc INTEGER,cr INTEGER,out INTEGER,msgs INTEGER);
CREATE INDEX usage_filter ON usage(day,agent,model);
CREATE TABLE turns(id INTEGER PRIMARY KEY,agent TEXT,session TEXT,model TEXT,day TEXT,timestamp TEXT,input TEXT,output TEXT,source TEXT,file TEXT,identity TEXT,fingerprint TEXT);
CREATE INDEX turn_filter ON turns(day,agent);
CREATE INDEX turn_file ON turns(file);
CREATE INDEX turn_identity ON turns(identity);
""" + EXTRA_INDEXES + """CREATE VIRTUAL TABLE search USING fts5(search_text,content='',contentless_delete=1,tokenize='trigram');
CREATE TABLE terms(term TEXT,turn_id INTEGER,count INTEGER,PRIMARY KEY(term,turn_id));
CREATE INDEX terms_turn ON terms(turn_id);
CREATE TABLE files(path TEXT PRIMARY KEY,agent TEXT,size INTEGER,warnings TEXT);
CREATE TABLE file_usage(path TEXT,agent TEXT,key TEXT,day TEXT,model TEXT,session TEXT,new INTEGER,cc INTEGER,cr INTEGER,out INTEGER,msgs INTEGER);
CREATE INDEX file_usage_filter ON file_usage(day,agent,model);
CREATE TABLE session_usage(agent TEXT,session TEXT,new INTEGER,cc INTEGER,cr INTEGER,out INTEGER,msgs INTEGER,PRIMARY KEY(agent,session));
CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
"""
# 跨文件重复的用量按 (agent,key) 取各字段最大值；同一 key 改写过 session id 时归给字典序最小的那个。
DEDUPED_USAGE = ("SELECT agent,key,day,model,MIN(session) AS session,MAX(new) AS new,MAX(cc) AS cc,"
                 "MAX(cr) AS cr,MAX(out) AS out,MAX(msgs) AS msgs FROM file_usage GROUP BY agent,key,day,model")
OUTPUT_TEXT = "COALESCE((SELECT group_concat(json_extract(part.value,'$.text'),char(10)) FROM json_each(turns.output) AS part), '')"
SEARCH_TEXT = "input || char(10) || " + OUTPUT_TEXT
SEARCH_SCOPES = {
    'all': None,
    'input': (),
    'text': ('正文',),
    'thinking': ('思考',),
    'tool': ('工具调用', '工具结果'),
}
SCOPE_WEIGHTS = {'输入': 3, '正文': 2, '思考': 1, '工具调用': 1, '工具结果': 1, '上下文': 1, '过程': 1}


def allowed_hosts(bind='127.0.0.1', extra=()):
    names = set(LOOPBACK) | set(DEFAULT_ALLOWED)
    if bind not in ('0.0.0.0', '::'):
        names.add(bind.casefold())
    for entry in extra:
        name = urlsplit('//' + entry.strip()).hostname
        if name:
            names.add(name)
    return names


def clean_remotes(entries, strict=False):
    if not isinstance(entries, list):
        if strict:
            raise ValueError('remotes 须为数组')
        return []
    remotes, seen = [], set()
    for entry in entries:
        if not isinstance(entry, dict):
            if strict:
                raise ValueError('每台远端须为对象')
            continue
        host = urlsplit('//' + str(entry.get('host', '')).strip()).hostname
        try:
            port = int(entry.get('port'))
        except (TypeError, ValueError):
            port = 0
        if not host or not 1 <= port <= 65535:
            if strict:
                raise ValueError('主机或端口无效')
            continue
        if (host, port) in seen:
            continue
        seen.add((host, port))
        label = str(entry.get('label') or '').strip()[:60]
        sections = {key: bool(entry.get(key, True)) for key in ('usage', 'search', 'words')}
        sections['search'] = sections['search'] or sections['words']
        remotes.append({'host': host, 'port': port, 'label': label,
                        'enabled': bool(entry.get('enabled', True)), **sections})
    return remotes


def load_remotes(path=REMOTES_FILE):
    try:
        return clean_remotes(json.loads(path.read_text(encoding='utf-8')))
    except (OSError, ValueError):
        return []


def save_remotes(entries, path=REMOTES_FILE):
    remotes = clean_remotes(entries, strict=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(remotes, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    return remotes


def clean_local(config):
    """把 local.json 清洗成本机启动参数；非法字段丢弃，缺失即用内置默认。"""
    if not isinstance(config, dict):
        return {}
    cleaned = {}
    port = config.get('port')
    if isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535:
        cleaned['port'] = port
    for key in ('host', 'home'):
        value = config.get(key)
        if isinstance(value, str) and value.strip():
            cleaned[key] = value.strip()
    allowed = config.get('allow_host')
    if isinstance(allowed, str):
        allowed = [allowed]
    if isinstance(allowed, list):
        cleaned['allow_host'] = [item.strip() for item in allowed if isinstance(item, str) and item.strip()]
    return cleaned


def load_local(path=LOCAL_FILE):
    try:
        return clean_local(json.loads(path.read_text(encoding='utf-8')))
    except (OSError, ValueError):
        return {}


def save_local(config, path=LOCAL_FILE):
    """把本机启动参数写回 local.json（启动成功后同步，供下次启动复用）。"""
    cleaned = clean_local(config)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    return cleaned


def index_suffix(home):
    """索引文件后缀：按解析后的日志根目录（home，缺省为当前用户主目录）区分，形如 -<8位哈希>。"""
    resolved = str(Path(home).resolve()) if home else str(Path.home().resolve())
    return '-' + hashlib.sha256(resolved.encode()).hexdigest()[:8]


def display_url(host, port):
    address = host if host not in ('0.0.0.0', '::') else '127.0.0.1'
    if ':' in address:
        address = '[' + address + ']'
    return f'http://{address}:{port}'


def fetch_remote(remote, path, timeout=30):
    host = remote['host']
    if ':' in host and not host.startswith('['):
        host = '[' + host + ']'
    with urllib.request.urlopen(f"http://{host}:{remote['port']}{path}", timeout=timeout) as response:
        return json.load(response)


def remote_key(remote):
    return hashlib.sha1(f"{remote['host']}:{remote['port']}".encode()).hexdigest()[:8]


def remote_path(remote, directory):
    return directory / f"remote-{remote['host'].replace(':', '_')}-{remote['port']}.sqlite"


def meta_value(con, key, default=None):
    try:
        row = con.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    except sqlite3.Error:
        return default
    return json.loads(row[0]) if row else default


def remote_status(remote, directory):
    status = {'turns': 0, 'updated': None, 'synced_at': None, 'error': None}
    path = remote_path(remote, directory)
    if not path.exists():
        return status
    try:
        with closing(connect(path)) as con:
            status['turns'] = con.execute('SELECT count(*) FROM turns').fetchone()[0]
            status['updated'] = meta_value(con, 'updated')
            status['synced_at'] = meta_value(con, 'synced_at')
            status['error'] = meta_value(con, 'sync_error') or None
    except sqlite3.Error as error:
        status['error'] = str(error)
    if index_version(path) != SCHEMA_VERSION:
        status['error'] = status['error'] or '索引格式已升级，请重新同步'
    return status


def remotes_payload(directory, path=REMOTES_FILE):
    return {'remotes': [{**remote, 'status': remote_status(remote, directory)} for remote in load_remotes(path)]}


def excluded_words(path=EXCLUDE_FILE):
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
    except OSError:
        return []
    return [term for term in (line.strip().casefold() for line in lines) if term]


def save_excluded(words, path=EXCLUDE_FILE):
    cleaned = []
    for word in words:
        if not isinstance(word, str):
            continue
        term = word.strip().casefold()
        if term and len(term) <= 40 and term not in cleaned:
            cleaned.append(term)
    temporary = path.with_suffix('.tmp')
    temporary.write_text('\n'.join(cleaned) + ('\n' if cleaned else ''), encoding='utf-8')
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    return cleaned


def tokenize(text):
    import jieba
    return [word.casefold() for word in jieba.cut(text, HMM=False)
            if re.fullmatch(r'[\w\u4e00-\u9fff]+', word) and len(word) > 1
            and not word.isnumeric() and word.casefold() not in STOP]


def connect(path):
    con = sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def index_version(path):
    try:
        with closing(connect(path)) as con:
            row = con.execute("SELECT value FROM meta WHERE key='version'").fetchone()
        return int(json.loads(row[0])) if row else 0
    except (sqlite3.Error, ValueError, OSError):
        return 0


def identity_hash(agent, session, key):
    return hashlib.sha1(f'{agent}\x1f{session}\x1f{key}'.encode('utf-8', 'ignore')).hexdigest()[:16]


def turn_fingerprint(agent, session, timestamp, text, output_text):
    material = '\x1f'.join((agent or '', session or '', timestamp or '', text or '', output_text or ''))
    return hashlib.sha1(material.encode('utf-8', 'ignore')).hexdigest()[:16]


def stored_text(text, output):
    parts = json.loads(output)
    if isinstance(parts, list):
        parts = '\n'.join(part['text'] for part in parts)
    return text + '\n' + parts


def require_contentless():
    if sqlite3.sqlite_version_info < (3, 43):
        raise RuntimeError(f'需要 SQLite ≥ 3.43 的 contentless_delete，当前为 {sqlite3.sqlite_version}')


def insert_turn(con, agent, session, model, timestamp, text, output, source,
                terms=(), searchable=True, file=None, identity=None, fp=None):
    output_text = '\n'.join(part['text'] for part in output) if isinstance(output, list) else output
    stored = json.dumps(output, ensure_ascii=False) if isinstance(output, list) else output
    if fp is None:
        fp = turn_fingerprint(agent, session, timestamp, text, output_text)
    row = con.execute('INSERT INTO turns(agent,session,model,day,timestamp,input,output,source,file,identity,fingerprint)'
                      ' VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                      (agent, session, model, timestamp[:10], timestamp, text, stored, source, file, identity, fp))
    if searchable:
        con.execute('INSERT INTO search(rowid,search_text) VALUES(?,?)', (row.lastrowid, text + '\n' + output_text))
    if terms:
        con.executemany('INSERT INTO terms VALUES(?,?,?)', [(term, row.lastrowid, count) for term, count in terms])
    return row.lastrowid


def delete_file_rows(con, path):
    con.execute('DELETE FROM search WHERE rowid IN (SELECT id FROM turns WHERE file=?)', (path,))
    con.execute('DELETE FROM terms WHERE turn_id IN (SELECT id FROM turns WHERE file=?)', (path,))
    con.execute('DELETE FROM turns WHERE file=?', (path,))
    con.execute('DELETE FROM file_usage WHERE path=?', (path,))
    con.execute('DELETE FROM files WHERE path=?', (path,))


def index_search(con):
    con.execute('INSERT INTO search(rowid,search_text) SELECT id,' + SEARCH_TEXT + ' FROM turns')


def store_dataset(con, source_path, data, searchable=True, identities=None):
    for t in data.turns:
        identity = identity_hash(t['agent'], t['session'], t['key'])
        if identities is not None:
            if identity in identities:
                continue
            identities.add(identity)
        elif con.execute('SELECT 1 FROM turns WHERE identity=? LIMIT 1', (identity,)).fetchone():
            continue
        terms = list(Counter(tokenize(t['input'])).items()) if t.get('words', True) else []
        insert_turn(con, t['agent'], t['session'], t['model'], t['timestamp'], t['input'], t['output'],
                    t['source'], terms, searchable, source_path, identity)
    con.executemany('INSERT INTO file_usage VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    [(source_path, u['agent'], u['key'], u['day'], u['model'], u['session'],
                      u['new'], u['cc'], u['cr'], u['out'], u['msgs'])
                     for u in data.usage])


def parse_into(con, agent, file, searchable=True, identities=None):
    try:
        size = file.stat().st_size
    except OSError:
        size = -1
    data = Dataset()
    try:
        read_agent(data, agent, file)
        warnings = list(data.warnings)
    except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error) as error:
        data, warnings = Dataset(), [f'{agent}/{file.name}: {error}']
    con.execute('INSERT OR REPLACE INTO files VALUES(?,?,?,?)',
                (str(file), agent, size, json.dumps(warnings, ensure_ascii=False)))
    store_dataset(con, str(file), data, searchable, identities=identities)


def refresh_usage(con):
    con.execute('DELETE FROM usage')
    con.execute('INSERT INTO usage(day,agent,model,new,cc,cr,out,msgs)'
                ' SELECT day,agent,model,SUM(new),SUM(cc),SUM(cr),SUM(out),SUM(msgs) FROM ('
                + DEDUPED_USAGE + ') GROUP BY day,agent,model')
    con.execute('DELETE FROM session_usage')
    con.execute('INSERT INTO session_usage(agent,session,new,cc,cr,out,msgs)'
                ' SELECT agent,session,SUM(new),SUM(cc),SUM(cr),SUM(out),SUM(msgs) FROM ('
                + DEDUPED_USAGE + ') GROUP BY agent,session')


def write_meta(con, indexed_at):
    warnings, sources = [], defaultdict(int)
    for row in con.execute('SELECT agent,warnings FROM files'):
        sources[row['agent']] += 1
        warnings.extend(json.loads(row['warnings']))
    meta = dict(version=SCHEMA_VERSION, indexed_at=indexed_at,
                updated=datetime.now().astimezone().isoformat(),
                warnings=warnings, sources=dict(sources), agents=TOOLS)
    con.execute('DELETE FROM meta')
    con.executemany('INSERT INTO meta VALUES(?,?)', [(k, json.dumps(v, ensure_ascii=False)) for k, v in meta.items()])


def ensure_indexes(path):
    """对既有 schema v5 索引库幂等补建辅助索引，不触发全量重建。"""
    if not path.exists() or index_version(path) != SCHEMA_VERSION:
        return
    con = sqlite3.connect(str(path), timeout=30)
    try:
        with con:
            con.executescript(EXTRA_INDEXES)
    except sqlite3.Error:
        pass
    finally:
        con.close()


def rebuild(path, home=None):
    import jieba
    jieba.setLogLevel(40)
    require_contentless()
    started = int(datetime.now().timestamp() * 1000)
    temporary = path.with_suffix('.building')
    temporary.unlink(missing_ok=True)
    temporary.touch(mode=0o600)
    con = sqlite3.connect(str(temporary), timeout=30)
    con.row_factory = sqlite3.Row
    identities = set()
    try:
        con.executescript(SCHEMA)
        for agent, file in discover(home):
            parse_into(con, agent, file, searchable=False, identities=identities)
        index_search(con)
        refresh_usage(con)
        write_meta(con, started)
        con.commit()
    except BaseException:
        con.close()
        temporary.unlink(missing_ok=True)
        raise
    con.close()
    os.replace(temporary, path)


def agy_companion_mtime_ms(path):
    try:
        db = path.parents[4] / 'conversations' / (path.parents[2].name + '.db')
    except IndexError:
        return 0
    latest = 0
    for candidate in (db, db.with_name(db.name + '-wal')):
        try:
            latest = max(latest, int(candidate.stat().st_mtime * 1000))
        except OSError:
            pass
    return latest


def changed_files(con, home=None):
    """对比索引记录，返回需要重解析的文件（agent 为 None 表示已消失）。"""
    indexed_at = int(meta_value(con, 'indexed_at') or 0)
    known = {row['path']: row['size'] for row in con.execute('SELECT path,size FROM files')}
    found = {str(file): agent for agent, file in discover(home)}
    changed = []
    for raw, agent in found.items():
        try:
            stat = Path(raw).stat()
        except OSError:
            continue
        mtime_ms = int(stat.st_mtime * 1000)
        if agent == 'agy':
            mtime_ms = max(mtime_ms, agy_companion_mtime_ms(Path(raw)))
        if raw in known and mtime_ms <= indexed_at and stat.st_size == known[raw]:
            continue
        changed.append((raw, agent))
    changed.extend((raw, None) for raw in set(known) - set(found))
    return changed


def update_index(path, home=None):
    if index_version(path) != SCHEMA_VERSION:
        return rebuild(path, home)
    started = int(datetime.now().timestamp() * 1000)
    con = sqlite3.connect(str(path), timeout=30)
    con.row_factory = sqlite3.Row
    changed = 0
    try:
        with con:
            con.executescript(EXTRA_INDEXES)
        for raw, agent in changed_files(con, home):
            with con:
                delete_file_rows(con, raw)
                if agent is not None:
                    parse_into(con, agent, Path(raw))
            changed += 1
        refresh_usage(con)
        write_meta(con, started)
        con.commit()
        return changed
    finally:
        con.close()


def sleep_until(deadline, step=AUTO_REFRESH_STEP_SECONDS, clock=time.time, sleep=time.sleep):
    """分片睡眠到挂钟 deadline；系统睡眠跨过检查点时唤醒后提前到期。"""
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            return
        sleep(min(remaining, step))


def watch_index(server, interval=AUTO_REFRESH_SECONDS, clock=time.time, sleep=time.sleep):
    while True:
        sleep_until(clock() + interval, clock=clock, sleep=sleep)
        if not server.refresh_lock.acquire(blocking=False):
            continue
        try:
            with closing(connect(server.db)) as con:
                pending = len(changed_files(con, server.home))
            if pending:
                count = update_index(server.db, server.home)
                print(f'自动刷新：{count} 个 session 文件有变化，索引已更新', flush=True)
        except Exception as error:
            print(f'自动刷新失败：{error}', flush=True)
        finally:
            server.refresh_lock.release()


def sync_remote(remote, directory, fetch=fetch_remote):
    path = remote_path(remote, directory)
    status = {'host': remote['host'], 'port': remote['port'], 'updated': None, 'cursor': 0,
              'turns': 0, 'error': None, 'synced_at': None}
    con = sqlite3.connect(str(path), timeout=30)
    con.row_factory = sqlite3.Row
    try:
        if meta_value(con, 'version') != SCHEMA_VERSION:
            require_contentless()
            con.executescript('DROP TABLE IF EXISTS usage; DROP TABLE IF EXISTS turns; DROP TABLE IF EXISTS search;'
                              ' DROP TABLE IF EXISTS terms; DROP TABLE IF EXISTS files; DROP TABLE IF EXISTS file_usage;'
                              ' DROP TABLE IF EXISTS session_usage; DROP TABLE IF EXISTS meta;')
            con.executescript(SCHEMA)
            con.commit()
            con.execute('VACUUM')
        else:
            con.executescript(EXTRA_INDEXES)
            con.commit()
        con.isolation_level = None
        con.execute('BEGIN IMMEDIATE')
        try:
            cursor = int(meta_value(con, 'cursor') or 0)
            cursor_fp = meta_value(con, 'cursor_fingerprint')
            page = fetch(remote, '/api/export?section=usage')
            updated = (page.get('meta') or {}).get('updated')
            # sources（按 agent 的文件数）只在带 meta 的页里；usage 页总带，turns 首页在 cursor=0 时带。
            sources = (page.get('meta') or {}).get('sources') or {}
            con.execute('DELETE FROM usage')
            con.executemany('INSERT INTO usage VALUES(?,?,?,?,?,?,?,?)',
                            [(r['day'], r['agent'], r['model'], r['new'], r['cc'], r['cr'], r['out'], r['msgs'])
                             for r in page.get('usage', [])])
            con.execute('DELETE FROM session_usage')
            # 旧版远端不返回 sessions，此时该远端的会话不带用量。
            con.executemany('INSERT INTO session_usage VALUES(?,?,?,?,?,?,?)',
                            [(r['agent'], r['session'], r['new'], r['cc'], r['cr'], r['out'], r['msgs'])
                             for r in page.get('sessions') or []])
            stable = False
            if cursor:
                probe = fetch(remote, f'/api/export?section=turns&cursor={cursor - 1}&limit=1')
                first = (probe.get('turns') or [None])[0]
                stable = bool(first) and first.get('id') == cursor and first.get('fingerprint') == cursor_fp
            if not stable:
                con.execute("INSERT INTO search(search) VALUES('delete-all')")
                con.execute('DELETE FROM turns')
                con.execute('DELETE FROM terms')
                cursor, cursor_fp = 0, None
            while True:
                page = fetch(remote, f'/api/export?section=turns&limit=500&cursor={cursor}')
                turns = page.get('turns') or []
                if page.get('meta'):
                    updated = page['meta'].get('updated')
                    sources = page['meta'].get('sources') or sources
                for turn in turns:
                    terms = [(t['term'], t['count']) for t in turn.get('terms', [])]
                    insert_turn(con, turn['agent'], turn['session'], turn['model'], turn['timestamp'],
                                turn['input'], turn['output'],
                                f"{remote['host']}:{remote['port']} · " + (turn.get('source') or ''),
                                terms, True, None, None, turn.get('fingerprint'))
                    cursor, cursor_fp = turn['id'], turn.get('fingerprint')
                if not page.get('cursor') or not turns:
                    break
            synced_at = datetime.now().astimezone().isoformat()
            meta = dict(version=SCHEMA_VERSION, host=remote['host'], port=remote['port'],
                        updated=updated, sources=sources,
                        cursor=cursor or 0, cursor_fingerprint=cursor_fp, synced_at=synced_at, sync_error='')
            con.execute('DELETE FROM meta')
            con.executemany('INSERT INTO meta VALUES(?,?)', [(k, json.dumps(v, ensure_ascii=False)) for k, v in meta.items()])
            con.execute('COMMIT')
            status.update(updated=updated, cursor=cursor or 0, synced_at=synced_at,
                          turns=con.execute('SELECT count(*) FROM turns').fetchone()[0])
        except BaseException:
            con.execute('ROLLBACK')
            raise
    except Exception as error:
        status['error'] = f'{type(error).__name__}: {error}'
        try:
            con.execute("INSERT INTO meta VALUES('sync_error',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (json.dumps(status['error'], ensure_ascii=False),))
            con.commit()
        except sqlite3.Error:
            pass
    finally:
        con.close()
    return status


def buckets_between(start, end, unit):
    buckets = []
    cursor = start
    while cursor <= end:
        key = bucket(cursor.isoformat(), unit)
        if not buckets or buckets[-1] != key:
            buckets.append(key)
        cursor += timedelta(days=1)
    return buckets


def period(unit, n, today=None):
    today = today or date.today()
    if unit == 'day':
        start = today - timedelta(days=n - 1)
    elif unit == 'week':
        start = today - timedelta(days=today.weekday() + 7 * (n - 1))
    elif unit == 'month':
        months = today.year * 12 + today.month - n
        start = date(months // 12, months % 12 + 1, 1)
    else:
        raise ValueError('周期须为 day、week 或 month')
    return start.isoformat(), today.isoformat(), buckets_between(start, today, unit)


def bucket(day, unit):
    d = date.fromisoformat(day)
    return day if unit == 'day' else ((d - timedelta(days=d.weekday())).isoformat() if unit == 'week' else day[:7])


def bounds(params):
    clauses, values = [], []
    if params.get('agent'):
        clauses.append('agent = ?')
        values.append(params['agent'])
    if params.get('model'):
        # 同一模型在不同日志里大小写不一（如 pi 的 Qwen3.8-Flash-Next-IQ3_S / qwen3.8-flash-next-iq3_s），模型筛选不区分大小写
        clauses.append('lower(model) = lower(?)')
        values.append(params['model'])
    for key, sign in (('start', '>='), ('end', '<=')):
        if params.get(key):
            date.fromisoformat(params[key])
            clauses.append(f'day {sign} ?')
            values.append(params[key])
    return clauses, values


def scoped_bounds(params):
    clauses, values = bounds(params)
    if params.get('model'):
        idx = clauses.index('lower(model) = lower(?)')
        clauses[idx] = "instr(lower(' · ' || model || ' · '), lower(?)) > 0"
        values[idx] = ' · ' + params['model'] + ' · '
    return clauses, values


def merged_models(totals):
    """模型名按小写归一（同一模型大小写写法不一）；显示名取用量最多的写法。"""
    display = {}
    for model, total in totals.items():
        key = model.lower()
        if key not in display or total > totals[display[key]]:
            display[key] = model
    return sorted(display.values())


def positive(params, key, default, maximum):
    try:
        value = int(params.get(key, default))
    except ValueError:
        value = 0
    if not 1 <= value <= maximum:
        raise ValueError(f'{key} 须为 1–{maximum} 的整数')
    return value


def length_bound(p, key, maximum=100000):
    if not p.get(key):
        return None
    try:
        value = int(p[key])
    except ValueError:
        raise ValueError(f'{key} 须为非负整数')
    if not 0 <= value <= maximum:
        raise ValueError(f'{key} 须为 0–{maximum} 的整数')
    return value


def index_meta(con):
    keys = ('updated', 'sources', 'warnings')
    rows = con.execute("SELECT * FROM meta WHERE key IN (?,?,?)", keys)
    return {r['key']: json.loads(r['value']) for r in rows}


class Source:
    __slots__ = ('key', 'label', 'con', 'usage', 'search', 'words')

    def __init__(self, key, label, con, usage=True, search=True, words=True):
        self.key, self.label, self.con = key, label, con
        self.usage, self.search, self.words = usage, search, words


def open_sources(con, remotes, directory):
    sources = [Source('local', '本机', con)]
    for remote in remotes:
        if not remote['enabled']:
            continue
        path = remote_path(remote, directory)
        if not path.exists():
            continue
        try:
            remote_con = connect(path)
        except sqlite3.Error:
            continue
        try:
            row = remote_con.execute("SELECT value FROM meta WHERE key='version'").fetchone()
            version = int(json.loads(row[0])) if row else 0
        except (sqlite3.Error, ValueError, TypeError):
            version = 0
        # 索引格式落后的远端库等下次同步全量重拉，期间不参与查询（状态见 /api/remotes）。
        if version != SCHEMA_VERSION:
            remote_con.close()
            continue
        label = remote.get('label') or f"{remote['host']}:{remote['port']}"
        sources.append(Source(remote_key(remote), label, remote_con,
                              remote['usage'], remote['search'], remote['words']))
    return sources


def close_sources(sources):
    for source in sources[1:]:
        source.con.close()


def like_escape(term):
    return '%' + term.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'


def json_literal_safe(term):
    return all(ch not in ('"', '\\') and ord(ch) >= 32 for ch in term)


def parse_search_query(raw):
    """解析空格分隔关键词与双引号精确短语，保序并忽略大小写去重。"""
    terms, i, n = [], 0, len(raw or '')
    while i < n:
        if raw[i].isspace():
            i += 1
            continue
        if raw[i] == '"':
            end = raw.find('"', i + 1)
            if end == -1:
                token, i = raw[i + 1:].strip(), n
            else:
                token, i = raw[i + 1:end], end + 1
            if token and not token.isspace():
                terms.append(token)
            continue
        j = i
        while j < n and not raw[j].isspace():
            j += 1
        terms.append(raw[i:j])
        i = j
    seen, deduped = set(), []
    for term in terms:
        key = term.casefold()
        if key not in seen:
            seen.add(key)
            deduped.append(term)
    return deduped


def parse_output_parts(output_raw):
    if isinstance(output_raw, list):
        return output_raw
    try:
        parts = json.loads(output_raw or '[]')
        return parts if isinstance(parts, list) else []
    except (ValueError, TypeError):
        return []


def scoped_segments(input_text, output_parts, scope='all'):
    allowed = SEARCH_SCOPES[scope]
    segs = []
    if allowed is None or scope == 'input':
        segs.append(('输入', input_text or ''))
    if scope != 'input':
        for part in output_parts:
            kind = part.get('kind') or '正文'
            if allowed is None or kind in allowed:
                segs.append((kind, part.get('text') or ''))
    return segs


def search_clauses(p, terms, scope='all'):
    clauses, values = scoped_bounds(p)
    session_q = (p.get('session') or '').strip()
    if session_q:
        clauses.append('instr(lower(turns.session), lower(?)) > 0')
        values.append(session_q)
    long_terms = [term for term in terms if len(term) >= 3]
    if long_terms:
        clauses.append('id IN (SELECT rowid FROM search WHERE search MATCH ?)')
        values.append(' AND '.join('"' + term.replace('"', '""') + '"' for term in long_terms))
    if scope == 'input':
        for term in terms:
            clauses.append("input LIKE ? ESCAPE '\\'")
            values.append(like_escape(term))
    elif scope in ('text', 'thinking', 'tool'):
        kind_tags = SEARCH_SCOPES[scope]
        clauses.append('(' + ' OR '.join('instr(turns.output, ?) > 0' for _ in kind_tags) + ')')
        values.extend(f'"{k}"' for k in kind_tags)
        for term in terms:
            if json_literal_safe(term):
                clauses.append("turns.output LIKE ? ESCAPE '\\'")
                values.append(like_escape(term))
    else:
        for term in terms:
            if len(term) >= 3:
                continue
            if json_literal_safe(term):
                pattern = like_escape(term)
                clauses.append("(input LIKE ? ESCAPE '\\' OR turns.output LIKE ? ESCAPE '\\')")
                values.extend([pattern, pattern])
            else:
                clauses.append('(' + SEARCH_TEXT + ") LIKE ? ESCAPE '\\'")
                values.append(like_escape(term))
    if p.get('term'):
        clauses.append('id IN (SELECT turn_id FROM terms WHERE term=?)')
        values.append(p['term'].casefold())
    for key, sign in (('minlen', '>='), ('maxlen', '<=')):
        value = length_bound(p, key)
        if value is not None:
            clauses.append(f'length(input) {sign} ?')
            values.append(value)
    return clauses, values


def turn_matches_and_score(input_text, output_raw, terms, exclude_terms, scope, need_score):
    if scope == 'all' and not exclude_terms and not need_score:
        inp_folded = (input_text or '').casefold()
        if all(t.casefold() in inp_folded for t in terms):
            return 0
    parts = [] if scope == 'input' else parse_output_parts(output_raw)
    segs = scoped_segments(input_text, parts, scope)
    if not segs and (terms or exclude_terms or scope != 'all'):
        return None
    folded_segs = [(kind, text.casefold()) for kind, text in segs if text]
    for ex in exclude_terms:
        needle = ex.casefold()
        if any(needle in text for _, text in folded_segs):
            return None
    for term in terms:
        needle = term.casefold()
        if not any(needle in text for _, text in folded_segs):
            return None
    if not need_score or not terms:
        return 0
    score = 0
    for term in terms:
        needle = term.casefold()
        for kind, folded in folded_segs:
            cnt = folded.count(needle)
            if cnt:
                score += min(cnt, 5) * SCOPE_WEIGHTS.get(kind, 1)
                if kind == '输入' and folded[:240].find(needle) != -1:
                    score += 2
    return score


def build_hit_summary(input_text, output_parts, highlight_terms, scope='all'):
    part_count = len(output_parts)
    input_len = len(input_text or '')
    if not highlight_terms:
        return [], [], '', part_count, input_len
    segs = scoped_segments(input_text, output_parts, scope)
    folded_terms = [t.casefold() for t in highlight_terms if t]
    hit_kinds, seen_kinds = [], set()
    all_hits = []
    for seg_idx, (kind, text) in enumerate(segs):
        if not text:
            continue
        folded = text.casefold()
        seg_hit = False
        for t_idx, needle in enumerate(folded_terms):
            pos = folded.find(needle)
            if pos != -1:
                seg_hit = True
                all_hits.append((seg_idx, kind, t_idx, pos, text, len(highlight_terms[t_idx])))
        if seg_hit and kind not in seen_kinds:
            seen_kinds.add(kind)
            hit_kinds.append(kind)

    # 卡片 preview 已显示 input 前 240 字，避免紧邻重复展示相同文本。
    covered_in_preview = set()
    candidates = []
    for item in all_hits:
        seg_idx, kind, t_idx, pos, text, tlen = item
        if kind == '输入' and pos + tlen <= 210:
            covered_in_preview.add(t_idx)
            continue
        candidates.append(item)

    def make_snippet(text, pos):
        start = max(0, pos - 55)
        end = min(len(text), start + 200)
        if end - start < 200 and start > 0:
            start = max(0, end - 200)
        snippet = text[start:end].strip()
        if start > 0:
            snippet = '…' + snippet
        if end < len(text):
            snippet = snippet + '…'
        return snippet

    matches, used_windows = [], []
    covered_terms = set(covered_in_preview)

    def add_candidate(seg_idx, kind, pos, text):
        label = '输入(后文)' if kind == '输入' else kind
        snip = make_snippet(text, pos)
        matches.append({'kind': label, 'text': snip})
        used_windows.append((seg_idx, pos))
        snip_folded = snip.casefold()
        for idx, needle in enumerate(folded_terms):
            if needle in snip_folded:
                covered_terms.add(idx)

    for seg_idx, kind, t_idx, pos, text, _ in candidates:
        if t_idx in covered_terms:
            continue
        if any(s == seg_idx and abs(p - pos) < 120 for s, p in used_windows):
            continue
        add_candidate(seg_idx, kind, pos, text)
        if len(matches) >= 3:
            break

    if len(matches) < 2:
        for seg_idx, kind, _, pos, text, _ in candidates:
            if any(s == seg_idx and abs(p - pos) < 120 for s, p in used_windows):
                continue
            add_candidate(seg_idx, kind, pos, text)
            if len(matches) >= 3:
                break

    return hit_kinds, matches, (matches[0]['text'] if matches else ''), part_count, input_len


def format_search_item(key, label, row, input_text, output_parts, highlight_terms, scope):
    hit_kinds, matches, legacy_match, part_count, input_len = build_hit_summary(
        input_text, output_parts, highlight_terms, scope
    )
    item = {
        'id': f'{key}:{row["id"]}',
        'agent': row['agent'],
        'session': row['session'],
        'model': row['model'],
        'timestamp': row['timestamp'],
        'preview': row['preview'],
        'label': label,
        'hit_kinds': hit_kinds,
        'matches': matches,
        'part_count': part_count,
        'input_len': input_len,
    }
    if legacy_match:
        item['match'] = legacy_match
    return item


def fetch_turn_details(sources, by_key):
    details = {}
    for source in sources:
        ids = list(dict.fromkeys(by_key.get(source.key) or ()))
        if not ids:
            continue
        for row in source.con.execute(
                'SELECT id,input,output FROM turns WHERE id IN (' + ','.join('?' * len(ids)) + ')', ids):
            details[source.key, row['id']] = (row['input'], parse_output_parts(row['output']))
    return details


def query_search(sources, p):
    q = (p.get('q') or '').strip()
    if len(q) > 500:
        raise ValueError('搜索词不能超过 500 字')
    exclude_raw = (p.get('exclude') or '').strip()
    if len(exclude_raw) > 300:
        raise ValueError('排除词不能超过 300 字')
    scope = (p.get('scope') or 'all').strip() or 'all'
    if scope not in SEARCH_SCOPES:
        raise ValueError('scope 须为 all、input、text、thinking 或 tool')
    sort = (p.get('sort') or 'newest').strip() or 'newest'
    if sort not in ('newest', 'oldest', 'relevance'):
        raise ValueError('sort 须为 newest、oldest 或 relevance')
    group = (p.get('group') or 'turn').strip() or 'turn'
    if group not in ('turn', 'session'):
        raise ValueError('group 须为 turn 或 session')
    source_filter = (p.get('source') or '').strip()

    terms = parse_search_query(q)
    exclude_terms = parse_search_query(exclude_raw)
    term_kw = (p.get('term') or '').strip()
    highlight_terms = terms or ([term_kw] if term_kw else [])
    page = positive(p, 'page', 1, 1000000)

    active_sources = [s for s in sources if s.search and (not source_filter or s.key == source_filter)]
    need_python_filter = (
        scope != 'all'
        or bool(exclude_terms)
        or sort == 'relevance'
        or '"' in q
        or any(len(t) < 3 or '\n' in t for t in terms)
    )
    order_sql = 'ASC' if sort == 'oldest' else 'DESC'

    if not need_python_filter and group == 'turn':
        total, collected = 0, []
        for source in active_sources:
            clauses, values = search_clauses(p, terms, 'all')
            where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
            total += source.con.execute('SELECT count(*) FROM turns' + where, values).fetchone()[0]
            rows = source.con.execute(
                'SELECT id,agent,session,model,timestamp,substr(input,1,240) AS preview FROM turns'
                + where + f' ORDER BY timestamp {order_sql},id {order_sql} LIMIT ?',
                values + [page * 20],
            ).fetchall()
            for row in rows:
                collected.append((row['timestamp'] or '', row['id'], source.key, source.label, dict(row)))
        collected.sort(key=lambda entry: (entry[0], entry[1], entry[2]), reverse=(sort != 'oldest'))
        picked = collected[(page - 1) * 20:page * 20]
        by_key = defaultdict(list)
        for _, _, key, _, item in picked:
            by_key[key].append(item['id'])
        details = fetch_turn_details(active_sources, by_key)
        rows = [
            format_search_item(key, label, item, *details[key, item['id']], highlight_terms, 'all')
            for _, _, key, label, item in picked
        ]
        return dict(rows=rows, total=total, total_turns=total, group='turn', page=page, pages=(total + 19) // 20)

    if not need_python_filter and group == 'session':
        session_groups, total_turns = [], 0
        for source in active_sources:
            clauses, values = search_clauses(p, terms, 'all')
            where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
            sql = ('SELECT agent,session,count(*) AS hit_turns,min(timestamp) AS first_ts,max(timestamp) AS last_ts'
                   ' FROM turns' + where + ' GROUP BY agent,session')
            for r in source.con.execute(sql, values):
                total_turns += r['hit_turns']
                session_groups.append({
                    'key': source.key, 'label': source.label, 'source': source,
                    'agent': r['agent'], 'session': r['session'],
                    'hit_turns': r['hit_turns'], 'first': r['first_ts'], 'last': r['last_ts'],
                })
        if sort == 'oldest':
            session_groups.sort(key=lambda g: (g['first'] or '', g['key'], g['agent'], g['session']))
        else:
            session_groups.sort(key=lambda g: (g['last'] or '', g['key'], g['agent'], g['session']), reverse=True)
        total = len(session_groups)
        picked_sessions = session_groups[(page - 1) * 20:page * 20]
        out_groups = []
        for g in picked_sessions:
            clauses, values = search_clauses(p, terms, 'all')
            clauses.extend(['agent = ?', 'session = ?'])
            values.extend([g['agent'], g['session']])
            where = ' WHERE ' + ' AND '.join(clauses)
            turn_rows = g['source'].con.execute(
                'SELECT id,agent,session,model,timestamp,substr(input,1,240) AS preview,input,output FROM turns'
                + where + f' ORDER BY timestamp {order_sql},id {order_sql} LIMIT 5',
                values,
            ).fetchall()
            sample_turns = [
                format_search_item(
                    g['key'], g['label'], dict(tr), tr['input'],
                    parse_output_parts(tr['output']), highlight_terms, 'all'
                )
                for tr in turn_rows
            ]
            out_groups.append({
                'key': g['key'], 'label': g['label'], 'agent': g['agent'], 'session': g['session'],
                'hit_turns': g['hit_turns'], 'first': g['first'], 'last': g['last'], 'turns': sample_turns,
            })
        return dict(rows=out_groups, total=total, total_turns=total_turns, group='session', page=page, pages=(total + 19) // 20)

    # Exact Python-refined path (scope != all / exclude / relevance / exact phrase)
    need_score = sort == 'relevance' and bool(terms)
    matched = []  # (score, timestamp, id, source, meta)
    for source in active_sources:
        clauses, values = search_clauses(p, terms, scope)
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        for row in source.con.execute(
                'SELECT id,agent,session,model,timestamp,substr(input,1,240) AS preview,input,output FROM turns' + where,
                values):
            score = turn_matches_and_score(row['input'], row['output'], terms, exclude_terms, scope, need_score)
            if score is None:
                continue
            matched.append((
                score, row['timestamp'] or '', row['id'], source,
                {'id': row['id'], 'agent': row['agent'], 'session': row['session'],
                 'model': row['model'], 'timestamp': row['timestamp'], 'preview': row['preview']},
            ))

    if sort == 'oldest':
        matched.sort(key=lambda e: (e[1], e[2], e[3].key))
    elif need_score:
        matched.sort(key=lambda e: (e[0], e[1], e[2], e[3].key), reverse=True)
    else:
        matched.sort(key=lambda e: (e[1], e[2], e[3].key), reverse=True)

    if group == 'turn':
        total = len(matched)
        picked = matched[(page - 1) * 20:page * 20]
        by_key = defaultdict(list)
        for _, _, _, src, item in picked:
            by_key[src.key].append(item['id'])
        details = fetch_turn_details(active_sources, by_key)
        rows = [
            format_search_item(src.key, src.label, item, *details[src.key, item['id']], highlight_terms, scope)
            for _, _, _, src, item in picked
        ]
        return dict(rows=rows, total=total, total_turns=total, group='turn', page=page, pages=(total + 19) // 20)

    # Python-refined group == 'session'
    grouped = {}
    for score, ts, _, src, item in matched:
        gkey = (src.key, item['agent'], item['session'])
        bucket_entry = grouped.get(gkey)
        if bucket_entry is None:
            bucket_entry = {
                'key': src.key, 'label': src.label, 'agent': item['agent'], 'session': item['session'],
                'hit_turns': 0, 'first': ts, 'last': ts, 'max_score': score, 'sum_score': 0, 'items': [],
            }
            grouped[gkey] = bucket_entry
        bucket_entry['hit_turns'] += 1
        bucket_entry['sum_score'] += score
        if score > bucket_entry['max_score']:
            bucket_entry['max_score'] = score
        if ts and (not bucket_entry['first'] or ts < bucket_entry['first']):
            bucket_entry['first'] = ts
        if ts and (not bucket_entry['last'] or ts > bucket_entry['last']):
            bucket_entry['last'] = ts
        if len(bucket_entry['items']) < 5:
            bucket_entry['items'].append(item)

    session_list = list(grouped.values())
    if sort == 'oldest':
        session_list.sort(key=lambda g: (g['first'] or '', g['key'], g['agent'], g['session']))
    elif need_score:
        session_list.sort(key=lambda g: (g['max_score'], g['sum_score'], g['last'] or '', g['key']), reverse=True)
    else:
        session_list.sort(key=lambda g: (g['last'] or '', g['key'], g['agent'], g['session']), reverse=True)

    total = len(session_list)
    total_turns = len(matched)
    picked_sessions = session_list[(page - 1) * 20:page * 20]
    by_key = defaultdict(list)
    for g in picked_sessions:
        for item in g['items']:
            by_key[g['key']].append(item['id'])
    details = fetch_turn_details(active_sources, by_key)
    out_groups = [
        {
            'key': g['key'], 'label': g['label'], 'agent': g['agent'], 'session': g['session'],
            'hit_turns': g['hit_turns'], 'first': g['first'], 'last': g['last'],
            'turns': [
                format_search_item(g['key'], g['label'], item, *details[g['key'], item['id']], highlight_terms, scope)
                for item in g['items']
            ],
        }
        for g in picked_sessions
    ]
    return dict(rows=out_groups, total=total, total_turns=total_turns, group='session', page=page, pages=(total + 19) // 20)


def compact_preview(text, limit=140):
    collapsed = ' '.join((text or '').split())
    return (collapsed[:limit] + '…') if len(collapsed) > limit else collapsed


def split_session_models(raw):
    """从 group_concat(DISTINCT model) 拆出按大小写去重的模型列表。"""
    if not raw:
        return []
    seen = {}
    for chunk in raw.split(','):
        for piece in chunk.split(' · '):
            name = piece.strip()
            if not name or name.lower() == 'none':
                continue
            seen.setdefault(name.casefold(), name)
    ordered = list(seen.values())
    real = [m for m in ordered if m.lower() != 'unknown']
    return real or ordered


def fetch_first_inputs(con, turn_ids, limit=260):
    out = {}
    clean_ids = list(dict.fromkeys(tid for tid in turn_ids if tid))
    for i in range(0, len(clean_ids), 500):
        chunk = clean_ids[i:i + 500]
        marks = ','.join('?' * len(chunk))
        for row in con.execute(f'SELECT id, substr(input, 1, ?) AS inp FROM turns WHERE id IN ({marks})', [limit, *chunk]):
            out[row['id']] = row['inp'] or ''
    return out


def query_words(sources, p, exclude=()):
    limit = positive(p, 'limit', 40, 200)
    order = (p.get('order') or 'desc').strip() or 'desc'
    if order not in ('desc', 'asc'):
        raise ValueError('order 须为 desc 或 asc')
    sort = (p.get('sort') or 'count').strip() or 'count'
    if sort not in ('count', 'turns', 'avg'):
        raise ValueError('sort 须为 count、turns 或 avg')
    kind = (p.get('kind') or 'all').strip() or 'all'
    if kind not in ('all', 'zh', 'code'):
        raise ValueError('kind 须为 all、zh 或 code')
    min_turns_raw = p.get('min_turns')
    min_turns = positive(dict(p, min_turns=min_turns_raw), 'min_turns', 1, 100000) if min_turns_raw not in (None, '') else 1
    source_filter = (p.get('source') or '').strip()
    word_q = (p.get('q') or '').strip().casefold()
    if len(word_q) > 100:
        raise ValueError('词语过滤不能超过 100 字')
    hide_digits = p.get('hide_digits') == '1'
    hide_english = p.get('hide_english') == '1'

    merged = {}
    scoped_turns = 0
    for source in sources:
        if not source.words or (source_filter and source.key != source_filter):
            continue
        clauses, values = scoped_bounds(p)
        use_turns = bool(clauses)
        if exclude:
            clauses.append('term NOT IN (' + ','.join('?' * len(exclude)) + ')')
            values.extend(exclude)
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        base = 'terms JOIN turns ON turns.id=terms.turn_id' if use_turns else 'terms'
        scoped_turns += source.con.execute('SELECT count(DISTINCT turn_id) FROM ' + base + where, values).fetchone()[0]
        if word_q:
            clauses.append('instr(term, ?) > 0')
            values.append(word_q)
            where = ' WHERE ' + ' AND '.join(clauses)
        for row in source.con.execute(
                'SELECT term,sum(count) AS count,count(*) AS turns FROM ' + base + where + ' GROUP BY term', values):
            entry = merged.setdefault(row['term'], [0, 0])
            entry[0] += row['count']
            entry[1] += row['turns']

    for term in list(merged):
        count, turns = merged[term]
        if turns < min_turns:
            del merged[term]
            continue
        if kind == 'zh' and re.search(r'[\da-zA-Z]', term):
            del merged[term]
            continue
        if kind == 'code' and not re.search(r'[\da-zA-Z_]', term):
            del merged[term]
            continue
        if hide_digits and re.search(r'\d', term):
            del merged[term]
        elif hide_english and re.search(r'[a-zA-Z]', term):
            del merged[term]

    total_count = sum(c for c, _ in merged.values())
    max_turns = max((t for _, t in merged.values()), default=0)
    words = []
    for term, (count, turns) in merged.items():
        avg = round(count / turns, 1) if turns else 0.0
        share = round(count / total_count * 100, 2) if total_count else 0.0
        turn_rate = round(turns / scoped_turns * 100, 1) if scoped_turns else 0.0
        words.append(dict(term=term, count=count, turns=turns, avg=avg, share=share, turn_rate=turn_rate))

    if sort == 'turns':
        words.sort(key=lambda w: (w['turns'] if order == 'asc' else -w['turns'],
                                  w['count'] if order == 'asc' else -w['count'], w['term']))
    elif sort == 'avg':
        words.sort(key=lambda w: (w['avg'] if order == 'asc' else -w['avg'],
                                  w['turns'] if order == 'asc' else -w['turns'], w['term']))
    else:
        words.sort(key=lambda w: (w['count'] if order == 'asc' else -w['count'], w['term']))

    return dict(
        words=words[:limit],
        excluded=list(exclude),
        summary=dict(
            unique_terms=len(words),
            total_count=total_count,
            scoped_turns=scoped_turns,
            max_turns=max_turns,
        ),
    )


def query_sessions(sources, p):
    page = positive(p, 'page', 1, 1000000)
    sort = (p.get('sort') or 'created').strip() or 'created'
    if sort not in ('created', 'active', 'tokens', 'turns'):
        raise ValueError('sort 须为 created、active、tokens 或 turns')
    source_filter = (p.get('source') or '').strip()
    session_q = (p.get('q') or '').strip()
    if len(session_q) > 200:
        raise ValueError('过滤词不能超过 200 字')

    where_clauses, where_vals = [], []
    if p.get('agent'):
        where_clauses.append('turns.agent = ?')
        where_vals.append(p['agent'])
    having_clauses, having_vals = scoped_bounds(dict(p, agent=''))
    where_sql = ' WHERE ' + ' AND '.join(where_clauses) if where_clauses else ''
    having_sql = (' HAVING SUM(CASE WHEN ' + ' AND '.join(having_clauses) + ' THEN 1 ELSE 0 END) > 0') if having_clauses else ''

    entries = []
    source_map = {}
    for source in sources:
        if not source.search or (source_filter and source.key != source_filter):
            continue
        source_map[source.key] = source
        # 该来源未勾选用量时只留轮次统计，用量列为空。
        columns = ','.join(f'u.{field}' for field in FIELDS) if source.usage else ','.join(f'NULL AS {field}' for field in FIELDS)
        sql = (
            "SELECT turns.agent AS agent,turns.session AS session,count(*) AS turns,"
            "min(timestamp) AS first,max(timestamp) AS last,"
            "min(printf('%s\x01%010d', timestamp, turns.id)) AS first_key,"
            "group_concat(DISTINCT turns.model) AS raw_models," + columns +
            " FROM turns LEFT JOIN session_usage AS u ON u.agent=turns.agent AND u.session=turns.session"
            + where_sql + " GROUP BY turns.agent,turns.session" + having_sql
        )
        for row in source.con.execute(sql, where_vals + having_vals):
            entry = dict(row, key=source.key, label=source.label)
            first_key = entry.pop('first_key', '') or ''
            entry['_first_id'] = int(first_key.partition('\x01')[2] or 0)
            entry['_raw_models'] = entry.pop('raw_models', '') or ''
            entry['total'] = None if entry['new'] is None else sum(entry[field] for field in FIELDS[:-1])
            entries.append(entry)

    if session_q:
        needle = session_q.casefold()
        kept = []
        check_by_src = defaultdict(list)
        for entry in entries:
            if needle in (entry['session'] or '').casefold():
                kept.append(entry)
            elif entry['_first_id']:
                check_by_src[entry['key']].append(entry)
        for key, group in check_by_src.items():
            texts = fetch_first_inputs(source_map[key].con, [e['_first_id'] for e in group])
            for entry in group:
                inp = texts.get(entry['_first_id'], '')
                if needle in inp.casefold():
                    entry['topic'] = compact_preview(inp)
                    kept.append(entry)
        entries = kept

    entries.sort(key=lambda entry: (entry['key'], entry['agent'], entry['session']))
    if sort == 'active':
        entries.sort(key=lambda entry: (entry['last'] or '', entry['first'] or ''), reverse=True)
    elif sort == 'turns':
        entries.sort(key=lambda entry: (entry['turns'], entry['last'] or ''), reverse=True)
    elif sort == 'tokens':
        entries.sort(key=lambda entry: entry['last'] or '', reverse=True)
        entries.sort(key=lambda entry: (-1, 0) if entry['total'] is None else (0, entry['total']), reverse=True)
    else:
        entries.sort(key=lambda entry: entry['first'] or '', reverse=True)

    total_turns = sum(e['turns'] for e in entries)
    has_usage = any(e['total'] is not None for e in entries)
    total_tokens = sum(e['total'] for e in entries if e['total'] is not None) if has_usage else None

    page_rows = entries[(page - 1) * 50:page * 50]
    need_by_src = defaultdict(list)
    for entry in page_rows:
        if 'topic' not in entry and entry['_first_id']:
            need_by_src[entry['key']].append(entry)
    for key, group in need_by_src.items():
        texts = fetch_first_inputs(source_map[key].con, [e['_first_id'] for e in group], limit=200)
        for entry in group:
            entry['topic'] = compact_preview(texts.get(entry['_first_id'], ''))

    for entry in page_rows:
        entry.setdefault('topic', '')
        entry['models'] = split_session_models(entry.pop('_raw_models', ''))
        entry.pop('_first_id', None)

    return dict(
        rows=page_rows,
        total=len(entries),
        page=page,
        pages=(len(entries) + 49) // 50,
        summary=dict(sessions=len(entries), turns=total_turns, total_tokens=total_tokens),
    )


def query_session(sources, p):
    key = p.get('key') or 'local'
    agent, session = str(p.get('agent') or ''), str(p.get('session') or '')
    if not agent or not session:
        raise ValueError('agent 与 session 不能为空')
    source = next((item for item in sources if item.key == key and item.search), None)
    rows = source.con.execute(
        "SELECT id,model,timestamp,substr(input,1,240) AS preview,length(input) AS input_len,"
        "CASE WHEN json_valid(output) AND json_type(output)='array' THEN json_array_length(output) ELSE 0 END AS part_count,"
        "source FROM turns WHERE agent=? AND session=? ORDER BY timestamp,id",
        (agent, session)).fetchall() if source else []
    if not rows:
        raise ValueError('会话不存在，请刷新会话列表')
    usage = None
    if source.usage:
        u_row = source.con.execute(
            'SELECT new,cc,cr,out,msgs FROM session_usage WHERE agent=? AND session=?',
            (agent, session),
        ).fetchone()
        if u_row:
            usage = {field: u_row[field] for field in FIELDS}
            usage['total'] = sum(usage[field] for field in FIELDS[:-1])
    models = split_session_models(','.join(row['model'] for row in rows if row['model']))
    turns = []
    for row in rows:
        item = dict(row)
        item.pop('source')
        item['id'] = f'{key}:{item["id"]}'
        turns.append(item)
    return dict(
        key=key, label=source.label, agent=agent, session=session,
        source=rows[0]['source'], first=rows[0]['timestamp'], last=rows[-1]['timestamp'],
        models=models, usage=usage, turns=turns,
    )


def query(con, endpoint, p, exclude=(), writable=True, sources=None):
    sources = sources or [Source('local', '本机', con)]
    if endpoint == '/api/meta':
        result = {r['key']: json.loads(r['value']) for r in con.execute("SELECT * FROM meta WHERE key NOT IN ('version','indexed_at')")}
        turns = sessions = 0
        models = defaultdict(int)
        counts = defaultdict(int)
        warnings = list(result.get('warnings') or [])
        for source in sources:
            if source.search:
                turns += source.con.execute('SELECT count(*) FROM turns').fetchone()[0]
                sessions += source.con.execute('SELECT count(*) FROM (SELECT DISTINCT agent,session FROM turns)').fetchone()[0]
            if source.usage:
                for model, total in source.con.execute('SELECT model,new+cc+cr+out FROM usage'):
                    models[model] += total
            for agent, count in (meta_value(source.con, 'sources') or {}).items():
                counts[agent] += count
            if source.key != 'local':
                error = meta_value(source.con, 'sync_error')
                if error:
                    warnings.append(f'远端 {source.label} 同步失败：{error}')
        result.update(turns=turns, sessions=sessions, models=merged_models(models),
                      sources=dict(counts),
                      search_sources=[{'key': s.key, 'label': s.label} for s in sources if s.search],
                      words_sources=[{'key': s.key, 'label': s.label} for s in sources if s.words],
                      warnings=warnings, writable=writable)
        return result
    if endpoint == '/api/export':
        section = p.get('section', 'usage')
        if section == 'usage':
            return dict(meta=index_meta(con), usage=[dict(r) for r in con.execute(
                'SELECT day,agent,model,new,cc,cr,out,msgs FROM usage ORDER BY day,agent,model')],
                sessions=[dict(r) for r in con.execute(
                    'SELECT agent,session,new,cc,cr,out,msgs FROM session_usage ORDER BY agent,session')])
        if section == 'turns':
            try:
                cursor = int(p.get('cursor', 0) or 0)
            except ValueError:
                raise ValueError('cursor 须为整数')
            limit = positive(p, 'limit', 100, 500)
            rows = [dict(r) for r in con.execute(
                'SELECT id,agent,session,model,timestamp,input,output,source,fingerprint FROM turns WHERE id>? ORDER BY id LIMIT ?',
                (cursor, limit + 1))]
            more = len(rows) > limit
            rows = rows[:limit]
            terms = defaultdict(list)
            if rows:
                marks = ','.join('?' * len(rows))
                for row in con.execute(
                        f'SELECT turn_id,term,count FROM terms WHERE turn_id IN ({marks}) ORDER BY term',
                        [row['id'] for row in rows]):
                    terms[row['turn_id']].append(dict(term=row['term'], count=row['count']))
            for row in rows:
                row['output'] = json.loads(row['output'])
                row['terms'] = terms[row['id']]
            result = dict(turns=rows, cursor=rows[-1]['id'] if more else None)
            if not cursor:
                result['meta'] = index_meta(con)
            return result
        raise ValueError('section 须为 usage 或 turns')
    if endpoint == '/api/usage':
        unit = p.get('unit', 'day')
        if p.get('start') or p.get('end'):
            start = date.fromisoformat(p.get('start') or p.get('end'))
            end = date.fromisoformat(p.get('end') or p.get('start'))
            if start > end:
                start, end = end, start
            start, end = start.isoformat(), end.isoformat()
            buckets = buckets_between(date.fromisoformat(start), date.fromisoformat(end), unit)
        else:
            start, end, buckets = period(unit, positive(p, 'n', 14, 366))
        clauses, values = bounds(dict(p, start=start, end=end))
        rows = []
        for source in sources:
            if not source.usage:
                continue
            rows.extend(dict(r) for r in source.con.execute(
                'SELECT * FROM usage WHERE ' + ' AND '.join(clauses), values))
        for row in rows:
            row['bucket'] = bucket(row['day'], unit)
            row['total'] = sum(row[k] for k in FIELDS[:-1])
        return dict(rows=rows, buckets=buckets, start=start, end=end)
    if endpoint == '/api/words':
        return query_words(sources, p, exclude)
    if endpoint == '/api/search':
        return query_search(sources, p)
    if endpoint == '/api/sessions':
        return query_sessions(sources, p)
    if endpoint == '/api/session':
        return query_session(sources, p)
    if endpoint == '/api/turn':
        key, _, number = str(p.get('id', '')).partition(':')
        if not number:
            key, number = 'local', key
        source = next((item for item in sources if item.key == key), None)
        if source is None:
            raise ValueError('会话轮次不存在，请刷新搜索结果')
        try:
            turn_id = int(number)
        except ValueError:
            raise ValueError('id 须为整数')
        row = source.con.execute('SELECT id,agent,session,model,timestamp,input,output,source FROM turns WHERE id=?', (turn_id,)).fetchone()
        if not row:
            raise ValueError('会话轮次不存在，请刷新搜索结果')
        result = dict(row)
        result['id'] = f'{key}:{result["id"]}'
        result['output'] = json.loads(result['output'])
        return result
    raise ValueError('未知接口')


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allowed_names = LOOPBACK


class Handler(BaseHTTPRequestHandler):
    def respond(self, code, body, mime='application/json; charset=utf-8', cache=None, etag=None):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', cache or 'no-store')
        if etag:
            self.send_header('ETag', etag)
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def not_modified(self, etag):
        self.send_response(304)
        self.send_header('ETag', etag)
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()

    def hostname(self):
        return urlsplit('//' + self.headers.get('Host', '')).hostname or ''

    def allowed(self):
        if self.hostname() not in self.server.allowed_names:
            return False
        origin = self.headers.get('Origin')
        if not origin:
            return True
        parsed = urlsplit(origin)
        return parsed.scheme in ('http', 'https') and parsed.hostname in self.server.allowed_names

    def is_local(self):
        if self.hostname() not in LOOPBACK or self.client_address[0] not in LOOPBACK:
            return False
        hops = (self.headers.get('X-Forwarded-For', '') + ',' + self.headers.get('X-Real-IP', '')).split(',')
        return all(not hop.strip() or hop.strip() in LOOPBACK for hop in hops)

    def payload(self):
        raw_length = self.headers.get('Content-Length')
        if raw_length is None or raw_length == '':
            return {}
        try:
            length = int(raw_length)
        except ValueError:
            raise ValueError('Content-Length 无效')
        if length < 0 or length > MAX_BODY_BYTES:
            raise ValueError('请求体大小超出限制')
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length) or b'{}')

    def do_GET(self):
        if not self.allowed():
            return self.respond(403, {'error': 'Host 或来源不在允许列表'})
        parsed = urlsplit(self.path)
        try:
            if parsed.path.startswith('/api/'):
                p = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
                with closing(connect(self.server.db)) as con:
                    if parsed.path == '/api/remotes':
                        return self.respond(200, remotes_payload(self.server.directory))
                    sources = open_sources(con, load_remotes(), self.server.directory)
                    try:
                        result = query(con, parsed.path, p, excluded_words(), self.is_local(), sources)
                    finally:
                        close_sources(sources)
                return self.respond(200, result)
            assets = {'/': ('index.html', 'text/html'), '/app.js': ('app.js', 'text/javascript'), '/style.css': ('style.css', 'text/css'), '/favicon.ico': ('favicon.ico', 'image/x-icon')}
            if parsed.path not in assets:
                return self.respond(404, {'error': '未找到页面'})
            name, mime = assets[parsed.path]
            path = ROOT / 'static' / name
            stat = path.stat()
            etag = f'"{stat.st_mtime_ns}-{stat.st_size}"'
            if self.headers.get('If-None-Match') == etag:
                return self.not_modified(etag)
            self.respond(200, path.read_bytes(), mime + '; charset=utf-8', cache='no-cache', etag=etag)
        except ValueError as error:
            self.respond(400, {'error': str(error)})
        except sqlite3.Error as error:
            self.respond(500, {'error': f'索引不可用：{error}'})

    def do_POST(self):
        if not self.allowed() or not self.is_local() or self.headers.get('X-Stats-Request') != '1':
            return self.respond(403, {'error': '仅允许本机同源操作'})
        if self.path == '/api/words-exclude':
            try:
                payload = self.payload()
                words = payload.get('words')
                if not isinstance(words, list):
                    raise ValueError('words 须为数组')
                return self.respond(200, {'ok': True, 'excluded': save_excluded(words)})
            except ValueError as error:
                return self.respond(400, {'error': str(error)})
            except OSError as error:
                return self.respond(500, {'error': f'排除词保存失败：{error}'})
        if self.path == '/api/remotes':
            try:
                previous = load_remotes()
                saved = save_remotes(self.payload().get('remotes'))
                keep = {(remote['host'], remote['port']) for remote in saved}
                for remote in previous:
                    if (remote['host'], remote['port']) not in keep:
                        remote_path(remote, self.server.directory).unlink(missing_ok=True)
                return self.respond(200, {'ok': True, **remotes_payload(self.server.directory)})
            except ValueError as error:
                return self.respond(400, {'error': str(error)})
            except OSError as error:
                return self.respond(500, {'error': f'远端配置保存失败：{error}'})
        if self.path == '/api/sync':
            try:
                payload = self.payload()
            except ValueError as error:
                return self.respond(400, {'error': str(error)})
            if not isinstance(payload, dict):
                return self.respond(400, {'error': '请求体须为对象'})
            host = str(payload.get('host') or '').strip()
            remotes = load_remotes()
            if host:
                selected = [r for r in remotes if r['host'] == host and (not payload.get('port') or r['port'] == payload['port'])]
                if not selected:
                    return self.respond(400, {'error': '远端不存在，请先保存配置'})
            else:
                selected = [r for r in remotes if r['enabled']]
            if not self.server.refresh_lock.acquire(blocking=False):
                return self.respond(409, {'error': '正在同步或刷新，请稍候'})
            try:
                statuses = [sync_remote(remote, self.server.directory) for remote in selected]
                return self.respond(200, {'ok': True, 'remotes': statuses})
            finally:
                self.server.refresh_lock.release()
        if self.path != '/api/refresh':
            return self.respond(404, {'error': '未知接口'})
        if not self.server.refresh_lock.acquire(blocking=False):
            return self.respond(409, {'error': '正在刷新索引，请稍候'})
        try:
            update_index(self.server.db, self.server.home)
            statuses = [sync_remote(remote, self.server.directory) for remote in load_remotes() if remote['enabled']]
            self.respond(200, {'ok': True, 'remotes': statuses})
        except Exception as error:
            self.respond(500, {'error': f'刷新失败，保留旧索引：{error}'})
        finally:
            self.server.refresh_lock.release()

    def log_message(self, fmt, *args):
        pass


def parse_args(argv=None, defaults=None):
    """解析启动参数；defaults 为 local.json 的默认值（缺省时现读），命令行显式参数优先。"""
    if defaults is None:
        defaults = load_local()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=defaults.get('port', 18763))
    parser.add_argument('--host', default=defaults.get('host', '127.0.0.1'), help='监听地址，默认仅本机')
    parser.add_argument('--allow-host', action='append', default=None, metavar='HOST',
                        help=f'额外放行的访问域名或 IP（可重复；默认已放行 {"、".join(DEFAULT_ALLOWED)}）')
    parser.add_argument('--home', default=defaults.get('home'), help='日志所在的用户目录')
    parser.add_argument('--reindex', action='store_true', help='全量重建索引后退出')
    args = parser.parse_args(argv)
    if args.allow_host is None:
        args.allow_host = list(defaults.get('allow_host', []))
    return args


def main():
    args = parse_args()
    directory = ROOT / '.stats'
    directory.mkdir(mode=0o700, exist_ok=True)
    home = str(Path(args.home).resolve()) if args.home else str(Path.home().resolve())
    path = directory / f'index{index_suffix(args.home)}.sqlite'
    if args.reindex:
        print('正在全量重建索引…', flush=True)
        rebuild(path, args.home)
        print('索引完成。')
        return
    # 先落地址再建索引：控制脚本要按实际端口探测就绪（端口可能来自 local.json，命令行里没有）
    url = display_url(args.host, args.port)
    (directory / 'server.url').write_text(url + '\n', encoding='utf-8')
    if not path.exists() or index_version(path) != SCHEMA_VERSION:
        print('正在读取本机会话并构建索引…', flush=True)
        rebuild(path, args.home)
    else:
        ensure_indexes(path)
    for remote_db in directory.glob('remote-*.sqlite'):
        ensure_indexes(remote_db)
    server = Server((args.host, args.port), Handler)
    server.db, server.home, server.directory = path, args.home, directory
    server.refresh_lock = threading.Lock()
    server.allowed_names = allowed_hosts(args.host, args.allow_host)
    # 启动成功（已监听）后把生效参数同步回 local.json，供下次启动复用；写失败不影响服务
    try:
        save_local({'port': args.port, 'host': args.host, 'home': home, 'allow_host': list(args.allow_host)})
    except OSError as error:
        print(f'无法写入 local.json：{error}', flush=True)
    threading.Thread(target=watch_index, args=(server,), daemon=True).start()
    print(f'打开 {url} · Ctrl+C 停止', flush=True)
    print(f'每 {AUTO_REFRESH_SECONDS // 60} 分钟自动检查会话更新', flush=True)
    print('远端放行 Host：' + '、'.join(sorted(server.allowed_names - LOOPBACK)), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
