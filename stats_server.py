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
SCHEMA_VERSION = 4
AUTO_REFRESH_SECONDS = 30 * 60
AUTO_REFRESH_STEP_SECONDS = 60
STOP = set('的 了 和 是 在 我 你 他 她 它 这 那 一个 一些 我们 你们 他们 可以 需要 使用 进行 以及 并且 如果 然后 这个 那个 不 有 就 都 也 到 与 为 对 中 将 请 把 被 要 吗 呢 啊 the a an and or is are to of in for on with this that it be as at by from you your i we'.split())
LOOPBACK = frozenset({'127.0.0.1', 'localhost', '::1'})
DEFAULT_ALLOWED = ('100.64.216.70',)

SCHEMA = """
CREATE TABLE usage(day TEXT,agent TEXT,model TEXT,new INTEGER,cc INTEGER,cr INTEGER,out INTEGER,msgs INTEGER);
CREATE INDEX usage_filter ON usage(day,agent,model);
CREATE TABLE turns(id INTEGER PRIMARY KEY,agent TEXT,session TEXT,model TEXT,day TEXT,timestamp TEXT,input TEXT,output TEXT,source TEXT,file TEXT,identity TEXT,fingerprint TEXT);
CREATE INDEX turn_filter ON turns(day,agent);
CREATE INDEX turn_file ON turns(file);
CREATE INDEX turn_identity ON turns(identity);
CREATE VIRTUAL TABLE search USING fts5(search_text,content='',contentless_delete=1,tokenize='trigram');
CREATE TABLE terms(term TEXT,turn_id INTEGER,count INTEGER,PRIMARY KEY(term,turn_id));
CREATE INDEX terms_turn ON terms(turn_id);
CREATE TABLE files(path TEXT PRIMARY KEY,agent TEXT,size INTEGER,warnings TEXT);
CREATE TABLE file_usage(path TEXT,agent TEXT,key TEXT,day TEXT,model TEXT,new INTEGER,cc INTEGER,cr INTEGER,out INTEGER,msgs INTEGER);
CREATE INDEX file_usage_filter ON file_usage(day,agent,model);
CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
"""
SEARCH_TEXT = ("input || char(10) || COALESCE((SELECT group_concat(json_extract(part.value,'$.text'),char(10))"
               " FROM json_each(turns.output) AS part), '')")


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
        sections = {key: bool(entry.get(key, True)) for key in ('usage', 'search', 'words')}
        sections['search'] = sections['search'] or sections['words']
        remotes.append({'host': host, 'port': port, 'enabled': bool(entry.get('enabled', True)), **sections})
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


def store_dataset(con, source_path, data, searchable=True):
    for t in data.turns:
        identity = identity_hash(t['agent'], t['session'], t['key'])
        if con.execute('SELECT 1 FROM turns WHERE identity=? LIMIT 1', (identity,)).fetchone():
            continue
        terms = list(Counter(tokenize(t['input'])).items()) if t.get('words', True) else []
        insert_turn(con, t['agent'], t['session'], t['model'], t['timestamp'], t['input'], t['output'],
                    t['source'], terms, searchable, source_path, identity)
    con.executemany('INSERT INTO file_usage VALUES(?,?,?,?,?,?,?,?,?,?)',
                    [(source_path, u['agent'], u['key'], u['day'], u['model'], u['new'], u['cc'], u['cr'], u['out'], u['msgs'])
                     for u in data.usage])


def parse_into(con, agent, file, searchable=True):
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
    store_dataset(con, str(file), data, searchable)


