-- Run this once in Supabase: SQL Editor > New query > paste > Run
create table if not exists public.sessions (
  id            bigint generated always as identity primary key,
  created_at    timestamptz not null default now(),
  player_id     uuid not null,
  name          text not null check (char_length(name) between 1 and 24),
  ended         text not null check (char_length(ended) <= 25),
  elapsed_sec   int  not null check (elapsed_sec between 600 and 86400),
  start_level   int  check (start_level between 0 and 99),
  levels_gained int  default 0 check (levels_gained between 0 and 10),
  pct_per_hr    real not null check (pct_per_hr > 0 and pct_per_hr <= 400),
  exp_per_hr    bigint default 0 check (exp_per_hr >= 0),
  meso_per_hr   bigint default 0 check (meso_per_hr >= 0),
  class         text check (char_length(class) <= 30),
  map           text check (char_length(map) <= 80),
  route         text check (char_length(route) <= 300),
  party_size    int  check (party_size between 1 and 6),
  notes         text check (char_length(notes) <= 500),
  unique (player_id, ended)
);
alter table public.sessions enable row level security;
create policy "anyone can read"   on public.sessions for select using (true);
create policy "anyone can submit" on public.sessions for insert with check (true);
-- no update/delete policies: rows can't be edited from the browser. Moderate from the Supabase dashboard.
create index if not exists sessions_created_idx on public.sessions (created_at desc);
