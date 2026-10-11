begin;
do $$
declare j public.ci_jobs; link_job public.ci_jobs; other_job public.ci_jobs; later_job public.ci_jobs;
  combined text; denied boolean;
begin
  perform public.ci_receive_text('+15555550100','+15555550501','burst-request','In 5 minutes brief me on PRs:','');
  perform public.ci_receive_text('+15555550100','+15555550501','burst-link','https://github.com/example/repo','');
  perform public.ci_receive_text('+15555550100','+15555550501','burst-empty','','https://example.com/link-preview');
  perform public.ci_receive_text('+15555550100','+15555550502','other-sender','private other sender','');
  perform public.ci_receive_text('+15555550100','+15555550501','later-request','Separate request','');
  select * into later_job from public.ci_jobs where message_id=(select id from public.ci_messages where provider_handle='later-request');
  update public.ci_jobs set created_at=created_at+interval '6 seconds' where id=later_job.id;
  select * into link_job from public.ci_jobs where message_id=(select id from public.ci_messages where provider_handle='burst-link');
  select * into other_job from public.ci_jobs where message_id=(select id from public.ci_messages where provider_handle='other-sender');
  select * into j from public.ci_claim();
  denied:=false;
  begin perform public.ci_coalesce_user_turn(j.id,gen_random_uuid());
  exception when others then denied:=true; end;
  assert denied, 'wrong lease cannot merge turns';
  combined:=public.ci_coalesce_user_turn(j.id,j.lease_token);
  assert combined=E'In 5 minutes brief me on PRs:\nhttps://github.com/example/repo';
  assert public.ci_coalesce_user_turn(j.id,j.lease_token)=combined, 'lease retries keep the combined request';
  assert (select status='failed' and error_code='coalesced_turn' from public.ci_jobs where id=link_job.id);
  assert (select status='queued' from public.ci_jobs where id=other_job.id), 'never combine another sender';
  assert (select status='queued' from public.ci_jobs where id=later_job.id), 'later requests stay separate';
  assert (public.ci_runtime_status()->>'failed_24h')::integer=0, 'combined events are not processing failures';
  assert (select count(*) from public.ci_messages where provider_handle in ('burst-request','burst-link','burst-empty'))=3,
    'original provider messages remain intact';
end $$;
set local role authenticated;
do $$ begin
  assert not has_function_privilege(current_user,'public.ci_coalesce_user_turn(uuid,uuid)','execute');
end $$;
reset role;
rollback;
