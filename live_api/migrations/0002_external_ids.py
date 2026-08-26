from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("live_api", "0001_initial")]
    operations = [
        migrations.AddField(
            model_name="competition", name="external_event_id",
            field=models.CharField(blank=True, max_length=200, null=True, unique=True),
        ),
        migrations.AddField(
            model_name="task", name="external_manga_id",
            field=models.CharField(blank=True, max_length=200, null=True, unique=True),
        ),
    ]
