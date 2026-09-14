-- Esquema base del demo de clínica dental (proyecto Supabase nuevo).
-- Requiere 001_message_buffer.sql y 002_channel_flags.sql aplicadas antes.
--
-- Tablas que usa la app y que antes venían de un setup previo:
--   n8n_chat_histories (memoria), bot_settings (toggle por conversación),
--   contactos (CRM), crm_atributo_opciones (dropdowns del dashboard),
--   documents + match_documents (RAG opcional de M1).

create extension if not exists vector with schema extensions;

-- ---------------------------------------------------------------------------
-- Memoria conversacional (formato LangChain: session_id + message jsonb)
-- ---------------------------------------------------------------------------
create table if not exists public.n8n_chat_histories (
    id          bigserial primary key,
    session_id  text        not null,
    message     jsonb       not null,
    created_at  timestamptz not null default now()
);

create index if not exists n8n_chat_histories_session_idx
    on public.n8n_chat_histories (session_id, id);

-- ---------------------------------------------------------------------------
-- Toggle del bot por conversación (dashboard + handoff automático)
-- ---------------------------------------------------------------------------
create table if not exists public.bot_settings (
    chat_id       text        primary key,
    channel       text        not null,
    bot_enabled   boolean     not null default true,
    last_read_at  timestamptz,
    updated_at    timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- CRM de pacientes. DEMO: columnas reutilizadas con mapeo semántico
-- (ver app/tools/contactos.py::LEAD_COLUMNS).
-- ---------------------------------------------------------------------------
create table if not exists public.contactos (
    id                 bigserial   primary key,
    created_at         timestamptz not null default now(),
    updated_at         timestamptz not null default now(),
    chat_id            text,
    canal              text,
    handle             text,
    nombre             text,
    correo             text,
    telefono           text,
    zona_interes       text,
    presupuesto_max    numeric,
    tipo_credito       text,
    fecha_visita       timestamptz,
    etapa_seguimiento  text        default 'nuevo',
    notas_internas     text,
    asesor_asignado    text
);

create index if not exists contactos_chat_id_idx on public.contactos (chat_id);
create index if not exists contactos_correo_idx on public.contactos (correo);

comment on table  public.contactos is 'Pacientes del demo de clínica dental';
comment on column public.contactos.zona_interes is 'DEMO dental: motivo de consulta (dolor_urgencia, limpieza_revision, estetica_blanqueamiento, ortodoncia, implantes_protesis, odontopediatria)';
comment on column public.contactos.presupuesto_max is 'DEMO dental: edad del paciente';
comment on column public.contactos.tipo_credito is 'DEMO dental: forma de pago de interés (contado, msi, plan_pagos)';
comment on column public.contactos.fecha_visita is 'DEMO dental: fecha y hora de la cita';
comment on column public.contactos.asesor_asignado is 'DEMO dental: doctor asignado';
comment on column public.contactos.notas_internas is 'Notas del personal. Línea [Bot]: urgencia, tipo de paciente, disponibilidad, tutor, origen';
comment on column public.contactos.etapa_seguimiento is 'nuevo, calificado, cita_agendada, atendido, handoff';

create or replace function public.contactos_touch_updated_at()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists contactos_updated_at on public.contactos;
create trigger contactos_updated_at
    before update on public.contactos
    for each row execute function public.contactos_touch_updated_at();

-- ---------------------------------------------------------------------------
-- Opciones de selects del dashboard
-- ---------------------------------------------------------------------------
create table if not exists public.crm_atributo_opciones (
    id        bigserial primary key,
    campo     text    not null,
    valor     text    not null,
    etiqueta  text    not null,
    color     text,
    orden     integer not null default 0,
    unique (campo, valor)
);

insert into public.crm_atributo_opciones (campo, valor, etiqueta, color, orden) values
    ('zona_interes', 'dolor_urgencia',          'Dolor o urgencia',          '#ef4444', 1),
    ('zona_interes', 'limpieza_revision',       'Limpieza o revisión',       '#22c55e', 2),
    ('zona_interes', 'estetica_blanqueamiento', 'Estética o blanqueamiento', '#a855f7', 3),
    ('zona_interes', 'ortodoncia',              'Ortodoncia',                '#3b82f6', 4),
    ('zona_interes', 'implantes_protesis',      'Implantes o prótesis',      '#f59e0b', 5),
    ('zona_interes', 'odontopediatria',         'Revisión de niño',          '#14b8a6', 6),
    ('tipo_credito', 'contado',                 'Contado',                   '#64748b', 1),
    ('tipo_credito', 'msi',                     'Meses sin intereses',       '#0ea5e9', 2),
    ('tipo_credito', 'plan_pagos',              'Plan de pagos',             '#8b5cf6', 3),
    ('etapa_seguimiento', 'nuevo',              'Nuevo',                     '#94a3b8', 1),
    ('etapa_seguimiento', 'calificado',         'Calificado',                '#3b82f6', 2),
    ('etapa_seguimiento', 'cita_agendada',      'Cita agendada',             '#22c55e', 3),
    ('etapa_seguimiento', 'atendido',           'Atendido',                  '#10b981', 4),
    ('etapa_seguimiento', 'handoff',            'Con especialista',          '#ef4444', 5),
    ('asesor_asignado', 'Dr. Arturo Ramirez',   'Dr. Arturo Ramirez',        null,      1)
on conflict (campo, valor) do nothing;

-- ---------------------------------------------------------------------------
-- RAG opcional de M1 (vacío en el demo: M1 usa app/clinic_profile.py)
-- ---------------------------------------------------------------------------
create table if not exists public.documents (
    id         bigserial primary key,
    content    text,
    metadata   jsonb,
    embedding  extensions.vector(1536)
);

create or replace function public.match_documents(
    query_embedding extensions.vector(1536),
    match_count     integer default 4,
    filter          jsonb   default '{}'
)
returns table (id bigint, content text, metadata jsonb, similarity double precision)
language sql
stable
set search_path = ''
as $$
    select d.id, d.content, d.metadata,
           1 - (d.embedding operator(extensions.<=>) query_embedding) as similarity
    from public.documents d
    where d.metadata @> filter
    order by d.embedding operator(extensions.<=>) query_embedding
    limit match_count;
$$;

-- ---------------------------------------------------------------------------
-- RLS: el backend usa la service key (se salta RLS). Sin policies, la anon
-- key no puede leer ni escribir nada.
-- ---------------------------------------------------------------------------
alter table public.n8n_chat_histories    enable row level security;
alter table public.bot_settings          enable row level security;
alter table public.contactos             enable row level security;
alter table public.crm_atributo_opciones enable row level security;
alter table public.documents             enable row level security;
alter table public.message_buffer        enable row level security;
alter table public.channel_flags         enable row level security;
