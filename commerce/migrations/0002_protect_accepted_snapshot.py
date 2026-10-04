"""Defend accepted commercial facts even against accidental bulk ORM updates."""
from django.db import migrations


def install(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    schema_editor.execute('''
        CREATE FUNCTION commerce_protect_accepted_snapshot() RETURNS trigger AS $$
        BEGIN
          IF ROW(NEW.order_id, NEW.location_id, NEW.currency, NEW.exponent,
                 NEW.total_minor, NEW.snapshot, NEW.snapshot_hash)
             IS DISTINCT FROM
             ROW(OLD.order_id, OLD.location_id, OLD.currency, OLD.exponent,
                 OLD.total_minor, OLD.snapshot, OLD.snapshot_hash) THEN
            RAISE EXCEPTION 'Accepted commerce snapshots are immutable' USING ERRCODE = '23514';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER commerce_accepted_snapshot_immutable
        BEFORE UPDATE ON commerce_acceptedorder
        FOR EACH ROW EXECUTE FUNCTION commerce_protect_accepted_snapshot();
    ''')


def uninstall(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute('DROP TRIGGER IF EXISTS commerce_accepted_snapshot_immutable ON commerce_acceptedorder; DROP FUNCTION IF EXISTS commerce_protect_accepted_snapshot();')


class Migration(migrations.Migration):
    dependencies = [('commerce', '0001_initial')]
    operations = [migrations.RunPython(install, uninstall)]
