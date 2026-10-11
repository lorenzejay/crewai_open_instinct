begin;
create table if not exists public.ci_notification_settings (
  workspace_id uuid primary key references public.ci_workspaces(id) on delete cascade,
  timezone text not null default 'UTC',
  quiet_start integer not null default -1,
  quiet_end integer not null default -1,
  daily_limit integer not null default 10 check(daily_limit between 1 and 50),
  paused boolean not null default false,
  check ((quiet_start=-1 and quiet_end=-1) or
    (quiet_start between 0 and 23 and quiet_end between 0 and 23 and quiet_start<>quiet_end))
);
create table if not exists public.ci_automations (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references public.ci_workspaces(id) on delete cascade,
  conversation_id uuid not null,
  foreign key(conversation_id,workspace_id) references public.ci_conversations(id,workspace_id) on delete cascade,
  name text not null check(length(name) between 1 and 100),
  kind text not null check(kind in ('reminder','briefing','monitor','followup','account_watch')),
  instruction text not null check(length(instruction) between 1 and 2000),
  timezone text not null,
  schedule jsonb not null default '{}'::jsonb,
  source jsonb not null default '{}'::jsonb,
  enabled boolean not null default true,
  cancelled boolean not null default false,
  next_run_at timestamptz,
  last_snapshot text,
  last_checked_at timestamptz,
  last_notified_at timestamptz,
  last_error text,
  trigger_id text unique,
  account_id text,
  account_user_id text,
  created_at timestamptz not null default now(),
  unique(workspace_id,conversation_id,name),
  unique(id,workspace_id,conversation_id)
);
create index if not exists ci_automations_due on public.ci_automations(next_run_at) where enabled;
create table if not exists public.ci_automation_events (
  id uuid primary key default gen_random_uuid(),
  automation_id uuid not null references public.ci_automations(id) on delete cascade,
  event_key text not null,
  created_at timestamptz not null default now(),
  unique(automation_id,event_key)
);
create table if not exists public.ci_runtime_health (
  component text primary key check(component in ('worker','scheduler')),
  seen_at timestamptz not null
);
alter table public.ci_jobs alter column message_id drop not null;
alter table public.ci_jobs add column if not exists kind text not null default 'user';
alter table public.ci_jobs add column if not exists automation_id uuid;
alter table public.ci_jobs add column if not exists input jsonb not null default '{}'::jsonb;
alter table public.ci_jobs add column if not exists not_before timestamptz not null default now();
alter table public.ci_jobs add column if not exists sent_at timestamptz;
alter table public.ci_jobs add column if not exists delivery_reserved_at timestamptz;
do $$ begin
  if not exists(select 1 from pg_constraint where conname='ci_jobs_automation_fk') then
    alter table public.ci_jobs add constraint ci_jobs_automation_fk
      foreign key(automation_id,workspace_id,conversation_id)
      references public.ci_automations(id,workspace_id,conversation_id) on delete cascade;
    alter table public.ci_jobs add constraint ci_jobs_origin_check check(
      (kind='user' and message_id is not null and automation_id is null) or
      (kind='proactive' and message_id is null and automation_id is not null));
  end if;
end $$;

-- All control tools require an active inbound-user job; proactive jobs cannot create rules.
create or replace function public.ci_user_job(p_job uuid,p_token uuid) returns public.ci_jobs
language plpgsql security definer set search_path='' as $$
declare j public.ci_jobs;
begin
  select * into j from public.ci_jobs where id=p_job and lease_token=p_token
    and leased_until>now() and status='running' and kind='user' for update;
  if not found then raise exception 'Inactive user job lease'; end if;
  return j;
end $$;

create or replace function public.ci_put_automation(p_job uuid,p_token uuid,p_name text,p_kind text,
  p_instruction text,p_timezone text,p_schedule jsonb,p_source jsonb,p_next timestamptz)
