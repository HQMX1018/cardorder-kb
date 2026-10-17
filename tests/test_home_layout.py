"""Task #49: home/search separation using isolated HTTP and SQLite fixtures."""
from html.parser import HTMLParser
import time
import unittest
from urllib.parse import parse_qs, urlsplit

from test_shared_questions import SharedQuestions
import store


class Elements(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.elements=[]
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag,dict(attrs)))


class HomeLayout(unittest.TestCase):
    setUp=SharedQuestions.setUp
    login=SharedQuestions.login

    def counts(self):
        with store.db() as c:
            return tuple(c.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in ['questions','jobs'])

    def test_home_has_one_primary_form_before_collapsed_examples(self):
        self.login('a')
        response=self.client.get('/')
        self.assertEqual(response.status_code,200)
        html=response.text
        elements=Elements(html).elements
        forms=[attrs for tag,attrs in elements if tag=='form']
        self.assertEqual(len(forms),1)
        self.assertEqual(forms[0]['action'],'/ask')
        self.assertIn('提交问题',html)
        self.assertIn('你的问题',html)
        self.assertLess(html.index('id="askf"'),html.index('id="role-heading"'))
        self.assertFalse(any('open' in attrs for tag,attrs in elements if tag=='details' and attrs.get('class')=='role-guide'))
        self.assertIn('class="kb-sidebar"',html)

    def test_search_and_prefill_do_not_create_questions(self):
        self.login('a')
        before=self.counts()
        search=self.client.get('/kb',params={'q':'找不到的关键词 发货+时间&问题'})
        self.assertEqual(search.status_code,200)
        self.assertIn('搜索不会提交新问题',search.text)
        links=[a['href'] for tag,a in Elements(search.text).elements if tag=='a' and 'draft=' in a.get('href','')]
        self.assertEqual(len(links),1)
        self.assertEqual(parse_qs(urlsplit(links[0]).query)['draft'],['找不到的关键词 发货+时间&问题'])
        home=self.client.get(links[0])
        self.assertIn('找不到的关键词 发货+时间&amp;问题</textarea>',home.text)
        self.assertEqual(self.counts(),before)

    def test_prefill_escapes_markup_and_limits_length(self):
        self.login('a')
        text='</textarea><script>alert(1)</script>'
        html=self.client.get('/',params={'draft':text}).text
        self.assertNotIn(text,html)
        self.assertIn('&lt;/textarea&gt;',html)
        html=self.client.get('/',params={'draft':'x'*2100}).text
        self.assertIn('x'*2000+'</textarea>',html)
        self.assertNotIn('x'*2001,html)

    def test_reference_links_preserve_current_form_by_opening_new_page(self):
        self.login('a')
        elements=Elements(self.client.get('/').text).elements
        links=[a for tag,a in elements if tag=='a' and a.get('href')=='/kb' and a.get('target')=='_blank']
        self.assertEqual(len(links),1)
        self.assertIn('noopener',links[0]['rel'])
        file_input=next(a for tag,a in elements if tag=='input' and a.get('type')=='file')
        self.assertEqual(file_input['id'],'images')

    def test_submit_keeps_original_archive_and_attachment_flow(self):
        self.login('a')
        png=b'\x89PNG\r\n\x1a\n'+b'fixture'
        response=self.client.post('/ask',data={'text':'布局验证的真实提问入口'},files={'images':('shot.png',png,'image/png')})
        self.assertIn('/q/',str(response.url))
        self.assertIn('布局验证的真实提问入口',response.text)
        self.assertIn('1.png',response.text)
        self.assertEqual(self.counts()[0],1)

    def test_worker_state_is_still_visible(self):
        self.login('a')
        self.assertIn('作答电脑暂时离线',self.client.get('/').text)
        with store.db() as c:
            c.execute('INSERT INTO workers VALUES(?,?)',('fixture-worker',time.time()))
        self.assertNotIn('作答电脑暂时离线',self.client.get('/').text)


if __name__=='__main__':
    unittest.main(verbosity=2)
