-- ============================================================================
-- neutralization.sql — Neutralización de una base de staging (Fase 7)
-- ============================================================================
-- Se ejecuta vía SSM con psql, inmediatamente después de restaurar la base de
-- producción en el entorno de staging. Evita efectos hacia sistemas externos
-- (correo, pagos, webhooks) y reapunta la URL pública al subdominio de staging.
--
-- Token reemplazado por environment._render_neutralization_sql():
--   %%STAGING_URL%%   URL pública del staging (ej.: https://staging.forum.primate.cloud)
--
-- NOTA (delta D5): el SQL original de la spec técnica §7.3 tenía sentencias
-- frágiles. Correcciones aplicadas:
--   * El filtro de crons por nombre de modelo era erróneo → se desactivan TODOS
--     los crons (lo más seguro para un staging; se reactivan a mano si hace falta).
--   * El parámetro catchall NO bloquea el envío → se elimina; el envío se corta
--     desactivando los servidores de correo saliente (sin servidor, Odoo no envía).
--   * Cada sentencia se envuelve en un guard `to_regclass(...)` para tolerar que
--     el módulo/tabla no esté instalado (no aborta el script).
-- ============================================================================

\set ON_ERROR_STOP on
BEGIN;

-- 1. Desactivar servidores de correo SALIENTE (corta el envío real).
DO $$ BEGIN
    IF to_regclass('public.ir_mail_server') IS NOT NULL THEN
        UPDATE ir_mail_server SET active = false;
    END IF;
END $$;

-- 2. Desactivar servidores de correo ENTRANTE (fetchmail).
DO $$ BEGIN
    IF to_regclass('public.fetchmail_server') IS NOT NULL THEN
        UPDATE fetchmail_server SET active = false;
    END IF;
END $$;

-- 3. Desactivar TODOS los crons (evita procesos automáticos del staging).
DO $$ BEGIN
    IF to_regclass('public.ir_cron') IS NOT NULL THEN
        UPDATE ir_cron SET active = false;
    END IF;
END $$;

-- 4. Desactivar proveedores de pago habilitados (evita cobros reales).
DO $$ BEGIN
    IF to_regclass('public.payment_provider') IS NOT NULL THEN
        UPDATE payment_provider SET state = 'disabled' WHERE state != 'disabled';
    END IF;
END $$;

-- 5. Desactivar automatizaciones / webhooks salientes.
DO $$ BEGIN
    IF to_regclass('public.base_automation') IS NOT NULL THEN
        UPDATE base_automation SET active = false;
    END IF;
END $$;

-- 6. Reapuntar la URL base al subdominio de staging y congelarla.
DO $$ BEGIN
    IF to_regclass('public.ir_config_parameter') IS NOT NULL THEN
        UPDATE ir_config_parameter SET value = '%%STAGING_URL%%' WHERE key = 'web.base.url';
        INSERT INTO ir_config_parameter (key, value)
            SELECT 'web.base.url', '%%STAGING_URL%%'
            WHERE NOT EXISTS (SELECT 1 FROM ir_config_parameter WHERE key = 'web.base.url');
        -- Evita que Odoo reescriba web.base.url con el dominio de producción.
        INSERT INTO ir_config_parameter (key, value)
            SELECT 'web.base.url.freeze', 'True'
            WHERE NOT EXISTS (SELECT 1 FROM ir_config_parameter WHERE key = 'web.base.url.freeze');
        -- Remitente por defecto de staging.
        INSERT INTO ir_config_parameter (key, value)
            SELECT 'mail.default.from', 'staging-noreply@primate.uy'
            WHERE NOT EXISTS (SELECT 1 FROM ir_config_parameter WHERE key = 'mail.default.from');
    END IF;
END $$;

-- 7. Cancelar la cola de correo pendiente heredada de produccion (Fase 8):
--    si alguien reactiva un mail server en el staging, NO deben salir los
--    mails encolados reales.
DO $$ BEGIN
    IF to_regclass('public.mail_mail') IS NOT NULL THEN
        UPDATE mail_mail SET state = 'cancel'
        WHERE state IN ('outgoing', 'exception');
    END IF;
END $$;

-- 8. Invalidar tokens externos (spec 14.3, Fase 8): API keys de Odoo.
--    No se toca totp_secret (dejaria a los usuarios sin poder entrar).
DO $$ BEGIN
    IF to_regclass('public.res_users_apikeys') IS NOT NULL THEN
        DELETE FROM res_users_apikeys;
    END IF;
END $$;

-- 9. Reapuntar el dominio de website al staging (Fase 8): evita que el sitio
--    del staging redirija a produccion.
DO $$ BEGIN
    IF to_regclass('public.website') IS NOT NULL THEN
        UPDATE website SET domain = '%%STAGING_URL%%';
    END IF;
END $$;

COMMIT;
