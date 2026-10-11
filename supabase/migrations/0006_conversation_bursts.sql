-- Combine a text and its immediately following link into one durable turn.
begin;
create or replace function public.ci_coalesce_user_turn(p_job uuid,p_token uuid) returns text
language plpgsql security definer set search_path='' as $$
declare j public.ci_jobs; following record; combined text;
begin
  j:=public.ci_user_job(p_job,p_token);
  select coalesce(j.input->>'combined_content',m.content) into combined
    from public.ci_messages m where m.id=j.message_id;
  for following in
    select q.id,m.content from public.ci_jobs q join public.ci_messages m on m.id=q.message_id
    where q.workspace_id=j.workspace_id and q.conversation_id=j.conversation_id
      and q.kind='user' and q.status='queued' and q.seq>j.seq
      and q.created_at<=j.created_at+interval '5 seconds'
    order by q.seq for update of q skip locked limit 10
  loop
    if length(combined)+length(following.content)+1>16000 then exit; end if;
    if length(trim(following.content))>0 then
      combined:=concat_ws(E'\n',nullif(combined,''),following.content);
    end if;
    update public.ci_jobs set status='failed',error_code='coalesced_turn',input=input||jsonb_build_object('coalesced_into',j.id)
      where id=following.id;
  end loop;
  update public.ci_jobs set input=input||jsonb_build_object('combined_content',combined) where id=j.id;
  return combined;
end $$;
revoke all on function public.ci_coalesce_user_turn(uuid,uuid) from public,anon,authenticated;
grant execute on function public.ci_coalesce_user_turn(uuid,uuid) to service_role;
create or replace function public.ci_runtime_status() returns jsonb
language sql stable security definer set search_path='' as $$
  select jsonb_build_object('database_now',now(),'heartbeats',
    (select coalesce(jsonb_object_agg(component,seen_at),'{}'::jsonb) from public.ci_runtime_health),
    'queued',(select count(*) from public.ci_jobs where status in ('queued','ready')),
    'uncertain',(select count(*) from public.ci_jobs where status='uncertain' or (status='sending' and leased_until<now())),
    'failed_24h',(select count(*) from public.ci_jobs where status='failed'
      and error_code not in ('coalesced_turn','proactive_no_notification','automation_disabled','automation_replaced','proactive_paused')
      and created_at>now()-interval '1 day'));
$$;
notify pgrst, 'reload schema';
commit;
