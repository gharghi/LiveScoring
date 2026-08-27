import hashlib
import json
import uuid
from django.db import models


class ApiApplication(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    key_prefix = models.CharField(max_length=16, unique=True)
    key_hash = models.CharField(max_length=64, unique=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    @staticmethod
    def digest(key):
        return hashlib.sha256(key.encode("utf-8")).hexdigest()


class Competition(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(ApiApplication, on_delete=models.PROTECT, related_name="competitions")
    name = models.CharField(max_length=200)
    external_event_id = models.CharField(max_length=200, unique=True, null=True, blank=True)
    settings = models.JSONField(default=dict)
    status = models.CharField(max_length=20, default="draft")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class Task(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    competition = models.ForeignKey(Competition, on_delete=models.CASCADE, related_name="tasks")
    name = models.CharField(max_length=200)
    external_manga_id = models.CharField(max_length=200, unique=True, null=True, blank=True)
    version = models.PositiveIntegerField(default=1)
    settings = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)


class TrackingPoint(models.Model):
    id = models.BigAutoField(primary_key=True)
    competition = models.ForeignKey(Competition, on_delete=models.CASCADE, related_name="tracking_points")
    task = models.ForeignKey(Task, on_delete=models.CASCADE, null=True, blank=True, related_name="tracking_points")
    pilot_id = models.CharField(max_length=200)
    event_id = models.CharField(max_length=200, blank=True, default="")
    timestamp = models.DateTimeField()
    latitude = models.FloatField()
    longitude = models.FloatField()
    altitude_gps = models.FloatField(null=True, blank=True)
    altitude_baro = models.FloatField(null=True, blank=True)
    source = models.CharField(max_length=100, blank=True, default="")
    fingerprint = models.CharField(max_length=64, db_index=True)
    raw = models.JSONField(default=dict)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["task_id", "pilot_id", "timestamp"],
                name="tracking_point_dedup_uniq"
            ),
        ]
        indexes = [
            models.Index(fields=["task_id", "pilot_id", "timestamp"], name="tracking_point_dedup_idx"),
            models.Index(fields=["task_id", "timestamp", "-id"], name="tracking_point_task_time_idx"),
            models.Index(fields=["fingerprint"], name="tracking_point_fingerprint_idx"),
        ]

    @staticmethod
    def make_fingerprint(pilot_id, timestamp, latitude, longitude):
        value = f"{pilot_id}|{timestamp.isoformat()}|{latitude:.7f}|{longitude:.7f}"
        return hashlib.sha256(value.encode()).hexdigest()


class TrackingPointArchive(models.Model):
    """Archive of old tracking points for long-term storage. Same schema as TrackingPoint."""
    id = models.BigAutoField(primary_key=True)
    competition = models.ForeignKey(Competition, on_delete=models.CASCADE, related_name="tracking_points_archive")
    task = models.ForeignKey(Task, on_delete=models.CASCADE, null=True, blank=True, related_name="tracking_points_archive")
    pilot_id = models.CharField(max_length=200)
    event_id = models.CharField(max_length=200, blank=True, default="")
    timestamp = models.DateTimeField()
    latitude = models.FloatField()
    longitude = models.FloatField()
    altitude_gps = models.FloatField(null=True, blank=True)
    altitude_baro = models.FloatField(null=True, blank=True)
    source = models.CharField(max_length=100, blank=True, default="")
    fingerprint = models.CharField(max_length=64, db_index=True)
    raw = models.JSONField(default=dict)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["task_id", "timestamp"], name="track_arch_task_time"),
            models.Index(fields=["pilot_id", "timestamp"], name="track_arch_pilot_time"),
            models.Index(fields=["fingerprint"], name="track_arch_fingerprint"),
        ]


class TaskResultSnapshot(models.Model):
    task = models.OneToOneField(Task, on_delete=models.CASCADE, related_name="score_snapshot")
    competition = models.ForeignKey(Competition, on_delete=models.CASCADE, related_name="task_score_snapshots")
    computed_at = models.DateTimeField(auto_now=True)
    processed_epoch = models.BigIntegerField(null=True, blank=True)
    point_count = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=20, default="pending")
    task_score = models.JSONField(default=dict)
    timings = models.JSONField(default=dict)
    error = models.TextField(blank=True, default="")

    class Meta:
        indexes = [
            models.Index(fields=["competition", "computed_at"], name="task_score_comp_time_idx"),
        ]


class PilotScoreSnapshot(models.Model):
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="pilot_score_snapshots")
    pilot_id = models.CharField(max_length=200)
    rank = models.PositiveIntegerField()
    state = models.CharField(max_length=40)
    score = models.FloatField(default=0)
    distance_m = models.FloatField(default=0)
    speed_kmh = models.FloatField(null=True, blank=True)
    ess = models.BooleanField(default=False)
    goal = models.BooleanField(default=False)
    position = models.JSONField(default=dict)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["task", "pilot_id"], name="pilot_score_task_pilot_uniq"),
        ]
        indexes = [
            models.Index(fields=["task", "rank"], name="pilot_score_task_rank_idx"),
        ]


class TaskIngestionState(models.Model):
    task = models.OneToOneField(Task, on_delete=models.CASCADE, related_name="ingestion_state")
    competition = models.ForeignKey(Competition, on_delete=models.CASCADE, related_name="task_ingestion_states")
    latest_epoch = models.BigIntegerField(null=True, blank=True)
    point_count = models.PositiveIntegerField(default=0)
    dirty = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["dirty", "updated_at"], name="task_ingest_dirty_idx"),
        ]
