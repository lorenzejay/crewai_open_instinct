-- Isolated database only; all fixtures roll back.
begin;
do $$
declare a uuid; b uuid; j public.ci_jobs; k public.ci_jobs; ctx jsonb; denied boolean;
begin
  perform public.ci_receive_text('+15555550100','+15555550301','mcp-a','hello','');
  perform public.ci_receive_text('+15555550100','+15555550302','mcp-b','hello','');
  select workspace_id into a from public.ci_phone_bindings where phone='+15555550301';
  select workspace_id into b from public.ci_phone_bindings where phone='+15555550302';
  insert into public.ci_account_identities values(a,'existing-pilot-owner');
  select * into j from public.ci_claim();
  assert j.workspace_id=a;
  ctx:=public.ci_account_session(j.id,j.lease_token);
  assert ctx->>'user_id'='existing-pilot-owner' and ctx->>'session_id' is null;
  ctx:=public.ci_account_session(j.id,j.lease_token,'session-a');
  assert ctx->>'session_id'='session-a';
  assert public.ci_account_session(j.id,j.lease_token,'losing-session')->>'session_id'='session-a';
  denied:=false;
  begin perform public.ci_account_session(j.id,gen_random_uuid(),'stolen');
  exception when others then denied:=true; end;
  assert denied, 'wrong lease cannot read or write sessions';
  select * into k from public.ci_claim();
  assert k.workspace_id=b;
  ctx:=public.ci_account_session(k.id,k.lease_token);
  assert ctx->>'user_id'=b::text and ctx->>'session_id' is null;
  denied:=false;
  begin perform public.ci_account_session(k.id,k.lease_token,'session-a');
  exception when unique_violation then denied:=true; end;
  assert denied, 'two workspaces cannot share one session';
  perform public.ci_account_session(k.id,k.lease_token,'session-b');
  update public.ci_jobs set leased_until=now()-interval '1 second' where id=k.id;
  denied:=false;
  begin perform public.ci_account_session(k.id,k.lease_token);
  exception when others then denied:=true; end;
  assert denied, 'expired lease cannot disclose MCP session';
  update public.ci_account_identities set user_id='changed-owner' where workspace_id=a;
  denied:=false;
  begin perform public.ci_account_session(j.id,j.lease_token);
  exception when others then denied:=true; end;
  assert denied, 'identity changes cannot reuse another owner session';
end $$;
set local role authenticated;
do $$ begin
  assert not has_table_privilege(current_user,'public.ci_account_sessions','select');
  assert not has_table_privilege(current_user,'public.ci_account_identities','select');
  assert not has_function_privilege(current_user,'public.ci_account_session(uuid,uuid,text)','execute');
end $$;
reset role;
rollback;
