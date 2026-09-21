#!/usr/bin/env python3
"""统计 agent 的 token 用量；从 server 的 /api/usage 读取（含启用中的远端），日期按本地时区。"""
import argparse
import json
import sys
import urllib.request
from collections import defaultdict
from datetime import date, timedelta
from stats_data import FIELDS, TOOLS

DEFAULT_URL = 'http://127.0.0.1:18763'


def total(row):
    return sum(row[k] for k in FIELDS[:-1])


def fetch_usage(url, params):
    query = '&'.join(f'{key}={value}' for key, value in params.items())
    with urllib.request.urlopen(f'{url}/api/usage?{query}', timeout=15) as response:
        return json.loads(response.read().decode('utf-8'))


def render_table(headers, rows, aligns):
    widths = [max([len(h)] + [len(row[i]) for row in rows]) for i, h in enumerate(headers)]

    def pad(value, width, align):
        return value.rjust(width) if align == 'r' else value.ljust(width)

    def line(cells):
        return ' | '.join(pad(cell, width, align) for cell, width, align in zip(cells, widths, aligns))

    lines = [line(headers), ' | '.join('-' * width for width in widths)]
    lines += [line(row) for row in rows]
    lines.append('─' * (sum(widths) + 3 * (len(widths) - 1)))
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('date', nargs='?', type=date.fromisoformat, help='YYYY-MM-DD，默认今天')
    period = parser.add_mutually_exclusive_group()
    period.add_argument('--week', action='store_true', help='最近 7 天')
    period.add_argument('--days', type=int, help='最近 N 天')
    parser.add_argument('--json', action='store_true', help='输出按日期、agent、模型聚合的 JSON')
    parser.add_argument('--url', default=DEFAULT_URL, help=f'server 地址，默认 {DEFAULT_URL}')
    args = parser.parse_args()
    if args.days is not None and args.days < 1:
        parser.error('--days 需要正整数')
    if args.date and (args.week or args.days):
        parser.error('指定日期不能与 --week / --days 同时使用')
    params = {'unit': 'day'}
    if args.date:
        params['start'] = params['end'] = args.date.isoformat()
    else:
        params['n'] = str(7 if args.week else args.days or 1)
    try:
        data = fetch_usage(args.url, params)
    except (OSError, ValueError) as error:
        sys.exit(f'无法从 {args.url} 获取用量：{error}（server 是否已启动？）')
    start = date.fromisoformat(data['start'])
    end = date.fromisoformat(data['end'])
    n = (end - start).days + 1
    rows = defaultdict(lambda: dict.fromkeys(FIELDS, 0))
    for row in data['rows']:
        key = (row['day'], row['agent'], row['model'])
        for k in FIELDS:
            rows[key][k] += row[k]
    if args.json:
        print(json.dumps({'start': data['start'], 'end': data['end'], 'usage': [
            dict(day=d, agent=a, model=m, **v, total=total(v)) for (d, a, m), v in sorted(rows.items())
        ]}, ensure_ascii=False, indent=2))
        return
    print(f'日期: {start}' + (f' 至 {end}' if n > 1 else ''))
    print('口径: input 不含缓存；out 包含思考；total = input + cache_w + cache_r + out')
    if n > 1:
        headers = ['date', *TOOLS, 'total']
        data = []
        for i in range(n):
            day = (start + timedelta(days=i)).isoformat()
            values = [sum(total(v) for (d, a, m), v in rows.items() if d == day and a == agent) for agent in TOOLS]
            data.append([day] + [f'{v:,}' for v in values] + [f'{sum(values):,}'])
        print('\n' + render_table(headers, data, ['l'] + ['r'] * (len(headers) - 1)))
    grouped = defaultdict(lambda: dict.fromkeys(FIELDS, 0))
    for (d, a, m), values in rows.items():
        for k in FIELDS:
            grouped[(a, m)][k] += values[k]
    headers = ['agent / model', 'calls', 'input', 'cache_w', 'cache_r', 'out', 'total']
    data = []
    summed = dict.fromkeys(FIELDS, 0)
    for (a, m), v in sorted(grouped.items()):
        data.append([f'{a} / {m}', f'{v["msgs"]:,}'] + [f'{v[k]:,}' for k in FIELDS[:-1]] + [f'{total(v):,}'])
        for k in FIELDS:
            summed[k] += v[k]
    data.append(['total', f'{summed["msgs"]:,}'] + [f'{summed[k]:,}' for k in FIELDS[:-1]] + [f'{total(summed):,}'])
    print('\n' + render_table(headers, data, ['l'] + ['r'] * (len(headers) - 1)))


if __name__ == '__main__':
    main()
