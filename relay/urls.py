from django.urls import path
from . import views

urlpatterns = [
    path("send/", views.send_bulk_email, name="relay_send_bulk"),
]
