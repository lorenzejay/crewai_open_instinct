-- A private workspace can belong to a phone before any web account exists.
begin;
alter table public.ci_workspaces alter column owner_user_id drop not null;

create or replace function public.ci_receive_text(p_line text,p_sender text,p_handle text,
  p_content text,p_media_url text,p_link_hash text default null) returns text
language plpgsql security definer set search_path = '' as $$
declare w uuid;
begin
  if p_sender is null or p_sender !~ '^\+[1-9][0-9]{7,14}$'
    or p_line is null or p_line !~ '^\+[1-9][0-9]{7,14}$'
    or p_handle is null or length(p_handle) not between 1 and 200 then
    raise exception 'Invalid inbound identity';
  end if;
  -- Serialize first messages from the same phone. Workspace, binding, message
  -- and job commit together; concurrent deliveries cannot create two tenants.
  perform pg_advisory_xact_lock(hashtextextended(p_sender,0));
  -- Preserve older explicit account-linking flows for existing callers.
  if p_link_hash is not null then
    return public.ci_receive(p_line,p_sender,p_handle,p_content,p_media_url,p_link_hash);
  end if;
  if exists(select 1 from public.ci_messages where line=p_line and provider_handle=p_handle) then
    return 'duplicate';
  end if;
  if coalesce(btrim(p_content),'')='' and coalesce(p_media_url,'')='' then
    return 'ignored';
  end if;
  select workspace_id into w from public.ci_phone_bindings where phone=p_sender;
  if w is null then
    insert into public.ci_workspaces(owner_user_id) values(null) returning id into w;
    insert into public.ci_phone_bindings(workspace_id,phone) values(w,p_sender);
  end if;
  return public.ci_receive(p_line,p_sender,p_handle,coalesce(p_content,''),coalesce(p_media_url,''),null);
end $$;

-- A sender identity is trusted only after the server verifies Sendblue's
-- webhook secret. Browser/anonymous clients cannot provision or read tenants.
revoke all on function public.ci_receive_text(text,text,text,text,text,text) from public,anon,authenticated;
grant execute on function public.ci_receive_text(text,text,text,text,text,text) to service_role;
notify pgrst, 'reload schema';
commit;
