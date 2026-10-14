-- Assessments, certified reports and reruns.
-- Supabase project "ingqiqo-executables-leads" (zlwsqhlzwhqwxnekaejn). Builds on 0001_leads.sql,
-- which lives in the private ingqiqo-executables-site repository (leads, lead_events, capture_lead).
--
-- What this adds
--   assessment_series        one respondent's (or one client's) run of an instrument over time
--   assessment_runs          one immutable, versioned, signed result; a rerun is a new row, never an edit
--   certificate_withdrawals  certificates whose rows were erased (POPIA request), so verify can say so
--
-- What the browser can call with the publishable key (the tables stay closed to it)
--   submit_self_check(p jsonb)        score a Decision Maturity Self-Check, store it, sign it, diff it
--   get_assessment_series(p_key text) reload a respondent's own runs with their private key
--   verify_certificate(p_cert_id)     public check of a certificate ID
-- Service role only
--   issue_engagement_certificate(p)   certify a delivered Decision Maturity Assessment (assessment-kit/certify.py)
--   erase_assessment_series(code, reason)  POPIA erasure that leaves a withdrawal notice behind
--
-- Signing: payload_text is the exact text that is hashed (SHA-256, public) and signed
-- (HMAC-SHA-256 with a secret held in Supabase Vault as 'ingqiqo_certificate_hmac').
-- Anyone can recompute the hash; only this database can confirm the signature.

create extension if not exists pgcrypto;

-- ---------------------------------------------------------------- tables

create table if not exists public.assessment_series (
  id uuid primary key default gen_random_uuid(),
  code text not null unique,                 -- public, in every certificate ID of the series
  instrument text not null check (instrument in ('dmsc', 'dma')),
  lead_id uuid references public.leads(id) on delete cascade,
  organisation text,
  access_key_sha256 text unique,             -- SHA-256 of the holder's private key
  created_at timestamptz not null default now()
);
create index if not exists assessment_series_lead_idx on public.assessment_series (lead_id);

create table if not exists public.assessment_runs (
  id uuid primary key default gen_random_uuid(),
  series_id uuid not null references public.assessment_series(id) on delete cascade,
  version integer not null check (version >= 1),
  cert_id text not null unique,
  instrument text not null,
  instrument_version text not null,
  basis text not null check (basis in ('self-reported', 'assessor-scored')),
  subject jsonb not null,
  answers jsonb not null,
  scores jsonb not null,
  diff jsonb,
  consent jsonb not null,
  payload_text text not null,
  payload_sha256 text not null,
  signature text not null,
  issued_at timestamptz not null default now(),
  unique (series_id, version)
);

create table if not exists public.certificate_withdrawals (
  cert_id text primary key,
  withdrawn_at timestamptz not null default now(),
  reason text not null
);

alter table public.assessment_series enable row level security;
alter table public.assessment_runs enable row level security;
alter table public.certificate_withdrawals enable row level security;
revoke all on public.assessment_series, public.assessment_runs, public.certificate_withdrawals from anon, authenticated;

-- A result is never edited. A rerun is a new version. Erasure deletes and leaves a withdrawal notice.
create or replace function public.assessment_immutable() returns trigger
language plpgsql as $$
begin
  raise exception 'assessment results are immutable; submit a new version instead';
end $$;

drop trigger if exists assessment_runs_no_update on public.assessment_runs;
create trigger assessment_runs_no_update before update on public.assessment_runs
  for each row execute function public.assessment_immutable();
drop trigger if exists assessment_series_no_update on public.assessment_series;
create trigger assessment_series_no_update before update on public.assessment_series
  for each row execute function public.assessment_immutable();

create or replace function public.assessment_note_withdrawal() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  insert into public.certificate_withdrawals (cert_id, reason)
  values (old.cert_id, coalesce(current_setting('ingqiqo.withdrawal_reason', true), 'Erased at the holder''s request'))
  on conflict (cert_id) do nothing;
  return old;
end $$;

drop trigger if exists assessment_runs_withdrawal on public.assessment_runs;
create trigger assessment_runs_withdrawal after delete on public.assessment_runs
  for each row execute function public.assessment_note_withdrawal();