returns jsonb language plpgsql security definer set search_path='' as $$
declare j public.ci_jobs; a public.ci_automations;
begin
  j:=public.ci_user_job(p_job,p_token);
  perform 1 from pg_timezone_names where name=p_timezone;
  if not found then raise exception 'Unknown timezone'; end if;
  if p_kind not in ('reminder','briefing','monitor','followup') or p_next<=now() or p_next is null
    or jsonb_typeof(p_schedule)<>'object' or jsonb_typeof(p_source)<>'object' then
    raise exception 'Invalid automation';
  end if;
  perform 1 from public.ci_workspaces where id=j.workspace_id for update;
  if exists(select 1 from public.ci_automations where workspace_id=j.workspace_id
    and conversation_id=j.conversation_id and name=p_name and trigger_id is not null) then
    raise exception 'Use a different name for a scheduled automation'; end if;
  if (select count(*) from public.ci_automations where workspace_id=j.workspace_id and not cancelled)>=50
    and not exists(select 1 from public.ci_automations where workspace_id=j.workspace_id
      and conversation_id=j.conversation_id and name=p_name) then raise exception 'Automation limit'; end if;
  insert into public.ci_automations(workspace_id,conversation_id,name,kind,instruction,timezone,schedule,source,next_run_at)
    values(j.workspace_id,j.conversation_id,p_name,p_kind,p_instruction,p_timezone,p_schedule,p_source,p_next)
    on conflict(workspace_id,conversation_id,name) do update set kind=excluded.kind,
      instruction=excluded.instruction,timezone=excluded.timezone,schedule=excluded.schedule,source=excluded.source,
      next_run_at=excluded.next_run_at,enabled=true,cancelled=false,last_snapshot=null,last_error=null,
      trigger_id=null,account_id=null,account_user_id=null returning * into a;
  update public.ci_jobs set status='failed',error_code='automation_replaced',lease_token=null,leased_until=null
    where automation_id=a.id and status in ('queued','running','ready');
  insert into public.ci_notification_settings(workspace_id,timezone) values(j.workspace_id,p_timezone)
    on conflict(workspace_id) do nothing;
  return jsonb_build_object('name',a.name,'kind',a.kind,'next_run_at',a.next_run_at,'timezone',a.timezone);
end $$;

create or replace function public.ci_manage_automations(p_job uuid,p_token uuid,p_action text,
  p_name text default '',p_timezone text default '',p_quiet_start integer default -1,
  p_quiet_end integer default -1,p_limit integer default 10)
returns jsonb language plpgsql security definer set search_path='' as $$
declare j public.ci_jobs; result jsonb; a public.ci_automations;
begin
  j:=public.ci_user_job(p_job,p_token);
  insert into public.ci_notification_settings(workspace_id) values(j.workspace_id) on conflict do nothing;
  if p_action in ('pause','resume','cancel') then
    update public.ci_automations set enabled=(p_action='resume'),cancelled=(p_action='cancel')
      where workspace_id=j.workspace_id and conversation_id=j.conversation_id and name=p_name
      and not cancelled returning * into a;
    if not found then raise exception 'Automation not found'; end if;
    if p_action='cancel' then
      update public.ci_jobs set status='failed',error_code='automation_disabled',lease_token=null,leased_until=null
        where automation_id=a.id and status in ('queued','running','ready');
    end if;
  elsif p_action in ('pause_all','resume_all') then
    update public.ci_notification_settings set paused=(p_action='pause_all') where workspace_id=j.workspace_id;
  elsif p_action='settings' then
    perform 1 from pg_timezone_names where name=p_timezone;
    if not found then raise exception 'Unknown timezone'; end if;
    update public.ci_notification_settings set timezone=p_timezone,quiet_start=p_quiet_start,
      quiet_end=p_quiet_end,daily_limit=p_limit where workspace_id=j.workspace_id;
  elsif p_action<>'list' then raise exception 'Unknown action'; end if;
  select coalesce(jsonb_agg(jsonb_build_object('name',name,'kind',kind,'instruction',instruction,
      'enabled',enabled,'next_run_at',next_run_at,'timezone',timezone,'schedule',schedule,
      'last_checked_at',last_checked_at,'last_notified_at',last_notified_at,'last_error',last_error)
      order by created_at),'[]'::jsonb) into result
    from public.ci_automations where workspace_id=j.workspace_id and conversation_id=j.conversation_id and not cancelled;
  return jsonb_build_object('automations',result,'settings',
    (select to_jsonb(s)-'workspace_id' from public.ci_notification_settings s where workspace_id=j.workspace_id));
end $$;

