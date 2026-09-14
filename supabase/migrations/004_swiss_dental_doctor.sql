-- Cambia las opciones de doctor del dashboard a Swiss Dental (Dr. Arturo Ramirez).
delete from public.crm_atributo_opciones
where campo = 'asesor_asignado' and valor <> 'Dr. Arturo Ramirez';

insert into public.crm_atributo_opciones (campo, valor, etiqueta, color, orden)
values ('asesor_asignado', 'Dr. Arturo Ramirez', 'Dr. Arturo Ramirez', null, 1)
on conflict (campo, valor) do nothing;
