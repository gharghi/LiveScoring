from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('live_api', '0007_remove_trackingpoint_live_api_tr_competi_5a0a2b_idx_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='pilotscoresnapshot',
            name='distance_points',
            field=models.FloatField(default=0),
        ),
        migrations.AddField(
            model_name='pilotscoresnapshot',
            name='time_points',
            field=models.FloatField(default=0),
        ),
        migrations.AddField(
            model_name='pilotscoresnapshot',
            name='leading_points',
            field=models.FloatField(default=0),
        ),
        migrations.AddField(
            model_name='pilotscoresnapshot',
            name='lc',
            field=models.FloatField(default=0),
        ),
    ]