def refresh_usage(con):
    con.execute('DELETE FROM usage')
    con.execute("""
        INSERT INTO usage(day,agent,model,new,cc,cr,out,msgs)
        SELECT day,agent,model,SUM(new),SUM(cc),SUM(cr),SUM(out),SUM(msgs) FROM (
            SELECT agent,key,day,model,MAX(new) AS new,MAX(cc) AS cc,MAX(cr) AS cr,MAX(out) AS out,MAX(msgs) AS msgs
            FROM file_usage GROUP BY agent,key,day,model)
        GROUP BY day,agent,model
    """)


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
    try:
        con.executescript(SCHEMA)
        for agent, file in discover(home):
            parse_into(con, agent, file, searchable=False)
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
        if raw in known and int(stat.st_mtime * 1000) <= indexed_at and stat.st_size == known[raw]:
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
                              ' DROP TABLE IF EXISTS meta;')
            con.executescript(SCHEMA)
            con.commit()
            con.execute('VACUUM')
        con.isolation_level = None
        con.execute('BEGIN IMMEDIATE')
        try:
            cursor = int(meta_value(con, 'cursor') or 0)
            cursor_fp = meta_value(con, 'cursor_fingerprint')
            page = fetch(remote, '/api/export?section=usage')
            updated = (page.get('meta') or {}).get('updated')
            con.execute('DELETE FROM usage')
            con.executemany('INSERT INTO usage VALUES(?,?,?,?,?,?,?,?)',
                            [(r['day'], r['agent'], r['model'], r['new'], r['cc'], r['cr'], r['out'], r['msgs'])
                             for r in page.get('usage', [])])
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
                        updated=updated, sources=(page.get('meta') or {}).get('sources') or {},
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
    buckets = []
    cursor = start
    while cursor <= today:
        key = bucket(cursor.isoformat(), unit)
        if not buckets or buckets[-1] != key:
            buckets.append(key)
        cursor += timedelta(days=1)
    return start.isoformat(), today.isoformat(), buckets


def bucket(day, unit):
    d = date.fromisoformat(day)
    return day if unit == 'day' else ((d - timedelta(days=d.weekday())).isoformat() if unit == 'week' else day[:7])


def bounds(params):
    clauses, values = [], []
    for key in ('agent', 'model'):
        if params.get(key):
            clauses.append(f'{key} = ?')
            values.append(params[key])
    for key, sign in (('start', '>='), ('end', '<=')):
        if params.get(key):
            date.fromisoformat(params[key])
            clauses.append(f'day {sign} ?')
            values.append(params[key])
    return clauses, values


def scoped_bounds(params):
    clauses, values = bounds(params)
    if params.get('model'):
        idx = clauses.index('model = ?')
        clauses[idx] = "instr(' · ' || model || ' · ', ?) > 0"
        values[idx] = ' · ' + params['model'] + ' · '
    return clauses, values


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
        sources.append(Source(remote_key(remote), f"{remote['host']}:{remote['port']}", remote_con,
                              remote['usage'], remote['search'], remote['words']))
    return sources


def close_sources(sources):
    for source in sources[1:]:
        source.con.close()


