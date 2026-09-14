-- Id del contacto en GoHighLevel para no duplicar pacientes al sincronizar.
alter table public.contactos add column if not exists ghl_contact_id text;
create index if not exists contactos_ghl_contact_id_idx on public.contactos (ghl_contact_id);