-- ---------------------------------------------------------------- reference data

-- The fifteen criteria of scoring-rubric.md v1.0, in order.
create or replace function public.dma_criteria()
returns table (id text, stage text, stage_name text, label text, ord int)
language sql immutable as $$
  values
    ('F1','F','Framing','Answerable question',1), ('F2','F','Framing','Scope and out-of-scope',2),
    ('F3','F','Framing','Stakes and reversibility',3),
    ('E1','E','Evidence','Sourced claims',4), ('E2','E','Evidence','Epistemic kind',5),
    ('E3','E','Evidence','Terms and meaning',6),
    ('J1','J','Judgement','Authority and limits',7), ('J2','J','Judgement','Options and rationale',8),
    ('J3','J','Judgement','Predicted outcome',9),
    ('X1','X','Execution','Conditions',10), ('X2','X','Execution','Stop rule',11),
    ('X3','X','Execution','Fidelity',12),
    ('R1','R','Review','Review happened',13), ('R2','R','Review','Independence',14),
    ('R3','R','Review','Decision quality vs outcome',15)
$$;

create or replace function public.dma_stages()
returns table (stage text, stage_name text, ord int)
language sql immutable as $$
  values ('F','Framing',1), ('E','Evidence',2), ('J','Judgement',3), ('X','Execution',4), ('R','Review',5)
$$;

create or replace function public.dma_evidence_cap(p_evidence text) returns int
language sql immutable as $$
  select case p_evidence when 'artefact' then 3 when 'policy' then 2 when 'testimony' then 1 else 0 end
$$;

-- Crockford base32, no I L O U.
create or replace function public.assessment_code(p_len int) returns text
language sql volatile as $$
  select string_agg(substr('0123456789ABCDEFGHJKMNPQRSTVWXYZ', (get_byte(b, i) % 32) + 1, 1), '' order by i)
  from (select gen_random_bytes(p_len) b) r, generate_series(0, p_len - 1) i
$$;

create or replace function public.assessment_hmac(p_text text) returns text
language plpgsql stable security definer set search_path = public, vault as $$
declare v_secret text;
begin
  select decrypted_secret into v_secret from vault.decrypted_secrets where name = 'ingqiqo_certificate_hmac';
  if v_secret is null then raise exception 'certificate signing key is not configured'; end if;
  return encode(hmac(convert_to(p_text, 'UTF8'), convert_to(v_secret, 'UTF8'), 'sha256'), 'hex');
end $$;

-- ---------------------------------------------------------------- scoring (self-check)

-- answers: {"F1": {"level": 0..3, "evidence": "artefact|policy|testimony|none", "unknown": false}, ...}
-- Returns {"criteria": {...}, "stages": {...}, "weakest": [...], "profile": "..."}.
create or replace function public.score_self_check(p_answers jsonb) returns jsonb
language plpgsql immutable as $$
declare
  c record; s record;
  v_level int; v_ev text; v_unknown boolean; v_score int;
  v_criteria jsonb := '{}'; v_stages jsonb := '{}';
  v_scores int[]; v_el int; v_min int; v_med numeric; v_weak int := 99; v_weakest jsonb := '[]';
  v_profile text := '';
