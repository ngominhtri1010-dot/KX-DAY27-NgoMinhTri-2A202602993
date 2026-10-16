"""Offline checks: no provider requests and no GitHub writes."""
import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command
from common.schemas import PRAnalysis
from common.db import replay_events
from exercises import exercise_1_confidence as ex1
from exercises import exercise_2_hitl as ex2
from exercises import exercise_3_escalation as ex3
from exercises import exercise_4_audit as ex4
from exercises import review_llm

URL = 'https://github.com/example/demo/pull/1'
PR = SimpleNamespace(title='Test', diff='diff --git a/a.py b/a.py\n+safe()', files_changed=['a.py'], head_sha='abc1234')

def analysis(c):
    return PRAnalysis(summary='Review', confidence=c, confidence_reasoning='Test reasoning',
                      escalation_questions=['a.py: intended behavior?'] if c < .58 else [])

class RoutingTests(unittest.TestCase):
    def test_boundaries(self):
        for c, expected in [(0, 'escalate'), (.579, 'escalate'), (.58, 'human_approval'),
                            (.72, 'human_approval'), (.73, 'auto_approve'), (1, 'auto_approve')]:
            self.assertEqual(ex1.node_route({'analysis': analysis(c)})['decision'], expected)

    def test_early_exercises(self):
        for module in (ex1, ex2, ex3):
            with patch.object(module, 'fetch_pr', return_value=PR), patch.object(module, 'analyze', return_value=analysis(.65)) as llm:
                graph = module.build_graph()
                cfg = {'configurable': {'thread_id': 'offline'}}
                result = graph.invoke({'pr_url': URL}, cfg)
                if module != ex1:
                    self.assertIn('__interrupt__', result)
                    with patch.object(module, 'post_review_comment') as post:
                        result = graph.invoke(Command(resume={'choice': 'reject'}), cfg)
                        post.assert_not_called()
                    self.assertEqual(result['final_action'], 'rejected')
                llm.assert_called_once()

class DurableTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'HITL_DB_PATH': str(Path(self.tmp.name)/'audit.db'), 'GITHUB_USER':'tester'})
        self.env.start()

    async def asyncTearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    async def invoke(self, value, thread='test'):
        async with AsyncSqliteSaver.from_conn_string(os.environ['HITL_DB_PATH']) as cp:
            await cp.setup()
            return await ex4.build_graph(cp).ainvoke(value, {'configurable': {'thread_id':thread}})

    async def test_resume_reopens_db_without_another_analysis(self):
        with patch.object(ex4, 'fetch_pr', return_value=PR), patch.object(ex4, 'analyze_async', new_callable=AsyncMock, return_value=analysis(.65)) as llm, patch.object(ex4, 'post_review_comment') as post:
            first = await self.invoke({'pr_url':URL, 'thread_id':'test'})
            self.assertEqual(first['__interrupt__'][0].value['kind'], 'approval_request')
            post.assert_not_called()
            final = await self.invoke(Command(resume={'choice':'approve'}))
            self.assertEqual(final['final_action'], 'committed')
            post.assert_called_once()
            llm.assert_awaited_once()
        events = await replay_events('test')
        self.assertEqual(sum(e['action']=='human_approval' and e['decision']=='pending' for e in events), 1)
        self.assertTrue(any(e['reviewer_id']=='tester' and e['decision']=='approve' for e in events))

    async def test_escalation_requires_final_confirmation(self):
        with patch.object(ex4, 'fetch_pr', return_value=PR), patch.object(ex4, 'analyze_async', new_callable=AsyncMock, side_effect=[analysis(.4),analysis(.8)]) as llm, patch.object(ex4, 'post_review_comment') as post:
            first = await self.invoke({'pr_url':URL, 'thread_id':'test'})
            self.assertEqual(first['__interrupt__'][0].value['kind'], 'escalation')
            second = await self.invoke(Command(resume={'a.py: intended behavior?':'Context'}))
            self.assertEqual(second['__interrupt__'][0].value['kind'], 'approval_request')
            post.assert_not_called()
            final = await self.invoke(Command(resume={'choice':'reject'}))
            self.assertEqual(final['final_action'], 'rejected')
            self.assertEqual(llm.await_count, 2)
            post.assert_not_called()
        events = await replay_events('test')
        self.assertEqual(sum(e['action']=='escalate' and e['decision']=='pending' for e in events), 1)

    async def test_auto_and_edit_and_post_failure(self):
        for thread, c, response, expected in [('auto',.9,None,'auto_committed'), ('edit',.65,{'choice':'edit','feedback':'Replacement review'},'committed'), ('failure',.9,None,'auto_commit_failed')]:
            with patch.object(ex4, 'fetch_pr', return_value=PR), patch.object(ex4, 'analyze_async', new_callable=AsyncMock, return_value=analysis(c)), patch.object(ex4, 'post_review_comment', side_effect=RuntimeError('offline failure') if thread=='failure' else None) as post:
                result = await self.invoke({'pr_url':URL,'thread_id':thread},thread)
                if response:
                    result = await self.invoke(Command(resume=response),thread)
                    self.assertEqual(post.call_args.args[1], 'Replacement review')
                self.assertEqual(result['final_action'],expected)
                post.assert_called_once()

