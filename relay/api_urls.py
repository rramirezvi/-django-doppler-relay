from django.urls import path

from . import api


urlpatterns = [
    path("bulk-sends/", api.bulk_send_list, name="api_bulk_send_list"),
    path("bulk-sends/<int:pk>/", api.bulk_send_detail, name="api_bulk_send_detail"),
    path("bulk-sends/<int:pk>/process/", api.bulk_send_process, name="api_bulk_send_process"),
    path("bulk-sends/<int:pk>/process-report/", api.bulk_send_process_report, name="api_bulk_send_process_report"),
    path("reports/<int:pk>/download/", api.report_download, name="api_report_download"),
    path("jobs/", api.background_job_list, name="api_background_job_list"),
    path("jobs/<int:pk>/retry/", api.background_job_retry, name="api_background_job_retry"),
    path("templates/", api.templates, name="api_templates"),
    path("templates/<str:template_id>/preview/", api.template_preview, name="api_template_preview"),
    path("senders/", api.senders, name="api_senders"),
    path("csv/preview/", api.csv_preview, name="api_csv_preview"),
]
