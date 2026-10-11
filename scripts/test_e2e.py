"""Real-model E2E against disposable local SQL; captures all outbound messages.

Requires an already migrated empty test database. Never connects to Supabase,
Sendblue, or account MCP. Browserbase is optional and reads example.com only.
"""
import argparse, contextlib, json, os, re, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live-model', action='store_true', required=True,
                        help='Authorize paid model calls with the configured provider key')
    parser.add_argument('--browser', action='store_true', help='Also test a real Browserbase session')
    args = parser.parse_args()
    url = os.environ['TEST_DATABASE_URL']
    parsed = urlsplit(url)
    if parsed.hostname not in {'127.0.0.1', 'localhost'} or not re.search(r'(test|e2e)', parsed.path, re.I):
        raise SystemExit('Use a disposable localhost database whose name contains test or e2e')
    import psycopg
    from psycopg import sql
    from psycopg.types.json import Jsonb
    from fastapi.testclient import TestClient
    from crewai_open_instinct.config import load_environment
    load_environment()
    # Synthetic tenants can never use real messaging or account credentials.
    os.environ.update(SENDBLUE_PHONE_NUMBER='+15555550100',
                      SENDBLUE_WEBHOOK_SECRET='isolated-e2e-secret',
                      SENDBLUE_API_KEY='', SENDBLUE_API_SECRET='',
                      SUPABASE_URL='https://isolated.invalid', SUPABASE_SECRET_KEY='',
                      COMPOSIO_API_KEY='', COMPOSIO_WEBHOOK_SECRET='')
    from crewai_open_instinct import api, main, worker
    browser_calls = []
    real_read_page = main.read_page
    def tracked_read_page(url):
        browser_calls.append(url)
        return real_read_page(url)
    from crewai_open_instinct.schedules import scheduler_tick

    class SqlDB:
        def __init__(self):
            self.conn = psycopg.connect(url, autocommit=True)
        def close(self):
            self.conn.close()
        def rpc(self, name, **params):
            if not re.fullmatch(r'ci_[a-z_]+', name):
                raise ValueError('Unexpected RPC')
            pairs = sql.SQL(',').join(sql.SQL('{} => %s').format(sql.Identifier(k)) for k in params)
            call = sql.SQL('public.{}({})').format(sql.Identifier(name), pairs)
            query = (sql.SQL('SELECT row_to_json(v) FROM {} v').format(call) if name == 'ci_claim'
                     else sql.SQL('SELECT {}').format(call))
            values = [Jsonb(v) if isinstance(v, (dict, list)) else v for v in params.values()]
            rows = self.conn.execute(query, values).fetchall()
            return [r[0] for r in rows] if name == 'ci_claim' else rows[0][0]

    class CaptureDelivery:
        def __init__(self):
            self.messages, self.signals = [], []
        def send(self, **data):
            handle = 'isolated-out-' + str(uuid4())
            self.messages.append(dict(data, handle=handle))
            return handle
        def mark_read(self, **data):
            self.signals.append('read')
        def typing(self, **data):
            self.signals.append('typing-' + data['state'])

    db, delivery = SqlDB(), CaptureDelivery()
    if db.conn.execute('SELECT count(*) FROM public.ci_workspaces').fetchone()[0]:
        db.close()
        raise SystemExit('Refusing to run against a database containing tenant data; create a fresh test database')
    app = api.create_app(); app.state.db = db
    http = TestClient(app)
    passed = []
    def check(condition, label):
        if not condition:
            raise AssertionError(label)
        passed.append(label)
        print('PASS ' + label, flush=True)
    def inbound(content, sender='+15555550101', handle=None, authorized=True):
        event = dict(message_handle=handle or 'isolated-in-' + str(uuid4()), is_outbound=False,
                     from_number=sender, number=sender, sendblue_number='+15555550100', content=content)
        headers = {'sb-signing-secret':'isolated-e2e-secret'} if authorized else {}
        return http.post('/webhooks/sendblue', json=event, headers=headers)
    def drain():
        start = len(delivery.messages)
        # CrewAI output is intentionally kept out of the public test report.
        with tempfile.TemporaryFile(mode='w+') as private, contextlib.redirect_stdout(private), contextlib.redirect_stderr(private):
            for _ in range(40):
                jobs = db.rpc('ci_claim')
                if not jobs: break
                worker.process_job(db, delivery, jobs[0])
            else: raise AssertionError('Queue failed to drain')
        errors = db.conn.execute("SELECT count(*) FROM ci_jobs WHERE status IN ('failed','uncertain') AND coalesce(error_code,'') != 'coalesced_turn'").fetchone()[0]
        if errors: raise AssertionError('Unexpected failed or uncertain isolated job')
        if any("couldn't finish processing" in m['content'] for m in delivery.messages[start:]):
            raise AssertionError('Worker produced an error fallback')
        return delivery.messages[start:]
    def turn(content, sender='+15555550101'):
        result = inbound(content, sender)
        assert result.status_code == 200 and result.json()['status'] == 'queued'
        messages = drain()
        assert len(messages) == 1
        return messages[0]['content']

    try:
        with patch.object(main, 'Supabase', SqlDB), patch.object(main, 'account_mcps', lambda *a, **k: []), patch.object(main, 'read_page', tracked_read_page):
            check(inbound('Hello', authorized=False).status_code == 401 and
                  db.conn.execute('SELECT count(*) FROM ci_workspaces').fetchone()[0] == 0,
                  'Unsigned webhook rejected before tenant creation')
            handle = 'dedup-' + str(uuid4())
            check(inbound('Remember a note named e2e_preference: my preferred meal is vegetarian.', handle=handle).json()['status']=='queued',
                  'Signed webhook provisions text-first tenant and queues a turn')
            check(inbound('Remember a note named e2e_preference: my preferred meal is vegetarian.', handle=handle).json()['status']=='duplicate',
                  'Repeated provider handle is deduplicated')
            messages = drain()
            check(len(messages)==1 and db.conn.execute("SELECT count(*) FROM ci_memories WHERE name='e2e_preference' AND content ILIKE '%vegetarian%'").fetchone()[0]==1,
                  'Live model calls remember_note and saves one durable note')
            check(all(s in delivery.signals for s in ('read','typing-start','typing-stop')),
                  'Worker invokes read receipt and typing start/stop')
            check('vegetarian' in turn('What is my saved meal preference?').lower(),
                  'Live model recalls the persisted preference')
            other = turn('What meal preference do you have saved for me?', '+15555550102')
            check('vegetarian' not in other.lower() and db.conn.execute('SELECT count(*) FROM ci_workspaces').fetchone()[0]==2,
                  'Second sender gets an isolated tenant without first sender notes')
            turn('Delete the saved note named e2e_preference using forget_note.')
            check(db.conn.execute("SELECT count(*) FROM ci_memories WHERE name='e2e_preference'").fetchone()[0]==0,
                  'Live model calls forget_note and removes the persisted note')
            reply = turn('My timezone is America/Los_Angeles. Remind me in 5 minutes to review the E2E results.')
            rule = db.conn.execute('SELECT id,timezone,next_run_at,conversation_id FROM ci_automations').fetchone()
            received = db.conn.execute('SELECT created_at FROM ci_messages WHERE direction=\'user\' ORDER BY created_at DESC LIMIT 1').fetchone()[0]
            check(rule is not None and rule[1]=='America/Los_Angeles' and abs((rule[2]-received).total_seconds()-300)<1 and 'Saved' in reply,
                  'Relative reminder saves exact receipt-plus-five-minutes deadline and IANA timezone')
            db.close(); db=SqlDB(); app.state.db=db
            check(db.conn.execute('SELECT next_run_at FROM ci_automations WHERE id=%s',(rule[0],)).fetchone()[0]==rule[2],
                  'Schedule survives closing and reopening database clients')
            # Accelerate only the disposable database clock target, never the live phone test.
            past = datetime.now(timezone.utc)-timedelta(seconds=2)
            db.conn.execute('UPDATE ci_automations SET next_run_at=%s,schedule=%s WHERE id=%s',
                            (past,Jsonb({'type':'once','at':past.isoformat()}),rule[0]))
            check(scheduler_tick(db)==1 and scheduler_tick(db)==0,
                  'Scheduler dispatches a due reminder atomically exactly once')
            messages=drain()
            check(len(messages)==1 and 'e2e results' in messages[0]['content'].lower() and
                  db.conn.execute("SELECT count(*) FROM ci_jobs WHERE kind='proactive' AND status='sent'").fetchone()[0]==1,
                  'Proactive worker produces one notification through the persisted outbox')
            inbound('My timezone is America/Los_Angeles. In 5 minutes brief me on PRs I should review from this repository:')
            inbound('https://github.com/crewAIInc/crewAI')
            messages=drain()
            check(len(messages)==1 and db.conn.execute("SELECT count(*) FROM ci_automations WHERE source->>'github_repo'='crewAIInc/crewAI'").fetchone()[0]==1 and
                  db.conn.execute("SELECT count(*) FROM ci_jobs WHERE error_code='coalesced_turn'").fetchone()[0]==1,
                  'Burst request plus separately sent GitHub URL becomes one briefing and acknowledgment')
            if args.browser:
                reply=turn('Read https://example.com using read_public_page and give me the page title and a one-sentence summary. Do not use prior knowledge.')
                check(browser_calls == ['https://example.com'] and 'example domain' in reply.lower() and 'browser research failed' not in reply.lower(),
                      'Conversation researches a public page through a real Browserbase session')
            from crewai_open_instinct.browser import public_url
            blocked=False
            try: public_url('http://127.0.0.1/')
            except ValueError: blocked=True
            check(blocked,'Private network browser URL rejected')
        print(json.dumps({'result':'passed','checks':len(passed),'model':os.getenv('LLM_MODEL'),'outbound_transport':'capture-only','browserbase':args.browser}),flush=True)
    finally:
        http.close();db.close()

if __name__=='__main__':
    run()