begin
  for c in select * from public.dma_criteria() order by ord loop
    if not (p_answers ? c.id) then raise exception 'missing answer for %', c.id; end if;
    v_unknown := coalesce((p_answers -> c.id ->> 'unknown')::boolean, false);
    if v_unknown then
      v_level := 0; v_ev := 'none';
    else
      v_level := (p_answers -> c.id ->> 'level')::int;
      v_ev := p_answers -> c.id ->> 'evidence';
      if v_level is null or v_level not between 0 and 3 then raise exception 'level for % must be 0 to 3', c.id; end if;
      if v_ev is null or v_ev not in ('artefact', 'policy', 'testimony', 'none') then
        raise exception 'evidence for % must be artefact, policy, testimony or none', c.id; end if;
    end if;
    v_score := least(v_level, public.dma_evidence_cap(v_ev));
    v_criteria := v_criteria || jsonb_build_object(c.id, jsonb_build_object(
      'label', c.label, 'stage', c.stage, 'level', v_level, 'evidence', v_ev,
      'score', v_score, 'capped', v_level > v_score, 'el', v_unknown));
  end loop;

  for s in select * from public.dma_stages() order by ord loop
    -- Unknown answers are a limitation, not a failure: the stage is scored on what is known,
    -- and when more than a third of it is unknown it is not scored at all (rubric section 3).
    select array_agg((v ->> 'score')::int order by k) filter (where not (v ->> 'el')::boolean),
           count(*) filter (where (v ->> 'el')::boolean)
      into v_scores, v_el
      from jsonb_each(v_criteria) as e(k, v) where v ->> 'stage' = s.stage;
    if v_el * 3 > 3 then
      v_stages := v_stages || jsonb_build_object(s.stage, jsonb_build_object(
        'name', s.stage_name, 'status', 'insufficient', 'score', null, 'median', null, 'el', v_el));
      v_profile := v_profile || s.stage || '?';
    else
      select min(x), percentile_cont(0.5) within group (order by x) into v_min, v_med from unnest(v_scores) x;
      v_stages := v_stages || jsonb_build_object(s.stage, jsonb_build_object(
        'name', s.stage_name, 'status', 'scored', 'score', v_min, 'median', v_med, 'el', v_el));
      v_profile := v_profile || s.stage || v_min;
      if v_min < v_weak then v_weak := v_min; v_weakest := jsonb_build_array(s.stage);
      elsif v_min = v_weak then v_weakest := v_weakest || to_jsonb(s.stage); end if;
    end if;
    v_profile := v_profile || ' ';
  end loop;

  return jsonb_build_object('criteria', v_criteria, 'stages', v_stages,
                            'weakest', v_weakest, 'profile', rtrim(v_profile));
end $$;

-- ---------------------------------------------------------------- diff between versions

-- p_prev / p_cur: {"scores": ..., "subject": ..., "issued_at": ..., "cert_id": ..., "version": n}
create or replace function public.assessment_diff(p_instrument text, p_prev jsonb, p_cur jsonb)
returns jsonb language plpgsql immutable as $$
declare
  c record; s record;
  pc jsonb; cc jsonb; ps jsonb; cs jsonb;
  v_items jsonb := '[]'; v_stage_items jsonb := '[]'; v_summary jsonb := '[]';
  v_impl text; v_change text; v_changed int := 0;
  v_engagement boolean := p_instrument = 'dma';
  v_prev_weak text; v_cur_weak text; v_days int;
  v_prev_label text; v_cur_label text;
