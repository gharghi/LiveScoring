from django.urls import path
from live_api import views

urlpatterns = [
    path("health", views.health),
    path("openapi.json", views.openapi),
    path("api/v1/api-keys", views.api_keys),
    path("api/v1/competitions", views.competitions),
    path("api/v1/competitions/<uuid:competition_id>", views.competition_detail),
    path("api/v1/competitions/<uuid:competition_id>/tasks", views.tasks),
    path("api/v1/competitions/<uuid:competition_id>/tracking", views.tracking),
    path("api/v1/competitions/<uuid:competition_id>/results", views.results),
]
