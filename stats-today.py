#!/usr/bin/env python3
"""统计本机五类 agent 的 token 用量；日期按本地时区。"""
import argparse
import json
from collections import defaultdict
from datetime import date, timedelta
from stats_data import FIELDS, TOOLS, collect


def total(row):
    return sum(row[k] for k in FIELDS[:-1])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('date', nargs='?', type=date.fromisoformat, help='YYYY-MM-DD，默认今天')
    period = parser.add_mutually_exclusive_group()
    period.add_argument('--week', action='store_true', help='最近 7 天')
    period.add_argument('--days', type=int, help='最近 N 天')
    parser.add_argument('--json', action='store_true', help='输出按日期、agent、模型聚合的 JSON')
    parser.add_argument('--home', help='从指定用户目录读取日志')
    args = parser.parse_args()
    n = 7 if args.week else args.days or 1
    if args.days is not None and args.days < 1:
        parser.error('--days 需要正整数')
    if args.date and (args.week or args.days):
        parser.error('指定日期不能与 --week / --days 同时使用')
    end = args.date or date.today()
    start = end - timedelta(days=n - 1)
    data = collect(args.home)
    rows = defaultdict(lambda: dict.fromkeys(FIELDS, 0))
    for event in data.usage:
        if start.isoformat() <= event['day'] <= end.isoformat():
            key = (event['day'], event['agent'], event['model'])
            for k in FIELDS:
                rows[key][k] += event[k]
    if args.json:
        print(json.dumps({'start': str(start), 'end': str(end), 'usage': [
            dict(day=d, agent=a, model=m, **v, total=total(v)) for (d, a, m), v in sorted(rows.items())
        ], 'warnings': data.warnings}, ensure_ascii=False, indent=2))
        return
    print(f'日期: {start}' + (f' 至 {end}' if n > 1 else ''))
    print('口径: input 不含缓存；out 包含思考；total = input + cache_w + cache_r + out')
    if n > 1:
        print(f"{'date':<12}" + ''.join(f'{a:>14}' for a in TOOLS) + f"{'total':>14}")
        for i in range(n):
            day = (start + timedelta(days=i)).isoformat()
            values = [sum(total(v) for (d, a, m), v in rows.items() if d == day and a == agent) for agent in TOOLS]
            print(f'{day:<12}' + ''.join(f'{v:>14,}' for v in values) + f'{sum(values):>14,}')
    grouped = defaultdict(lambda: dict.fromkeys(FIELDS, 0))
    for (d, a, m), values in rows.items():
        for k in FIELDS:
            grouped[(a, m)][k] += values[k]
    width = max([20] + [len(a + ' / ' + m) for a, m in grouped])
    print(f"\n{'agent / model':<{width}} {'calls':>7} {'input':>12} {'cache_w':>12} {'cache_r':>12} {'out':>12} {'total':>14}")
    summed = dict.fromkeys(FIELDS, 0)
    for (a, m), v in sorted(grouped.items()):
        print(f'{a + " / " + m:<{width}} {v["msgs"]:>7,}' + ''.join(f'{v[k]:>13,}' for k in FIELDS[:-1]) + f'{total(v):>15,}')
        for k in FIELDS:
            summed[k] += v[k]
    print(f'{"total":<{width}} {summed["msgs"]:>7,}' + ''.join(f'{summed[k]:>13,}' for k in FIELDS[:-1]) + f'{total(summed):>15,}')
    if data.warnings:
        print(f'\n读取提示 {len(data.warnings)} 条；使用 --json 查看明细。')


if __name__ == '__main__':
    main()
