from django.db import migrations, models
import django.db.models.deletion


def backfill_task_ingestion_state(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("""
        INSERT INTO live_api_taskingestionstate (
            task_id, competition_id, latest_epoch, point_count, dirty, updated_at
        )
        SELECT
            t.id,
            t.competition_id,
            EXTRACT(EPOCH FROM MAX(p.timestamp))::bigint,
            COUNT(p.id)::integer,
            (
                s.task_id IS NULL
                OR s.processed_epoch IS DISTINCT FROM EXTRACT(EPOCH FROM MAX(p.timestamp))::bigint
                OR s.point_count IS DISTINCT FROM COUNT(p.id)::integer
                OR s.status <> 'ok'
            ) AS dirty,
            now()
        FROM live_api_task t
        JOIN live_api_trackingpoint p ON p.task_id = t.id
        LEFT JOIN live_api_taskresultsnapshot s ON s.task_id = t.id
        GROUP BY t.id, t.competition_id, s.task_id, s.processed_epoch, s.point_count, s.status
        ON CONFLICT (task_id) DO UPDATE SET
            competition_id = EXCLUDED.competition_id,
            latest_epoch = EXCLUDED.latest_epoch,
            point_count = EXCLUDED.point_count,
            dirty = EXCLUDED.dirty,
            updated_at = now()
    """)


class Migration(migrations.Migration):
    dependencies = [("live_api", "0004_result_snapshots")]

    operations = [
        migrations.CreateModel(
            name="TaskIngestionState",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("latest_epoch", models.BigIntegerField(blank=True, null=True)),
                ("point_count", models.PositiveIntegerField(default=0)),
                ("dirty", models.BooleanField(default=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("competition", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="task_ingestion_states", to="live_api.competition")),
                ("task", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="ingestion_state", to="live_api.task")),
            ],
        ),
        migrations.AddIndex(
            model_name="taskingestionstate",
            index=models.Index(fields=["dirty", "updated_at"], name="task_ingest_dirty_idx"),
        ),
        migrations.RunPython(backfill_task_ingestion_state, migrations.RunPython.noop),
    ]
