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
    settings = models.JSONField(default=dict)
    status = models.CharField(max_length=20, default="draft")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class Task(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    competition = models.ForeignKey(Competition, on_delete=models.CASCADE, related_name="tasks")
    name = models.CharField(max_length=200)
    version = models.PositiveIntegerField(default=1)
    settings = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)


class TrackingPoint(models.Model):
    id = models.BigAutoField(primary_key=True)
    competition = models.ForeignKey(Competition, on_delete=models.CASCADE, related_name="tracking_points")
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

    @staticmethod
    def make_fingerprint(pilot_id, timestamp, latitude, longitude):
        value = f"{pilot_id}|{timestamp.isoformat()}|{latitude:.7f}|{longitude:.7f}"
        return hashlib.sha256(value.encode()).hexdigest()
