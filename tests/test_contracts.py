import hashlib
import http.client
import importlib.util
import io
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import stats_data
from stats_data import Dataset, collect, usage_values
from stats_server import (AUTO_REFRESH_SECONDS, LOOPBACK, SCHEMA_VERSION, Handler, Server, allowed_hosts,
                          changed_files, clean_local, clean_remotes, close_sources, connect, display_url,
                          ensure_indexes, excluded_words, index_suffix, index_version, load_local,
                          load_remotes, meta_value, open_sources, parse_args, period, query, rebuild,
                          remote_path, remote_status, remotes_payload, save_excluded, save_local,
                          save_remotes, sleep_until, sync_remote, sync_remotes, update_index)


TS = '2026-09-18T12:00:00+08:00'
DSH_TIME = 1789800000000


def pb_varint(value):
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def pb_bytes(number, payload):
    return pb_varint(number << 3 | 2) + pb_varint(len(payload)) + payload


def fake_handler(host, origin=None, names=None, client='127.0.0.1', forwarded=None):
    handler = object.__new__(Handler)
    handler.server = type('Server', (), {'server_port': 18763, 'allowed_names': set(names or allowed_hosts())})()
    handler.headers = {key: value for key, value in
                       {'Host': host, 'Origin': origin, 'X-Forwarded-For': forwarded}.items() if value is not None}
    handler.client_address = (client, 51000)
    return handler


class Contracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def write(self, relative, rows, home=None):
        path = (home or self.home) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('\n'.join(json.dumps({**dict(timestamp=TS), **row}) for row in rows) + '\n')
        return path

    def write_zstd(self, relative, rows, home=None):
        path = (home or self.home) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = '\n'.join(json.dumps(row, ensure_ascii=False) for row in rows) + '\n'
        subprocess.run(['zstd', '-q', '-o', str(path), '-'], input=raw.encode(), check=True)
        return path

    def test_codex_usage_includes_reasoning_once_and_preserves_legacy_turns(self):
        usage = dict(input_tokens=100, cached_input_tokens=60, cache_write_input_tokens=10,
                     output_tokens=30, reasoning_output_tokens=20, total_tokens=130)
        path = self.write('.codex/sessions/a.jsonl', [
            dict(type='turn_context', payload=dict(model='old', turn_id='old-turn')),
            dict(type='event_msg', payload=dict(type='token_count', info=dict(last_token_usage=usage,total_token_usage=usage))),
            dict(type='event_msg', payload=dict(type='token_count', info=dict(last_token_usage=usage,total_token_usage=usage))),
            dict(type='turn_context', payload=dict(model='new', turn_id='new-turn')),
            dict(type='token_usage_record', payload=dict(usage=usage, response_id='r1', turn_id='new-turn')),
            dict(type='event_msg', payload=dict(type='token_count', info=dict(last_token_usage=usage,total_token_usage=usage))),
        ])
        d = Dataset()
        d.read_file('codex',path)
        self.assertEqual([u['model'] for u in d.usage], ['old','new'])
        self.assertEqual(sum(d.usage[0][k] for k in ('new','cc','cr','out')),130)
        self.assertEqual(d.usage[0]['out'],30)

    def test_claude_streamed_usage_updates_without_counting_twice(self):
        rows = [
            dict(type='user', uuid='u', message=dict(role='user',content='缓存统计')),
            dict(type='assistant',uuid='a1',message=dict(id='m',model='claude-model',role='assistant',content=[dict(type='thinking',thinking='检查')],usage=dict(input_tokens=20,output_tokens=1))),
            dict(type='user',uuid='t',message=dict(role='user',content=[dict(type='tool_result',content='查询结果')])),
            dict(type='assistant',uuid='a2',message=dict(id='m',model='claude-model',role='assistant',content=[dict(type='text',text='完成')],usage=dict(input_tokens=20,output_tokens=12))),
        ]
        d=Dataset()
        d.read_file('claude',self.write('.claude/projects/a.jsonl',rows))
        self.assertEqual(len(d.turns),1)
        self.assertEqual([p['kind'] for p in d.turns[0]['output']],['思考','工具结果','正文'])
        self.assertEqual(len(d.usage),1)
        self.assertEqual(d.usage[0]['out'],12)

    def test_grok_splits_models_without_adding_aggregate_usage(self):
        rows=[]
        for i,u in enumerate([
            dict(sessionUpdate='user_message_chunk',content=dict(type='text',text='问题'),_meta=dict(modelId='A',promptIndex=0)),
            dict(sessionUpdate='agent_message_chunk',content=dict(type='text',text='答')),
            dict(sessionUpdate='agent_message_chunk',content=dict(type='text',text='案')),
            dict(sessionUpdate='turn_completed',usage=dict(inputTokens=30,outputTokens=5,modelUsage={'A':dict(inputTokens=10,outputTokens=2),'B':dict(inputTokens=20,outputTokens=3)})),
        ]):
            rows.append(dict(params=dict(sessionId='s',update=u,_meta=dict(eventId=str(i)))))
        d=Dataset()
        d.read_file('grok',self.write('.grok/sessions/s/updates.jsonl',rows))
        self.assertEqual(sum(u['new']+u['out'] for u in d.usage),35)
        self.assertEqual(d.turns[0]['output'][0]['text'],'答案')

    def test_pi_model_switch_and_tool_results_stay_in_one_round(self):
        path=self.write('.pi/agent/sessions/a.jsonl',[
            dict(type='message',id='u',message=dict(role='user',content='统计')),
            dict(type='message',id='a',message=dict(role='assistant',model='model-a',content=[dict(type='toolCall',name='read',arguments={})],usage=dict(input=20,output=5))),
            dict(type='message',id='t',message=dict(role='toolResult',content='工具输出')),
            dict(type='message',id='b',message=dict(role='assistant',model='model-b',content=[dict(type='text',text='结论')],usage=dict(input=30,output=8))),
        ])
        d=Dataset()
        d.read_file('pi',path)
        self.assertEqual(len(d.turns),1)
        self.assertEqual(d.turns[0]['model'],'model-a · model-b')
        self.assertEqual([p['kind'] for p in d.turns[0]['output']],['工具调用','工具结果','正文'])

    def test_omp_output_includes_thinking_and_dedupes_by_message(self):
        path=self.write('.omp/agent/sessions/slug/2026-01-01T00-00-00-000Z_sid.jsonl',[
            dict(type='session',version=3,id='sid',cwd='/x'),
            dict(type='model_change',id='mc',model='provider/model-a'),
            dict(type='message',id='u',message=dict(role='user',content=[dict(type='text',text='统计')])),
            dict(type='message',id='a',message=dict(role='assistant',provider='provider',model='model-a',
                 content=[dict(type='thinking',thinking='想'),dict(type='toolCall',name='read',arguments={})],
                 usage=dict(input=20,cacheWrite=3,cacheRead=40,output=7,totalTokens=70))),
            dict(type='message',id='t',message=dict(role='toolResult',content=[dict(type='text',text='工具输出')])),
            dict(type='message',id='b',message=dict(role='assistant',provider='provider',model='model-a',
                 content=[dict(type='text',text='结论')],usage=dict(input=20,cacheWrite=3,cacheRead=40,output=9,totalTokens=72))),
            dict(type='message',id='b',message=dict(role='assistant',provider='provider',model='model-a',
                 content=[],usage=dict(input=20,cacheWrite=3,cacheRead=40,output=99,totalTokens=162))),
        ])
        d=Dataset()
        d.read_file('omp',path)
        self.assertEqual(len(d.turns),1)
        self.assertEqual([p['kind'] for p in d.turns[0]['output']],['思考','工具调用','工具结果','正文'])
        self.assertEqual(d.turns[0]['model'],'model-a')  # model_change 的 provider/model 全名不混入轮次
        self.assertEqual([u['out'] for u in d.usage],[7,9])  # 同 id 的重复消息只计一次
        self.assertEqual((d.usage[0]['new'],d.usage[0]['cc'],d.usage[0]['cr'],d.usage[0]['msgs']),(20,3,40,1))

    def test_agy_reads_transcript_model_protobuf_and_checkpoint(self):
        session='agy-session'
        path=self.write(f'.gemini/antigravity-cli/brain/{session}/.system_generated/logs/transcript.jsonl',[
            dict(step_index=0,source='USER_EXPLICIT',type='USER_INPUT',created_at='2026-09-18T12:00:00Z',
                 content='<USER_REQUEST>\n统计 AGYWORD\n</USER_REQUEST>\n'
                         '<ADDITIONAL_METADATA>\nSECRETCTX\n</ADDITIONAL_METADATA>'),
            dict(step_index=1,source='MODEL',type='PLANNER_RESPONSE',created_at='2026-09-18T12:00:01Z',
                 input_tokens=100,cache_read_tokens=20,output_tokens=10,
                 thinking='想',tool_calls=[dict(name='read_file',args=dict(path='x'))]),
            dict(step_index=2,source='MODEL',type='GENERIC',created_at='2026-09-18T12:00:02Z',
                 content='工具输出'),
            dict(step_index=3,source='MODEL',type='PLANNER_RESPONSE',created_at='2026-09-18T12:00:03Z',
                 input_tokens=50,cache_read_tokens=110,output_tokens=5,content='结论'),
            dict(step_index=4,source='SYSTEM',type='CHECKPOINT',created_at='2026-09-18T12:00:04Z',
                 input_tokens=7,output_tokens=3,content='压缩摘要'),
            dict(step_index=5,source='SYSTEM',type='SYSTEM_MESSAGE',created_at='2026-09-18T12:00:05Z',
                 content='系统提示'),
            # 同 step 重复出现：按 (step, 类型) 只计一次，不能被 MAX 抬成 999。
            dict(step_index=3,source='MODEL',type='PLANNER_RESPONSE',created_at='2026-09-18T12:00:03Z',
                 input_tokens=999,cache_read_tokens=999,output_tokens=999,content='重复'),
        ])
        db=self.home/f'.gemini/antigravity-cli/conversations/{session}.db'
        db.parent.mkdir(parents=True)
        con=sqlite3.connect(db)
        con.execute('CREATE TABLE gen_metadata(idx INTEGER,data BLOB,size INTEGER)')
        con.executemany('INSERT INTO gen_metadata VALUES(?,?,?)',[
            (0,pb_bytes(2,pb_varint(1))+pb_bytes(1,pb_bytes(19,b'model-a')),0),
            (1,pb_bytes(2,pb_varint(3))+pb_bytes(1,pb_bytes(19,b'model-b')),0),
        ])
        con.commit(); con.close()
        d=Dataset()
        d.read_file('agy',path)
        self.assertEqual([(u['model'],u['new'],u['cc'],u['cr'],u['out'],u['msgs']) for u in d.usage],
                         [('model-a',100,0,20,10,1),('model-b',50,0,110,5,1),('model-b',7,0,0,3,1)])
        self.assertEqual([t['input'] for t in d.turns],['统计 AGYWORD','压缩摘要'])
        self.assertEqual([p['kind'] for p in d.turns[0]['output']],['思考','工具调用','工具结果','正文'])
        self.assertTrue(d.turns[0]['output'][1]['text'].startswith('read_file'))
        self.assertEqual(d.turns[0]['model'],'model-a · model-b')
        self.assertFalse(d.turns[1].get('words',True))
        self.assertEqual(d.sources['agy'],1)
        self.assertEqual(collect(self.home).sources['agy'],1)

    def test_agy_missing_model_db_still_reads_usage(self):
        path=self.write('.gemini/antigravity-cli/brain/no-db/.system_generated/logs/transcript.jsonl',[
            dict(step_index=0,source='USER_EXPLICIT',type='USER_INPUT',created_at='2026-09-18T12:00:00Z',
                 content='<USER_REQUEST>问</USER_REQUEST>'),
            dict(step_index=1,source='MODEL',type='PLANNER_RESPONSE',created_at='2026-09-18T12:00:01Z',
                 input_tokens=3,output_tokens=2,content='答'),
        ])
        d=Dataset()
        d.read_file('agy',path)
        self.assertEqual([(u['model'],u['new'],u['out']) for u in d.usage],[('unknown',3,2)])
        self.assertEqual([t['input'] for t in d.turns],['问'])

    def test_agy_prefers_full_transcript_without_double_counting_and_falls_back(self):
        # 会话 A 同时存在 compact 与 full：优先读完整版，且同一会话只计一次来源与用量。
        self.write('.gemini/antigravity-cli/brain/both/.system_generated/logs/transcript.jsonl',[
            dict(step_index=0,source='USER_EXPLICIT',type='USER_INPUT',created_at='2026-09-18T12:00:00Z',
                 content='<USER_REQUEST>长文问题</USER_REQUEST>'),
            dict(step_index=1,source='MODEL',type='PLANNER_RESPONSE',created_at='2026-09-18T12:00:01Z',
                 input_tokens=10,output_tokens=5,content='开头\n<truncated 9000 bytes>\n结尾',truncated_fields=['content']),
        ])
        self.write('.gemini/antigravity-cli/brain/both/.system_generated/logs/transcript_full.jsonl',[
            dict(step_index=0,source='USER_EXPLICIT',type='USER_INPUT',created_at='2026-09-18T12:00:00Z',
                 content='<USER_REQUEST>长文问题</USER_REQUEST>'),
            dict(step_index=1,source='MODEL',type='PLANNER_RESPONSE',created_at='2026-09-18T12:00:01Z',
                 input_tokens=10,output_tokens=5,content='开头\n完整中间段\n结尾'),
        ])
        # 会话 B 只有 compact：正常回退。
        self.write('.gemini/antigravity-cli/brain/compact-only/.system_generated/logs/transcript.jsonl',[
            dict(step_index=0,source='USER_EXPLICIT',type='USER_INPUT',created_at='2026-09-18T12:01:00Z',
                 content='<USER_REQUEST>仅精简版</USER_REQUEST>'),
            dict(step_index=1,source='MODEL',type='PLANNER_RESPONSE',created_at='2026-09-18T12:01:01Z',
                 input_tokens=4,output_tokens=2,content='精简答复'),
        ])
        d=collect(self.home)
        self.assertEqual(d.sources['agy'],2)
        self.assertEqual(sum(u['new'] for u in d.usage),14)
        by_session={t['session']:t for t in d.turns}
        self.assertEqual(by_session['both']['output'][0]['text'],'开头\n完整中间段\n结尾')
        self.assertTrue(by_session['both']['source'].endswith('transcript_full.jsonl'))
        self.assertEqual(by_session['compact-only']['output'][0]['text'],'精简答复')
        self.assertTrue(by_session['compact-only']['source'].endswith('transcript.jsonl'))

    def test_agy_incremental_upgrade_from_compact_to_full_transcript_replaces_turns(self):
        session='agy-upgrade'
        self.write(f'.gemini/antigravity-cli/brain/{session}/.system_generated/logs/transcript.jsonl',[
            dict(step_index=0,source='USER_EXPLICIT',type='USER_INPUT',created_at='2026-09-18T12:00:00Z',
                 content='<USER_REQUEST>升级测试</USER_REQUEST>'),
            dict(step_index=1,source='MODEL',type='PLANNER_RESPONSE',created_at='2026-09-18T12:00:01Z',
                 input_tokens=12,output_tokens=6,content='前文\n<truncated 100 bytes>\n后文',truncated_fields=['content']),
        ])
        db=self.home/'agy-upgrade.sqlite'
        rebuild(db,self.home)
        with closing(connect(db)) as con:
            before_rev=meta_value(con,'export_revision')
            before_turn=query(con,'/api/turn',{'id':'local:1'})
            self.assertIn('<truncated 100 bytes>',before_turn['output'][0]['text'])
        # 出现完整版 transcript_full.jsonl 后，增量刷新须先清理旧文件再入库，不能因 identity 冲突丢失轮次。
        self.write(f'.gemini/antigravity-cli/brain/{session}/.system_generated/logs/transcript_full.jsonl',[
            dict(step_index=0,source='USER_EXPLICIT',type='USER_INPUT',created_at='2026-09-18T12:00:00Z',
                 content='<USER_REQUEST>升级测试</USER_REQUEST>'),
            dict(step_index=1,source='MODEL',type='PLANNER_RESPONSE',created_at='2026-09-18T12:00:01Z',
                 input_tokens=12,output_tokens=6,content='前文\n完整未截断正文\n后文'),
        ])
        self.assertGreaterEqual(update_index(db,self.home),2)
        with closing(connect(db)) as con:
            after_rev=meta_value(con,'export_revision')
            self.assertNotEqual(after_rev,before_rev)
            sess=query(con,'/api/session',{'agent':'agy','session':session})
            self.assertEqual(len(sess['turns']),1)
            turn=query(con,'/api/turn',{'id':sess['turns'][0]['id']})
            self.assertEqual(turn['output'][0]['text'],'前文\n完整未截断正文\n后文')
            self.assertTrue(turn['source'].endswith('transcript_full.jsonl'))
            hits=query(con,'/api/search',{'q':'完整未截断正文'})
            self.assertEqual(hits['total'],1)

    def test_opencode_reads_messages_and_parts_without_writing_source(self):
        db=self.home/'.local/share/opencode/opencode.db'
        db.parent.mkdir(parents=True)
        con=sqlite3.connect(db)
        con.executescript('CREATE TABLE message(id,session_id,time_created,data); CREATE TABLE part(id,message_id,time_created,data);')
        # 时间戳用真实量级的 epoch 毫秒：1970 年的日期在 Python 3.14/Windows 上 astimezone() 会抛 OSError
        con.executemany('INSERT INTO message VALUES(?,?,?,?)',[
            ('u','s',1700000001000,json.dumps(dict(role='user',model=dict(modelID='x')))),
            ('a','s',1700000002000,json.dumps(dict(role='assistant',parentID='u',modelID='x',tokens=dict(input=10,output=4,reasoning=2,cache=dict(read=3,write=1))))),
        ])
        con.executemany('INSERT INTO part VALUES(?,?,?,?)',[
            ('p','u',1700000001000,json.dumps(dict(type='text',text='测试'))),
            ('q','a',1700000002000,json.dumps(dict(type='text',text='回答'))),
            ('r','a',1700000003000,json.dumps(dict(type='tool',tool='bash',state=dict(status='completed',input=dict(command='ls'),output='文件列表')))),
        ])
        con.commit();con.close()
        before=db.read_bytes()
        data=collect(self.home)
        self.assertEqual([p['kind'] for p in data.turns[0]['output']],['正文','工具调用','工具结果'])
        self.assertEqual(data.turns[0]['output'][1]['text'],'bash\n{\n  "command": "ls"\n}')
        self.assertEqual(data.turns[0]['output'][2]['text'],'文件列表')
        self.assertEqual(data.usage[0]['out'],6)
        self.assertEqual(db.read_bytes(),before)

    def test_opencode_reads_v2_session_message_schema(self):
        db=self.home/'.local/share/opencode/opencode.db'
        db.parent.mkdir(parents=True)
        con=sqlite3.connect(db)
        con.executescript('CREATE TABLE session_v2(id,model); CREATE TABLE session_message(id,session_id,type,seq,time_created,data);')
        con.execute('INSERT INTO session_v2 VALUES(?,?)',('s',json.dumps(dict(id='v2-model',providerID='p'))))
        con.executemany('INSERT INTO session_message VALUES(?,?,?,?,?,?)',[
            ('u','s','user',1,1700000001000,json.dumps(dict(text='测试',files=[],agents=[]))),
            ('a','s','assistant',2,1700000002000,json.dumps(dict(model=dict(id='v2-model',providerID='p'),content=[
                dict(type='reasoning',text='先想'),
                dict(type='tool',name='bash',state=dict(status='completed',input=dict(command='ls'),content=[dict(type='text',text='文件列表')])),
                dict(type='text',text='回答'),
            ],tokens=dict(input=10,output=4,reasoning=2,cache=dict(read=3,write=1))))),
            ('i','s','idle',3,1700000003000,json.dumps(dict(outcome='succeeded'))),
            ('y','s','synthetic',4,1700000004000,json.dumps(dict(text='SERVERWORD 注入'))),
        ])
        con.commit();con.close()
        before=db.read_bytes()
        data=collect(self.home)
        self.assertEqual([t['input'] for t in data.turns],['测试'])
        self.assertEqual([p['kind'] for p in data.turns[0]['output']],['思考','工具调用','工具结果','正文'])
        self.assertEqual(data.turns[0]['output'][2]['text'],'文件列表')
        self.assertEqual(data.usage[0]['out'],6)
        self.assertEqual([t for t in data.turns if t.get('words') is False],[])
        self.assertEqual(db.read_bytes(),before)

    def test_opencode_reads_v1_and_v2_tables_side_by_side(self):
        db=self.home/'.local/share/opencode/opencode.db'
        db.parent.mkdir(parents=True)
        con=sqlite3.connect(db)
        con.executescript('CREATE TABLE message(id,session_id,time_created,data); CREATE TABLE part(id,message_id,time_created,data);'
                          ' CREATE TABLE session_v2(id,model); CREATE TABLE session_message(id,session_id,type,seq,time_created,data);')
        con.executemany('INSERT INTO message VALUES(?,?,?,?)',[
            ('u1','old',1700000001000,json.dumps(dict(role='user',model=dict(modelID='v1-model')))),
            ('a1','old',1700000002000,json.dumps(dict(role='assistant',parentID='u1',modelID='v1-model',
                                                      tokens=dict(input=10,output=4,reasoning=2,cache=dict(read=3,write=1))))),
        ])
        con.executemany('INSERT INTO part VALUES(?,?,?,?)',[
            ('p1','u1',1700000001000,json.dumps(dict(type='text',text='旧版输入'))),
            ('p2','a1',1700000002000,json.dumps(dict(type='text',text='旧版回答'))),
        ])
        con.execute('INSERT INTO session_v2 VALUES(?,?)',('new',json.dumps(dict(id='v2-model'))))
        con.executemany('INSERT INTO session_message VALUES(?,?,?,?,?,?)',[
            ('u2','new','user',1,1700000003000,json.dumps(dict(text='新版输入'))),
            ('a2','new','assistant',2,1700000004000,json.dumps(dict(model=dict(id='v2-model'),
                                                                   content=[dict(type='text',text='新版回答')],
                                                                   tokens=dict(input=20,output=5)))),
        ])
        con.commit();con.close()
        data=collect(self.home)
        self.assertEqual(sorted(t['input'] for t in data.turns),['新版输入','旧版输入'])
        self.assertEqual(sorted((u['session'],u['model']) for u in data.usage),[('new','v2-model'),('old','v1-model')])
        self.assertEqual(sum(u['out'] for u in data.usage),11)

    def test_malformed_records_become_warnings_instead_of_crashing_collect(self):
        self.write('.pi/agent/sessions/good.jsonl',[
            dict(type='message',id='u',message=dict(role='user',content='正常输入')),
        ])
        bad=self.home/'.codex/sessions/bad.jsonl'
        bad.parent.mkdir(parents=True,exist_ok=True)
        bad.write_text(json.dumps(dict(timestamp=TS,type='turn_context',payload=[1,2]))+'\n[]\n')
        data=collect(self.home)
        self.assertEqual([t['input'] for t in data.turns],['正常输入'])
        self.assertTrue(any('bad.jsonl' in warning for warning in data.warnings))
        self.assertTrue(any('非对象' in warning for warning in data.warnings))

    def test_word_index_counts_only_human_typed_input(self):
        self.write('.claude/projects/a.jsonl',[
            dict(type='user',uuid='u',message=dict(role='user',content='HUMANWORD')),
            dict(type='user',uuid='c',isCompactSummary=True,message=dict(role='user',content='COMPACTWORD')),
            dict(type='user',uuid='s',isSidechain=True,message=dict(role='user',content='SIDEWORD')),
            dict(type='user',uuid='o',message=dict(role='user',content='<local-command-stdout>STDOUTWORD</local-command-stdout>')),
            dict(type='user',uuid='w',message=dict(role='user',content='<!-- wt_5: $HOME/.worktrees/wt_5 -->\n\nHUMANWORD2')),
        ])
        self.write('.codex/sessions/c.jsonl',[
            dict(type='response_item',payload=dict(id='e',type='message',role='user',content=[dict(type='input_text',text='<environment_context>ENVWORD</environment_context>')])),
            dict(type='response_item',payload=dict(id='i',type='message',role='user',content=[dict(type='input_text',text='# AGENTS.md instructions for /tmp\n\n<INSTRUCTIONS>RULESTEXT</INSTRUCTIONS>')])),
        ])
        self.write('.pi/agent/sessions/s.jsonl',[
            dict(type='message',id='k',message=dict(role='user',content='<skill name="x" location="/tmp/x">SKILLTEXT</skill>')),
        ])
        db=self.home/'.local/share/opencode/opencode.db'
        db.parent.mkdir(parents=True,exist_ok=True)
        con=sqlite3.connect(db)
        con.executescript('CREATE TABLE message(id,session_id,time_created,data); CREATE TABLE part(id,message_id,time_created,data);')
        con.executemany('INSERT INTO message VALUES(?,?,?,?)',[
            ('u1','s',10,json.dumps(dict(role='user'))),
            ('u2','s',20,json.dumps(dict(role='user'))),
        ])
        con.executemany('INSERT INTO part VALUES(?,?,?,?)',[
            ('p1','u1',10,json.dumps(dict(type='text',text='<!-- main: $HOME/demo -->\n\nHUMANWORD3'))),
            ('p2','u2',20,json.dumps(dict(type='text',text='[idle-notify:busy->idle] NOTIFYWORD'))),
        ])
        con.commit();con.close()
        review=self.home/'.codex/sessions/r.jsonl'
        review.parent.mkdir(parents=True,exist_ok=True)
        review.write_text('\n'.join(json.dumps(row) for row in [
            dict(timestamp=TS,type='session_meta',payload=dict(id='r',source=dict(subagent=dict(other='guardian')))),
            dict(timestamp=TS,type='response_item',payload=dict(id='m',type='message',role='user',content=[dict(type='input_text',text='REVIEWWORD')])),
        ])+'\n')
        db=self.home/'index.sqlite'
        rebuild(db,self.home)
        con=connect(db)
        self.addCleanup(con.close)
        terms={w['term'] for w in query(con,'/api/words',dict(limit='200'))['words']}
        self.assertIn('humanword',terms)
        self.assertIn('humanword2',terms)
        self.assertIn('humanword3',terms)
        for excluded in ('compactword','sideword','reviewword','stdoutword','envword','environment','rulestext','skilltext','worktrees','demo','notifyword'):
            self.assertNotIn(excluded,terms)
        for found in ('REVIEWWORD','COMPACTWORD','SIDEWORD','STDOUTWORD','ENVWORD','RULESTEXT','SKILLTEXT','HUMANWORD2','HUMANWORD3','NOTIFYWORD'):
            self.assertEqual(query(con,'/api/search',dict(q=found))['total'],1)

    def test_command_wrapper_rounds_are_dropped_or_readable(self):
        self.write('.claude/projects/a.jsonl',[
            dict(type='user',uuid='c',message=dict(role='user',content='<command-name>/clear</command-name>\n<command-message>clear</command-message>\n<command-args></command-args>')),
            dict(type='user',uuid='w',message=dict(role='user',content='<command-message>improve-writing</command-message>\n<command-name>/improve-writing</command-name>\n<command-args>@docs/a.md</command-args>')),
            dict(type='assistant',uuid='a',message=dict(role='assistant',model='m',content=[dict(type='text',text='WORKRESULT')],usage=dict(input_tokens=1,output_tokens=1))),
        ])
        db=self.home/'index.sqlite'
        rebuild(db,self.home)
        con=connect(db)
        self.addCleanup(con.close)
        self.assertEqual(query(con,'/api/search',dict(q=''))['total'],1)
        self.assertEqual(query(con,'/api/search',dict(q='clear'))['total'],0)
        rows=query(con,'/api/search',dict(q='improve-writing'))['rows']
        self.assertEqual(rows[0]['preview'][:33],'/improve-writing @docs/a.md')
        self.assertEqual(query(con,'/api/search',dict(q='WORKRESULT'))['total'],1)
        terms={w['term'] for w in query(con,'/api/words',dict(limit='100'))['words']}
        self.assertNotIn('command',terms)
        self.assertNotIn('clear',terms)

    def test_word_exclusions_persist_and_filter_words(self):
        path=self.home/'words-exclude.txt'
        self.assertEqual(save_excluded([' 用户 ','USER ','用户',7,''],path),['用户','user'])
        self.assertEqual(excluded_words(path),['用户','user'])
        self.write('.pi/agent/sessions/a.jsonl',[
            dict(type='message',id='u',message=dict(role='user',content='用户 users 集群')),
        ])
        db=self.home/'index.sqlite'
        rebuild(db,self.home)
        con=connect(db)
        self.addCleanup(con.close)
        result=query(con,'/api/words',dict(limit='100'),excluded_words(path))
        terms=[w['term'] for w in result['words']]
        self.assertIn('users',terms)
        self.assertNotIn('用户',terms)
        self.assertEqual(result['excluded'],['用户','user'])
        self.assertEqual(query(con,'/api/words',dict(limit='100'))['excluded'],[])

    def test_words_order_ascending_and_descending(self):
        self.write('.pi/agent/sessions/a.jsonl',[
            dict(type='message',id='u',message=dict(role='user',content='alpha alpha alpha beta beta gamma delta')),
        ])
        db=self.home/'index.sqlite'
        rebuild(db,self.home)
        con=connect(db)
        self.addCleanup(con.close)
        default_res=query(con,'/api/words',dict(limit='40'))
        self.assertEqual([(w['term'],w['count']) for w in default_res['words']],
                         [('alpha',3),('beta',2),('delta',1),('gamma',1)])
        desc_res=query(con,'/api/words',dict(limit='2',order='desc'))
        self.assertEqual([(w['term'],w['count']) for w in desc_res['words']],
                         [('alpha',3),('beta',2)])
        asc_res=query(con,'/api/words',dict(limit='2',order='asc'))
        self.assertEqual([(w['term'],w['count']) for w in asc_res['words']],
                         [('delta',1),('gamma',1)])
        asc_all=query(con,'/api/words',dict(limit='40',order='asc'))
        self.assertEqual([(w['term'],w['count']) for w in asc_all['words']],
                         [('delta',1),('gamma',1),('beta',2),('alpha',3)])
        with self.assertRaises(ValueError) as ctx:
            query(con,'/api/words',dict(order='random'))
        self.assertIn('order 须为 desc 或 asc',str(ctx.exception))

    def test_words_hide_digits_and_english(self):
        self.write('.pi/agent/sessions/a.jsonl',[
            dict(type='message',id='u',message=dict(role='user',content='北京 北京 alpha debug3')),
        ])
        db=self.home/'index.sqlite'
        rebuild(db,self.home)
        con=connect(db)
        self.addCleanup(con.close)
        base={w['term'] for w in query(con,'/api/words',dict(limit='100'))['words']}
        self.assertIn('北京',base)
        self.assertIn('alpha',base)
        self.assertIn('debug3',base)
        digits={w['term'] for w in query(con,'/api/words',dict(limit='100',hide_digits='1'))['words']}
        self.assertIn('北京',digits)
        self.assertIn('alpha',digits)
        self.assertNotIn('debug3',digits)
        english={w['term'] for w in query(con,'/api/words',dict(limit='100',hide_english='1'))['words']}
        self.assertIn('北京',english)
        self.assertNotIn('alpha',english)
        self.assertNotIn('debug3',english)
        both={w['term'] for w in query(con,'/api/words',dict(limit='100',hide_digits='1',hide_english='1'))['words']}
        self.assertEqual(both,{'北京'})

    def test_words_filtered_by_model_case_insensitively(self):
        self.write('.pi/agent/sessions/a.jsonl',[
            dict(type='model_change',modelId='TestModel'),
            dict(type='message',id='u1',message=dict(role='user',content='alpha alpha beta')),
        ])
        self.write('.pi/agent/sessions/b.jsonl',[
            dict(type='model_change',modelId='OtherModel'),
            dict(type='message',id='u2',message=dict(role='user',content='gamma gamma')),
        ])
        db=self.home/'index.sqlite'
        rebuild(db,self.home)
        con=connect(db)
        self.addCleanup(con.close)
        terms={w['term']:w['count'] for w in query(con,'/api/words',dict(limit='100',model='testmodel'))['words']}
        self.assertEqual(terms,{'alpha':2,'beta':1})

    def test_index_is_opened_read_only(self):
        missing=self.home/'index.sqlite'
        with self.assertRaises(sqlite3.Error):
            connect(missing)
        self.assertFalse(missing.exists())
        built=self.home/'built.sqlite'
        rebuild(built,self.home)
        con=connect(built)
        self.addCleanup(con.close)
        with self.assertRaises(sqlite3.Error):
            con.execute('CREATE TABLE probe(x)')

    def test_search_matches_whole_round_and_word_index_only_counts_inputs(self):
        self.write('.pi/agent/sessions/a.jsonl',[
            dict(type='message',id='u',message=dict(role='user',content='模型 模型 Kafka 100% <script>alert(1)</script>')),
            dict(type='message',id='a',message=dict(role='assistant',model='x',content=[dict(type='text',text='缓存 ONLYOUTPUT')],usage=dict(input=10,output=2))),
            dict(type='message',id='v',message=dict(role='user',content='另一个问题')),
        ])
        db=self.home/'index.sqlite'
        rebuild(db,self.home)
        con=connect(db)
        self.addCleanup(con.close)
        result=query(con,'/api/search',dict(q='模型 ONLYOUTPUT'))
        self.assertEqual(result['total'],1)
        detail=query(con,'/api/turn',dict(id=result['rows'][0]['id']))
        self.assertIn('<script>',detail['input'])
        words=query(con,'/api/words',dict(limit='100'))['words']
        self.assertEqual(next(w['count'] for w in words if w['term']=='模型'),2)
        self.assertNotIn('onlyoutput',[w['term'] for w in words])
        self.assertEqual(query(con,'/api/search',dict(q='100%'))['total'],1)
        self.assertEqual(query(con,'/api/search',dict(q='%" OR 1=1 --'))['total'],0)
        self.assertEqual(query(con,'/api/search',dict(term='模型'))['total'],1)
        self.assertEqual(query(con,'/api/search',dict(q='不存在'))['total'],0)
        self.assertEqual(query(con,'/api/search',dict(agent='grok'))['total'],0)

    def test_codex_injected_context_is_not_a_user_round(self):
        path=self.write('.codex/sessions/a.jsonl',[
            dict(type='response_item',payload=dict(type='message',role='user',content=[dict(type='input_text',text='agent instructions')],internal_chat_message_metadata_passthrough=dict(content_item_kinds=['agents_md.instructions']))),
            dict(type='response_item',payload=dict(type='message',role='user',content=[dict(type='input_text',text='真正问题')],internal_chat_message_metadata_passthrough=dict(content_item_kinds=['user.text']))),
        ])
        data=Dataset()
        data.read_file('codex',path)
        self.assertEqual([t['input'] for t in data.turns],['真正问题'])

    def test_calendar_periods_include_zero_usage_buckets(self):
        today=date(2026,1,2)
        self.assertEqual(period('month',2,today),('2025-12-01','2026-01-02',['2025-12','2026-01']))
        self.assertEqual(period('week',2,today)[2],['2025-12-22','2025-12-29'])
        self.assertEqual(len(period('day',14,today)[2]),14)

    def test_search_filters_by_user_input_length(self):
        self.write('.pi/agent/sessions/a.jsonl',[
            dict(type='message',id='u',message=dict(role='user',content='甲乙丙丁戊')),
            dict(type='message',id='v',message=dict(role='user',content='甲乙丙')),
            dict(type='message',id='w',message=dict(role='user',content='甲乙丙丁戊己庚辛壬癸')),
        ])
        db=self.home/'index.sqlite'
        rebuild(db,self.home)
        con=connect(db)
        self.addCleanup(con.close)
        self.assertEqual(query(con,'/api/search',dict(q='甲乙'))['total'],3)
        self.assertEqual(query(con,'/api/search',dict(q='甲乙',minlen='4'))['total'],2)
        self.assertEqual(query(con,'/api/search',dict(q='甲乙',minlen='5',maxlen='9'))['total'],1)
        self.assertEqual(query(con,'/api/search',dict(q='甲乙',maxlen='3'))['total'],1)
        self.assertEqual(query(con,'/api/search',dict(minlen='4'))['total'],2)
        self.assertEqual(query(con,'/api/search',dict(maxlen='3'))['total'],1)
        with self.assertRaises(ValueError):
            query(con,'/api/search',dict(minlen='-1'))
        with self.assertRaises(ValueError):
            query(con,'/api/search',dict(maxlen='x'))

    def test_export_serves_usage_and_paged_turns(self):
        self.write('.pi/agent/sessions/a.jsonl',[
            dict(type='message',id='u',message=dict(role='user',content='导出 甲')),
            dict(type='message',id='a',message=dict(role='assistant',model='m',content=[dict(type='text',text='答')],usage=dict(input=5,output=1))),
            dict(type='message',id='v',message=dict(role='user',content='导出 乙')),
            dict(type='message',id='b',message=dict(role='assistant',model='m',content=[dict(type='text',text='再答')],usage=dict(input=3,output=1))),
        ])
        db=self.home/'index.sqlite'
        rebuild(db,self.home)
        con=connect(db)
        self.addCleanup(con.close)
        usage=query(con,'/api/export',dict(section='usage'))
        self.assertEqual(len(usage['usage']),1)
        self.assertEqual(usage['usage'][0]['msgs'],2)
        self.assertTrue(usage['meta']['updated'])
        first=query(con,'/api/export',dict(section='turns',limit='1'))
        self.assertEqual(len(first['turns']),1)
        self.assertIn('meta',first)
        self.assertIsNotNone(first['cursor'])
        self.assertIsInstance(first['turns'][0]['output'],list)
        self.assertTrue(first['turns'][0]['terms'])
        fingerprint=first['turns'][0]['fingerprint']
        self.assertTrue(fingerprint)
        con.close()
        rebuild(db,self.home)
        con2=connect(db)
        self.addCleanup(con2.close)
        self.assertEqual(query(con2,'/api/export',dict(section='turns',limit='1'))['turns'][0]['fingerprint'],fingerprint)
        second=query(con2,'/api/export',dict(section='turns',limit='1',cursor=str(first['cursor'])))
        self.assertEqual(len(second['turns']),1)
        self.assertIsNone(second['cursor'])
        self.assertNotIn('meta',second)
        with self.assertRaises(ValueError):
            query(con,'/api/export',dict(section='nope'))

    def test_remotes_config_validates_dedupes_and_forces_words_to_search(self):
        path=self.home/'remotes.json'
        self.assertEqual(clean_remotes([
            dict(host=' Desk.local ',port='28763',usage=True,search=False,words=True,label=' 书房 Mac '),
            dict(host='desk.local',port=28763),
            dict(host='',port=1),
            dict(host='ok.local',port=70000),
            'junk',
        ]),[dict(host='desk.local',port=28763,label='书房 Mac',enabled=True,usage=True,search=True,words=True)])
        self.assertEqual(clean_remotes([dict(host='off.local',port=9,enabled=False)]),
                         [dict(host='off.local',port=9,label='',enabled=False,usage=True,search=True,words=True)])
        self.assertEqual(clean_remotes([dict(host='x.local',port=9,label=7)])[0]['label'],'7')
        saved=save_remotes([dict(host=' Desk.local ',port='28763',usage=True,search=False,words=True)],path)
        self.assertEqual(saved,[dict(host='desk.local',port=28763,label='',enabled=True,usage=True,search=True,words=True)])
        self.assertEqual(load_remotes(path),saved)
        with self.assertRaises(ValueError):
            save_remotes([dict(host='x.local',port=0)],path)
        with self.assertRaises(ValueError):
            save_remotes([dict(host='x.local',port=1),'junk'],path)
        self.assertEqual(load_remotes(path),saved)
        (self.home/'broken.json').write_text('{nope',encoding='utf-8')
        self.assertEqual(load_remotes(self.home/'broken.json'),[])

    def test_local_config_cleans_fields_and_ignores_invalid(self):
        self.assertEqual(clean_local({'port':18764,'host':' 127.0.0.1 ','home':'/tmp/h',
                                      'allow_host':['a.example',' b.example ','','x',7],'extra':1}),
                         {'port':18764,'host':'127.0.0.1','home':'/tmp/h',
                          'allow_host':['a.example','b.example','x']})
        self.assertEqual(clean_local({'allow_host':'solo.example'}),{'allow_host':['solo.example']})
        # allow_host 合法但为空时保留空数组（便于把「全部参数」原样写回 local.json）
        self.assertEqual(clean_local({'allow_host':[1,None]}),{'allow_host':[]})
        self.assertEqual(clean_local({'allow_host':[]}),{'allow_host':[]})
        for bad in ({'port':0},{'port':70000},{'port':True},{'port':'18764'},{'host':'  '},
                    {'home':5},[],'x'):
            self.assertEqual(clean_local(bad),{})
        self.assertEqual(display_url('0.0.0.0',18764),'http://127.0.0.1:18764')
        self.assertEqual(display_url('::1',18764),'http://[::1]:18764')
        self.assertEqual(display_url('127.0.0.1',18763),'http://127.0.0.1:18763')

    def test_local_config_missing_or_broken_falls_back_to_defaults(self):
        self.assertEqual(load_local(self.home/'nope.json'),{})
        path=self.home/'local.json'
        path.write_text('{nope',encoding='utf-8')
        self.assertEqual(load_local(path),{})
        path.write_text('{"port": 18764, "home": "/tmp/h"}',encoding='utf-8')
        self.assertEqual(load_local(path),{'port':18764,'home':'/tmp/h'})

    def test_local_config_round_trips_through_save(self):
        path=self.home/'local.json'
        saved=save_local({'port':18764,'host':'127.0.0.1','home':'/tmp/h',
                          'allow_host':['a.example']},path)
        self.assertEqual(saved,{'port':18764,'host':'127.0.0.1','home':'/tmp/h','allow_host':['a.example']})
        self.assertEqual(load_local(path),saved)
        if os.name != 'nt':  # Windows 不保留 POSIX 权限位，chmod 只切换只读
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
        # 空 allow_host 也写回，四个键齐全
        self.assertEqual(save_local({'port':18763,'host':'127.0.0.1','home':'/home/u','allow_host':[]},path),
                         {'port':18763,'host':'127.0.0.1','home':'/home/u','allow_host':[]})
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')),
                         {'port':18763,'host':'127.0.0.1','home':'/home/u','allow_host':[]})

    def test_index_suffix_keys_by_resolved_home(self):
        current=str(Path.home().resolve())
        expected=lambda path:'-'+hashlib.sha256(str(Path(path).resolve()).encode()).hexdigest()[:8]
        self.assertEqual(index_suffix(None),expected(current))
        self.assertEqual(index_suffix(current),expected(current))
        self.assertEqual(index_suffix('/tmp/other'),expected('/tmp/other'))
        self.assertNotEqual(index_suffix('/tmp/other'),index_suffix('/tmp/another'))

    def test_startup_args_prefer_cli_over_local_config(self):
        defaults={'port':18764,'host':'0.0.0.0','home':'/tmp/h','allow_host':['cfg.example']}
        args=parse_args([],defaults)
        self.assertEqual((args.port,args.host,args.home,args.allow_host,args.reindex),
                         (18764,'0.0.0.0','/tmp/h',['cfg.example'],False))
        over=parse_args(['--port','18765','--host','127.0.0.1','--home','/tmp/other',
                         '--allow-host','cli.example'],defaults)
        self.assertEqual((over.port,over.host,over.home,over.allow_host),
                         (18765,'127.0.0.1','/tmp/other',['cli.example']))
        plain=parse_args([],{})
        self.assertEqual((plain.port,plain.host,plain.home,plain.allow_host),
                         (18763,'127.0.0.1',None,[]))

    def remote_log(self, home, rows):
        return self.write('.pi/agent/sessions/a.jsonl', rows, home=home)

    def remote_fetch(self, index):
        calls = []
        def fetch(remote, path):
            calls.append(path)
            parsed = urlsplit(path)
            remote_con = connect(index)
            try:
                return query(remote_con, parsed.path, {k: v[-1] for k, v in parse_qs(parsed.query).items()})
            finally:
                remote_con.close()
        return fetch, calls

    def test_incremental_update_only_reindexes_changed_files(self):
        file_a=self.remote_log(self.home,[
            dict(type='message',id='u1',message=dict(role='user',content='增量甲')),
            dict(type='message',id='a1',message=dict(role='assistant',model='m',content=[dict(type='text',text='答')],usage=dict(input=5,output=2))),
        ])
        db=self.home/'index.sqlite'
        rebuild(db,self.home)
        con=connect(db)
        self.addCleanup(con.close)
        self.assertEqual(query(con,'/api/meta',{})['turns'],1)
        before=file_a.stat()
        with file_a.open('a',encoding='utf-8') as handle:
            handle.write(json.dumps(dict(timestamp=TS,type='message',id='u2',message=dict(role='user',content='追加乙')))+'\n')
        os.utime(file_a,(before.st_atime,before.st_mtime))
        self.write('.pi/agent/sessions/b.jsonl',[
            dict(type='message',id='u1',message=dict(role='user',content='新增丙')),
        ])
        changed=update_index(db,self.home)
        self.assertGreaterEqual(changed,2)
        self.assertEqual(query(con,'/api/meta',{})['turns'],3)
        self.assertEqual(query(con,'/api/search',dict(q='追加乙'))['total'],1)
        self.assertEqual(query(con,'/api/search',dict(q='新增丙'))['total'],1)
        self.assertEqual(update_index(db,self.home),0)
        (self.home/'.pi/agent/sessions/b.jsonl').unlink()
        update_index(db,self.home)
        self.assertEqual(query(con,'/api/meta',{})['turns'],2)
        self.assertEqual(query(con,'/api/search',dict(q='新增丙'))['total'],0)

    def test_refresh_without_file_changes_preserves_index_and_update_time(self):
        self.remote_log(self.home,[dict(type='message',id='u1',message=dict(role='user',content='UNCHANGED'))])
        db=self.home/'index.sqlite'
        rebuild(db,self.home)
        before=db.read_bytes()
        modified=db.stat().st_mtime_ns
        self.assertEqual(update_index(db,self.home),0)
        self.assertEqual(db.read_bytes(),before)
        self.assertEqual(db.stat().st_mtime_ns,modified)

    def test_touched_and_appended_files_preserve_existing_turn_ids_and_usage(self):
        first=[dict(type='message',id='u1',message=dict(role='user',content='HISTORYWORD')),
               dict(type='message',id='a1',message=dict(role='assistant',model='m',
                    content=[dict(type='text',text='历史输出')],usage=dict(input=5,output=2)))]
        path=self.remote_log(self.home,first)
        self.write('.pi/agent/sessions/b.jsonl',[
            dict(type='message',id='u2',message=dict(role='user',content='ANOTHERWORD'))])
        db=self.home/'index.sqlite'
        rebuild(db,self.home)
        with closing(connect(db)) as con:
            original=query(con,'/api/session',dict(agent='pi',session='a'))['turns'][0]
            watermark=meta_value(con,'indexed_at')
        os.utime(path,ns=(path.stat().st_atime_ns,(watermark+1)*1000000))
        self.assertEqual(update_index(db,self.home),1)
        with closing(connect(db)) as con:
            self.assertEqual(query(con,'/api/session',dict(agent='pi',session='a'))['turns'][0],original)
        appended=first+[
            dict(type='message',id='u3',message=dict(role='user',content='APPENDEDWORD')),
            dict(type='message',id='a3',message=dict(role='assistant',model='m',
                 content=[dict(type='text',text='追加输出')],usage=dict(input=3,output=1)))]
        self.remote_log(self.home,appended)
        self.assertEqual(update_index(db,self.home),1)
        with closing(connect(db)) as con:
            detail=query(con,'/api/session',dict(agent='pi',session='a'))
            self.assertEqual(detail['turns'][0],original)
            self.assertEqual(len(detail['turns']),2)
            self.assertEqual(detail['usage']['total'],11)
            self.assertEqual(query(con,'/api/search',dict(q='APPENDEDWORD'))['total'],1)
            words={row['term']:row['count'] for row in query(con,'/api/words',{})['words']}
            self.assertEqual(words['historyword'],1)
            self.assertEqual(words['appendedword'],1)
            self.assertEqual(query(con,'/api/export',dict(section='usage'))['usage'][0]['msgs'],2)

    def test_refresh_replaces_edited_output_kinds_and_removes_deleted_turns(self):
        user=dict(type='message',id='u1',message=dict(role='user',content='EDITWORD'))
        def answer(kind,tokens):
            return dict(type='message',id='a1',message=dict(role='assistant',model='m',
                        content=[dict(type=kind,**{kind:'SAMEOUTPUT'})],usage=dict(input=tokens,output=2)))
        self.remote_log(self.home,[user,answer('text',5),
            dict(type='message',id='u2',message=dict(role='user',content='DELETEWORD'))])
        db=self.home/'index.sqlite'
        rebuild(db,self.home)
        self.remote_log(self.home,[user,answer('thinking',9)])
        self.assertEqual(update_index(db,self.home),1)
        with closing(connect(db)) as con:
            detail=query(con,'/api/session',dict(agent='pi',session='a'))
            self.assertEqual(len(detail['turns']),1)
            turn=query(con,'/api/turn',dict(id=detail['turns'][0]['id']))
            self.assertEqual(turn['output'][0]['kind'],'思考')
            self.assertEqual(detail['usage']['total'],11)
            self.assertEqual(query(con,'/api/search',dict(q='SAMEOUTPUT',scope='thinking'))['total'],1)
            self.assertEqual(query(con,'/api/search',dict(q='SAMEOUTPUT',scope='text'))['total'],0)
            self.assertEqual(query(con,'/api/search',dict(q='DELETEWORD'))['total'],0)
            self.assertNotIn('deleteword',{r['term'] for r in query(con,'/api/words',{})['words']})

    def test_refresh_rechecks_word_eligibility_when_text_is_unchanged(self):
        user=dict(type='response_item',payload=dict(id='u',type='message',role='user',
                                                  content=[dict(type='input_text',text='ELIGIBLEWORD')]))
        self.write('.codex/sessions/r.jsonl',[user])
        db=self.home/'index.sqlite'
        rebuild(db,self.home)
        with closing(connect(db)) as con:
            self.assertIn('eligibleword',{r['term'] for r in query(con,'/api/words',{})['words']})
        self.write('.codex/sessions/r.jsonl',[
            dict(type='session_meta',payload=dict(id='r',source=dict(subagent=dict(other='guardian')))),user])
        self.assertEqual(update_index(db,self.home),1)
        with closing(connect(db)) as con:
            self.assertEqual(query(con,'/api/search',dict(q='ELIGIBLEWORD'))['total'],1)
            self.assertNotIn('eligibleword',{r['term'] for r in query(con,'/api/words',{})['words']})

    def test_refresh_failure_rolls_back_all_changed_files_and_usage(self):
        for name in ('a','b'):
            self.write(f'.pi/agent/sessions/{name}.jsonl',[
                dict(type='message',id='u1',message=dict(role='user',content='ORIGINALWORD'))])
        db=self.home/'index.sqlite'
        rebuild(db,self.home)
        with closing(connect(db)) as con:
            before=query(con,'/api/meta',{})
        for name in ('a','b'):
            self.write(f'.pi/agent/sessions/{name}.jsonl',[
                dict(type='message',id='u2',message=dict(role='user',content='REPLACEMENTWORD'))])
        from stats_server import parse_into
        def fail_second(con,agent,path,**kwargs):
            if path.name=='b.jsonl':
                raise RuntimeError('write failed')
            return parse_into(con,agent,path,**kwargs)
        with mock.patch('stats_server.parse_into',side_effect=fail_second):
            with self.assertRaisesRegex(RuntimeError,'write failed'):
                update_index(db,self.home)
        with closing(connect(db)) as con:
            self.assertEqual(query(con,'/api/meta',{}),before)
            self.assertEqual(query(con,'/api/search',dict(q='ORIGINALWORD'))['total'],2)
            self.assertEqual(query(con,'/api/search',dict(q='REPLACEMENTWORD'))['total'],0)
            self.assertEqual(len(changed_files(con,self.home)),2)

    @unittest.skipUnless(hasattr(time,'tzset'),'需要可切换本地时区')
    def test_numeric_timestamps_match_iso_dates_across_day_and_dst_boundaries(self):
        cases=[('2026-03-08T06:59:59.123+00:00','2026-03-08T01:59:59.123000-05:00'),
               ('2026-03-08T07:00:00+00:00','2026-03-08T03:00:00-04:00'),
               ('2026-11-01T05:30:00+00:00','2026-11-01T01:30:00-04:00'),
               ('2026-11-01T06:30:00+00:00','2026-11-01T01:30:00-05:00'),
               ('2026-09-18T01:00:00+00:00','2026-09-17T21:00:00-04:00')]
        try:
            with mock.patch.dict(os.environ,{'TZ':'America/New_York'}):
                time.tzset()
                for iso,expected in cases:
                    seconds=datetime.fromisoformat(iso).timestamp()
                    with self.subTest(iso=iso):
                        self.assertEqual(stats_data.stamp(seconds),expected)
                        self.assertEqual(stats_data.stamp(seconds*1000),expected)
                        self.assertEqual(stats_data.stamp(iso),expected)
                for invalid in (None,{},[],'invalid'):
                    self.assertEqual(stats_data.stamp(invalid),'')
        finally:
            time.tzset()

    def test_remote_batch_sync_runs_concurrently_and_preserves_config_order(self):
        remotes=[dict(host=name,port=18763) for name in ('slow','failed','fast')]
        ready=threading.Barrier(len(remotes))
        def sync(remote,directory):
            ready.wait(timeout=5)
            return dict(host=remote['host'],error='unreachable' if remote['host']=='failed' else None)
        with mock.patch('stats_server.sync_remote',side_effect=sync):
            statuses=sync_remotes(remotes,self.home)
            self.assertEqual(sync_remotes([],self.home),[])
        self.assertEqual([r['host'] for r in statuses],['slow','failed','fast'])
        self.assertEqual([r['error'] for r in statuses],[None,'unreachable',None])

    def test_remote_sync_incremental_and_boundary_fallback(self):
        remote_home=self.home/'remote'
        remote_home.mkdir()
        self.remote_log(remote_home,[
            dict(type='message',id='u1',message=dict(role='user',content='REMOTE 甲')),
            dict(type='message',id='a1',message=dict(role='assistant',model='rm',content=[dict(type='text',text='甲答')],usage=dict(input=5,output=2))),
        ])
        remote_index=self.home/'remote-index.sqlite'
        rebuild(remote_index,remote_home)
        fetch,calls=self.remote_fetch(remote_index)
        remote=dict(host='desk.local',port=28763,enabled=True,usage=True,search=True,words=True)
        directory=self.home/'store'
        directory.mkdir()
        first=sync_remote(remote,directory,fetch)
        self.assertIsNone(first['error'])
        self.assertEqual(first['turns'],1)
        self.assertTrue(remote_path(remote,directory).exists())
        appended=[
            dict(type='message',id='u2',message=dict(role='user',content='REMOTE 乙')),
            dict(type='message',id='a2',message=dict(role='assistant',model='rm',content=[dict(type='text',text='乙答')],usage=dict(input=3,output=1))),
        ]
        with (remote_home/'.pi/agent/sessions/a.jsonl').open('a',encoding='utf-8') as handle:
            handle.write('\n'.join(json.dumps(dict(timestamp=TS,**row)) for row in appended)+'\n')
        update_index(remote_index,remote_home)
        calls.clear()
        second=sync_remote(remote,directory,fetch)
        self.assertEqual(second['turns'],2)
        self.assertTrue(any('limit=500&cursor=1' in path for path in calls))
        self.assertFalse(any('limit=500&cursor=0' in path for path in calls))
        (remote_home/'.pi/agent/sessions/a.jsonl').write_text(
            json.dumps(dict(timestamp=TS,type='message',id='x1',message=dict(role='user',content='REMOTE 丙')))+'\n')
        rebuild(remote_index,remote_home)
        fetch,calls=self.remote_fetch(remote_index)
        third=sync_remote(remote,directory,fetch)
        self.assertIsNone(third['error'])
        self.assertEqual(third['turns'],1)
        self.assertTrue(any('limit=500&cursor=0' in path for path in calls))
        store=connect(remote_path(remote,directory))
        self.addCleanup(store.close)
        self.assertEqual(query(store,'/api/search',dict(q='REMOTE 丙'))['total'],1)
        self.assertEqual(query(store,'/api/search',dict(q='REMOTE 甲'))['total'],0)

    def test_remote_sync_detects_historical_edits_and_deletes_without_moving_cursor(self):
        remote_home=self.home/'history-remote'
        remote_home.mkdir()
        path=self.remote_log(remote_home,[
            dict(type='message',id='u1',message=dict(role='user',content='ORIGINALWORD'))])
        boundary=[dict(type='message',id='u2',message=dict(role='user',content='BOUNDARYWORD'))]
        self.write('.pi/agent/sessions/b.jsonl',boundary,home=remote_home)
        index=self.home/'history-index.sqlite'
        rebuild(index,remote_home)
        fetch,calls=self.remote_fetch(index)
        remote=dict(host='history.local',port=18763,enabled=True,usage=True,search=True,words=True)
        directory=self.home/'history-store'
        directory.mkdir()
        self.assertEqual(sync_remote(remote,directory,fetch)['turns'],2)
        with closing(connect(remote_path(remote,directory))) as con:
            boundary_id=query(con,'/api/search',dict(q='BOUNDARYWORD'))['rows'][0]['id']
        with closing(connect(index)) as con:
            before=query(con,'/api/export',dict(section='turns',cursor='1',limit='1'))['turns'][0]
        self.remote_log(remote_home,[
            dict(type='message',id='u1',message=dict(role='user',content='EDITEDWORD'))])
        update_index(index,remote_home)
        with closing(connect(index)) as con:
            after=query(con,'/api/export',dict(section='turns',cursor='1',limit='1'))['turns'][0]
        self.assertEqual(before['id'],after['id'])
        self.assertNotEqual(before['fingerprint'],after['fingerprint'])
        calls.clear()
        self.assertEqual(sync_remote(remote,directory,fetch)['turns'],2)
        self.assertTrue(any('limit=500&cursor=0' in call for call in calls))
        with closing(connect(remote_path(remote,directory))) as con:
            self.assertEqual(query(con,'/api/search',dict(q='ORIGINALWORD'))['total'],0)
            self.assertEqual(query(con,'/api/search',dict(q='EDITEDWORD'))['total'],1)
        path.unlink()
        update_index(index,remote_home)
        self.assertEqual(sync_remote(remote,directory,fetch)['turns'],1)
        with closing(connect(remote_path(remote,directory))) as con:
            self.assertEqual(query(con,'/api/search',dict(q='BOUNDARYWORD'))['rows'][0]['id'],boundary_id)
        self.write('.pi/agent/sessions/b.jsonl',boundary+[
            dict(type='message',id='u3',message=dict(role='user',content='APPENDWORD'))],home=remote_home)
        update_index(index,remote_home)
        calls.clear()
        self.assertEqual(sync_remote(remote,directory,fetch)['turns'],2)
        self.assertFalse(any('limit=500&cursor=0' in call for call in calls))

    def test_remote_full_resync_reuses_turns_and_rolls_back_partial_term_updates(self):
        remote_home=self.home/'terms-remote'
        remote_home.mkdir()
        user=dict(type='response_item',payload=dict(id='u',type='message',role='user',
                                                  content=[dict(type='input_text',text='ELIGIBLEWORD')]))
        self.write('.codex/sessions/r.jsonl',[user],home=remote_home)
        self.remote_log(remote_home,[
            dict(type='message',id='u2',message=dict(role='user',content='BOUNDARYWORD'))])
        index=self.home/'terms-index.sqlite'
        rebuild(index,remote_home)
        fetch,_=self.remote_fetch(index)
        remote=dict(host='terms.local',port=18763,enabled=True,usage=True,search=True,words=True)
        directory=self.home/'terms-store'
        directory.mkdir()
        self.assertEqual(sync_remote(remote,directory,fetch)['turns'],2)
        store=remote_path(remote,directory)
        with closing(connect(store)) as con:
            turn_id=query(con,'/api/search',dict(q='ELIGIBLEWORD'))['rows'][0]['id']
        self.write('.codex/sessions/r.jsonl',[
            dict(type='session_meta',payload=dict(id='r',source=dict(subagent=dict(other='guardian')))),user],home=remote_home)
        update_index(index,remote_home)
        def interrupted(remote,path):
            params=parse_qs(urlsplit(path).query)
            if params.get('limit')==['500']:
                if params.get('cursor')!=['0']:
                    raise OSError('partial download failed')
                page=fetch(remote,path)
                page['turns']=page['turns'][:1]
                page['cursor']=page['turns'][0]['id']
                return page
            return fetch(remote,path)
        status=sync_remote(remote,directory,interrupted)
        self.assertIn('partial download failed',status['error'])
        with closing(connect(store)) as con:
            self.assertEqual(query(con,'/api/search',dict(q='ELIGIBLEWORD'))['rows'][0]['id'],turn_id)
            self.assertIn('eligibleword',{r['term'] for r in query(con,'/api/words',{})['words']})
        self.assertEqual(sync_remote(remote,directory,fetch)['turns'],2)
        with closing(connect(store)) as con:
            self.assertEqual(query(con,'/api/search',dict(q='ELIGIBLEWORD'))['rows'][0]['id'],turn_id)
            self.assertNotIn('eligibleword',{r['term'] for r in query(con,'/api/words',{})['words']})

    def test_remote_sync_keeps_sources_across_export_pages(self):
        remote_home=self.home/'pages-remote'
        remote_home.mkdir()
        self.remote_log(remote_home, [
            row
            for index in range(600)
            for row in (
                dict(type='message',id=f'u{index}',message=dict(role='user',content=f'SOURCES {index}')),
                dict(type='message',id=f'a{index}',message=dict(role='assistant',model='rm',content=[dict(type='text',text='答')],usage=dict(input=5,output=2))),
            )
        ])
        remote_index=self.home/'pages-index.sqlite'
        rebuild(remote_index,remote_home)
        fetch,calls=self.remote_fetch(remote_index)
        remote=dict(host='desk.local',port=28763,enabled=True,usage=True,search=True,words=True)
        directory=self.home/'pages-store'
        directory.mkdir()
        status=sync_remote(remote,directory,fetch)
        self.assertIsNone(status['error'])
        self.assertEqual(status['turns'],600)
        self.assertTrue(any('cursor=0' in path for path in calls))
        self.assertTrue(any('cursor=500' in path for path in calls))
        store=connect(remote_path(remote,directory))
        self.addCleanup(store.close)
        self.assertEqual(meta_value(store,'sources'),{'pi':1})

    def test_remote_sync_failure_keeps_previous_data(self):
        remote_home=self.home/'fail-remote'
        remote_home.mkdir()
        self.remote_log(remote_home,[
            dict(type='message',id='u1',message=dict(role='user',content='KEEPFAR')),
        ])
        remote_index=self.home/'fail-index.sqlite'
        rebuild(remote_index,remote_home)
        fetch,_=self.remote_fetch(remote_index)
        remote=dict(host='fail.local',port=9,enabled=True,usage=True,search=True,words=True)
        directory=self.home/'fail-store'
        directory.mkdir()
        self.assertEqual(sync_remote(remote,directory,fetch)['turns'],1)
        def broken(remote,path):
            raise OSError('unreachable')
        status=sync_remote(remote,directory,broken)
        self.assertIn('unreachable',status['error'])
        self.assertIn('unreachable',remote_status(remote,directory)['error'])
        store=connect(remote_path(remote,directory))
        self.addCleanup(store.close)
        self.assertEqual(query(store,'/api/search',dict(q='KEEPFAR'))['total'],1)

    def test_query_merges_local_and_enabled_remote_sources(self):
        local_home=self.home/'merge-local'
        local_home.mkdir()
        self.remote_log(local_home,[
            dict(type='message',id='u',message=dict(role='user',content='LOCALWORD 本地问题')),
            dict(type='message',id='a',message=dict(role='assistant',model='local-model',content=[dict(type='text',text='本地回答')],usage=dict(input=2,output=1))),
        ])
        local_index=self.home/'merge-local.sqlite'
        rebuild(local_index,local_home)
        remote_home=self.home/'merge-remote'
        remote_home.mkdir()
        self.remote_log(remote_home,[
            dict(type='message',id='u',message=dict(role='user',content='REMOTEWORD 远端问题')),
            dict(type='message',id='a',message=dict(role='assistant',model='remote-model',content=[dict(type='text',text='远端回答')],usage=dict(input=7,output=3))),
        ])
        remote_index=self.home/'merge-remote.sqlite'
        rebuild(remote_index,remote_home)
        fetch,_=self.remote_fetch(remote_index)
        remote=dict(host='desk.local',port=28763,enabled=True,usage=True,search=True,words=True)
        directory=self.home/'merge-store'
        directory.mkdir()
        sync_remote(remote,directory,fetch)
        config=self.home/'remotes.json'
        save_remotes([remote],config)
        con=connect(local_index)
        self.addCleanup(con.close)
        sources=open_sources(con,load_remotes(config),directory)
        self.addCleanup(lambda: close_sources(sources))
        meta=query(con,'/api/meta',{},sources=sources)
        self.assertEqual(meta['turns'],2)
        self.assertEqual(meta['sessions'],2)
        self.assertEqual(sum(r['total'] for r in query(con,'/api/usage',dict(start=TS[:10],end=TS[:10]),sources=sources)['rows']),13)
        terms={w['term'] for w in query(con,'/api/words',dict(limit='100'),sources=sources)['words']}
        self.assertIn('localword',terms)
        self.assertIn('remoteword',terms)
        asc_terms={w['term'] for w in query(con,'/api/words',dict(limit='100',order='asc'),sources=sources)['words']}
        self.assertIn('localword',asc_terms)
        self.assertIn('remoteword',asc_terms)
        search=query(con,'/api/search',dict(q='REMOTEWORD'),sources=sources)
        self.assertEqual(search['total'],1)
        remote_id=search['rows'][0]['id']
        self.assertNotEqual(remote_id.split(':')[0],'local')
        turn=query(con,'/api/turn',dict(id=remote_id),sources=sources)
        self.assertTrue(turn['source'].startswith('desk.local:28763 · '))
        self.assertEqual(turn['output'],[dict(kind='正文',text='远端回答')])
        self.assertEqual(remotes_payload(directory,config)['remotes'][0]['status']['turns'],1)
        save_remotes([dict(remote,enabled=False)],config)
        disabled=open_sources(con,load_remotes(config),directory)
        self.addCleanup(lambda: close_sources(disabled))
        self.assertEqual(query(con,'/api/meta',{},sources=disabled)['turns'],1)
        self.assertTrue(remote_path(remote,directory).exists())
        save_remotes([dict(remote,search=False,words=False)],config)
        limited=open_sources(con,load_remotes(config),directory)
        self.addCleanup(lambda: close_sources(limited))
        self.assertEqual(query(con,'/api/meta',{},sources=limited)['turns'],1)
        self.assertEqual(sum(r['total'] for r in query(con,'/api/usage',dict(start=TS[:10],end=TS[:10]),sources=limited)['rows']),13)

    def test_usage_honors_explicit_start_end_over_n(self):
        home=self.home/'usage-range'
        home.mkdir()
        self.remote_log(home,[
            dict(type='message',id='u',message=dict(role='user',content='问题')),
            dict(type='message',id='a',message=dict(role='assistant',model='m',content=[dict(type='text',text='答')],usage=dict(input=5,output=2))),
        ])
        index=self.home/'usage-range.sqlite'
        rebuild(index,home)
        con=connect(index)
        self.addCleanup(con.close)
        day=TS[:10]
        single=query(con,'/api/usage',dict(unit='day',start=day,end=day))
        self.assertEqual((single['start'],single['end'],single['buckets']),(day,day,[day]))
        self.assertEqual(sum(r['total'] for r in single['rows']),7)
        span=query(con,'/api/usage',dict(unit='day',start='2026-09-16',end='2026-09-18'))
        self.assertEqual(span['buckets'],['2026-09-16','2026-09-17','2026-09-18'])
        self.assertEqual(sum(r['total'] for r in span['rows']),7)
        flipped=query(con,'/api/usage',dict(unit='day',start='2026-09-18',end='2026-09-16'))
        self.assertEqual((flipped['start'],flipped['end']),('2026-09-16','2026-09-18'))
        empty=query(con,'/api/usage',dict(unit='day',start='2026-09-01',end='2026-09-01'))
        self.assertEqual(empty['rows'],[])
        self.assertEqual(empty['buckets'],['2026-09-01'])

    def test_model_filter_and_model_list_ignore_case(self):
        home=self.home/'model-case'
        home.mkdir()
        self.remote_log(home,[
            dict(type='message',id='u1',message=dict(role='user',content='问题')),
            dict(type='message',id='a1',message=dict(role='assistant',model='Big-Model',content=[dict(type='text',text='答')],usage=dict(input=5,output=1))),
            dict(type='message',id='u2',message=dict(role='user',content='追问')),
            dict(type='message',id='a2',message=dict(role='assistant',model='big-model',content=[dict(type='text',text='答')],usage=dict(input=2,output=1))),
        ])
        index=self.home/'model-case.sqlite'
        rebuild(index,home)
        con=connect(index)
        self.addCleanup(con.close)
        # 模型下拉只列一项，取用量最多的写法
        self.assertEqual(query(con,'/api/meta',{})['models'],['Big-Model'])
        # 两种写法筛选都命中全部用量；/api/usage 行保留日志里的原始写法
        for chosen in ('Big-Model','big-model'):
            rows=query(con,'/api/usage',dict(model=chosen,start=TS[:10],end=TS[:10]))['rows']
            self.assertEqual(sorted(r['model'] for r in rows),['Big-Model','big-model'])
            self.assertEqual(sum(r['total'] for r in rows),9)
            self.assertEqual(query(con,'/api/search',dict(q='答',model=chosen))['total'],2)

    def test_http_security_rejects_foreign_hosts_and_origins(self):
        self.assertTrue(fake_handler('127.0.0.1:18763').allowed())
        self.assertTrue(fake_handler('localhost:18763').allowed())
        self.assertFalse(fake_handler('evil.example').allowed())
        self.assertFalse(fake_handler('127.0.0.1:18763',origin='https://evil.example').allowed())

    def test_allow_host_admits_proxy_names_and_bind_address(self):
        names=allowed_hosts('127.0.0.1',['stats.example.com','192.168.1.5:8443'])
        self.assertEqual(names,set(LOOPBACK)|{'100.64.216.70','stats.example.com','192.168.1.5'})
        self.assertTrue(fake_handler('stats.example.com',names=names).allowed())
        self.assertTrue(fake_handler('stats.example.com:443',origin='https://stats.example.com',names=names).allowed())
        self.assertFalse(fake_handler('evil.example',names=names).allowed())
        self.assertFalse(fake_handler('stats.example.com',origin='https://evil.example',names=names).allowed())
        self.assertTrue(fake_handler('100.64.216.70:18763').allowed())
        self.assertIn('100.64.216.70',allowed_hosts())
        self.assertIn('192.168.1.5',allowed_hosts('192.168.1.5'))
        self.assertNotIn('0.0.0.0',allowed_hosts('0.0.0.0'))

    def test_writes_require_direct_loopback_client(self):
        self.assertTrue(fake_handler('127.0.0.1:18763').is_local())
        self.assertFalse(fake_handler('stats.example.com',names=allowed_hosts('127.0.0.1',['stats.example.com'])).is_local())
        self.assertFalse(fake_handler('127.0.0.1:18763',client='192.168.1.9').is_local())
        self.assertFalse(fake_handler('127.0.0.1:18763',forwarded='203.0.113.5').is_local())
        self.assertTrue(fake_handler('127.0.0.1:18763',forwarded='127.0.0.1').is_local())

    def test_static_assets_return_etag_and_304(self):
        server=Server(('127.0.0.1',0),Handler)
        server.directory=self.home
        server.allowed_names=allowed_hosts()
        threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client=http.client.HTTPConnection('127.0.0.1',server.server_address[1],timeout=5)
        self.addCleanup(client.close)
        client.request('GET','/app.js')
        first=client.getresponse()
        body=first.read()
        etag=first.getheader('ETag')
        self.assertEqual(first.status,200)
        self.assertTrue(etag)
        self.assertEqual(first.getheader('Cache-Control'),'no-cache')
        self.assertTrue(body)
        client.request('GET','/app.js',headers={'If-None-Match':etag})
        second=client.getresponse()
        self.assertEqual(second.status,304)
        self.assertEqual(second.getheader('ETag'),etag)
        self.assertEqual(second.read(),b'')

    def test_meta_reports_writable_for_local_clients_only(self):
        self.write('.pi/agent/sessions/a.jsonl',[
            dict(type='message',id='u',message=dict(role='user',content='概览')),
        ])
        db=self.home/'index.sqlite'
        rebuild(db,self.home)
        con=connect(db)
        self.addCleanup(con.close)
        self.assertTrue(query(con,'/api/meta',{})['writable'])
        self.assertFalse(query(con,'/api/meta',{},writable=False)['writable'])

    @unittest.skipUnless(shutil.which('zstd'),'需要系统 zstd 命令')
    def test_dsh_compressed_session_usage_turns_and_machine_context(self):
        session='.dsh/sessions/--Users-clark-proj--/session-abc/session.v3.jsonl.zstd'
        self.write_zstd(session,[
            dict(type='session',id='session-abc',createdAt=DSH_TIME,cwd='/tmp/proj',delegationDepth=0),
            dict(type='user/message',time=DSH_TIME,data=dict(content=[dict(type='text',text='DSHWORD 问题')],source=dict(kind='user'),role='user',id='u1')),
            dict(type='user/message',time=DSH_TIME+1,data=dict(content=[dict(type='text',text='PLUGINWORD 上下文')],source=dict(kind='plugin'),role='user',id='p1')),
            dict(type='assistant/message',time=DSH_TIME+2,data=dict(turn=1,step=1,message=dict(role='assistant',id='a1',
                 source=dict(kind='model',provider='local',model='dsh-model'),
                 content=[dict(type='reasoning',text='先想想'),dict(type='tool-call',id='c1',name='read',arguments='{"file_path": "/tmp/a"}')]),
                 usage=dict(inputTokens=10,outputTokens=4,cacheReadTokens=2,totalTokens=16))),
            dict(type='tool/result',time=DSH_TIME+3,data=dict(turn=1,step=1,message=dict(source=dict(kind='tool',callId='c1'),
                 content=[dict(type='tool-result',toolCallId='c1',content=[dict(type='text',text='TOOLTEXT')])]))),
            dict(type='assistant/message',time=DSH_TIME+4,data=dict(turn=1,step=2,message=dict(role='assistant',id='a2',
                 source=dict(kind='model',provider='local',model='dsh-model'),
                 content=[dict(type='text',text='DSHANSEWR')]),usage=dict(inputTokens=20,outputTokens=6,cacheReadTokens=1))),
            dict(type='compaction/summary',time=DSH_TIME+5,data=dict(compactionId='cmp1',model='dsh-model',
                 summary=[dict(type='text',text='SUMMARYWORD 摘要')],usage=dict(inputTokens=100,outputTokens=50))),
        ])
        self.write_zstd('.dsh/sessions/--Users-clark-proj--/session-abc/session.jsonl.zstd',[
            dict(type='session',id='session-abc',createdAt=DSH_TIME),
            dict(type='user/message',time=DSH_TIME,data=dict(content=[dict(type='text',text='LEGACYWORD')],source=dict(kind='user'),role='user',id='old')),
        ])
        data=collect(self.home)
        self.assertEqual(data.sources['dsh'],1)
        self.assertEqual([t['session'] for t in data.turns],['session-abc','session-abc'])
        self.assertEqual(data.turns[0]['input'],'DSHWORD 问题')
        self.assertEqual([p['kind'] for p in data.turns[0]['output']],['上下文','思考','工具调用','工具结果','正文'])
        self.assertEqual(data.turns[0]['output'][2]['text'],'read\n{\n  "file_path": "/tmp/a"\n}')
        self.assertEqual(data.turns[0]['model'],'dsh-model')
        self.assertEqual(data.turns[1]['input'],'SUMMARYWORD 摘要')
        self.assertFalse(data.turns[1].get('words',True))
        dsh=[u for u in data.usage if u['agent']=='dsh']
        self.assertEqual(len(dsh),3)
        self.assertEqual([sum(u[field] for u in dsh) for field in ('new','out','cr','msgs')],[130,60,3,3])
        db=self.home/'index.sqlite'
        rebuild(db,self.home)
        con=connect(db)
        self.addCleanup(con.close)
        self.assertEqual(query(con,'/api/search',dict(q='LEGACYWORD'))['total'],0)
        self.assertEqual(query(con,'/api/search',dict(q='DSHANSEWR'))['total'],1)
        self.assertEqual(query(con,'/api/search',dict(q='PLUGINWORD'))['total'],1)
        terms={w['term'] for w in query(con,'/api/words',dict(limit='200'))['words']}
        self.assertIn('dshword',terms)
        for excluded in ('pluginword','summaryword','legacyword'):
            self.assertNotIn(excluded,terms)

    def test_sessions_list_groups_orders_pages_and_filters(self):
        for name,stamps in (('a',('2026-09-18T08:00:00+08:00','2026-09-18T09:00:00+08:00')),('b',('2026-09-18T10:00:00+08:00',))):
            self.write(f'.pi/agent/sessions/{name}.jsonl',[
                dict(timestamp=stamps[0],type='message',id=name+'1',message=dict(role='user',content=name.upper()+'WORD')),
            ]+([dict(timestamp=stamps[1],type='message',id=name+'2',message=dict(role='user',content=name.upper()+'TWO'))] if len(stamps)>1 else []))
        db=self.home/'sessions.sqlite'
        rebuild(db,self.home)
        con=connect(db)
        self.addCleanup(con.close)
        first=query(con,'/api/sessions',{})
        self.assertEqual([(r['session'],r['turns']) for r in first['rows']],[('b',1),('a',2)])
        self.assertEqual((first['total'],first['page'],first['pages']),(2,1,1))
        self.assertEqual(first['rows'][0]['label'],'本机')
        self.assertEqual(query(con,'/api/sessions',dict(agent='pi'))['total'],2)
        self.assertEqual(query(con,'/api/sessions',dict(agent='codex'))['total'],0)
        self.assertEqual(query(con,'/api/sessions',dict(page='2'))['rows'],[])
        detail=query(con,'/api/session',dict(agent='pi',session='a'))
        self.assertEqual([t['id'] for t in detail['turns']],['local:1','local:2'])
        self.assertEqual([t['timestamp'] for t in detail['turns']],['2026-09-18T08:00:00+08:00','2026-09-18T09:00:00+08:00'])
        self.assertEqual(detail['first'],'2026-09-18T08:00:00+08:00')
        self.assertTrue(detail['source'].endswith('a.jsonl'))
        with self.assertRaises(ValueError):
            query(con,'/api/session',dict(agent='pi',session='missing'))
        with self.assertRaises(ValueError):
            query(con,'/api/session',dict(agent='',session='a'))

    def test_sessions_carry_token_totals_and_count_duplicates_once(self):
        rows=[dict(type='message',id='u1',message=dict(role='user',content='TOKENWORD 甲')),
              dict(type='message',id='a1',message=dict(role='assistant',model='m',content=[dict(type='text',text='答')],
                                                       usage=dict(input=10,cacheWrite=4,cacheRead=6,output=3)))]
        self.write('.pi/agent/sessions/a.jsonl',rows)
        # 同一条用量出现在两个文件（历史分叉复制）：按 (agent,key) 只计一次，归给字典序最小的会话。
        self.write('.pi/agent/sessions/a2.jsonl',rows)
        self.write('.pi/agent/sessions/b.jsonl',[
            dict(type='message',id='u2',message=dict(role='user',content='TOKENWORD 乙')),
            dict(type='message',id='a2',message=dict(role='assistant',model='m',content=[dict(type='text',text='答')],
                                                     usage=dict(input=1,output=2)))])
        db=self.home/'tokens.sqlite'
        rebuild(db,self.home)
        con=connect(db)
        self.addCleanup(con.close)
        totals={r['session']:r['total'] for r in query(con,'/api/sessions',{})['rows']}
        self.assertEqual(totals,{'a':23,'a2':None,'b':3})
        session_a=next(r for r in query(con,'/api/sessions',{})['rows'] if r['session']=='a')
        self.assertEqual((session_a['new'],session_a['cc'],session_a['cr'],session_a['out'],session_a['msgs']),(10,4,6,3,1))
        usage=query(con,'/api/usage',dict(start=TS[:10],end=TS[:10]))
        self.assertEqual(sum(r['total'] for r in usage['rows']),26)

    def test_session_totals_reach_remote_and_honor_usage_flag(self):
        remote_home=self.home/'token-remote'
        remote_home.mkdir()
        self.remote_log(remote_home,[
            dict(type='message',id='u',message=dict(role='user',content='REMOTE 用量')),
            dict(type='message',id='a',message=dict(role='assistant',model='rm',content=[dict(type='text',text='答')],
                                                    usage=dict(input=7,cacheRead=5,output=3)))])
        remote_index=self.home/'token-remote.sqlite'
        rebuild(remote_index,remote_home)
        with closing(connect(remote_index)) as remote_con:
            self.assertEqual(query(remote_con,'/api/export',dict(section='usage'))['sessions'][0]['new'],7)
        fetch,_=self.remote_fetch(remote_index)
        remote=dict(host='desk.local',port=28763,enabled=True,usage=True,search=True,words=True)
        directory=self.home/'token-store'
        directory.mkdir()
        sync_remote(remote,directory,fetch)
        local_home=self.home/'token-local'
        local_home.mkdir()
        self.remote_log(local_home,[dict(type='message',id='u',message=dict(role='user',content='LOCAL 用量'))])
        local_index=self.home/'token-local.sqlite'
        rebuild(local_index,local_home)
        con=connect(local_index)
        self.addCleanup(con.close)
        config=self.home/'token-remotes.json'
        save_remotes([remote],config)
        sources=open_sources(con,load_remotes(config),directory)
        self.addCleanup(lambda: close_sources(sources))
        remote_row=next(r for r in query(con,'/api/sessions',{},sources=sources)['rows'] if r['key']!='local')
        self.assertEqual(remote_row['total'],15)
        save_remotes([dict(remote,usage=False)],config)
        muted=open_sources(con,load_remotes(config),directory)
        self.addCleanup(lambda: close_sources(muted))
        quiet=next(r for r in query(con,'/api/sessions',{},sources=muted)['rows'] if r['key']!='local')
        self.assertIsNone(quiet['total'])

    def test_session_detail_routes_remote_and_honors_search_flag(self):
        local_home=self.home/'session-local'
        local_home.mkdir()
        self.remote_log(local_home,[dict(type='message',id='u',message=dict(role='user',content='LOCAL 会话'))])
        local_index=self.home/'session-local.sqlite'
        rebuild(local_index,local_home)
        remote_home=self.home/'session-remote'
        remote_home.mkdir()
        self.remote_log(remote_home,[dict(type='message',id='u',message=dict(role='user',content='REMOTE 会话'))])
        remote_index=self.home/'session-remote.sqlite'
        rebuild(remote_index,remote_home)
        fetch,_=self.remote_fetch(remote_index)
        remote=dict(host='desk.local',port=28763,enabled=True,usage=True,search=True,words=True)
        directory=self.home/'session-store'
        directory.mkdir()
        sync_remote(remote,directory,fetch)
        config=self.home/'session-remotes.json'
        save_remotes([remote],config)
        con=connect(local_index)
        self.addCleanup(con.close)
        sources=open_sources(con,load_remotes(config),directory)
        self.addCleanup(lambda: close_sources(sources))
        listing=query(con,'/api/sessions',{},sources=sources)
        self.assertEqual(len(listing['rows']),2)
        self.assertEqual({r['label'] for r in listing['rows']},{'本机','desk.local:28763'})
        remote_entry=next(r for r in listing['rows'] if r['key']!='local')
        detail=query(con,'/api/session',dict(key=remote_entry['key'],agent=remote_entry['agent'],session=remote_entry['session']),sources=sources)
        self.assertEqual(detail['turns'][0]['id'].split(':')[0],remote_entry['key'])
        self.assertTrue(detail['source'].startswith('desk.local:28763 · '))
        save_remotes([dict(remote,search=False,words=False)],config)
        limited=open_sources(con,load_remotes(config),directory)
        self.addCleanup(lambda: close_sources(limited))
        self.assertEqual(len(query(con,'/api/sessions',{},sources=limited)['rows']),1)
        with self.assertRaises(ValueError):
            query(con,'/api/session',dict(key=remote_entry['key'],agent=remote_entry['agent'],session=remote_entry['session']),sources=limited)

    def test_sleep_until_slices_by_wall_clock_and_wakes_after_system_sleep(self):
        now=[0.0]
        sleeps=[]
        def paced(seconds):
            sleeps.append(seconds)
            now[0]+=seconds
        sleep_until(AUTO_REFRESH_SECONDS,clock=lambda: now[0],sleep=paced)
        self.assertEqual(sleeps,[60]*(AUTO_REFRESH_SECONDS//60))
        self.assertEqual(now[0],AUTO_REFRESH_SECONDS)
        sleeps.clear()
        def suspended(seconds):
            sleeps.append(seconds)
            now[0]+=900
        sleep_until(now[0]+AUTO_REFRESH_SECONDS,clock=lambda: now[0],sleep=suspended)
        self.assertEqual(len(sleeps),2)

    def test_auto_refresh_gate_sees_only_new_activity(self):
        file_a=self.remote_log(self.home,[
            dict(type='message',id='u1',message=dict(role='user',content='闸门甲')),
        ])
        db=self.home/'index.sqlite'
        rebuild(db,self.home)
        con=connect(db)
        self.addCleanup(con.close)
        self.assertEqual(changed_files(con,self.home),[])
        with file_a.open('a',encoding='utf-8') as handle:
            handle.write(json.dumps(dict(timestamp=TS,type='message',id='u2',message=dict(role='user',content='闸门乙')))+'\n')
        self.assertEqual([Path(path).resolve() for path,_ in changed_files(con,self.home)],[file_a.resolve()])
        update_index(db,self.home)
        self.assertEqual(changed_files(con,self.home),[])
        file_a.unlink()
        self.assertEqual([(Path(path).resolve(),agent) for path,agent in changed_files(con,self.home)],
                         [(file_a.resolve(),None)])

    def test_dsh_without_zstd_command_warns_and_skips(self):
        path=self.home/'.dsh/sessions/--proj--/session-x/session.v3.jsonl.zstd'
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(b'')
        with mock.patch.object(stats_data,'ZSTD',None):
            data=collect(self.home)
        self.assertEqual(data.turns,[])
        self.assertTrue(any('zstd' in warning for warning in data.warnings))

    def test_remote_label_flows_to_sessions_and_search(self):
        local_home=self.home/'lbl-local'
        local_home.mkdir()
        self.remote_log(local_home,[dict(type='message',id='u',message=dict(role='user',content='LOCAL 标识'))])
        local_index=self.home/'lbl-local.sqlite'
        rebuild(local_index,local_home)
        remote_home=self.home/'lbl-remote'
        remote_home.mkdir()
        self.remote_log(remote_home,[dict(type='message',id='u',message=dict(role='user',content='LABELWORD 远端'))])
        remote_index=self.home/'lbl-remote.sqlite'
        rebuild(remote_index,remote_home)
        fetch,_=self.remote_fetch(remote_index)
        remote=dict(host='127.0.0.1',port=28763,label='书房 Mac',enabled=True,usage=True,search=True,words=True)
        directory=self.home/'lbl-store'
        directory.mkdir()
        sync_remote(remote,directory,fetch)
        config=self.home/'lbl-remotes.json'
        save_remotes([remote],config)
        con=connect(local_index)
        self.addCleanup(con.close)
        sources=open_sources(con,load_remotes(config),directory)
        self.addCleanup(lambda: close_sources(sources))
        sessions=query(con,'/api/sessions',{},sources=sources)['rows']
        self.assertEqual({r['label'] for r in sessions},{'本机','书房 Mac'})
        search=query(con,'/api/search',dict(q='LABELWORD'),sources=sources)['rows']
        self.assertEqual(search[0]['label'],'书房 Mac')

    def test_sessions_filter_by_query_case_insensitively(self):
        self.write('.pi/agent/sessions/Alpha-Task.jsonl',[
            dict(type='message',id='u1',message=dict(role='user',content='甲')),
        ])
        self.write('.pi/agent/sessions/Beta-Run.jsonl',[
            dict(type='message',id='u2',message=dict(role='user',content='乙')),
        ])
        db=self.home/'q-sessions.sqlite'
        rebuild(db,self.home)
        con=connect(db)
        self.addCleanup(con.close)
        hits=query(con,'/api/sessions',dict(q='alpha'))
        self.assertEqual([r['session'] for r in hits['rows']],['Alpha-Task'])
        self.assertEqual(query(con,'/api/sessions',dict(q='missing'))['total'],0)

    def test_ensure_indexes_backfills_missing_turn_indexes(self):
        self.write('.pi/agent/sessions/a.jsonl',[
            dict(type='message',id='u1',message=dict(role='user',content='索引测试')),
        ])
        db=self.home/'idx.sqlite'
        rebuild(db,self.home)
        rw=sqlite3.connect(db)
        rw.execute('DROP INDEX IF EXISTS turn_session')
        rw.execute('DROP INDEX IF EXISTS turn_time')
        rw.commit()
        rw.close()
        ensure_indexes(db)
        self.assertEqual(index_version(db),SCHEMA_VERSION)
        with closing(connect(db)) as con:
            names={r['name'] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertIn('turn_session',names)
        self.assertIn('turn_time',names)

    def test_agy_companion_db_mtime_triggers_incremental_refresh(self):
        session='agy-watch'
        path=self.write(f'.gemini/antigravity-cli/brain/{session}/.system_generated/logs/transcript.jsonl',[
            dict(step_index=0,source='USER_EXPLICIT',type='USER_INPUT',created_at='2026-09-18T12:00:00Z',
                 content='<USER_REQUEST>测试伴随库</USER_REQUEST>'),
            dict(step_index=1,source='MODEL',type='PLANNER_RESPONSE',created_at='2026-09-18T12:00:01Z',
                 input_tokens=10,output_tokens=2,content='答'),
        ])
        db=self.home/'agy-watch.sqlite'
        rebuild(db,self.home)
        with closing(connect(db)) as con:
            self.assertEqual(changed_files(con,self.home),[])
            self.assertEqual(query(con,'/api/meta',{})['models'],['unknown'])
        comp=self.home/f'.gemini/antigravity-cli/conversations/{session}.db'
        comp.parent.mkdir(parents=True,exist_ok=True)
        c_con=sqlite3.connect(comp)
        c_con.execute('CREATE TABLE gen_metadata(idx INTEGER,data BLOB,size INTEGER)')
        c_con.execute('INSERT INTO gen_metadata VALUES(?,?,?)',
                      (0,pb_bytes(2,pb_varint(1))+pb_bytes(1,pb_bytes(19,b'gemini-4-hydrogen')),0))
        c_con.commit(); c_con.close()
        future=path.stat().st_mtime+10
        os.utime(comp,(future,future))
        with closing(connect(db)) as con:
            self.assertEqual([Path(p).resolve() for p,_ in changed_files(con,self.home)],[path.resolve()])
        self.assertGreaterEqual(update_index(db,self.home),1)
        with closing(connect(db)) as con:
            self.assertEqual(query(con,'/api/meta',{})['models'],['gemini-4-hydrogen'])

    def test_handler_payload_rejects_invalid_and_oversized_lengths(self):
        def make_handler(length_header,body=b''):
            handler=fake_handler('127.0.0.1:18763')
            if length_header is not None:
                handler.headers['Content-Length']=length_header
            handler.rfile=io.BytesIO(body)
            return handler
        self.assertEqual(make_handler(None).payload(),{})
        self.assertEqual(make_handler('0').payload(),{})
        self.assertEqual(make_handler('8',b'{"ok":1}').payload(),{'ok':1})
        for bad in ('-1','abc',str(2*1024*1024)):
            with self.assertRaises(ValueError):
                make_handler(bad).payload()

    def test_stats_today_reads_server_url_and_merges_model_case(self):
        spec=importlib.util.spec_from_file_location('stats_today',Path(__file__).resolve().parent.parent/'stats-today.py')
        mod=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        url_file=self.home/'server.url'
        url_file.write_text('http://127.0.0.1:19999\n',encoding='utf-8')
        self.assertEqual(mod.default_url(url_file),'http://127.0.0.1:19999')
        self.assertEqual(mod.default_url(self.home/'missing.url'),mod.DEFAULT_URL)
        rows={
            ('2026-09-18','pi','Big-Model'):{'new':10,'cc':0,'cr':0,'out':5,'msgs':1},
            ('2026-09-18','pi','big-model'):{'new':2,'cc':0,'cr':0,'out':1,'msgs':1},
        }
        self.assertEqual(mod.merge_model_names(rows),{'big-model':'Big-Model'})

    def test_search_phrase_exclude_and_validation(self):
        self.write('.pi/agent/sessions/phrase.jsonl',[
            dict(type='message',id='u1',timestamp='2026-09-18T10:00:00Z',
                 message=dict(role='user',content='检查 "hello world" 与 -v 参数')),
            dict(type='message',id='a1',timestamp='2026-09-18T10:00:01Z',
                 message=dict(role='assistant',content=[dict(type='text',text='保留结果')])),
            dict(type='message',id='u2',timestamp='2026-09-18T10:01:00Z',
                 message=dict(role='user',content='间隔 hello   world 与 world hello')),
            dict(type='message',id='u3',timestamp='2026-09-18T10:02:00Z',
                 message=dict(role='user',content='普通 hello world 命中 -v 标志')),
            dict(type='message',id='a3',timestamp='2026-09-18T10:02:01Z',
                 message=dict(role='assistant',content=[dict(type='text',text='SKIPWORD 需要过滤')])),
        ])
        db=self.home/'phrase.sqlite'
        rebuild(db,self.home)
        con=connect(db)
        self.addCleanup(con.close)
        phrase_hits=query(con,'/api/search',dict(q='"hello world"'))
        self.assertEqual(phrase_hits['total'],2)
        flag_hits=query(con,'/api/search',dict(q='-v'))
        self.assertEqual(flag_hits['total'],2)
        ex_word=query(con,'/api/search',dict(q='"hello world"',exclude='SKIPWORD'))
        self.assertEqual(ex_word['total'],1)
        ex_phrase=query(con,'/api/search',dict(q='-v',exclude='"hello world"'))
        self.assertEqual(ex_phrase['total'],0)
        ex_partial=query(con,'/api/search',dict(q='hello',exclude='"world hello"'))
        self.assertEqual(ex_partial['total'],2)
        for bad in (dict(scope='bogus'),dict(sort='random'),dict(group='flat')):
            with self.assertRaises(ValueError):
                query(con,'/api/search',bad)

    def test_search_scopes_relevance_and_deduped_matches(self):
        long_prefix='A'*260
        self.write('.claude/projects/p/scoped.jsonl',[
            dict(type='user',uuid='u1',timestamp='2026-09-18T10:00:00Z',
                 message=dict(role='user',content='工具轮')),
            dict(type='assistant',uuid='a1',timestamp='2026-09-18T10:00:01Z',
                 message=dict(id='m1',model='claude-a',role='assistant',content=[
                     dict(type='tool_use',id='t1',name='run',input=dict(cmd='echo FINDME FINDME')),
                 ])),
            dict(type='user',uuid='u1r',timestamp='2026-09-18T10:00:02Z',
                 message=dict(role='user',content=[dict(type='tool_result',tool_use_id='t1',content='FINDME output')])),
            dict(type='user',uuid='u2',timestamp='2026-09-18T11:00:00Z',
                 message=dict(role='user',content='思考正文轮')),
            dict(type='assistant',uuid='a2',timestamp='2026-09-18T11:00:01Z',
                 message=dict(id='m2',model='claude-a',role='assistant',content=[
                     dict(type='thinking',thinking='推导 FINDME 原因'),
                     dict(type='text',text='最终正文 FINDME 说明'),
                 ])),
            dict(type='user',uuid='u3',timestamp='2026-09-18T12:00:00Z',
                 message=dict(role='user',content=f'输入首部 FINDME {long_prefix} 尾部 TAILKEY 检查')),
            dict(type='assistant',uuid='a3',timestamp='2026-09-18T12:00:01Z',
                 message=dict(id='m3',model='claude-a',role='assistant',content=[
                     dict(type='text',text='回复里的 TAILKEY 上下文')
                 ])),
        ])
        db=self.home/'scoped.sqlite'
        rebuild(db,self.home)
        con=connect(db)
        self.addCleanup(con.close)
        self.assertEqual([r['preview'][:5] for r in query(con,'/api/search',dict(q='FINDME',scope='input'))['rows']],['输入首部 '])
        self.assertEqual([r['preview'] for r in query(con,'/api/search',dict(q='FINDME',scope='thinking'))['rows']],['思考正文轮'])
        self.assertEqual([r['preview'] for r in query(con,'/api/search',dict(q='FINDME',scope='text'))['rows']],['思考正文轮'])
        self.assertEqual([r['preview'] for r in query(con,'/api/search',dict(q='FINDME',scope='tool'))['rows']],['工具轮'])
        newest=query(con,'/api/search',dict(q='FINDME',sort='newest'))['rows']
        oldest=query(con,'/api/search',dict(q='FINDME',sort='oldest'))['rows']
        relevant=query(con,'/api/search',dict(q='FINDME',sort='relevance'))['rows']
        self.assertEqual([r['preview'][:4] for r in newest],['输入首部','思考正文','工具轮'])
        self.assertEqual([r['preview'][:4] for r in oldest],['工具轮','思考正文','输入首部'])
        self.assertEqual([r['preview'][:4] for r in relevant],['输入首部','思考正文','工具轮'])
        preview_only=query(con,'/api/search',dict(q='FINDME',scope='input'))['rows'][0]
        self.assertEqual(preview_only['hit_kinds'],['输入'])
        self.assertEqual(preview_only['matches'],[])
        tail_hit=query(con,'/api/search',dict(q='TAILKEY'))['rows'][0]
        self.assertEqual(tail_hit['hit_kinds'],['输入','正文'])
        self.assertEqual([m['kind'] for m in tail_hit['matches']],['输入(后文)','正文'])
        self.assertTrue(all('TAILKEY' in m['text'] for m in tail_hit['matches']))
        term_hit=query(con,'/api/search',dict(term='FINDME'))['rows'][0]
        self.assertEqual(term_hit['hit_kinds'],['输入'])

    def test_search_group_session_source_and_session_filter(self):
        local_home=self.home/'g-local'
        local_home.mkdir()
        self.write('.pi/agent/sessions/Alpha-One.jsonl',[
            dict(type='message',id='u1',timestamp='2026-09-18T10:00:00Z',message=dict(role='user',content='GROUPKEY 第一轮')),
            dict(type='message',id='u2',timestamp='2026-09-18T10:05:00Z',message=dict(role='user',content='GROUPKEY 第二轮')),
        ],home=local_home)
        self.write('.pi/agent/sessions/Beta-Two.jsonl',[
            dict(type='message',id='u3',timestamp='2026-09-18T11:00:00Z',message=dict(role='user',content='GROUPKEY 第三轮')),
        ],home=local_home)
        local_index=self.home/'g-local.sqlite'
        rebuild(local_index,local_home)
        remote_home=self.home/'g-remote'
        remote_home.mkdir()
        self.write('.pi/agent/sessions/Gamma-Remote.jsonl',[
            dict(type='message',id='u4',timestamp='2026-09-18T12:00:00Z',message=dict(role='user',content='GROUPKEY 远端轮')),
        ],home=remote_home)
        remote_index=self.home/'g-remote.sqlite'
        rebuild(remote_index,remote_home)
        fetch,_=self.remote_fetch(remote_index)
        remote=dict(host='127.0.0.1',port=28763,label='工作站',enabled=True,usage=True,search=True,words=True)
        directory=self.home/'g-store'
        directory.mkdir()
        sync_remote(remote,directory,fetch)
        config=self.home/'g-remotes.json'
        save_remotes([remote],config)
        con=connect(local_index)
        self.addCleanup(con.close)
        sources=open_sources(con,load_remotes(config),directory)
        self.addCleanup(lambda: close_sources(sources))
        meta=query(con,'/api/meta',{},sources=sources)
        self.assertEqual([s['label'] for s in meta['search_sources']],['本机','工作站'])
        grouped=query(con,'/api/search',dict(q='GROUPKEY',group='session',sort='newest'),sources=sources)
        self.assertEqual(grouped['group'],'session')
        self.assertEqual(grouped['total'],3)
        self.assertEqual(grouped['total_turns'],4)
        self.assertEqual([(g['session'],g['hit_turns'],g['label']) for g in grouped['rows']],
                         [('Gamma-Remote',1,'工作站'),('Beta-Two',1,'本机'),('Alpha-One',2,'本机')])
        alpha_turns=grouped['rows'][2]['turns']
        self.assertEqual([t['preview'] for t in alpha_turns],['GROUPKEY 第二轮','GROUPKEY 第一轮'])
        only_local=query(con,'/api/search',dict(q='GROUPKEY',source='local'),sources=sources)
        self.assertEqual(only_local['total'],3)
        only_alpha=query(con,'/api/search',dict(q='GROUPKEY',session='alpha'),sources=sources)
        self.assertEqual(only_alpha['total'],2)

    def test_sessions_enhanced_sort_filters_topic_models_and_detail_stats(self):
        self.write('.pi/agent/sessions/s-old-heavy.jsonl',[
            dict(type='model_change',modelId='Model-Alpha',timestamp='2026-09-10T08:00:00+08:00'),
            dict(type='message',id='u1',timestamp='2026-09-10T08:00:00+08:00',
                 message=dict(role='user',content='设计旧重会话主题   多空格压缩')),
            dict(type='message',id='a1',timestamp='2026-09-10T08:00:01+08:00',
                 message=dict(role='assistant',model='Model-Alpha',
                              content=[dict(type='thinking',thinking='推演'),dict(type='text',text='回复')],
                              usage=dict(input=500,cacheWrite=100,cacheRead=50,output=50))),
            dict(type='message',id='u2',timestamp='2026-09-19T12:00:00+08:00',
                 message=dict(role='user',content='后续继续追踪')),
            dict(type='message',id='a2',timestamp='2026-09-19T12:00:01+08:00',
                 message=dict(role='assistant',model='Model-Beta',content=[dict(type='text',text='ok')],
                              usage=dict(input=10,output=5))),
        ])
        self.write('.pi/agent/sessions/s-mid-many.jsonl',[
            dict(type='message',id='m1',timestamp='2026-09-15T09:00:00+08:00',
                 message=dict(role='user',content='多轮会话第1问')),
            dict(type='message',id='m2',timestamp='2026-09-15T09:05:00+08:00',
                 message=dict(role='user',content='多轮会话第2问')),
            dict(type='message',id='m3',timestamp='2026-09-15T09:10:00+08:00',
                 message=dict(role='user',content='多轮会话第3问')),
            dict(type='message',id='ma3',timestamp='2026-09-15T09:10:01+08:00',
                 message=dict(role='assistant',model='model-alpha',content=[dict(type='text',text='答')],
                              usage=dict(input=20,output=10))),
        ])
        self.write('.pi/agent/sessions/s-new-nousage.jsonl',[
            dict(type='message',id='n1',timestamp='2026-09-18T10:00:00+08:00',
                 message=dict(role='user',content='无用量新会话 TOPICKEY 检查')),
        ])
        db=self.home/'sess-enh.sqlite'
        rebuild(db,self.home)
        con=connect(db)
        self.addCleanup(con.close)

        by_created=query(con,'/api/sessions',{})
        self.assertEqual([r['session'] for r in by_created['rows']],['s-new-nousage','s-mid-many','s-old-heavy'])
        self.assertEqual(by_created['summary'],{'sessions':3,'turns':6,'total_tokens':745})
        self.assertEqual(by_created['rows'][2]['topic'],'设计旧重会话主题 多空格压缩')
        self.assertEqual(by_created['rows'][2]['models'],['Model-Alpha','Model-Beta'])

        self.assertEqual([r['session'] for r in query(con,'/api/sessions',dict(sort='active'))['rows']],
                         ['s-old-heavy','s-new-nousage','s-mid-many'])
        self.assertEqual([r['session'] for r in query(con,'/api/sessions',dict(sort='turns'))['rows']],
                         ['s-mid-many','s-old-heavy','s-new-nousage'])
        self.assertEqual([r['session'] for r in query(con,'/api/sessions',dict(sort='tokens'))['rows']],
                         ['s-old-heavy','s-mid-many','s-new-nousage'])

        self.assertEqual([r['session'] for r in query(con,'/api/sessions',dict(q='topickey'))['rows']],['s-new-nousage'])
        by_model=query(con,'/api/sessions',dict(model='MODEL-BETA'))
        self.assertEqual([r['session'] for r in by_model['rows']],['s-old-heavy'])
        self.assertEqual(by_model['rows'][0]['turns'],2)
        by_date=query(con,'/api/sessions',dict(start='2026-09-18',end='2026-09-20'))
        self.assertEqual({r['session'] for r in by_date['rows']},{'s-old-heavy','s-new-nousage'})

        detail=query(con,'/api/session',dict(agent='pi',session='s-old-heavy'))
        self.assertEqual(detail['models'],['Model-Alpha','Model-Beta'])
        self.assertEqual(detail['usage'],{'new':510,'cc':100,'cr':50,'out':55,'msgs':2,'total':715})
        self.assertEqual((detail['turns'][0]['input_len'],detail['turns'][0]['part_count']),(len('设计旧重会话主题   多空格压缩'),2))
        detail_none=query(con,'/api/session',dict(agent='pi',session='s-new-nousage'))
        self.assertIsNone(detail_none['usage'])
        with self.assertRaises(ValueError):
            query(con,'/api/sessions',dict(sort='invalid'))

    def test_words_enhanced_sort_kind_min_turns_source_query_and_summary(self):
        local_home=self.home/'w-local'
        local_home.mkdir()
        self.write('.pi/agent/sessions/w1.jsonl',[
            dict(type='message',id='u1',message=dict(role='user',content='spamword spamword spamword spamword 设计 api3')),
            dict(type='message',id='u2',message=dict(role='user',content='设计 api3 status')),
            dict(type='message',id='u3',message=dict(role='user',content='设计 status')),
        ],home=local_home)
        local_index=self.home/'w-local.sqlite'
        rebuild(local_index,local_home)

        remote_home=self.home/'w-remote'
        remote_home.mkdir()
        self.write('.pi/agent/sessions/wr.jsonl',[
            dict(type='message',id='r1',message=dict(role='user',content='remoteword remoteword')),
        ],home=remote_home)
        remote_index=self.home/'w-remote.sqlite'
        rebuild(remote_index,remote_home)
        fetch,_=self.remote_fetch(remote_index)
        remote=dict(host='127.0.0.1',port=28763,label='远端',enabled=True,usage=True,search=True,words=True)
        directory=self.home/'w-store'
        directory.mkdir()
        sync_remote(remote,directory,fetch)
        config=self.home/'w-remotes.json'
        save_remotes([remote],config)
        con=connect(local_index)
        self.addCleanup(con.close)
        sources=open_sources(con,load_remotes(config),directory)
        self.addCleanup(lambda: close_sources(sources))

        meta=query(con,'/api/meta',{},sources=sources)
        self.assertEqual([s['key'] for s in meta['words_sources']],['local',meta['words_sources'][1]['key']])

        by_count=query(con,'/api/words',dict(source='local'),sources=sources)
        self.assertEqual(by_count['words'][0]['term'],'spamword')
        self.assertEqual(by_count['summary']['scoped_turns'],3)

        by_turns=query(con,'/api/words',dict(source='local',sort='turns',min_turns='2'),sources=sources)
        terms=[w['term'] for w in by_turns['words']]
        self.assertNotIn('spamword',terms)
        self.assertEqual(terms[0],'设计')
        self.assertGreater(by_turns['words'][0]['turn_rate'],90.0)

        by_avg=query(con,'/api/words',dict(source='local',sort='avg'),sources=sources)
        self.assertEqual(by_avg['words'][0]['term'],'spamword')
        self.assertEqual(by_avg['words'][0]['avg'],4.0)

        zh_only=query(con,'/api/words',dict(source='local',kind='zh'),sources=sources)
        self.assertEqual([w['term'] for w in zh_only['words']],['设计'])
        code_only=query(con,'/api/words',dict(source='local',kind='code',q='api'),sources=sources)
        self.assertEqual([w['term'] for w in code_only['words']],['api3'])

        with_remote=query(con,'/api/words',dict(q='remoteword'),sources=sources)
        self.assertEqual(len(with_remote['words']),1)
        only_local_remote=query(con,'/api/words',dict(source='local',q='remoteword'),sources=sources)
        self.assertEqual(only_local_remote['words'],[])

        for bad in (dict(sort='freq'),dict(kind='emoji'),dict(min_turns='0')):
            with self.assertRaises(ValueError):
                query(con,'/api/words',bad,sources=sources)


if __name__=='__main__':
    unittest.main()
