from django.contrib import admin
from django.urls import path
from live_api import views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("health", views.health),
    path("openapi.json", views.openapi),
    path("docs/", views.swagger_docs),
    path("api/v1/api-keys", views.api_keys),
    path("api/v1/competitions", views.competitions),
    path("api/v1/competitions/<uuid:competition_id>", views.competition_detail),
    path("api/v1/competitions/<uuid:competition_id>/tasks", views.tasks),
    path("api/v1/competitions/<uuid:competition_id>/tracking", views.tracking),
    path("api/v1/competitions/<uuid:competition_id>/results", views.results),
    path("events/sync", views.event_sync),
    path("events/<str:event_id>/mangas/sync", views.manga_sync),
    path("mangas/<str:manga_id>/points", views.manga_points),
]
