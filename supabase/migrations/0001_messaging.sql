-- Additive schema: separate ci_* tables preserve existing application data.
begin;
create table if not exists public.ci_workspaces (
  id uuid primary key default gen_random_uuid(),
  owner_user_id uuid not null unique references auth.users(id) on delete cascade,
  created_at timestamptz not null default now()
);
create table if not exists public.ci_phone_links (
  hash text primary key,
  workspace_id uuid not null references public.ci_workspaces(id) on delete cascade,
  expires_at timestamptz not null,
  consumed_at timestamptz
);
create table if not exists public.ci_phone_bindings (
  workspace_id uuid primary key references public.ci_workspaces(id) on delete cascade,
  phone text not null unique check (phone ~ '^\+[1-9][0-9]{7,14}$')
);
create table if not exists public.ci_conversations (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references public.ci_workspaces(id) on delete cascade,
  line text not null,
  phone text not null,
  unique(workspace_id, line, phone),
  unique(id, workspace_id)
);
create table if not exists public.ci_messages (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references public.ci_workspaces(id) on delete cascade,
  conversation_id uuid not null,
  foreign key (conversation_id, workspace_id) references public.ci_conversations(id, workspace_id) on delete cascade,
  line text not null,
  provider_handle text,
  direction text not null check (direction in ('user', 'assistant')),
  content text not null,
  media_url text,
  created_at timestamptz not null default now(),
  unique(line, provider_handle),
  unique(id, workspace_id, conversation_id)
);
create table if not exists public.ci_jobs (
  id uuid primary key default gen_random_uuid(),
  seq bigint generated always as identity unique,
  workspace_id uuid not null references public.ci_workspaces(id) on delete cascade,
  conversation_id uuid not null,
  foreign key (conversation_id, workspace_id) references public.ci_conversations(id, workspace_id) on delete cascade,
  message_id uuid not null unique,
  foreign key (message_id,workspace_id,conversation_id) references public.ci_messages(id,workspace_id,conversation_id) on delete cascade,
  status text not null default 'queued' check (status in ('queued','running','ready','sending','sent','failed','uncertain')),
  attempts integer not null default 0,
  lease_token uuid,
  leased_until timestamptz,
  reply text,
  outbound_handle text,
  error_code text,
  created_at timestamptz not null default now()
);
create index if not exists ci_jobs_pending on public.ci_jobs (seq) where status not in ('sent','failed');
create table if not exists public.ci_memories (
  workspace_id uuid not null references public.ci_workspaces(id) on delete cascade,
  name text not null check(length(name) between 1 and 100),
  content text not null check(length(content) between 1 and 2000),
  updated_at timestamptz not null default now(),
  primary key(workspace_id, name)
);

create or replace function public.ci_owns(p_workspace uuid) returns boolean
language sql stable security definer set search_path = '' as $$
  select exists(select 1 from public.ci_workspaces where id=p_workspace and owner_user_id=auth.uid());
$$;

alter table public.ci_workspaces enable row level security;
alter table public.ci_phone_links enable row level security;
alter table public.ci_phone_bindings enable row level security;
alter table public.ci_conversations enable row level security;
alter table public.ci_messages enable row level security;
alter table public.ci_jobs enable row level security;
alter table public.ci_memories enable row level security;
-- No user-facing grants on link hashes or jobs. RLS also remains enabled there.
revoke all on public.ci_workspaces,public.ci_phone_links,public.ci_phone_bindings,
  public.ci_conversations,public.ci_messages,public.ci_jobs,public.ci_memories from anon,authenticated;
grant select on public.ci_workspaces,public.ci_phone_bindings,public.ci_conversations,
  public.ci_messages,public.ci_memories to authenticated;
grant all on public.ci_workspaces,public.ci_phone_links,public.ci_phone_bindings,
  public.ci_conversations,public.ci_messages,public.ci_jobs,public.ci_memories to service_role;
grant usage,select on sequence public.ci_jobs_seq_seq to service_role;
do $$
begin
  if not exists(select 1 from pg_policies where schemaname='public' and tablename='ci_workspaces' and policyname='ci_owner') then
    create policy ci_owner on public.ci_workspaces for select to authenticated using(owner_user_id=(select auth.uid()));
  end if;
end $$;
do $$
declare t text;
begin
  foreach t in array array['ci_phone_bindings','ci_conversations','ci_messages','ci_memories'] loop
    if not exists(select 1 from pg_policies where schemaname='public' and tablename=t and policyname='ci_owner') then
      execute format('create policy ci_owner on public.%I for select to authenticated using(public.ci_owns(workspace_id))',t);
    end if;
  end loop;
