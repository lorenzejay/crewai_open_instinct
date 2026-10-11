-- Run only against the isolated test database. Roll back all fixtures.
begin;
insert into auth.users(id) values
 ('11111111-1111-1111-1111-111111111111'),('22222222-2222-2222-2222-222222222222');
do $$
declare a uuid; b uuid; j public.ci_jobs; ctx jsonb; count_jobs integer;
begin
  a:=public.ci_create_link('11111111-1111-1111-1111-111111111111','hash-a');
  b:=public.ci_create_link('22222222-2222-2222-2222-222222222222','hash-b');
  assert public.ci_receive('+15555550100','+15555550101','link-a','','','hash-a')='linked';
  assert public.ci_receive('+15555550100','+15555550102','link-b','','','hash-b')='linked';
  assert public.ci_receive('+15555550100','+15555550103','reuse','','','hash-a')='ignored';
  assert public.ci_receive('+15555550100','+15555550103','unknown','hello','',null)='ignored';
  assert public.ci_receive('+15555550100','+15555550101','message-a','remember coffee','',null)='queued';
  assert public.ci_receive('+15555550100','+15555550101','message-a','remember coffee','',null)='duplicate';
  assert public.ci_receive('+15555550100','+15555550101','message-a2','what do you remember?','',null)='queued';
  assert public.ci_receive('+15555550100','+15555550102','message-b','hello','',null)='queued';
  select * into j from public.ci_claim();
  assert j.workspace_id=a and j.status='running';
  assert not public.ci_save_memory(j.id,gen_random_uuid(),'secret','wrong lease');
  assert public.ci_save_memory(j.id,j.lease_token,'drink','coffee');
  assert public.ci_reply(j.id,j.lease_token,'Saved your preference.');
  select * into j from public.ci_claim();
  assert j.workspace_id=a and j.status='sending';
  assert public.ci_complete(j.id,j.lease_token,'reply-a');
  select * into j from public.ci_claim();
  assert j.workspace_id=a;
  ctx:=public.ci_context(j.id,j.lease_token);
  assert jsonb_array_length(ctx->'history')=2, 'prior assistant reply is included even when later inbound arrived first';
  assert ctx->'history'->0->>'direction'='user';
  assert ctx->'history'->1->>'direction'='assistant';
  assert ctx->'memories'->0->>'content'='coffee';
  select * into j from public.ci_claim();
  assert j.workspace_id=b, 'another tenant can progress while first tenant is busy';
  ctx:=public.ci_context(j.id,j.lease_token);
  assert jsonb_array_length(ctx->'history')=0, 'tenant B sees no tenant A history';
  assert jsonb_array_length(ctx->'memories')=0, 'tenant B sees no tenant A memory';
end $$;
-- RLS: simulate user B. It cannot read user A's saved note or hashes/jobs.
set local role authenticated;
set local request.jwt.claim.sub='22222222-2222-2222-2222-222222222222';
do $$
begin
  assert (select count(*) from public.ci_memories)=0;
  assert (select count(*) from public.ci_workspaces)=1;
  assert not has_table_privilege(current_user,'public.ci_jobs','select');
  assert not has_function_privilege(current_user,'public.ci_receive(text,text,text,text,text,text)','execute');
end $$;
reset role;
rollback;
