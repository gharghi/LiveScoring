# Generated migration for UPSERT optimization and archival strategy

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('live_api', '0005_task_ingestion_state'),
    ]

    operations = [
        # Add unique constraint for UPSERT deduplication
        migrations.AddConstraint(
            model_name='trackingpoint',
            constraint=models.UniqueConstraint(
                fields=['task_id', 'pilot_id', 'timestamp'],
                name='tracking_point_dedup_uniq'
            ),
        ),
        # Add composite index for fast UPSERT conflict detection
        migrations.AddIndex(
            model_name='trackingpoint',
            index=models.Index(
                fields=['task_id', 'pilot_id', 'timestamp'],
                name='tracking_point_dedup_idx'
            ),
        ),
        # Add index for result classification queries
        migrations.AddIndex(
            model_name='trackingpoint',
            index=models.Index(
                fields=['task_id', 'timestamp', '-id'],
                name='tracking_point_task_time_idx'
            ),
        ),
        # Add fingerprint index (explicit, not just db_index)
        migrations.AddIndex(
            model_name='trackingpoint',
            index=models.Index(
                fields=['fingerprint'],
                name='tracking_point_fingerprint_idx'
            ),
        ),
        # Create TrackingPointArchive model
        migrations.CreateModel(
            name='TrackingPointArchive',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('pilot_id', models.CharField(max_length=200)),
                ('event_id', models.CharField(blank=True, default='', max_length=200)),
                ('timestamp', models.DateTimeField()),
                ('latitude', models.FloatField()),
                ('longitude', models.FloatField()),
                ('altitude_gps', models.FloatField(blank=True, null=True)),
                ('altitude_baro', models.FloatField(blank=True, null=True)),
                ('source', models.CharField(blank=True, default='', max_length=100)),
                ('fingerprint', models.CharField(db_index=True, max_length=64)),
                ('raw', models.JSONField(default=dict)),
                ('received_at', models.DateTimeField(auto_now_add=True)),
                ('competition', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='tracking_points_archive', to='live_api.competition')),
                ('task', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='tracking_points_archive', to='live_api.task')),
            ],
            options={
                'indexes': [
                    models.Index(fields=['task_id', 'timestamp'], name='tracking_archive_task_time_idx'),
                    models.Index(fields=['pilot_id', 'timestamp'], name='tracking_archive_pilot_time_idx'),
                    models.Index(fields=['fingerprint'], name='tracking_archive_fingerprint_idx'),
                ],
            },
        ),
    ]