begin
  for c in select * from public.dma_criteria() order by ord loop
    pc := p_prev -> 'scores' -> 'criteria' -> c.id;
    cc := p_cur -> 'scores' -> 'criteria' -> c.id;
    if pc is null or cc is null then continue; end if;
    v_impl := null; v_change := null;

    if v_engagement then
      if (cc ->> 'score')::numeric > (pc ->> 'score')::numeric then
        v_change := 'up'; v_impl := 'The median across the sampled decisions rose. Confirm the gain shows in decisions outside the sample before generalising it.';
      elsif (cc ->> 'score')::numeric < (pc ->> 'score')::numeric then
        v_change := 'down'; v_impl := 'The median across the sampled decisions fell. Check whether the sample changed character before reading this as a decline in practice.';
      elsif coalesce((cc ->> 'el_count')::int, 0) <> coalesce((pc ->> 'el_count')::int, 0) then
        v_change := 'evidence'; v_impl := case when (cc ->> 'el_count')::int > (pc ->> 'el_count')::int
          then 'More of this criterion went unevidenced this time. Treat it as a limitation to close, not as failure.'
          else 'Fewer evidence gaps on this criterion than last time.' end;
      end if;
    elsif coalesce((pc ->> 'el')::boolean, false) and not coalesce((cc ->> 'el')::boolean, false) then
      v_change := 'answered'; v_impl := 'Answered now where it was unknown before. Check that the new answer rests on something you have seen.';
    elsif not coalesce((pc ->> 'el')::boolean, false) and coalesce((cc ->> 'el')::boolean, false) then
      v_change := 'unknown'; v_impl := 'Answered before, unknown now. Treat it as an evidence gap to close, not as a decline.';
    elsif (cc ->> 'score')::int > (pc ->> 'score')::int then
      v_change := 'up';
      v_impl := case cc ->> 'evidence'
        when 'artefact' then 'Improvement backed by a record from the decision itself. It should survive an independent check.'
        when 'policy' then 'A rule now requires this. The next test is whether it appears in the records of actual decisions.'
        when 'testimony' then 'Practice is reported but not recorded. Capture the record so the gain holds under scrutiny.'
        else 'Score rose.' end;
    elsif (cc ->> 'score')::int < (pc ->> 'score')::int then
      v_change := 'down';
      v_impl := case when (cc ->> 'level')::int < (pc ->> 'level')::int
        then 'Score fell. Either practice weakened or the earlier answer was generous. Establish which before acting on it.'
        else 'The same practice is claimed on weaker evidence, so the evidence cap lowered the score. Find the record.' end;
    elsif (cc ->> 'level')::int > (pc ->> 'level')::int then
      v_change := 'claimed'; v_impl := 'Practice is claimed to have improved, but the evidence cap holds the score. The claim is not yet evidenced.';
    elsif (cc ->> 'level')::int < (pc ->> 'level')::int then
      v_change := 'claimed'; v_impl := 'The claimed level fell, but the evidenced score is unchanged because the cap was already binding.';
    elsif (cc ->> 'evidence') is distinct from (pc ->> 'evidence') then
      v_change := 'evidence'; v_impl := 'The evidence behind this answer changed without moving the score.';
    end if;

    if v_change is not null then
      v_changed := v_changed + 1;
      v_items := v_items || jsonb_build_object('id', c.id, 'label', c.label, 'stage', c.stage,
        'change', v_change, 'before', pc - 'label' - 'stage', 'after', cc - 'label' - 'stage', 'implication', v_impl);
    end if;
  end loop;

  for s in select * from public.dma_stages() order by ord loop
    ps := p_prev -> 'scores' -> 'stages' -> s.stage;
    cs := p_cur -> 'scores' -> 'stages' -> s.stage;
    if ps is null or cs is null then continue; end if;
    v_impl := null;
    if ps ->> 'status' = 'scored' and cs ->> 'status' = 'insufficient' then
      v_impl := format('%s can no longer be scored: too many of its criteria lack evidence.', s.stage_name);
    elsif ps ->> 'status' = 'insufficient' and cs ->> 'status' = 'scored' then
      v_impl := format('%s can now be scored, at %s.', s.stage_name, cs ->> 'score');
    elsif (cs ->> 'score')::numeric > (ps ->> 'score')::numeric then
      v_impl := format('%s rose from %s to %s: its weakest criterion improved.', s.stage_name, ps ->> 'score', cs ->> 'score');
    elsif (cs ->> 'score')::numeric < (ps ->> 'score')::numeric then
      v_impl := format('%s fell from %s to %s. A stage is only as strong as its weakest record, so look at which criterion moved.', s.stage_name, ps ->> 'score', cs ->> 'score');
    end if;
    if v_impl is not null then
      v_stage_items := v_stage_items || jsonb_build_object('stage', s.stage, 'name', s.stage_name,
        'before', ps - 'name', 'after', cs - 'name', 'implication', v_impl);
    end if;
  end loop;

  v_days := (((p_cur ->> 'issued_at')::timestamptz)::date - ((p_prev ->> 'issued_at')::timestamptz)::date);
  v_summary := v_summary || to_jsonb(format('Compared with version %s, issued %s.', p_prev ->> 'version',
    case v_days when 0 then 'the same day' when 1 then 'the day before' else v_days || ' days earlier' end));

  if v_changed = 0 then
    v_summary := v_summary || to_jsonb('No answers changed in a way that matters to the rubric. The profile is unchanged.'::text);
  else
    v_summary := v_summary || to_jsonb(format('%s of 15 criteria changed.', v_changed));
  end if;

  select regexp_replace(string_agg(d.stage_name, ', ' order by d.ord), ', ([^,]+)$', ' and \1') into v_prev_weak
    from jsonb_array_elements_text(p_prev -> 'scores' -> 'weakest') w(k) join public.dma_stages() d on d.stage = w.k;
  select regexp_replace(string_agg(d.stage_name, ', ' order by d.ord), ', ([^,]+)$', ' and \1') into v_cur_weak
    from jsonb_array_elements_text(p_cur -> 'scores' -> 'weakest') w(k) join public.dma_stages() d on d.stage = w.k;
  if v_cur_weak is not null and v_prev_weak is distinct from v_cur_weak then
    v_summary := v_summary || to_jsonb(format('Your weakest point moved from %s to %s. That is where to act next.', coalesce(v_prev_weak, 'none scored'), v_cur_weak));
  elsif v_cur_weak is not null then
    v_summary := v_summary || to_jsonb(format('Your weakest point is still %s.', v_cur_weak));
  end if;

  v_prev_label := lower(btrim(coalesce(p_prev -> 'subject' ->> 'decision', '')));
  v_cur_label := lower(btrim(coalesce(p_cur -> 'subject' ->> 'decision', '')));
  if not v_engagement and v_prev_label <> v_cur_label then
    v_summary := v_summary || to_jsonb('You assessed a different decision this time. Differences may reflect the decision chosen rather than a change in practice.'::text);
  end if;
  if not v_engagement then
    v_summary := v_summary || to_jsonb('Both versions are self-reported. A change in answers is a change in what was reported, not yet a verified change in practice.'::text);
  end if;

  return jsonb_build_object('from_version', (p_prev ->> 'version')::int, 'from_cert_id', p_prev ->> 'cert_id',
    'days_between', v_days, 'changed', v_changed, 'summary', v_summary,
    'stages', v_stage_items, 'criteria', v_items);
