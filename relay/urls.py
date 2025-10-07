from django.urls import path
from . import views

urlpatterns = [
    path("send/", views.send_bulk_email, name="relay_send_bulk"),
    path("user/email-config/", views.get_user_email_config,
         name="get_user_email_config"),
    path("user/email-config/update/", views.update_user_email_config,
         name="update_user_email_config"),
]
