import json
import os
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from stats_data import Dataset, collect, usage_values
from stats_server import (LOOPBACK, Handler, allowed_hosts, clean_remotes, close_sources, connect,
                          excluded_words, load_remotes, open_sources, period, query, rebuild,
                          remote_path, remote_status, remotes_payload, save_excluded, save_remotes,
                          sync_remote, update_index)


TS = '2026-09-18T12:00:00+08:00'


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
        path.write_text('\n'.join(json.dumps(dict(timestamp=TS, **row)) for row in rows) + '\n')
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
        rebuild(db,self.home)
        con2=connect(db)
        self.addCleanup(con2.close)
        self.assertEqual(query(con2,'/api/export',dict(section='turns',limit='1'))['turns'][0]['fingerprint'],fingerprint)
        second=query(con,'/api/export',dict(section='turns',limit='1',cursor=str(first['cursor'])))
        self.assertEqual(len(second['turns']),1)
        self.assertIsNone(second['cursor'])
        self.assertNotIn('meta',second)
        with self.assertRaises(ValueError):
            query(con,'/api/export',dict(section='nope'))

    def test_remotes_config_validates_dedupes_and_forces_words_to_search(self):
        path=self.home/'remotes.json'
        self.assertEqual(clean_remotes([
            dict(host=' Desk.local ',port='28763',usage=True,search=False,words=True),
            dict(host='desk.local',port=28763),
            dict(host='',port=1),
            dict(host='ok.local',port=70000),
            'junk',
        ]),[dict(host='desk.local',port=28763,enabled=True,usage=True,search=True,words=True)])
        self.assertEqual(clean_remotes([dict(host='off.local',port=9,enabled=False)]),
                         [dict(host='off.local',port=9,enabled=False,usage=True,search=True,words=True)])
        saved=save_remotes([dict(host=' Desk.local ',port='28763',usage=True,search=False,words=True)],path)
        self.assertEqual(saved,[dict(host='desk.local',port=28763,enabled=True,usage=True,search=True,words=True)])
        self.assertEqual(load_remotes(path),saved)
        with self.assertRaises(ValueError):
            save_remotes([dict(host='x.local',port=0)],path)
        with self.assertRaises(ValueError):
            save_remotes([dict(host='x.local',port=1),'junk'],path)
        self.assertEqual(load_remotes(path),saved)
        (self.home/'broken.json').write_text('{nope',encoding='utf-8')
        self.assertEqual(load_remotes(self.home/'broken.json'),[])

    def remote_log(self, home, rows):
        return self.write('.pi/agent/sessions/a.jsonl', rows, home=home)

    def remote_fetch(self, index):
        remote_con = connect(index)
        self.addCleanup(remote_con.close)
        calls = []
        def fetch(remote, path):
            calls.append(path)
            parsed = urlsplit(path)
            return query(remote_con, parsed.path, {k: v[-1] for k, v in parse_qs(parsed.query).items()})
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
        self.assertTrue(any('limit=50&cursor=1' in path for path in calls))
        self.assertFalse(any('limit=50&cursor=0' in path for path in calls))
        (remote_home/'.pi/agent/sessions/a.jsonl').write_text(
            json.dumps(dict(timestamp=TS,type='message',id='x1',message=dict(role='user',content='REMOTE 丙')))+'\n')
        rebuild(remote_index,remote_home)
        fetch,calls=self.remote_fetch(remote_index)
        third=sync_remote(remote,directory,fetch)
        self.assertIsNone(third['error'])
        self.assertEqual(third['turns'],1)
        self.assertTrue(any('limit=50&cursor=0' in path for path in calls))
        store=connect(remote_path(remote,directory))
        self.addCleanup(store.close)
        self.assertEqual(query(store,'/api/search',dict(q='REMOTE 丙'))['total'],1)
        self.assertEqual(query(store,'/api/search',dict(q='REMOTE 甲'))['total'],0)

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
        self.assertEqual(sum(r['total'] for r in query(con,'/api/usage',dict(n='30'),sources=sources)['rows']),13)
        terms={w['term'] for w in query(con,'/api/words',dict(limit='100'),sources=sources)['words']}
        self.assertIn('localword',terms)
        self.assertIn('remoteword',terms)
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
        self.assertEqual(sum(r['total'] for r in query(con,'/api/usage',dict(n='30'),sources=limited)['rows']),13)

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


if __name__=='__main__':
    unittest.main()
