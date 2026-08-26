import uuid
from django.db import migrations, models
import django.db.models.deletion

class Migration(migrations.Migration):
    initial = True
    dependencies = []
    operations = [
        migrations.CreateModel(name="ApiApplication", fields=[
            ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
            ("name", models.CharField(max_length=200)), ("key_prefix", models.CharField(max_length=16, unique=True)),
            ("key_hash", models.CharField(max_length=64, unique=True)), ("active", models.BooleanField(default=True)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
        ]),
        migrations.CreateModel(name="Competition", fields=[
            ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
            ("name", models.CharField(max_length=200)), ("settings", models.JSONField(default=dict)),
            ("status", models.CharField(default="draft", max_length=20)), ("created_at", models.DateTimeField(auto_now_add=True)),
            ("updated_at", models.DateTimeField(auto_now=True)),
            ("owner", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="competitions", to="live_api.apiapplication")),
        ]),
        migrations.CreateModel(name="Task", fields=[
            ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
            ("competition", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="tasks", to="live_api.competition")),
            ("name", models.CharField(max_length=200)), ("version", models.PositiveIntegerField(default=1)),
            ("settings", models.JSONField(default=dict)), ("created_at", models.DateTimeField(auto_now_add=True)),
        ]),
        migrations.CreateModel(name="TrackingPoint", fields=[
            ("id", models.BigAutoField(primary_key=True, serialize=False)), ("competition", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="tracking_points", to="live_api.competition")),
            ("pilot_id", models.CharField(max_length=200)), ("event_id", models.CharField(blank=True, default="", max_length=200)),
            ("timestamp", models.DateTimeField()), ("latitude", models.FloatField()), ("longitude", models.FloatField()),
            ("altitude_gps", models.FloatField(blank=True, null=True)), ("altitude_baro", models.FloatField(blank=True, null=True)),
            ("source", models.CharField(blank=True, default="", max_length=100)), ("fingerprint", models.CharField(db_index=True, max_length=64)),
            ("raw", models.JSONField(default=dict)), ("received_at", models.DateTimeField(auto_now_add=True)),
        ]),
        migrations.AddIndex(model_name="trackingpoint", index=models.Index(fields=["competition", "pilot_id", "timestamp"], name="live_api_tr_competi_5a0a2b_idx")),
    ]