end $$;

create or replace function public.ci_create_link(p_user_id uuid,p_hash text) returns uuid
language plpgsql security definer set search_path = '' as $$
declare w uuid;
begin
  insert into public.ci_workspaces(owner_user_id) values(p_user_id)
    on conflict(owner_user_id) do update set owner_user_id=excluded.owner_user_id returning id into w;
  -- Limit each account to one live code. Codes carry 128 bits of randomness.
  delete from public.ci_phone_links where workspace_id=w;
  insert into public.ci_phone_links values(p_hash,w,now()+interval '10 minutes',null);
  return w;
end $$;

create or replace function public.ci_workspace(p_user_id uuid) returns jsonb
language sql stable security definer set search_path = '' as $$
  select jsonb_build_object('id',w.id,'phone',b.phone)
  from public.ci_workspaces w left join public.ci_phone_bindings b on b.workspace_id=w.id
  where w.owner_user_id=p_user_id;
$$;

create or replace function public.ci_receive(p_line text,p_sender text,p_handle text,
  p_content text,p_media_url text,p_link_hash text default null) returns text
language plpgsql security definer set search_path = '' as $$
declare w uuid; c uuid; m uuid;
begin
  if p_link_hash is not null then
    select workspace_id into w from public.ci_phone_links
      where hash=p_link_hash and consumed_at is null and expires_at>now() for update;
    if w is null then return 'ignored'; end if;
    if exists(select 1 from public.ci_phone_bindings where phone=p_sender and workspace_id<>w) then
      return 'phone_already_linked';
    end if;
    if exists(select 1 from public.ci_phone_bindings where workspace_id=w and phone<>p_sender) then
      -- A phone change needs a separate revocation flow that also cancels queued
      -- work. Do not send old workspace history to a replacement number.
      return 'workspace_already_linked';
    end if;
    insert into public.ci_phone_bindings(workspace_id,phone) values(w,p_sender)
      on conflict(workspace_id) do update set phone=excluded.phone;
    update public.ci_phone_links set consumed_at=now() where hash=p_link_hash;
    return 'linked';
  end if;
  select workspace_id into w from public.ci_phone_bindings where phone=p_sender;
  if w is null then return 'ignored'; end if;
  insert into public.ci_conversations(workspace_id,line,phone) values(w,p_line,p_sender)
    on conflict(workspace_id,line,phone) do update set phone=excluded.phone returning id into c;
  insert into public.ci_messages(workspace_id,conversation_id,line,provider_handle,direction,content,media_url)
    values(w,c,p_line,p_handle,'user',p_content,p_media_url)
    on conflict(line,provider_handle) do nothing returning id into m;
  if m is null then return 'duplicate'; end if;
  insert into public.ci_jobs(workspace_id,conversation_id,message_id) values(w,c,m);
  return 'queued';
end $$;

create or replace function public.ci_claim() returns setof public.ci_jobs
language plpgsql security definer set search_path = '' as $$
declare j public.ci_jobs;
begin
  select * into j from public.ci_jobs q
    where (q.status in ('queued','ready') or (q.status='running' and q.leased_until<now()))
    and not exists(select 1 from public.ci_jobs old where old.conversation_id=q.conversation_id
      and old.seq<q.seq and old.status not in ('sent','failed'))
    order by seq for update skip locked limit 1;
  if not found then return; end if;
  -- Exhausted generation attempts fail visibly rather than spinning forever.
  if j.status<>'ready' and j.attempts>=3 then
    update public.ci_jobs set status='failed',error_code='generation_attempts_exhausted' where id=j.id;
    return;
  end if;
  return query update public.ci_jobs set
    status=case when j.status='ready' then 'sending' else 'running' end,
    attempts=case when j.status='ready' then attempts else attempts+1 end,
    lease_token=gen_random_uuid(),leased_until=now()+interval '5 minutes'
    where id=j.id returning *;
end $$;