end $$;

-- ---------------------------------------------------------------- issuing

create or replace function public.assessment_issue(
  p_series public.assessment_series, p_instrument_version text, p_basis text,
  p_subject jsonb, p_answers jsonb, p_scores jsonb, p_consent jsonb
) returns public.assessment_runs
language plpgsql security definer set search_path = public as $$
declare
  v_prev public.assessment_runs; v_version int; v_cert text; v_now timestamptz := now();
  v_diff jsonb; v_payload jsonb; v_text text; v_run public.assessment_runs;
  v_name text := case p_series.instrument when 'dmsc' then 'Decision Maturity Self-Check' else 'Decision Maturity Assessment' end;
begin
  perform pg_advisory_xact_lock(hashtext(p_series.id::text));
  select * into v_prev from public.assessment_runs where series_id = p_series.id order by version desc limit 1;
  v_version := coalesce(v_prev.version, 0) + 1;
  v_cert := format('IQX-%s-%s-V%s', upper(p_series.instrument), p_series.code, v_version);

  if v_prev.id is not null then
    v_diff := public.assessment_diff(p_series.instrument,
      jsonb_build_object('scores', v_prev.scores, 'subject', v_prev.subject, 'issued_at', v_prev.issued_at,
                         'cert_id', v_prev.cert_id, 'version', v_prev.version),
      jsonb_build_object('scores', p_scores, 'subject', p_subject, 'issued_at', v_now,
                         'cert_id', v_cert, 'version', v_version));
  end if;

  v_payload := jsonb_build_object(
    'certificate', jsonb_build_object(
      'id', v_cert,
      'issuer', 'Ingqiqo Executables (Pty) Ltd',
      'instrument', jsonb_build_object('id', p_series.instrument, 'name', v_name, 'version', p_instrument_version),
      'series', p_series.code,
      'version', v_version,
      'supersedes', v_prev.cert_id,
      'issued_at', to_char(v_now at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
      'basis', p_basis,
      'statement', case p_basis
        when 'self-reported' then 'Ingqiqo Executables certifies that this report was issued by its instrument from the answers shown, scored under the rubric version shown, on the date shown. The answers are self-reported and have not been verified by Ingqiqo. This is not a certification of the organisation''s decision maturity.'
        else 'Ingqiqo Executables certifies that this report was issued by its assessor from the evidence-scored workbook shown, under the rubric version shown, on the date shown. Scores reflect the sampled decisions and the evidence provided; they are not a certification of the organisation as a whole.' end,
      'verify', 'https://nmbambo.github.io/verify/?id=' || v_cert),
    'subject', p_subject,
    'answers', p_answers,
    'scores', p_scores,
    'diff', v_diff);
  v_text := v_payload::text;

  insert into public.assessment_runs (series_id, version, cert_id, instrument, instrument_version, basis,
      subject, answers, scores, diff, consent, payload_text, payload_sha256, signature, issued_at)
  values (p_series.id, v_version, v_cert, p_series.instrument, p_instrument_version, p_basis,
      p_subject, p_answers, p_scores, v_diff, p_consent, v_text,
      encode(digest(convert_to(v_text, 'UTF8'), 'sha256'), 'hex'), public.assessment_hmac(v_text), v_now)
  returning * into v_run;
  return v_run;
end $$;

create or replace function public.assessment_run_json(r public.assessment_runs) returns jsonb
language sql stable as $$
  select jsonb_build_object('cert_id', r.cert_id, 'version', r.version, 'issued_at', r.issued_at,
    'payload', r.payload_text, 'sha256', r.payload_sha256, 'signature', r.signature)
$$;

-- p: {"key": null | "<private key>", "contact": {"name","email","organisation","role"},
--     "consent": {"processing": true, "contact": bool, "text_version": "..."},
--     "decision": "label", "answers": {...}, "context": {"page","referrer","utm_source","utm_medium","utm_campaign","ua"},
--     "hp": ""}
create or replace function public.submit_self_check(p jsonb) returns jsonb
language plpgsql security definer set search_path = public as $$
declare
  v_email text := lower(btrim(p -> 'contact' ->> 'email'));
  v_name text := left(btrim(p -> 'contact' ->> 'name'), 120);
  v_org text := left(btrim(p -> 'contact' ->> 'organisation'), 160);
  v_role text := left(btrim(p -> 'contact' ->> 'role'), 120);
  v_decision text := left(btrim(p ->> 'decision'), 200);
  v_key text := nullif(btrim(p ->> 'key'), '');
  v_new_key text;
  v_lead uuid; v_series public.assessment_series; v_run public.assessment_runs;
  v_scores jsonb; v_answers jsonb := '{}'; v_consent jsonb; c record; v_recent int;
begin
  if coalesce(p ->> 'hp', '') <> '' then raise exception 'rejected'; end if;
  if v_email is null or v_email !~ '^[^@\s]+@[^@\s]+\.[^@\s]+$' or length(v_email) > 160 then
    raise exception 'a valid email address is required'; end if;
  if coalesce(v_name, '') = '' then raise exception 'your name is required'; end if;
  if coalesce(v_org, '') = '' then raise exception 'your organisation is required'; end if;
  if coalesce(v_decision, '') = '' then raise exception 'name the decision you assessed'; end if;
  if coalesce((p -> 'consent' ->> 'processing')::boolean, false) is not true then
    raise exception 'consent to process your answers is required to issue a report'; end if;

  -- keep only the fifteen known answers, with only the known fields
  for c in select id from public.dma_criteria() loop
    v_answers := v_answers || jsonb_build_object(c.id, jsonb_build_object(
      'level', (p -> 'answers' -> c.id -> 'level'),
      'evidence', (p -> 'answers' -> c.id -> 'evidence'),
      'unknown', coalesce((p -> 'answers' -> c.id ->> 'unknown')::boolean, false)));
  end loop;
  v_scores := public.score_self_check(v_answers);

  v_lead := public.capture_lead(v_email, v_name, v_org,
    coalesce((p -> 'consent' ->> 'contact')::boolean, false), null,
    'assessment', 'Decision Maturity Self-Check',
    format('Self-check of "%s" · profile %s', v_decision, v_scores ->> 'profile'),
    left(p -> 'context' ->> 'page', 300), left(p -> 'context' ->> 'referrer', 300),
    left(p -> 'context' ->> 'utm_source', 100), left(p -> 'context' ->> 'utm_medium', 100),
    left(p -> 'context' ->> 'utm_campaign', 100), left(p -> 'context' ->> 'ua', 300));

  if v_key is not null then
    select * into v_series from public.assessment_series
     where access_key_sha256 = encode(digest(v_key, 'sha256'), 'hex') and instrument = 'dmsc';
    if v_series.id is null then raise exception 'that rerun key is not recognised'; end if;
    select count(*) into v_recent from public.assessment_runs
     where series_id = v_series.id and issued_at > now() - interval '1 hour';
    if v_recent >= 10 then raise exception 'too many versions in the last hour; try again later'; end if;
  else
    select count(*) into v_recent from public.assessment_series
     where lead_id = v_lead and created_at > now() - interval '1 hour';
    if v_recent >= 5 then raise exception 'too many new assessments in the last hour; try again later'; end if;
    v_new_key := encode(gen_random_bytes(18), 'hex');
    insert into public.assessment_series (code, instrument, lead_id, organisation, access_key_sha256)
    values (public.assessment_code(8), 'dmsc', v_lead, v_org, encode(digest(v_new_key, 'sha256'), 'hex'))
    returning * into v_series;
  end if;

  v_consent := jsonb_build_object(
    'processing', true,
    'processing_purpose', 'To score your answers, issue and store your certified report and its later versions, and let you or anyone you share the certificate ID with verify it.',
    'contact', coalesce((p -> 'consent' ->> 'contact')::boolean, false),
    'contact_purpose', 'For Ingqiqo Executables to contact you about your results and related services. You can withdraw at any time.',
    'text_version', left(p -> 'consent' ->> 'text_version', 60),
    'given_at', now(), 'lead_id', v_lead);

  v_run := public.assessment_issue(v_series, 'DMA rubric 1.0 · self-check 1.0', 'self-reported',
    jsonb_build_object('organisation', v_org, 'role', nullif(v_role, ''), 'decision', v_decision),
    v_answers, v_scores, v_consent);

  return public.assessment_run_json(v_run) || jsonb_build_object('key', v_new_key, 'series', v_series.code);
end $$;

create or replace function public.get_assessment_series(p_key text) returns jsonb
language plpgsql stable security definer set search_path = public as $$
declare v_series public.assessment_series; v_runs jsonb;
begin
  select * into v_series from public.assessment_series
   where access_key_sha256 = encode(digest(btrim(coalesce(p_key, '')), 'sha256'), 'hex');
  if v_series.id is null then return null; end if;
  select coalesce(jsonb_agg(public.assessment_run_json(r) order by r.version), '[]') into v_runs
    from public.assessment_runs r where r.series_id = v_series.id;
  return jsonb_build_object('series', v_series.code, 'instrument', v_series.instrument,
                            'organisation', v_series.organisation, 'runs', v_runs);
end $$;

create or replace function public.verify_certificate(p_cert_id text) returns jsonb
language plpgsql stable security definer set search_path = public as $$
declare v_id text := upper(btrim(coalesce(p_cert_id, ''))); r public.assessment_runs; w public.certificate_withdrawals; v_latest text;
begin
  select * into r from public.assessment_runs where cert_id = v_id;
  if r.id is null then
    select * into w from public.certificate_withdrawals where cert_id = v_id;
    if w.cert_id is not null then
      return jsonb_build_object('status', 'withdrawn', 'cert_id', w.cert_id, 'withdrawn_at', w.withdrawn_at, 'reason', w.reason);
    end if;
    return jsonb_build_object('status', 'unknown', 'cert_id', v_id);
  end if;
  select cert_id into v_latest from public.assessment_runs where series_id = r.series_id order by version desc limit 1;
  return jsonb_build_object(
    'status', 'valid',
    'cert_id', r.cert_id,
    'signature_valid', public.assessment_hmac(r.payload_text) = r.signature,
    'sha256', r.payload_sha256,
    'issued_at', r.issued_at,
    'instrument', r.payload_text::jsonb -> 'certificate' -> 'instrument',
    'basis', r.basis,
    'version', r.version,
    'latest_cert_id', v_latest,
    'organisation', r.subject ->> 'organisation',
    'decision', r.subject ->> 'decision',
    'engagement', r.subject ->> 'engagement',
    'profile', r.scores ->> 'profile');
end $$;

-- Delivered engagements. Called by assessment-kit/certify.py with the service-role key.
-- p: {"series": null | "<code>", "contact": {"email","name"}, "organisation": "...",
--     "engagement": "label", "period": "...", "decisions_sampled": n, "assessor": "...",
--     "answers": [workbook rows], "scores": {"criteria": {...}, "stages": {...}, "weakest": [...], "profile": "..."}}
create or replace function public.issue_engagement_certificate(p jsonb) returns jsonb
language plpgsql security definer set search_path = public as $$
declare v_series public.assessment_series; v_run public.assessment_runs; v_lead uuid; v_key text;
begin
  if nullif(p ->> 'series', '') is not null then
    select * into v_series from public.assessment_series where code = upper(p ->> 'series') and instrument = 'dma';
    if v_series.id is null then raise exception 'unknown engagement series %', p ->> 'series'; end if;
  else
    v_lead := public.capture_lead(lower(p -> 'contact' ->> 'email'), p -> 'contact' ->> 'name', p ->> 'organisation',
      false, null, 'engagement', 'Decision Maturity Assessment', p ->> 'engagement', null, null, null, null, null, null);
    v_key := encode(gen_random_bytes(18), 'hex');
    insert into public.assessment_series (code, instrument, lead_id, organisation, access_key_sha256)
    values (public.assessment_code(8), 'dma', v_lead, p ->> 'organisation', encode(digest(v_key, 'sha256'), 'hex'))
    returning * into v_series;
  end if;
  v_run := public.assessment_issue(v_series, 'DMA rubric 1.0', 'assessor-scored',
    jsonb_build_object('organisation', p ->> 'organisation', 'engagement', p ->> 'engagement',
      'period', p ->> 'period', 'decisions_sampled', (p ->> 'decisions_sampled')::int, 'assessor', p ->> 'assessor'),
    p -> 'answers', p -> 'scores',
    jsonb_build_object('basis', 'contract', 'note', 'Processed under the engagement scope letter.', 'given_at', now()));
  return public.assessment_run_json(v_run) || jsonb_build_object('key', v_key, 'series', v_series.code);
end $$;

create or replace function public.erase_assessment_series(p_code text, p_reason text default null) returns int
language plpgsql security definer set search_path = public as $$
declare v_n int;
begin
  perform set_config('ingqiqo.withdrawal_reason', coalesce(p_reason, 'Erased at the holder''s request'), true);
  delete from public.assessment_runs where series_id = (select id from public.assessment_series where code = upper(p_code));
  get diagnostics v_n = row_count;
  delete from public.assessment_series where code = upper(p_code);
  return v_n;
end $$;

-- ---------------------------------------------------------------- grants

revoke all on function public.submit_self_check(jsonb), public.get_assessment_series(text),
  public.verify_certificate(text), public.issue_engagement_certificate(jsonb),
  public.erase_assessment_series(text, text), public.assessment_issue(public.assessment_series, text, text, jsonb, jsonb, jsonb, jsonb),
  public.assessment_hmac(text), public.assessment_note_withdrawal(), public.score_self_check(jsonb),
  public.assessment_diff(text, jsonb, jsonb), public.assessment_run_json(public.assessment_runs)
  from public, anon, authenticated;
grant execute on function public.submit_self_check(jsonb), public.get_assessment_series(text),
  public.verify_certificate(text) to anon, authenticated, service_role;
grant execute on function public.issue_engagement_certificate(jsonb), public.erase_assessment_series(text, text)
  to service_role;
