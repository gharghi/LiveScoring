from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("live_api", "0002_external_ids")]
    operations = [
        migrations.AddField(
            model_name="trackingpoint",
            name="task",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                                    related_name="tracking_points", to="live_api.task"),
        ),
        migrations.AddIndex(
            model_name="trackingpoint",
            index=models.Index(fields=["task", "pilot_id", "timestamp"], name="tracking_task_time_idx"),
        ),
    ]
