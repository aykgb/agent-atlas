#!/usr/bin/env python3
"""Local usage dashboard and conversation index."""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import threading
from collections import Counter, defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from stats_data import FIELDS, TOOLS, collect

ROOT = Path(__file__).resolve().parent
EXCLUDE_FILE = ROOT / 'words-exclude.txt'
STOP = set('的 了 和 是 在 我 你 他 她 它 这 那 一个 一些 我们 你们 他们 可以 需要 使用 进行 以及 并且 如果 然后 这个 那个 不 有 就 都 也 到 与 为 对 中 将 请 把 被 要 吗 呢 啊 the a an and or is are to of in for on with this that it be as at by from you your i we'.split())


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


def rebuild(path, home=None):
    import jieba
    jieba.setLogLevel(40)
    data = collect(home)
    temporary = path.with_suffix('.building')
    temporary.unlink(missing_ok=True)
    temporary.touch(mode=0o600)
    con = sqlite3.connect(str(temporary), timeout=30)
    try:
        con.executescript("""
        CREATE TABLE usage(day TEXT,agent TEXT,model TEXT,new INTEGER,cc INTEGER,cr INTEGER,out INTEGER,msgs INTEGER);
        CREATE INDEX usage_filter ON usage(day,agent,model);
        CREATE TABLE turns(id INTEGER PRIMARY KEY,agent TEXT,session TEXT,model TEXT,day TEXT,timestamp TEXT,input TEXT,output TEXT,source TEXT,search_text TEXT);
        CREATE INDEX turn_filter ON turns(day,agent);
        CREATE VIRTUAL TABLE search USING fts5(text,content='',tokenize='trigram');
        CREATE TABLE terms(term TEXT,turn_id INTEGER,count INTEGER,PRIMARY KEY(term,turn_id));
        CREATE INDEX terms_turn ON terms(turn_id);
        CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
        """)
        grouped = defaultdict(lambda: [0] * len(FIELDS))
        for u in data.usage:
            values = grouped[(u['day'], u['agent'], u['model'])]
            for i, key in enumerate(FIELDS):
                values[i] += u[key]
        con.executemany('INSERT INTO usage VALUES(?,?,?,?,?,?,?,?)', [(*key, *values) for key, values in grouped.items()])
        seen = set()
        for t in data.turns:
            identity = (t['agent'], t['session'], t['key'])
            if identity in seen:
                continue
            seen.add(identity)
            output_text = '\n'.join(p['text'] for p in t['output'])
            search_text = t['input'] + '\n' + output_text
            row = con.execute('INSERT INTO turns(agent,session,model,day,timestamp,input,output,source,search_text) VALUES(?,?,?,?,?,?,?,?,?)',
                              (t['agent'], t['session'], t['model'], t['timestamp'][:10], t['timestamp'], t['input'], json.dumps(t['output'], ensure_ascii=False), t['source'], search_text))
            tid = row.lastrowid
            con.execute('INSERT INTO search(rowid,text) VALUES(?,?)', (tid, search_text))
            if t.get('words', True):
                con.executemany('INSERT INTO terms VALUES(?,?,?)', [(term, tid, count) for term, count in Counter(tokenize(t['input'])).items()])
        meta = dict(updated=datetime.now().astimezone().isoformat(), warnings=data.warnings, sources=dict(data.sources), agents=TOOLS)
        con.executemany('INSERT INTO meta VALUES(?,?)', [(k, json.dumps(v, ensure_ascii=False)) for k, v in meta.items()])
        con.commit()
    except BaseException:
        con.close()
        temporary.unlink(missing_ok=True)
        raise
    con.close()
    os.replace(temporary, path)


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


def positive(params, key, default, maximum):
    try:
        value = int(params.get(key, default))
    except ValueError:
        value = 0
    if not 1 <= value <= maximum:
        raise ValueError(f'{key} 须为 1–{maximum} 的整数')
    return value


