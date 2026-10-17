"""Task #47: isolated HTTP/SQLite integration tests; no live users, worker or data."""
import concurrent.futures
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing

ROOT = Path(__file__).resolve().parents[1]
BOOT = tempfile.TemporaryDirectory()
os.environ['KB_DATA'] = BOOT.name
os.environ['KB_WORKER_TOKEN'] = 'isolated-test-token'
sys.path.insert(0, str(ROOT / 'server'))
import main
import kb_queue as queue
import mcp_kb
import store
from fastapi import HTTPException
from fastapi.testclient import TestClient
from types import SimpleNamespace


class SharedQuestions(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        data = Path(self.folder.name)
        store.DATA, store.DB = data, data / 'kb.db'
        store.ENTRIES, store.UPLOADS = data / 'entries', data / 'uploads'
        store.PLATFORM_DB = str(data / 'platform.db')
        with closing(sqlite3.connect(store.PLATFORM_DB)) as c:
            c.execute('CREATE TABLE users(userid TEXT,name TEXT,role TEXT)')
            c.executemany('INSERT INTO users VALUES(?,?,?)', [('a','甲','tester'),('b','乙','tester'),('c','丙','tester'),('admin','管理员','admin')])
            c.commit()
        store.init()
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)

    def login(self, uid):
        response = self.client.post('/login', data={'userid':uid}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)

    def create(self, text='发货时间怎么算'):
        return queue.create_question(store.get_user('a'), text, card={'summary':text})

    def answer(self, qid, plain='第一轮回答', kept=None):
        with store.db() as c:
            job = queue._claim(c, 'unit-worker')
        self.assertIsNotNone(job)
        self.assertEqual(job['question_id'], qid)
        return self.client.post('/api/worker/result/'+job['id']+'?worker=unit-worker',
            headers={'X-KB-Worker':'isolated-test-token'},
            json={'result':{'plain':plain,'detail':'解释依据','confidence':'high'},
                  'kept':kept if kept is not None else [{'file':'lib/order.dart','line':10,'quote':'ship_by_at'}], 'commit':'test-only'})

    def test_pending_is_archived_and_other_member_can_read(self):
        self.login('a')
        response = self.client.post('/ask', data={'text':'未回答问题也记录'})
        qid = str(response.url).split('/')[-1]
        self.login('b')
        self.assertEqual(self.client.get('/q/'+qid).status_code, 200)
        page = self.client.get('/kb?q=未回答问题也记录').text
        self.assertIn('未回答问题也记录', page)
        archive = self.client.get('/kb/Q-'+qid).text
        self.assertIn('原始提问人：a', archive)
        self.assertIn('回到这个问题继续提问', archive)
        self.assertIn('/q/'+qid, archive)

    def test_b_then_c_follow_up_keep_authors_and_all_context(self):
        qid = self.create()
        self.assertEqual(self.answer(qid).status_code, 200)
        self.login('b')
        self.assertIn('继续提问', self.client.get('/q/'+qid).text)
        r = self.client.post('/q/'+qid+'/followup', data={'text':'乙追问的是付款分界'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.answer(qid, '第二轮回答').status_code, 200)
        self.login('c')
        self.client.post('/q/'+qid+'/followup', data={'text':'丙继续问冷静期'})
        with store.db() as c:
            job = c.execute("SELECT payload FROM jobs WHERE state='queued'").fetchone()
            card = json.loads(job['payload'])['card']
        self.assertEqual([m['author'] for m in card['thread'] if m['role']=='user'], ['a','b','c'])
        self.assertIn('第一轮回答', str(card['thread']))
        self.assertIn('第二轮回答', str(card['thread']))
        self.answer(qid, '第三轮回答')
        view = queue.question_view(qid)
        self.assertEqual(view['q']['asker'], 'a')
        self.assertEqual(len(view['answers']), 3)
        archive = self.client.get('/kb/'+view['q']['entry_id']).text
        self.assertIn('乙追问的是付款分界', archive)
        self.assertIn('第三轮回答', archive)
        self.assertIn('乙追问的是付款分界', self.client.get('/kb?q=乙追问的是付款分界').text)
        self.assertIn('/q/'+qid, self.client.get('/mine').text)

    def test_concurrent_followups_do_not_duplicate_active_job(self):
        qid = self.create()
        self.answer(qid)
        def submit(uid):
            try:
                queue.follow_up(store.get_user(uid), qid, uid+'的问题')
                return 200
            except HTTPException as exc:
                return exc.status_code
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, ['b','c']))
        self.assertEqual(sorted(results), [200,409])
        with store.db() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM jobs WHERE question_id=? AND state='queued'", (qid,)).fetchone()[0], 1)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM question_turns WHERE question_id=?',(qid,)).fetchone()[0], 2)

    def test_unreviewed_answer_archived_without_approval(self):
        qid = self.create()
        self.answer(qid, kept=[])
        self.login('b')
        self.assertIn('需审核', self.client.get('/kb/Q-'+qid).text)
        self.assertEqual(queue.question_view(qid)['answers'][0]['needs_review'], 1)
        self.assertEqual(self.client.post('/q/'+qid+'/publish').status_code, 403)
        self.login('admin')
        self.assertIn(qid, self.client.get('/admin').text)
        self.assertEqual(self.client.post('/q/'+qid+'/publish').status_code, 200)
        self.assertEqual(queue.question_view(qid)['answers'][0]['needs_review'], 0)

    def test_legacy_migration_and_restart_are_idempotent(self):
        now = time.time()
        with store.db() as c:
            c.execute('INSERT INTO questions(id,asker,raw_text,status,created_at) VALUES(?,?,?,?,?)', ('legacy','deleted-user','旧问题','失败',now-50))
            c.execute('INSERT INTO audit_log VALUES(?,?,?,?,?)',(now-20,'b','followup','legacy','旧追问片段'))
        store.init()
        store.init()
        with store.db() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM question_turns WHERE question_id='legacy'").fetchone()[0], 2)
        self.login('c')
        body = self.client.get('/kb/Q-legacy').text
        self.assertIn('deleted-user', body)
        self.assertIn('旧追问片段', body)
        self.assertIn('可能截断', body)
        self.client.post('/q/legacy/followup', data={'text':'接着旧问题追问'})
        store.init()
        self.assertEqual(queue.question_view('legacy')['q']['asker'], 'deleted-user')
        self.assertEqual(len(queue.question_view('legacy')['messages']), 3)

    def test_shared_screenshots_require_login(self):
        qid = queue.create_question(store.get_user('a'),'截图',images=['1.png'], card={})
        folder = store.UPLOADS/qid
        folder.mkdir()
        (folder/'1.png').write_bytes(b'fake test image')
        self.login('b')
        self.assertEqual(self.client.get('/upload/'+qid+'/1.png').status_code, 200)
        self.client.cookies.clear()
        self.assertIn('/login', str(self.client.get('/q/'+qid).url))
        self.assertEqual(self.client.get('/api/entries/search?q=截图').json(), [])
        self.assertEqual(self.client.get('/api/q/'+qid+'/state').status_code, 401)

    def test_failed_worker_retries_preserve_history(self):
        qid = self.create()
        for attempt in range(3):
            with store.db() as c:
                job = queue._claim(c,'unit-worker')
            r=self.client.post('/api/worker/result/'+job['id']+'?worker=unit-worker',headers={'X-KB-Worker':'isolated-test-token'},json={'error':'temporary failure'})
            self.assertEqual(r.status_code, 200)
        self.login('b')
        self.assertIn('状态：失败', self.client.get('/kb/Q-'+qid).text)
        self.assertIn('继续提问', self.client.get('/q/'+qid).text)
        self.client.post('/q/'+qid+'/followup',data={'text':'换个方式解释'})
        self.assertEqual(len(queue.question_view(qid)['messages']),2)

    def test_mcp_other_member_reads_and_follows_same_thread(self):
        qid=self.create()
        self.answer(qid)
        ctx=SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(headers={'x-kb-uid':'b'})))
        self.assertEqual(mcp_kb.kb_status(qid,ctx)['asker'],'a')
        self.assertEqual(mcp_kb.kb_followup(qid,'MCP追问',ctx)['question_id'],qid)
        self.assertEqual(queue.question_view(qid)['messages'][-1]['author'],'b')

    def test_bad_input_and_stale_result_rejected(self):
        qid=self.create()
        self.answer(qid)
        self.login('b')
        for text in [' ', 'x'*1001]:
            self.assertEqual(self.client.post('/q/'+qid+'/followup',data={'text':text}).status_code,400)
        response=self.client.post('/api/worker/result/missing?worker=unit-worker',headers={'X-KB-Worker':'isolated-test-token'},json={'result':{}})
        self.assertEqual(response.status_code,409)
        self.assertEqual(len(queue.question_view(qid)['answers']),1)

    def test_worker_search_does_not_use_unverified_archives_as_evidence(self):
        self.create('唯一关键词临时问答')
        self.assertEqual(queue.worker_entries_search('唯一关键词临时问答','isolated-test-token'),[])

    def test_legacy_schema_upgrade_preserves_curated_markdown(self):
        store.save_entry(dict(id='KB-0001',title='人工资料',status='published',scope='public'), '人工整理正文')
        curated=(store.ENTRIES/'KB-0001.md').read_bytes()
        with store.db() as c:
            c.execute('DROP TABLE question_turns')
            c.execute('ALTER TABLE answers DROP COLUMN turn_round')
            c.execute('INSERT INTO questions(id,asker,raw_text,status,created_at,entry_id) VALUES(?,?,?,?,?,?)', ('old','a','历史原问题','已发布',time.time()-10,'KB-0001'))
            c.execute('INSERT INTO answers VALUES(?,?,?,?,?,?,?,?,?,?,?)', ('old',1,'旧答案','旧依据','[]','[]','[]','low','old-sha',1,time.time()-5))
        store.init()
        self.assertEqual((store.ENTRIES/'KB-0001.md').read_bytes(),curated)
        self.assertEqual(queue.question_view('old')['q']['entry_id'],'Q-old')
        self.assertEqual(queue.question_view('old')['messages'][0]['author'],'a')
        self.login('b')
        self.assertIn('旧答案',self.client.get('/kb/Q-old').text)


if __name__=='__main__':
    unittest.main(verbosity=2)