create or replace function public.ci_due_automations() returns jsonb
language sql stable security definer set search_path='' as $$
  select coalesce(jsonb_agg(to_jsonb(a)||jsonb_build_object('database_now',now())),'[]'::jsonb) from (
    select a.id,a.schedule,a.timezone,a.next_run_at from public.ci_automations a
    left join public.ci_notification_settings s on s.workspace_id=a.workspace_id
    where a.enabled and not a.cancelled and a.next_run_at<=now() and not coalesce(s.paused,false)
    order by a.next_run_at limit 100) a;
$$;

create or replace function public.ci_dispatch_automation(p_rule uuid,p_expected timestamptz,p_next timestamptz)
returns boolean language plpgsql security definer set search_path='' as $$
declare a public.ci_automations; e uuid;
begin
  select * into a from public.ci_automations where id=p_rule and next_run_at=p_expected
    and next_run_at<=now() and enabled and not cancelled for update;
  if not found then return false; end if;
  if exists(select 1 from public.ci_notification_settings where workspace_id=a.workspace_id and paused) then return false; end if;
  if p_next is not null and p_next<=now() then raise exception 'Next run must be in future'; end if;
  insert into public.ci_automation_events(automation_id,event_key) values(a.id,'schedule:'||p_expected::text)
    on conflict do nothing returning id into e;
  if e is not null and not exists(select 1 from public.ci_jobs where automation_id=a.id
      and status not in ('sent','failed')) then
    insert into public.ci_jobs(workspace_id,conversation_id,kind,automation_id,input)
      values(a.workspace_id,a.conversation_id,'proactive',a.id,jsonb_build_object('scheduled_at',p_expected));
  end if;
  update public.ci_automations set next_run_at=p_next where id=a.id;
  return e is not null;
end $$;

create or replace function public.ci_claim() returns setof public.ci_jobs
language plpgsql security definer set search_path='' as $$
declare j public.ci_jobs;
begin
  select q.* into j from public.ci_jobs q
    where (q.status in ('queued','ready') or (q.status='running' and q.leased_until<now())) and q.not_before<=now()
      and (q.kind='user' or (exists(select 1 from public.ci_automations a where a.id=q.automation_id and a.enabled and not a.cancelled)
        and not exists(select 1 from public.ci_notification_settings s where s.workspace_id=q.workspace_id and s.paused)))
      and not exists(select 1 from public.ci_jobs old where old.conversation_id=q.conversation_id
        and old.seq<q.seq and old.status not in ('sent','failed')
        and (old.kind='user' or old.status in ('running','sending','uncertain') or
          (exists(select 1 from public.ci_automations a where a.id=old.automation_id and a.enabled and not a.cancelled)
          and not exists(select 1 from public.ci_notification_settings s where s.workspace_id=old.workspace_id and s.paused)))
        and (old.not_before<=now() or old.status in ('running','sending','uncertain')))
    order by q.seq for update skip locked limit 1;
  if not found then return; end if;
  if j.status<>'ready' and j.attempts>=3 then
    update public.ci_jobs set status='failed',error_code='generation_attempts_exhausted' where id=j.id;
    return;
  end if;
  return query update public.ci_jobs set status=case when j.status='ready' then 'sending' else 'running' end,
    attempts=case when j.status='ready' then attempts else attempts+1 end,
    lease_token=gen_random_uuid(),leased_until=now()+interval '5 minutes' where id=j.id returning *;
end $$;

create or replace function public.ci_context(p_job uuid,p_token uuid) returns jsonb
language plpgsql stable security definer set search_path='' as $$
declare j public.ci_jobs; ctx jsonb;
begin
  select * into j from public.ci_jobs where id=p_job and lease_token=p_token and leased_until>now()
    and status in ('running','sending');
  if not found then raise exception 'Inactive job lease'; end if;
  select jsonb_build_object('workspace_id',j.workspace_id,'line',c.line,'phone',c.phone,
    'kind',j.kind,'content',coalesce(m.content,''),'media_url',coalesce(m.media_url,''),
    'automation',(select to_jsonb(a)-'trigger_id'-'account_id'-'account_user_id' from public.ci_automations a where a.id=j.automation_id),
    'event',j.input,'database_now',now(),
    'notification_settings',(select to_jsonb(s)-'workspace_id' from public.ci_notification_settings s where s.workspace_id=j.workspace_id),
    'history',coalesce((select jsonb_agg(h order by h.turn_seq,h.turn_order) from (
      select history.direction,history.content,prior.seq as turn_seq,
        case when history.direction='user' then 0 else 1 end as turn_order
      from public.ci_messages history join public.ci_jobs prior on
        (history.id=prior.message_id or (history.direction='assistant' and history.provider_handle=prior.outbound_handle))
      where history.workspace_id=j.workspace_id and history.conversation_id=j.conversation_id
        and prior.conversation_id=j.conversation_id and prior.seq<j.seq
      order by prior.seq desc,turn_order desc limit 20) h),'[]'::jsonb),
    'memories',coalesce((select jsonb_agg(n) from (select name,content from public.ci_memories
      where workspace_id=j.workspace_id order by updated_at desc limit 30) n),'[]'::jsonb)) into ctx
    from public.ci_conversations c left join public.ci_messages m on m.id=j.message_id
    where c.id=j.conversation_id;
  return ctx;
