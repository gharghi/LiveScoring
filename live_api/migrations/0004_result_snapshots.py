from django.db import migrations, models
import django.db.models.deletion


def create_postgres_indexes(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("""
        CREATE INDEX IF NOT EXISTS tracking_task_pilot_ts_id_cover_idx
        ON live_api_trackingpoint (task_id, pilot_id, timestamp, id DESC)
        INCLUDE (latitude, longitude, altitude_baro, altitude_gps)
    """)


def drop_postgres_indexes(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("DROP INDEX IF EXISTS tracking_task_pilot_ts_id_cover_idx")


class Migration(migrations.Migration):
    dependencies = [("live_api", "0003_trackingpoint_task")]

    operations = [
        migrations.CreateModel(
            name="TaskResultSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("computed_at", models.DateTimeField(auto_now=True)),
                ("processed_epoch", models.BigIntegerField(blank=True, null=True)),
                ("point_count", models.PositiveIntegerField(default=0)),
                ("status", models.CharField(default="pending", max_length=20)),
                ("task_score", models.JSONField(default=dict)),
                ("timings", models.JSONField(default=dict)),
                ("error", models.TextField(blank=True, default="")),
                ("competition", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="task_score_snapshots", to="live_api.competition")),
                ("task", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="score_snapshot", to="live_api.task")),
            ],
        ),
        migrations.CreateModel(
            name="PilotScoreSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("pilot_id", models.CharField(max_length=200)),
                ("rank", models.PositiveIntegerField()),
                ("state", models.CharField(max_length=40)),
                ("score", models.FloatField(default=0)),
                ("distance_m", models.FloatField(default=0)),
                ("speed_kmh", models.FloatField(blank=True, null=True)),
                ("ess", models.BooleanField(default=False)),
                ("goal", models.BooleanField(default=False)),
                ("position", models.JSONField(default=dict)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("task", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="pilot_score_snapshots", to="live_api.task")),
            ],
        ),
        migrations.AddIndex(
            model_name="taskresultsnapshot",
            index=models.Index(fields=["competition", "computed_at"], name="task_score_comp_time_idx"),
        ),
        migrations.AddConstraint(
            model_name="pilotscoresnapshot",
            constraint=models.UniqueConstraint(fields=("task", "pilot_id"), name="pilot_score_task_pilot_uniq"),
        ),
        migrations.AddIndex(
            model_name="pilotscoresnapshot",
            index=models.Index(fields=["task", "rank"], name="pilot_score_task_rank_idx"),
        ),
        migrations.RunPython(create_postgres_indexes, drop_postgres_indexes),
    ]