def search_clauses(p, terms):
    clauses, values = scoped_bounds(p)
    long_terms = [term for term in terms if len(term) >= 3]
    if long_terms:
        clauses.append('id IN (SELECT rowid FROM search WHERE search MATCH ?)')
        values.append(' AND '.join('"' + term.replace('"', '""') + '"' for term in long_terms))
    for term in terms:
        if len(term) >= 3:
            continue
        clauses.append('(' + SEARCH_TEXT + ") LIKE ? ESCAPE '\\'")
        values.append('%' + term.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%')
    if p.get('term'):
        clauses.append('id IN (SELECT turn_id FROM terms WHERE term=?)')
        values.append(p['term'].casefold())
    for key, sign in (('minlen', '>='), ('maxlen', '<=')):
        value = length_bound(p, key)
        if value is not None:
            clauses.append(f'length(input) {sign} ?')
            values.append(value)
    return clauses, values


def query(con, endpoint, p, exclude=(), writable=True, sources=None):
    sources = sources or [Source('local', '本机', con)]
    if endpoint == '/api/meta':
        result = {r['key']: json.loads(r['value']) for r in con.execute("SELECT * FROM meta WHERE key NOT IN ('version','indexed_at')")}
        turns = sessions = 0
        models = set()
        counts = defaultdict(int)
        warnings = list(result.get('warnings') or [])
        for source in sources:
            if source.search:
                turns += source.con.execute('SELECT count(*) FROM turns').fetchone()[0]
                sessions += source.con.execute('SELECT count(*) FROM (SELECT DISTINCT agent,session FROM turns)').fetchone()[0]
            if source.usage:
                models.update(r[0] for r in source.con.execute('SELECT DISTINCT model FROM usage'))
            for agent, count in (meta_value(source.con, 'sources') or {}).items():
                counts[agent] += count
            if source.key != 'local':
                error = meta_value(source.con, 'sync_error')
                if error:
                    warnings.append(f'远端 {source.label} 同步失败：{error}')
        result.update(turns=turns, sessions=sessions, models=sorted(models),
                      sources=dict(counts), warnings=warnings, writable=writable)
        return result
    if endpoint == '/api/export':
        section = p.get('section', 'usage')
        if section == 'usage':
            return dict(meta=index_meta(con), usage=[dict(r) for r in con.execute(
                'SELECT day,agent,model,new,cc,cr,out,msgs FROM usage ORDER BY day,agent,model')])
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
        limit = positive(p, 'limit', 40, 200)
        order = p.get('order', 'desc') or 'desc'
        if order not in ('desc', 'asc'):
            raise ValueError('order 须为 desc 或 asc')
        hide_digits = p.get('hide_digits') == '1'
        hide_english = p.get('hide_english') == '1'
        merged = {}
        for source in sources:
            if not source.words:
                continue
            clauses, values = bounds(p)
            if p.get('model'):
                idx = clauses.index('model = ?')
                clauses[idx] = "instr(' · ' || model || ' · ', ?) > 0"
                values[idx] = ' · ' + p['model'] + ' · '
            if exclude:
                clauses.append('term NOT IN (' + ','.join('?' * len(exclude)) + ')')
                values.extend(exclude)
            where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
            for row in source.con.execute(
                    'SELECT term,sum(count) AS count,count(*) AS turns FROM terms JOIN turns ON turns.id=terms.turn_id'
                    + where + ' GROUP BY term', values):
                entry = merged.setdefault(row['term'], [0, 0])
                entry[0] += row['count']
                entry[1] += row['turns']
        if hide_digits or hide_english:
            for term in list(merged):
                if hide_digits and re.search(r'\d', term):
                    del merged[term]
                elif hide_english and re.search(r'[a-zA-Z]', term):
                    del merged[term]
        words = [dict(term=term, count=count, turns=turns) for term, (count, turns) in merged.items()]
        words.sort(key=lambda word: (word['count'] if order == 'asc' else -word['count'], word['term']))
        return dict(words=words[:limit], excluded=list(exclude))
    if endpoint == '/api/search':
        q = p.get('q', '').strip()
        if len(q) > 500:
            raise ValueError('搜索词不能超过 500 字')
        terms = q.split()
        page = positive(p, 'page', 1, 1000000)
        total, collected = 0, []
        for source in sources:
            if not source.search:
                continue
            clauses, values = search_clauses(p, terms)
            where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
            rows = source.con.execute(
                'SELECT id,agent,session,model,timestamp,substr(input,1,240) AS preview,COUNT(*) OVER () AS total FROM turns'
                + where + ' ORDER BY timestamp DESC,id DESC LIMIT ?', values + [page * 20]).fetchall()
            if rows:
                total += rows[0]['total']
            for row in rows:
                item = dict(row)
                item.pop('total')
                collected.append((item['timestamp'] or '', item['id'], source.key, item))
        collected.sort(key=lambda entry: (entry[0], entry[1], entry[2]), reverse=True)
        picked = collected[(page - 1) * 20:page * 20]
        by_key = defaultdict(list)
        for _, _, key, item in picked:
            by_key[key].append(item['id'])
        details = {}
        for source in sources:
            ids = by_key.get(source.key)
            if not ids:
                continue
            for row in source.con.execute(
                    'SELECT id,input,output FROM turns WHERE id IN (' + ','.join('?' * len(ids)) + ')', ids):
                details[source.key, row['id']] = stored_text(row['input'], row['output'])
        rows = []
        for _, _, key, item in picked:
            content = details[key, item['id']]
            item['id'] = f'{key}:{item["id"]}'
            if terms:
                pos = content.casefold().find(terms[0].casefold())
                item['match'] = content[max(0, pos - 65):max(0, pos - 65) + 240]
            rows.append(item)
        return dict(rows=rows, total=total, page=page, pages=(total + 19) // 20)
    if endpoint == '/api/sessions':
        page = positive(p, 'page', 1, 1000000)
        entries = []
        for source in sources:
            if not source.search:
                continue
            where, values = '', []
            if p.get('agent'):
                where, values = ' WHERE agent = ?', [p['agent']]
            for row in source.con.execute(
                    'SELECT agent,session,count(*) AS turns,min(timestamp) AS first,max(timestamp) AS last'
                    ' FROM turns' + where + ' GROUP BY agent,session', values):
                entries.append(dict(row, key=source.key, label=source.label))
        entries.sort(key=lambda entry: (entry['key'], entry['agent'], entry['session']))
        entries.sort(key=lambda entry: entry['first'] or '', reverse=True)
        return dict(rows=entries[(page - 1) * 50:page * 50], total=len(entries), page=page,
                    pages=(len(entries) + 49) // 50)
    if endpoint == '/api/session':
        key = p.get('key') or 'local'
        agent, session = str(p.get('agent') or ''), str(p.get('session') or '')
        if not agent or not session:
            raise ValueError('agent 与 session 不能为空')
        source = next((item for item in sources if item.key == key and item.search), None)
        rows = source.con.execute(
            'SELECT id,model,timestamp,substr(input,1,240) AS preview,source FROM turns'
            ' WHERE agent=? AND session=? ORDER BY timestamp,id', (agent, session)).fetchall() if source else []
        if not rows:
            raise ValueError('会话不存在，请刷新会话列表')
        turns = []
        for row in rows:
            item = dict(row)
            item.pop('source')
            item['id'] = f'{key}:{item["id"]}'
            turns.append(item)
        return dict(key=key, label=source.label, agent=agent, session=session, source=rows[0]['source'],
                    first=rows[0]['timestamp'], last=rows[-1]['timestamp'], turns=turns)
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
        length = int(self.headers.get('Content-Length', 0))
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
            except ValueError:
                payload = {}
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=18763)
    parser.add_argument('--host', default='127.0.0.1', help='监听地址，默认仅本机')
    parser.add_argument('--allow-host', action='append', default=[], metavar='HOST',
                        help=f'额外放行的访问域名或 IP（可重复；默认已放行 {"、".join(DEFAULT_ALLOWED)}）')
    parser.add_argument('--home', help='日志所在的用户目录')
    parser.add_argument('--reindex', action='store_true', help='全量重建索引后退出')
    args = parser.parse_args()
    directory = ROOT / '.stats'
    directory.mkdir(mode=0o700, exist_ok=True)
    suffix = '' if args.home is None else '-' + hashlib.sha256(str(Path(args.home).resolve()).encode()).hexdigest()[:8]
    path = directory / f'index{suffix}.sqlite'
    if args.reindex:
        print('正在全量重建索引…', flush=True)
        rebuild(path, args.home)
        print('索引完成。')
        return
    if not path.exists() or index_version(path) != SCHEMA_VERSION:
        print('正在读取本机会话并构建索引…', flush=True)
        rebuild(path, args.home)
    server = Server((args.host, args.port), Handler)
    server.db, server.home, server.directory = path, args.home, directory
    server.refresh_lock = threading.Lock()
    server.allowed_names = allowed_hosts(args.host, args.allow_host)
    threading.Thread(target=watch_index, args=(server,), daemon=True).start()
    address = args.host if args.host not in ('0.0.0.0', '::') else '127.0.0.1'
    if ':' in address:
        address = '[' + address + ']'
    print(f'打开 http://{address}:{args.port} · Ctrl+C 停止', flush=True)
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
