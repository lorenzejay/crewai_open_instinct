-- Run only against an isolated database after all migrations.
begin;
do $$
declare a uuid; b uuid; j public.ci_jobs; ctx jsonb; n bigint;
begin
  select count(*) into n from public.ci_workspaces;
  assert public.ci_receive_text('+15555550100','+15555550201','first-a','remember coffee','')='queued';
  select workspace_id into a from public.ci_phone_bindings where phone='+15555550201';
  assert a is not null, 'first text provisions a workspace';
  assert (select owner_user_id is null from public.ci_workspaces where id=a), 'no auth account needed';
  assert (select count(*) from public.ci_workspaces)=n+1;
  assert public.ci_receive_text('+15555550100','+15555550201','first-a','remember coffee','')='duplicate';
  assert (select count(*) from public.ci_jobs where workspace_id=a)=1, 'retry does not duplicate the first reply';
  assert public.ci_receive_text('+15555550100','+15555550201','second-a','what do you remember?','')='queued';
  assert (select count(*) from public.ci_workspaces)=n+1, 'same phone keeps one workspace';
  assert public.ci_receive_text('+15555550100','+15555550202','first-b','hello','')='queued';
  select workspace_id into b from public.ci_phone_bindings where phone='+15555550202';
  assert b<>a, 'different phones have different tenants';
  assert public.ci_receive_text('+15555550100','+15555550203','empty','  ','')='ignored';
  assert not exists(select 1 from public.ci_phone_bindings where phone='+15555550203');
  select * into j from public.ci_claim();
  assert j.workspace_id=a;
  assert public.ci_save_memory(j.id,j.lease_token,'drink','coffee');
  assert public.ci_reply(j.id,j.lease_token,'Saved your preference.');
  select * into j from public.ci_claim();
  assert j.workspace_id=a and j.status='sending';
  assert public.ci_complete(j.id,j.lease_token,'reply-text-a');
  select * into j from public.ci_claim();
  assert j.workspace_id=a;
  ctx:=public.ci_context(j.id,j.lease_token);
  assert jsonb_array_length(ctx->'history')=2;
  assert ctx->'memories'->0->>'content'='coffee';
  select * into j from public.ci_claim();
  assert j.workspace_id=b;
  ctx:=public.ci_context(j.id,j.lease_token);
  assert jsonb_array_length(ctx->'history')=0;
  assert jsonb_array_length(ctx->'memories')=0, 'another phone cannot see saved notes';
end $$;
set local role authenticated;
set local request.jwt.claim.sub='22222222-2222-2222-2222-222222222222';
do $$
begin
  assert (select count(*) from public.ci_workspaces)=0, 'a web account cannot read phone-only tenants';
  assert (select count(*) from public.ci_memories)=0;
  assert not has_function_privilege(current_user,'public.ci_receive_text(text,text,text,text,text,text)','execute');
end $$;
reset role;
rollback;
