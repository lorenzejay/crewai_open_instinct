begin;
create or replace function public.ci_bind_watch(p_job uuid,p_token uuid,p_name text,p_instruction text,
  p_trigger text,p_account text,p_user text,p_slug text,p_config jsonb) returns jsonb
language plpgsql security definer set search_path='' as $$
declare j public.ci_jobs; u text; a public.ci_automations;
begin
  j:=public.ci_user_job(p_job,p_token);
  perform 1 from public.ci_workspaces where id=j.workspace_id for update;
  select user_id into u from public.ci_account_identities where workspace_id=j.workspace_id;
  u:=coalesce(u,j.workspace_id::text);
  if u<>p_user or p_slug not in ('GMAIL_NEW_GMAIL_MESSAGE','GITHUB_COMMIT_EVENT')
    or length(p_trigger) not between 1 and 200 or length(p_account) not between 1 and 200 then
    raise exception 'Account watch identity mismatch'; end if;
  if (select count(*) from public.ci_automations where workspace_id=j.workspace_id and not cancelled)>=50
    and not exists(select 1 from public.ci_automations where workspace_id=j.workspace_id
      and conversation_id=j.conversation_id and name=p_name) then raise exception 'Automation limit'; end if;
  -- Provider upsert can reuse an instance. Keep one local rule per instance; never reassign another tenant.
  if exists(select 1 from public.ci_automations where trigger_id=p_trigger and
    (workspace_id<>j.workspace_id or conversation_id<>j.conversation_id or name<>p_name)) then
    raise exception 'Trigger already bound to another rule'; end if;
  if exists(select 1 from public.ci_automations where workspace_id=j.workspace_id
    and conversation_id=j.conversation_id and name=p_name and
    (kind<>'account_watch' or trigger_id<>p_trigger)) then raise exception 'Use a new watch name'; end if;
  insert into public.ci_automations(workspace_id,conversation_id,name,kind,instruction,timezone,
    source,trigger_id,account_id,account_user_id)
    values(j.workspace_id,j.conversation_id,p_name,'account_watch',p_instruction,'UTC',
      jsonb_build_object('trigger_slug',p_slug,'trigger_config',p_config),p_trigger,p_account,p_user)
    on conflict(workspace_id,conversation_id,name) do update set instruction=excluded.instruction,
      enabled=true,cancelled=false returning * into a;
  insert into public.ci_notification_settings(workspace_id) values(j.workspace_id) on conflict do nothing;
  return jsonb_build_object('name',a.name,'kind',a.kind,'enabled',a.enabled);
end $$;

create or replace function public.ci_account_event(p_event text,p_trigger text,p_account text,p_user text,
  p_slug text,p_payload jsonb) returns text language plpgsql security definer set search_path='' as $$
declare a public.ci_automations; u text; e uuid;
begin
  select * into a from public.ci_automations where trigger_id=p_trigger and account_id=p_account
    and account_user_id=p_user and source->>'trigger_slug'=p_slug and kind='account_watch'
    and enabled and not cancelled for update;
  if not found then return 'ignored'; end if;
  select user_id into u from public.ci_account_identities where workspace_id=a.workspace_id;
  if coalesce(u,a.workspace_id::text)<>a.account_user_id then return 'ignored'; end if;
  if exists(select 1 from public.ci_notification_settings where workspace_id=a.workspace_id and paused) then return 'ignored'; end if;
  if length(p_event) not between 1 and 200 or octet_length(p_payload::text)>65536 then raise exception 'Invalid event'; end if;
  insert into public.ci_automation_events(automation_id,event_key) values(a.id,'provider:'||p_event)
    on conflict do nothing returning id into e;
  if e is null then return 'duplicate'; end if;
  -- Bound provider bursts; excess events are dropped rather than building an unlimited private-data backlog.
  if (select count(*) from public.ci_jobs where automation_id=a.id and status not in ('sent','failed'))>=20 then return 'rate_limited'; end if;
  insert into public.ci_jobs(workspace_id,conversation_id,kind,automation_id,input)
    values(a.workspace_id,a.conversation_id,'proactive',a.id,p_payload);
  return 'queued';
end $$;
revoke all on function public.ci_bind_watch(uuid,uuid,text,text,text,text,text,text,jsonb) from public,anon,authenticated;
revoke all on function public.ci_account_event(text,text,text,text,text,jsonb) from public,anon,authenticated;
grant execute on function public.ci_bind_watch(uuid,uuid,text,text,text,text,text,text,jsonb) to service_role;
grant execute on function public.ci_account_event(text,text,text,text,text,jsonb) to service_role;
notify pgrst,'reload schema';
commit;
