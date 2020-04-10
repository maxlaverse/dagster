# pylint: disable=protected-access

import os
import subprocess

from sqlalchemy import create_engine

from dagster import seven
from dagster.core.instance import DagsterInstance
from dagster.core.storage.event_log.migration import migrate_event_log_data
from dagster.core.storage.event_log.sql_event_log import SqlEventLogStorage
from dagster.utils import file_relative_path


def test_0_6_6_postgres(hostname, conn_string):
    # Init a fresh postgres with a 0.6.6 snapshot
    engine = create_engine(conn_string)
    engine.execute('drop schema public cascade;')
    engine.execute('create schema public;')

    env = os.environ.copy()
    env['PGPASSWORD'] = 'test'
    subprocess.check_call(
        [
            'psql',
            '-h',
            hostname,
            '-p',
            '5432',
            '-U',
            'test',
            '-f',
            file_relative_path(__file__, 'snapshot_0_6_6/postgres/pg_dump.txt'),
        ],
        env=env,
    )

    run_id = '089287c5-964d-44c0-b727-357eb7ba522e'

    with seven.TemporaryDirectory() as tempdir:
        # Create the dagster.yaml
        with open(file_relative_path(__file__, 'dagster.yaml'), 'r') as template_fd:
            with open(os.path.join(tempdir, 'dagster.yaml'), 'w') as target_fd:
                template = template_fd.read().format(hostname=hostname)
                target_fd.write(template)

        instance = DagsterInstance.from_config(tempdir)

        # Runs will appear in DB, but event logs need migration
        runs = instance.get_runs()
        assert len(runs) == 1
        assert instance.get_run_by_id(run_id)

        assert instance.all_logs(run_id) == []

        # Post migration, event logs appear in DB
        instance.upgrade()

        runs = instance.get_runs()
        assert len(runs) == 1
        assert instance.get_run_by_id(run_id)

        assert len(instance.all_logs(run_id)) == 89


def test_0_7_6_postgres_pre_event_log_migration(hostname, conn_string):
    engine = create_engine(conn_string)
    engine.execute('drop schema public cascade;')
    engine.execute('create schema public;')

    env = os.environ.copy()
    env['PGPASSWORD'] = 'test'
    subprocess.check_call(
        [
            'psql',
            '-h',
            hostname,
            '-p',
            '5432',
            '-U',
            'test',
            '-f',
            file_relative_path(
                __file__, 'snapshot_0_7_6_pre_event_log_migration/postgres/pg_dump.txt'
            ),
        ],
        env=env,
    )

    run_id = 'ca7f1e33-526d-4f75-9bc5-3e98da41ab97'

    with seven.TemporaryDirectory() as tempdir:
        with open(file_relative_path(__file__, 'dagster.yaml'), 'r') as template_fd:
            with open(os.path.join(tempdir, 'dagster.yaml'), 'w') as target_fd:
                template = template_fd.read().format(hostname=hostname)
                target_fd.write(template)

        instance = DagsterInstance.from_config(tempdir)

        # Runs will appear in DB, but event logs need migration
        runs = instance.get_runs()
        assert len(runs) == 1
        assert instance.get_run_by_id(run_id)

        # Make sure the schema is migrated
        instance.upgrade()

        assert isinstance(instance._event_storage, SqlEventLogStorage)
        events_by_id = instance._event_storage.get_logs_for_run_by_log_id(run_id)
        assert len(events_by_id) == 40

        step_key_records = []
        for record_id, _event in events_by_id.items():
            row_data = instance._event_storage.get_event_log_table_data(run_id, record_id)
            if row_data.step_key is not None:
                step_key_records.append(row_data)
        assert len(step_key_records) == 0

        # run the event_log data migration
        migrate_event_log_data(instance=instance)

        step_key_records = []
        for record_id, _event in events_by_id.items():
            row_data = instance._event_storage.get_event_log_table_data(run_id, record_id)
            if row_data.step_key is not None:
                step_key_records.append(row_data)
        assert len(step_key_records) > 0