create or replace function public.ci_context(p_job uuid,p_token uuid) returns jsonb
language plpgsql stable security definer set search_path = '' as $$
declare j public.ci_jobs; ctx jsonb;
begin
  select * into j from public.ci_jobs where id=p_job and lease_token=p_token and leased_until>now()
    and status in ('running','sending');
  if not found then raise exception 'Inactive job lease'; end if;
  select jsonb_build_object('workspace_id',j.workspace_id,'line',c.line,'phone',c.phone,
    'content',m.content,'media_url',m.media_url,
    'history',coalesce((select jsonb_agg(h order by h.turn_seq,h.turn_order) from (
      select history.direction,history.content,prior.seq as turn_seq,
        case when history.direction='user' then 0 else 1 end as turn_order
      from public.ci_messages history join public.ci_jobs prior on
        (history.id=prior.message_id or (history.direction='assistant' and history.provider_handle=prior.outbound_handle))
      where history.workspace_id=j.workspace_id and history.conversation_id=j.conversation_id
        and prior.conversation_id=j.conversation_id and prior.seq<j.seq
      order by prior.seq desc,turn_order desc limit 20) h),'[]'::jsonb),
    'memories',coalesce((select jsonb_agg(n) from (
      select name,content from public.ci_memories where workspace_id=j.workspace_id
      order by updated_at desc limit 30) n),'[]'::jsonb)) into ctx
    from public.ci_conversations c join public.ci_messages m on m.id=j.message_id
    where c.id=j.conversation_id;
  return ctx;
end $$;

create or replace function public.ci_save_memory(p_job uuid,p_token uuid,p_name text,p_content text) returns boolean
language plpgsql security definer set search_path = '' as $$
declare w uuid;
begin
  select workspace_id into w from public.ci_jobs where id=p_job and lease_token=p_token
    and leased_until>now() and status='running' for update;
  if w is null then return false; end if;
  if (select count(*) from public.ci_memories where workspace_id=w)>=100
    and not exists(select 1 from public.ci_memories where workspace_id=w and name=p_name) then
    raise exception 'Memory capacity reached';
  end if;
  insert into public.ci_memories values(w,p_name,p_content,now())
    on conflict(workspace_id,name) do update set content=excluded.content,updated_at=now();
  return true;
end $$;

create or replace function public.ci_forget_memory(p_job uuid,p_token uuid,p_name text) returns boolean
language plpgsql security definer set search_path = '' as $$
declare w uuid;
begin
  select workspace_id into w from public.ci_jobs where id=p_job and lease_token=p_token
    and leased_until>now() and status='running' for update;
  if w is null then return false; end if;
  delete from public.ci_memories where workspace_id=w and name=p_name;
  return true;
end $$;

create or replace function public.ci_reply(p_job uuid,p_token uuid,p_reply text) returns boolean
language plpgsql security definer set search_path = '' as $$
begin
  if length(p_reply) not between 1 and 4000 then raise exception 'Invalid reply size'; end if;
  update public.ci_jobs set reply=p_reply,status='ready',lease_token=null,leased_until=null
    where id=p_job and lease_token=p_token and leased_until>now() and status='running';
  return found;
end $$;

create or replace function public.ci_complete(p_job uuid,p_token uuid,p_handle text) returns boolean
language plpgsql security definer set search_path = '' as $$
declare j public.ci_jobs; l text;
begin
  -- Sending jobs never auto-reclaim: accept a late confirmation for the same lease.
  update public.ci_jobs set status='sent',outbound_handle=p_handle,error_code=null
    where id=p_job and lease_token=p_token and status='sending' returning * into j;
  if not found then return false; end if;
  select line into l from public.ci_conversations where id=j.conversation_id;
  insert into public.ci_messages(workspace_id,conversation_id,line,provider_handle,direction,content)
    values(j.workspace_id,j.conversation_id,l,p_handle,'assistant',j.reply);
  return true;
end $$;

create or replace function public.ci_error(p_job uuid,p_token uuid,p_sending boolean,p_code text) returns boolean
language plpgsql security definer set search_path = '' as $$
begin
  update public.ci_jobs set status=case when p_sending then 'uncertain' else 'failed' end,
    error_code=p_code where id=p_job and lease_token=p_token
    and status=case when p_sending then 'sending' else 'running' end;
  return found;
end $$;

-- Functions that accept trusted user IDs or lease tokens are server-only.
do $$
declare fn record;
begin
  for fn in select p.oid::regprocedure as signature from pg_proc p join pg_namespace n on n.oid=p.pronamespace
    where n.nspname='public' and p.proname in ('ci_owns','ci_create_link','ci_workspace',
      'ci_receive','ci_claim','ci_context','ci_save_memory','ci_forget_memory',
      'ci_reply','ci_complete','ci_error') loop
    execute format('revoke all on function %s from public,anon,authenticated',fn.signature);
    execute format('grant execute on function %s to service_role',fn.signature);
  end loop;
end $$;
grant execute on function public.ci_owns(uuid) to authenticated;
commit;
