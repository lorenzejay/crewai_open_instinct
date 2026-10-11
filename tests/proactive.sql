-- Isolated PostgreSQL only. All fixtures roll back.
begin;
do $$
declare j public.ci_jobs; p public.ci_jobs; control public.ci_jobs; a uuid; w uuid;
  due timestamptz; denied boolean; ctx jsonb; count_before integer;
begin
  perform public.ci_receive_text('+15555550100','+15555550401','proactive-user','remind me','');
  select * into j from public.ci_claim();
  w:=j.workspace_id;
  ctx:=public.ci_put_automation(j.id,j.lease_token,'Call Alex','reminder','Call Alex','UTC',
    '{"type":"once","at":"2099-01-01T00:00:00Z"}','{}',now()+interval '1 hour');
  select id into a from public.ci_automations where workspace_id=w and name='Call Alex';
  assert a is not null;
  denied:=false;
  begin perform public.ci_put_automation(j.id,gen_random_uuid(),'stolen','reminder','stolen','UTC','{}','{}',now()+interval '1 hour');
  exception when others then denied:=true; end;
  assert denied, 'wrong lease cannot create schedules';
  update public.ci_automations set next_run_at=now()-interval '1 minute' where id=a returning next_run_at into due;
  assert public.ci_dispatch_automation(a,due,null);
  assert not public.ci_dispatch_automation(a,due,null), 'duplicate scheduler ticks are idempotent';
  assert (select count(*) from public.ci_jobs where automation_id=a)=1;
  assert public.ci_reply(j.id,j.lease_token,'Scheduled');
  select * into j from public.ci_claim();
  assert public.ci_complete(j.id,j.lease_token,'scheduled-ack');
  select * into p from public.ci_claim();
  assert p.kind='proactive' and p.message_id is null;
  ctx:=public.ci_context(p.id,p.lease_token);
  assert ctx->>'content'='' and ctx->>'kind'='proactive';
  assert jsonb_array_length(ctx->'history')=2, 'proactive job reads real conversation history';
  denied:=false;
  begin perform public.ci_manage_automations(p.id,p.lease_token,'pause_all');
  exception when others then denied:=true; end;
  assert denied, 'background jobs cannot create or change user notification permissions';
  assert public.ci_allow_proactive(p.id,p.lease_token);
  assert public.ci_finish_proactive(p.id,p.lease_token,'Reminder: Call Alex');
  select * into p from public.ci_claim();
  assert p.status='sending';
  assert public.ci_allow_proactive(p.id,p.lease_token);
  assert public.ci_complete(p.id,p.lease_token,'reminder-1');
  assert (select sent_at is not null from public.ci_jobs where id=p.id);
  assert (select count(*) from public.ci_messages where direction='user' and workspace_id=w)=1,
    'scheduler never fabricates a user message';

  perform public.ci_receive_text('+15555550100','+15555550401','proactive-control','pause reminders','');
  select * into control from public.ci_claim();
  -- Quiet hours defer before generation and do not consume a generation retry.
  perform public.ci_put_automation(control.id,control.lease_token,'Quiet check','reminder','Quiet','UTC',
    '{"type":"daily","time":"09:00"}','{}',now()+interval '1 hour');
  select id into a from public.ci_automations where name='Quiet check' and workspace_id=w;
  update public.ci_automations set next_run_at=now()-interval '1 minute' where id=a returning next_run_at into due;
  assert public.ci_dispatch_automation(a,due,now()+interval '1 day');
  -- Inject a valid active lease to exercise the gate while control remains running.
  update public.ci_jobs set status='running',lease_token=gen_random_uuid(),leased_until=now()+interval '5 minutes',attempts=1
    where automation_id=a returning * into p;
  perform public.ci_manage_automations(control.id,control.lease_token,'settings','','UTC',
    extract(hour from now() at time zone 'UTC')::integer,
    (extract(hour from now() at time zone 'UTC')::integer+1)%24,10);
  assert not public.ci_allow_proactive(p.id,p.lease_token);
  assert (select status='queued' and not_before>now() and attempts=0 from public.ci_jobs where id=p.id);
  perform public.ci_manage_automations(control.id,control.lease_token,'cancel','Quiet check');
  perform public.ci_manage_automations(control.id,control.lease_token,'settings','','UTC',-1,-1,10);
  -- Read-only account event scoping and retry deduplication.
  perform public.ci_bind_watch(control.id,control.lease_token,'Alex mail','Only notify for Alex',
    'ti_test','ca_test',w::text,'GMAIL_NEW_GMAIL_MESSAGE','{}');
  assert public.ci_account_event('ev-1','ti_test','ca_wrong',w::text,'GMAIL_NEW_GMAIL_MESSAGE','{}')='ignored';
  assert public.ci_account_event('ev-1','ti_test','ca_test','other-owner','GMAIL_NEW_GMAIL_MESSAGE','{}')='ignored';
  assert public.ci_account_event('ev-1','ti_test','ca_test',w::text,'GMAIL_NEW_GMAIL_MESSAGE','{"subject":"Hello"}')='queued';
  assert public.ci_account_event('ev-1','ti_test','ca_test',w::text,'GMAIL_NEW_GMAIL_MESSAGE','{"subject":"Hello"}')='duplicate';
  select * into p from public.ci_jobs where input->>'subject'='Hello';
  -- Pausing is reversible, and quiet/paused jobs do not block inbound chat.
  perform public.ci_manage_automations(control.id,control.lease_token,'pause_all');
  assert (select status='queued' from public.ci_jobs where id=p.id);
  assert public.ci_account_event('ev-paused','ti_test','ca_test',w::text,'GMAIL_NEW_GMAIL_MESSAGE','{}')='ignored';
  assert public.ci_reply(control.id,control.lease_token,'Paused');
  select * into control from public.ci_claim();
  assert public.ci_complete(control.id,control.lease_token,'pause-ack');
  perform public.ci_receive_text('+15555550100','+15555550401','proactive-resume','resume','');
  select * into control from public.ci_claim();
  assert control.kind='user', 'paused proactive work cannot block the next text';
  perform public.ci_manage_automations(control.id,control.lease_token,'resume_all');
  perform public.ci_manage_automations(control.id,control.lease_token,'settings','','UTC',-1,-1,1);
  assert public.ci_reply(control.id,control.lease_token,'Resumed');
  select * into p from public.ci_claim();
  assert p.kind='proactive';
  assert public.ci_finish_proactive(p.id,p.lease_token,'Email alert');
  select * into p from public.ci_claim();
  assert not public.ci_allow_proactive(p.id,p.lease_token), 'daily cap defers the second notification';
  assert (select status='ready' and not_before>now() from public.ci_jobs where id=p.id);
  select * into control from public.ci_claim();
  assert control.kind='user' and control.status='sending', 'deferred notification cannot block a chat reply';
  assert public.ci_complete(control.id,control.lease_token,'resume-ack');

  perform public.ci_receive_text('+15555550100','+15555550401','proactive-cancel','cancel watch','');
  select * into control from public.ci_claim();
  perform public.ci_manage_automations(control.id,control.lease_token,'cancel','Alex mail');
  assert (select status='failed' from public.ci_jobs where id=p.id), 'cancel removes unsent outbox work';
  assert public.ci_account_event('ev-after','ti_test','ca_test',w::text,'GMAIL_NEW_GMAIL_MESSAGE','{}')='ignored';
end $$;
set local role authenticated;
do $$ begin
  assert not has_table_privilege(current_user,'public.ci_automations','select');
  assert not has_table_privilege(current_user,'public.ci_notification_settings','select');
  assert not has_function_privilege(current_user,'public.ci_due_automations()','execute');
  assert not has_function_privilege(current_user,'public.ci_account_event(text,text,text,text,text,jsonb)','execute');
end $$;
reset role;
rollback;
