import secrets

from django.contrib import admin, messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import path, reverse
from django.utils.html import format_html

from .models import ApiApplication, Competition, Task, TrackingPoint


@admin.register(ApiApplication)
class ApiApplicationAdmin(admin.ModelAdmin):
    list_display = ("name", "key_prefix", "active", "created_at", "generate_key_link")
    list_filter = ("active",)
    search_fields = ("name", "key_prefix", "id")
    readonly_fields = ("id", "key_prefix", "key_hash", "created_at", "generate_key_link")

    @admin.display(description="API key")
    def generate_key_link(self, obj):
        if not obj or not obj.pk:
            return "Save the application first"
        url = reverse("admin:live_api_apiapplication_generate_key", args=[obj.pk])
        return format_html('<a class="button" href="{}">Generate / rotate key</a>', url)

    def get_urls(self):
        urls = super().get_urls()
        custom = [path("<path:object_id>/generate-key/", self.admin_site.admin_view(self.generate_key),
                       name="live_api_apiapplication_generate_key")]
        return custom + urls

    def generate_key(self, request: HttpRequest, object_id: str) -> HttpResponse:
        application = get_object_or_404(ApiApplication, pk=object_id)
        if request.method == "POST":
            plain = "ls_" + secrets.token_urlsafe(32)
            application.key_prefix = plain[:12]
            application.key_hash = ApiApplication.digest(plain)
            application.active = True
            application.save(update_fields=["key_prefix", "key_hash", "active"])
            messages.success(request, "The key was rotated. This plaintext key will not be shown again.")
            return render(request, "admin/live_api/apiapplication/generated_key.html",
                          {"application": application, "plain_key": plain, "opts": self.model._meta,
                           "title": "Generated API key"})
        return render(request, "admin/live_api/apiapplication/generate_key.html",
                      {"application": application, "opts": self.model._meta, "title": "Generate API key"})


@admin.register(Competition)
class CompetitionAdmin(admin.ModelAdmin):
    list_display = ("name", "owner", "status", "created_at", "updated_at")
    list_filter = ("status",)
    search_fields = ("name", "id")
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ("name", "competition", "version", "created_at")
    search_fields = ("name", "id", "competition__name")
    readonly_fields = ("id", "created_at")


@admin.register(TrackingPoint)
class TrackingPointAdmin(admin.ModelAdmin):
    list_display = ("competition", "pilot_id", "timestamp", "latitude", "longitude", "source", "received_at")
    list_filter = ("source",)
    search_fields = ("pilot_id", "event_id", "fingerprint")
    readonly_fields = [field.name for field in TrackingPoint._meta.fields]
    list_per_page = 100