end $$;

-- Recheck at generation and delivery; delayed notifications must not block normal chat.
create or replace function public.ci_allow_proactive(p_job uuid,p_token uuid) returns boolean
language plpgsql security definer set search_path='' as $$
declare j public.ci_jobs; a public.ci_automations; s public.ci_notification_settings;
  local_now timestamp; hour integer; defer_until timestamptz; total integer;
begin
  select * into j from public.ci_jobs where id=p_job and lease_token=p_token and leased_until>now()
    and status in ('running','sending') for update;
  if not found then raise exception 'Inactive job lease'; end if;
  if j.kind='user' then return true; end if;
  -- Serialize quota reservations across conversations within a tenant.
  perform 1 from public.ci_workspaces where id=j.workspace_id for update;
  select * into a from public.ci_automations where id=j.automation_id;
  select * into s from public.ci_notification_settings where workspace_id=j.workspace_id;
  if a.cancelled then
    update public.ci_jobs set status='failed',error_code='automation_disabled',lease_token=null,leased_until=null where id=j.id;
    return false;
  end if;
  if not a.enabled or coalesce(s.paused,false) then
    update public.ci_jobs set status=case when j.status='sending' then 'ready' else 'queued' end,
      attempts=case when j.status='running' then greatest(attempts-1,0) else attempts end,
      delivery_reserved_at=null,lease_token=null,leased_until=null where id=j.id;
    return false;
  end if;
  local_now:=now() at time zone coalesce(s.timezone,a.timezone);
  hour:=extract(hour from local_now);
  if s.quiet_start>=0 and ((s.quiet_start<s.quiet_end and hour>=s.quiet_start and hour<s.quiet_end)
      or (s.quiet_start>s.quiet_end and (hour>=s.quiet_start or hour<s.quiet_end))) then
    defer_until:=(date_trunc('day',local_now)+make_interval(hours=>s.quiet_end)+
      case when hour>=s.quiet_start then interval '1 day' * (s.quiet_start>s.quiet_end)::integer else interval '0 days' end)
      at time zone s.timezone;
  end if;
  if j.status='sending' then
    select count(*) into total from public.ci_jobs where workspace_id=j.workspace_id and kind='proactive'
      and id<>j.id and status in ('sent','sending','uncertain')
      and coalesce(sent_at,delivery_reserved_at)
        >=date_trunc('day',local_now) at time zone coalesce(s.timezone,a.timezone);
    if total>=coalesce(s.daily_limit,10) then
      defer_until:=greatest(defer_until,(date_trunc('day',local_now)+interval '1 day') at time zone coalesce(s.timezone,a.timezone));
    end if;
  end if;
  if defer_until is not null then
    update public.ci_jobs set status=case when j.status='sending' then 'ready' else 'queued' end,
      attempts=case when j.status='running' then greatest(attempts-1,0) else attempts end,
      not_before=defer_until,delivery_reserved_at=null,lease_token=null,leased_until=null where id=j.id;
    return false;
  end if;
  if j.status='sending' then update public.ci_jobs set delivery_reserved_at=now() where id=j.id; end if;
  return true;
end $$;