class CacheTests(unittest.TestCase):
    def test_cache_and_invalidation(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'LLM_CACHE_PATH':str(Path(tmp)/'cache.db'), 'OPENROUTER_API_KEY':'offline'}), patch.object(review_llm, 'ChatOpenAI') as factory:
            invoke = factory.return_value.with_structured_output.return_value.invoke
            invoke.return_value = analysis(.65)
            state={'pr_title':'Test','pr_diff':'one'}
            review_llm.analyze(state)
            review_llm.analyze(state)
            self.assertEqual(invoke.call_count,1)
            review_llm.analyze({**state,'pr_diff':'two'})
            self.assertEqual(invoke.call_count,2)
            refined={**state,'analysis':analysis(.65),'escalation_answers':{'q':'answer'}}
            review_llm.analyze(refined,refine=True)
            review_llm.analyze(refined,refine=True)
            self.assertEqual(invoke.call_count,3)
            review_llm.analyze({**refined,'escalation_answers':{'q':'new'}},refine=True)
            self.assertEqual(invoke.call_count,4)
            self.assertEqual(factory.call_args.kwargs['max_retries'],0)

    def test_failed_requests_are_not_cached(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'LLM_CACHE_PATH':str(Path(tmp)/'cache.db'), 'OPENROUTER_API_KEY':'offline'}), patch.object(review_llm, 'ChatOpenAI') as factory:
            invoke=factory.return_value.with_structured_output.return_value.invoke
            invoke.side_effect=[RuntimeError('Rate limit'),analysis(.65)]
            with self.assertRaises(RuntimeError):
                review_llm.analyze({'pr_diff':'one'})
            review_llm.analyze({'pr_diff':'one'})
            self.assertEqual(invoke.call_count,2)


class UITests(unittest.TestCase):
    def test_approval_and_restore_without_new_llm_calls(self):
        from streamlit.testing.v1 import AppTest
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'HITL_DB_PATH':str(Path(tmp)/'ui.db')}), patch.object(ex4,'fetch_pr',return_value=PR), patch.object(ex4,'analyze_async',new_callable=AsyncMock,return_value=analysis(.65)) as llm, patch.object(ex4,'post_review_comment') as post:
            app=AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'app.py')).run(timeout=15)
            self.assertFalse(app.exception)
            app.text_input[0].set_value(URL)
            next(b for b in app.button if b.label=='Run review').click().run(timeout=15)
            self.assertFalse(app.exception)
            self.assertTrue(any('Approval requested' in h.value for h in app.subheader))
            thread=app.session_state.thread_id
            # A new browser session restores the pending card from SQLite.
            restored=AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'app.py'))
            restored.query_params['thread']=thread
            restored.run(timeout=15)
            self.assertFalse(restored.exception)
            self.assertTrue(any('Approval requested' in h.value for h in restored.subheader))
            next(b for b in restored.button if b.label=='Reject').click().run(timeout=15)
            self.assertFalse(restored.exception)
            self.assertEqual(restored.session_state.final['final_action'],'rejected')
            llm.assert_awaited_once()
            post.assert_not_called()

    def test_escalation_form_and_manual_edit(self):
        from streamlit.testing.v1 import AppTest
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'HITL_DB_PATH':str(Path(tmp)/'ui.db')}), patch.object(ex4,'fetch_pr',return_value=PR), patch.object(ex4,'analyze_async',new_callable=AsyncMock,side_effect=[analysis(.4),analysis(.8)]) as llm, patch.object(ex4,'post_review_comment') as post:
            app=AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'app.py')).run(timeout=15)
            app.text_input[0].set_value(URL)
            next(b for b in app.button if b.label=='Run review').click().run(timeout=15)
            self.assertTrue(any('Strong escalation' in h.value for h in app.subheader))
            app.text_area[0].set_value('Human context')
            next(b for b in app.button if b.label=='Submit answers').click().run(timeout=15)
            self.assertFalse(app.exception)
            post.assert_not_called()
            self.assertTrue(any('Approval requested' in h.value for h in app.subheader))
            app.text_area[0].set_value('My corrected review')
            next(b for b in app.button if b.label=='Edit').click().run(timeout=15)
            self.assertFalse(app.exception)
            self.assertEqual(post.call_args.args[1],'My corrected review')
            self.assertEqual(llm.await_count,2)

if __name__ == '__main__':
    unittest.main()
