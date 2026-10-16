"""Run/resume a real lab demo and export review/audit evidence without secrets.

Start: python -m scripts.run_demo --pr 1
Resume: python -m scripts.run_demo --pr 1 --response evidence/response-pr1.json
"""
import argparse
import asyncio
import json
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command
from common.db import db_path, replay_events
from exercises.exercise_4_audit import build_graph


def dump(value):
    if hasattr(value, 'model_dump'):
        return value.model_dump(mode='json')
    raise TypeError(type(value).__name__)


async def run(number, response_path):
    thread = f'lab-demo-20261016-pr{number}'
    url = f'https://github.com/VinUni-AI20k/PR-Demo/pull/{number}'
    headers = {'Authorization': f"Bearer {os.environ['GITHUB_TOKEN']}",
               'Accept': 'application/vnd.github+json'}
    async with httpx.AsyncClient(timeout=30) as client:
        user = await client.get('https://api.github.com/user', headers=headers)
        user.raise_for_status()
        os.environ['GITHUB_USER'] = user.json()['login']
    cfg = {'configurable': {'thread_id': thread}}
    async with AsyncSqliteSaver.from_conn_string(db_path()) as cp:
        await cp.setup()
        app = build_graph(cp)
        snapshot = await app.aget_state(cfg)
        if response_path:
            if not snapshot.next:
                raise ValueError('Session is already finished or has not started; refusing duplicate submission')
            response = json.loads(Path(response_path).read_text())
            result = await app.ainvoke(Command(resume=response), cfg)
        elif snapshot.values:
            result = dict(snapshot.values)
            pending = [i for task in snapshot.tasks for i in task.interrupts]
            if pending:
                result['__interrupt__'] = pending
            elif snapshot.next:
                result = await app.ainvoke(None, cfg)
        else:
            result = await app.ainvoke({'pr_url': url, 'thread_id': thread}, cfg)
    pending = result.pop('__interrupt__', None)
    record = {'thread_id': thread, 'pr_url': url, 'reviewer_id': os.environ['GITHUB_USER'],
              'state': result, 'interrupt': pending[0].value if pending else None,
              'events': await replay_events(thread)}
    if result.get('final_action') in {'committed', 'auto_committed'}:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(f'https://api.github.com/repos/VinUni-AI20k/PR-Demo/issues/{number}/comments',
                                        headers=headers, params={'per_page': 100, 'since': record['events'][0]['timestamp']})
            response.raise_for_status()
            from exercises.exercise_4_audit import _render_comment_body
            body = _render_comment_body(result)
            matches = [c for c in response.json() if c['user']['login'] == os.environ['GITHUB_USER'] and c['body'] == body]
            record['github_comments'] = [{'id':c['id'], 'html_url':c['html_url'], 'created_at':c['created_at']} for c in matches]
            if not matches:
                raise RuntimeError('Posted comment could not be independently verified')
    out = Path('evidence') / f'pr{number}-result.json'
    serialized = json.dumps(record, default=dump, ensure_ascii=False, indent=2) + '\n'
    out.write_text(serialized)
    stage = record['interrupt']['kind'] if record['interrupt'] else result.get('final_action', 'checkpoint')
    (Path('evidence') / f'pr{number}-{stage}.json').write_text(serialized)
    print(json.dumps({'thread_id':thread, 'final_action':result.get('final_action'),
                      'confidence':result['analysis'].confidence,
                      'interrupt': record['interrupt'], 'github_comments':record.get('github_comments'),
                      'evidence':str(out)}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    load_dotenv()
    parser=argparse.ArgumentParser()
    parser.add_argument('--pr',type=int,choices=[1,2],required=True)
    parser.add_argument('--response')
    args=parser.parse_args()
    asyncio.run(run(args.pr,args.response))
