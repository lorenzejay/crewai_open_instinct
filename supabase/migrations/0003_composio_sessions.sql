begin;
-- Operator mapping for existing connections; new users default to workspace UUID.
create table if not exists public.ci_account_identities (
  workspace_id uuid primary key references public.ci_workspaces(id) on delete cascade,
  user_id text not null unique check(length(user_id) between 1 and 200)
);
create table if not exists public.ci_account_sessions (
  workspace_id uuid primary key references public.ci_workspaces(id) on delete cascade,
  user_id text not null unique check(length(user_id) between 1 and 200),
  session_id text not null unique check(length(session_id) between 1 and 200),
  created_at timestamptz not null default now()
);
alter table public.ci_account_sessions enable row level security;
alter table public.ci_account_identities enable row level security;
revoke all on public.ci_account_sessions,public.ci_account_identities from anon,authenticated;
grant all on public.ci_account_sessions,public.ci_account_identities to service_role;

-- Workspace identity comes from the active job, never an agent argument.
create or replace function public.ci_account_session(p_job uuid,p_token uuid,p_session text default null)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare w uuid; s text; u text; stored_user text;
begin
  select workspace_id into w from public.ci_jobs where id=p_job and lease_token=p_token
    and leased_until>now() and status='running' for update;
  if w is null then raise exception 'Inactive job lease'; end if;
  perform 1 from public.ci_workspaces where id=w for update;
  select user_id into u from public.ci_account_identities where workspace_id=w;
  u := coalesce(u,w::text);
  if p_session is not null then
    insert into public.ci_account_sessions(workspace_id,user_id,session_id) values(w,u,p_session)
      on conflict(workspace_id) do nothing;
  end if;
  select session_id,user_id into s,stored_user from public.ci_account_sessions where workspace_id=w;
  if stored_user is not null and stored_user<>u then raise exception 'Account identity changed'; end if;
  return jsonb_build_object('workspace_id',w,'user_id',u,'session_id',s);
end $$;
revoke all on function public.ci_account_session(uuid,uuid,text) from public,anon,authenticated;
grant execute on function public.ci_account_session(uuid,uuid,text) to service_role;
notify pgrst,'reload schema';
commit;