def query(con, endpoint, p, exclude=()):
    if endpoint == '/api/meta':
        result = {r['key']: json.loads(r['value']) for r in con.execute('SELECT * FROM meta')}
        result.update(turns=con.execute('SELECT count(*) FROM turns').fetchone()[0],
                      sessions=con.execute('SELECT count(*) FROM (SELECT DISTINCT agent,session FROM turns)').fetchone()[0],
                      models=[r[0] for r in con.execute('SELECT DISTINCT model FROM usage ORDER BY model')])
        return result
    if endpoint == '/api/usage':
        unit = p.get('unit', 'day')
        start, end, buckets = period(unit, positive(p, 'n', 14, 366))
        clauses, values = bounds(dict(p, start=start, end=end))
        rows = [dict(r) for r in con.execute('SELECT * FROM usage WHERE ' + ' AND '.join(clauses), values)]
        for row in rows:
            row['bucket'] = bucket(row['day'], unit)
            row['total'] = sum(row[k] for k in FIELDS[:-1])
        return dict(rows=rows, buckets=buckets, start=start, end=end)
    clauses, values = bounds(p)
    if p.get('model'):
        idx = clauses.index('model = ?')
        clauses[idx] = "instr(' · ' || model || ' · ', ?) > 0"
        values[idx] = ' · ' + p['model'] + ' · '
    if endpoint == '/api/words':
        if exclude:
            clauses.append('term NOT IN (' + ','.join('?' * len(exclude)) + ')')
            values.extend(exclude)
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        limit = positive(p, 'limit', 40, 200)
        words = [dict(r) for r in con.execute(
            'SELECT term,sum(count) AS count,count(*) AS turns FROM terms JOIN turns ON turns.id=terms.turn_id'
            + where + ' GROUP BY term ORDER BY count DESC,term LIMIT ?', values + [limit])]
        return dict(words=words, excluded=list(exclude))
    if endpoint == '/api/search':
        q = p.get('q', '').strip()
        if len(q) > 500:
            raise ValueError('搜索词不能超过 500 字')
        terms = q.split()
        long_terms = [term for term in terms if len(term) >= 3]
        if long_terms:
            clauses.append('id IN (SELECT rowid FROM search WHERE search MATCH ?)')
            values.append(' AND '.join('"' + term.replace('"', '""') + '"' for term in long_terms))
        for term in terms:
            clauses.append("search_text LIKE ? ESCAPE '\\'")
            values.append('%' + term.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%')
        if p.get('term'):
            clauses.append('id IN (SELECT turn_id FROM terms WHERE term=?)')
            values.append(p['term'].casefold())
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        page = positive(p, 'page', 1, 1000000)
        count = con.execute('SELECT count(*) FROM turns' + where, values).fetchone()[0]
        rows = [dict(r) for r in con.execute('SELECT id,agent,session,model,timestamp,substr(input,1,240) AS preview,search_text FROM turns'
                                            + where + ' ORDER BY timestamp DESC,id DESC LIMIT 20 OFFSET ?', values + [(page - 1) * 20])]
        for row in rows:
            content = row.pop('search_text')
            if terms:
                pos = content.casefold().find(terms[0].casefold())
                row['match'] = content[max(0, pos - 65):max(0, pos - 65) + 240]
        return dict(rows=rows, total=count, page=page, pages=(count + 19) // 20)
    if endpoint == '/api/turn':
        try:
            turn_id = int(p.get('id', 0))
        except ValueError:
            raise ValueError('id 须为整数')
        row = con.execute('SELECT id,agent,session,model,timestamp,input,output,source FROM turns WHERE id=?', (turn_id,)).fetchone()
        if not row:
            raise ValueError('会话轮次不存在，请刷新搜索结果')
        result = dict(row)
        result['output'] = json.loads(result['output'])
        return result
    raise ValueError('未知接口')


class Server(ThreadingHTTPServer):
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    def respond(self, code, body, mime='application/json; charset=utf-8'):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def allowed(self):
        host = self.headers.get('Host', '')
        origin = self.headers.get('Origin')
        expected = f'127.0.0.1:{self.server.server_port}'
        return host in (expected, f'localhost:{self.server.server_port}') and (not origin or origin in ('http://' + expected, f'http://localhost:{self.server.server_port}'))

    def do_GET(self):
        if not self.allowed():
            return self.respond(403, {'error': '仅允许本机同源访问'})
        parsed = urlsplit(self.path)
        try:
            if parsed.path.startswith('/api/'):
                p = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
                with closing(connect(self.server.db)) as con:
                    result = query(con, parsed.path, p, excluded_words())
                return self.respond(200, result)
            assets = {'/': ('index.html', 'text/html'), '/app.js': ('app.js', 'text/javascript'), '/style.css': ('style.css', 'text/css'), '/favicon.ico': ('favicon.ico', 'image/x-icon')}
            if parsed.path not in assets:
                return self.respond(404, {'error': '未找到页面'})
            name, mime = assets[parsed.path]
            self.respond(200, (ROOT / 'static' / name).read_bytes(), mime + '; charset=utf-8')
        except ValueError as error:
            self.respond(400, {'error': str(error)})
        except sqlite3.Error as error:
            self.respond(500, {'error': f'索引不可用：{error}'})

    def do_POST(self):
        if not self.allowed() or self.headers.get('X-Stats-Request') != '1':
            return self.respond(403, {'error': '仅允许本机同源操作'})
        if self.path == '/api/words-exclude':
            try:
                length = int(self.headers.get('Content-Length', 0))
                payload = json.loads(self.rfile.read(length) or b'{}')
                words = payload.get('words')
                if not isinstance(words, list):
                    raise ValueError('words 须为数组')
                return self.respond(200, {'ok': True, 'excluded': save_excluded(words)})
            except ValueError as error:
                return self.respond(400, {'error': str(error)})
            except OSError as error:
                return self.respond(500, {'error': f'排除词保存失败：{error}'})
        if self.path != '/api/refresh':
            return self.respond(404, {'error': '未知接口'})
        if not self.server.refresh_lock.acquire(blocking=False):
            return self.respond(409, {'error': '正在刷新索引，请稍候'})
        try:
            rebuild(self.server.db, self.server.home)
            self.respond(200, {'ok': True})
        except Exception as error:
            self.respond(500, {'error': f'刷新失败，保留旧索引：{error}'})
        finally:
            self.server.refresh_lock.release()

    def log_message(self, fmt, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=18763)
    parser.add_argument('--home', help='日志所在的用户目录')
    parser.add_argument('--reindex', action='store_true', help='重建索引后退出')
    args = parser.parse_args()
    directory = ROOT / '.stats'
    directory.mkdir(mode=0o700, exist_ok=True)
    suffix = '' if args.home is None else '-' + hashlib.sha256(str(Path(args.home).resolve()).encode()).hexdigest()[:8]
    path = directory / f'index{suffix}.sqlite'
    if args.reindex or args.home or not path.exists():
        print('正在读取本机会话并构建索引…', flush=True)
        rebuild(path, args.home)
    if args.reindex:
        print('索引完成。')
        return
    server = Server(('127.0.0.1', args.port), Handler)
    server.db, server.home, server.refresh_lock = path, args.home, threading.Lock()
    print(f'打开 http://127.0.0.1:{args.port} · Ctrl+C 停止', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