create or replace function public.ci_finish_proactive(p_job uuid,p_token uuid,p_reply text,p_snapshot text default null)
returns boolean language plpgsql security definer set search_path='' as $$
declare j public.ci_jobs;
begin
  select * into j from public.ci_jobs where id=p_job and lease_token=p_token and leased_until>now()
    and status='running' and kind='proactive' for update;
  if not found then return false; end if;
  if p_reply is not null and length(p_reply) not between 1 and 4000 then raise exception 'Invalid reply'; end if;
  update public.ci_automations set last_checked_at=now(),last_error=null,
    last_snapshot=coalesce(p_snapshot,last_snapshot) where id=j.automation_id;
  update public.ci_jobs set reply=p_reply,status=case when p_reply is null then 'failed' else 'ready' end,
    error_code=case when p_reply is null then 'proactive_no_notification' else null end,
    lease_token=null,leased_until=null where id=j.id;
  return true;
end $$;

create or replace function public.ci_complete(p_job uuid,p_token uuid,p_handle text) returns boolean
language plpgsql security definer set search_path='' as $$
declare j public.ci_jobs; l text;
begin
  update public.ci_jobs set status='sent',outbound_handle=p_handle,error_code=null,sent_at=now()
    where id=p_job and lease_token=p_token and status='sending' returning * into j;
  if not found then return false; end if;
  select line into l from public.ci_conversations where id=j.conversation_id;
  insert into public.ci_messages(workspace_id,conversation_id,line,provider_handle,direction,content)
    values(j.workspace_id,j.conversation_id,l,p_handle,'assistant',j.reply);
  if j.kind='proactive' then update public.ci_automations set last_notified_at=now() where id=j.automation_id; end if;
  return true;
end $$;

create or replace function public.ci_error(p_job uuid,p_token uuid,p_sending boolean,p_code text) returns boolean
language plpgsql security definer set search_path='' as $$
declare j public.ci_jobs;
begin
  update public.ci_jobs set status=case when p_sending then 'uncertain' else 'failed' end,error_code=p_code
    where id=p_job and lease_token=p_token and status=case when p_sending then 'sending' else 'running' end returning * into j;
  if not found then return false; end if;
  if j.kind='proactive' then update public.ci_automations set last_error=p_code,last_checked_at=now() where id=j.automation_id; end if;
  return true;
end $$;

create or replace function public.ci_worker_heartbeat() returns void language sql security definer set search_path='' as $$
  insert into public.ci_runtime_health values('worker',now()) on conflict(component) do update set seen_at=excluded.seen_at;
$$;
create or replace function public.ci_scheduler_heartbeat() returns void language sql security definer set search_path='' as $$
  insert into public.ci_runtime_health values('scheduler',now()) on conflict(component) do update set seen_at=excluded.seen_at;
$$;
create or replace function public.ci_runtime_status() returns jsonb language sql stable security definer set search_path='' as $$
  select jsonb_build_object('database_now',now(),'heartbeats',(select coalesce(jsonb_object_agg(component,seen_at),'{}'::jsonb) from public.ci_runtime_health),
    'queued',(select count(*) from public.ci_jobs where status in ('queued','ready')),
    'uncertain',(select count(*) from public.ci_jobs where status='uncertain' or (status='sending' and leased_until<now())),
    'failed_24h',(select count(*) from public.ci_jobs where status='failed' and error_code not in ('proactive_no_notification','automation_disabled','automation_replaced','proactive_paused') and created_at>now()-interval '1 day'));
$$;

do $$ declare t text; fn record; begin
  foreach t in array array['ci_notification_settings','ci_automations','ci_automation_events','ci_runtime_health'] loop
    execute format('alter table public.%I enable row level security',t);
    execute format('revoke all on public.%I from anon,authenticated',t);
    execute format('grant all on public.%I to service_role',t);
  end loop;
  for fn in select p.oid::regprocedure signature from pg_proc p join pg_namespace n on n.oid=p.pronamespace
    where n.nspname='public' and p.proname in ('ci_user_job','ci_put_automation','ci_manage_automations',
      'ci_due_automations','ci_dispatch_automation','ci_allow_proactive','ci_finish_proactive',
      'ci_worker_heartbeat','ci_scheduler_heartbeat','ci_runtime_status') loop
    execute format('revoke all on function %s from public,anon,authenticated',fn.signature);
    execute format('grant execute on function %s to service_role',fn.signature);
  end loop;
end $$;
notify pgrst,'reload schema';
commit;
