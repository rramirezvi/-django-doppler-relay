from __future__ import annotations

import csv
import io
import json
from datetime import datetime, time, timezone as dt_timezone
from pathlib import Path

from django.contrib.auth.decorators import login_required
from django.conf import settings
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import FileResponse, Http404, HttpRequest, JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from relay.models import BackgroundJob, BulkSend, UserEmailConfig
from relay.services.doppler_relay import DopplerRelayClient
from reports.models import GeneratedReport


EMAIL_COLUMNS = {"email", "correo", "e-mail", "mail", "email_address", "correo_electronico", "\ufeffemail"}
TEMPLATES_CACHE_KEY = "operator-app:templates"
TEMPLATES_CACHE_SECONDS = 300
REPORT_MANUAL_MIN_AGE_MINUTES = 15


def _can_operate(user) -> bool:
    return bool(
        user.is_active
        and user.is_staff
        and (
            user.has_perm("relay.change_bulksend")
            or user.has_perm("relay_super.change_bulksenduserconfigproxy")
        )
    )


def _can_view_jobs(user) -> bool:
    return bool(user.is_active and user.is_staff)


def _json_error(message: str, *, status: int = 400) -> JsonResponse:
    return JsonResponse({"ok": False, "error": message}, status=status)


def _normalize_template_items(payload) -> list[dict]:
    items = []
    if isinstance(payload, list):
        items = [item for item in payload if isinstance(item, dict)]
    elif isinstance(payload, dict):
        for key in ("items", "templates", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                items = [item for item in value if isinstance(item, dict)]
                break
            if isinstance(value, dict) and isinstance(value.get("items"), list):
                items = [item for item in value["items"] if isinstance(item, dict)]
                break
        else:
            if payload.get("id"):
                items = [payload]

    normalized = []
    seen = set()
    for item in items:
        template_id = item.get("id") or item.get("templateId") or item.get("template_id")
        if not template_id:
            continue
        template_id = str(template_id).strip()
        if not template_id or template_id in seen:
            continue
        seen.add(template_id)
        name = str(item.get("name") or template_id).strip()
        subject = str(item.get("subject") or "").strip()
        normalized.append({
            "id": template_id,
            "name": name,
            "subject": subject,
            "label": f"{name} (id={template_id})",
        })
    normalized.sort(key=lambda row: row["name"].lower())
    return normalized


def _bulk_days(bulk: BulkSend) -> set:
    local_day = bulk.created_at.date()
    try:
        utc_day = bulk.created_at.astimezone(dt_timezone.utc).date()
    except Exception:
        utc_day = local_day
    return {local_day, utc_day}


def _report_summary_for_bulk(bulk: BulkSend) -> dict:
    days = _bulk_days(bulk)
    reports_qs = GeneratedReport.objects.filter(
        report_type="deliveries",
        start_date__in=days,
        end_date__in=days,
    ).order_by("-id")
    reports = []
    seen_reports = set()
    for rep in reports_qs:
        key = (rep.report_type, rep.start_date, rep.end_date)
        if key in seen_reports:
            continue
        seen_reports.add(key)
        reports.append(rep)
    items = []
    latest_error = ""
    ready_loaded = False
    rows_inserted = 0
    for rep in reports:
        if rep.error_details and not latest_error:
            latest_error = rep.error_details
        if rep.state == GeneratedReport.STATE_READY and rep.loaded_to_db:
            ready_loaded = True
            rows_inserted += int(rep.rows_inserted or 0)
        items.append({
            "id": rep.pk,
            "type": rep.report_type,
            "start_date": rep.start_date.isoformat(),
            "end_date": rep.end_date.isoformat(),
            "state": rep.state,
            "loaded_to_db": rep.loaded_to_db,
            "rows_inserted": rep.rows_inserted,
            "file_path": rep.file_path,
            "error_details": rep.error_details,
            "created_at": rep.created_at.isoformat() if rep.created_at else None,
            "updated_at": rep.updated_at.isoformat() if rep.updated_at else None,
        })

    if any(rep.state == GeneratedReport.STATE_PROCESSING for rep in reports):
        state = "processing"
    elif any(rep.state == GeneratedReport.STATE_PENDING for rep in reports):
        state = "pending"
    elif bulk.post_reports_loaded_at:
        state = "ready"
    elif latest_error or bulk.post_reports_status == "error":
        state = "error"
    elif ready_loaded and rows_inserted <= 0:
        state = "zero_rows"
    else:
        state = "not_started"

    return {
        "state": state,
        "loaded_at": bulk.post_reports_loaded_at.isoformat() if bulk.post_reports_loaded_at else None,
        "status": bulk.post_reports_status or "",
        "rows_inserted": rows_inserted,
        "latest_error": latest_error,
        "reports": items,
    }


def _job_payload(job: BackgroundJob) -> dict:
    return {
        "id": job.pk,
        "job_type": job.job_type,
        "state": job.state,
        "message": job.message,
        "error": job.error,
        "attempts": job.attempts,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "triggered_by": job.triggered_by.username if job.triggered_by else "",
    }


def _report_refresh_age_minutes(bulk: BulkSend) -> tuple[float | None, int | None, bool]:
    reference_at = (
        bulk.jobs
        .filter(job_type=BackgroundJob.TYPE_BULK_SEND, state=BackgroundJob.STATE_DONE, finished_at__isnull=False)
        .order_by("-finished_at")
        .values_list("finished_at", flat=True)
        .first()
    ) or bulk.created_at
    if not reference_at:
        return None, None, False
    age_minutes = (timezone.now() - reference_at).total_seconds() / 60
    remaining = max(0, int(round(REPORT_MANUAL_MIN_AGE_MINUTES - age_minutes)))
    return age_minutes, remaining, age_minutes >= REPORT_MANUAL_MIN_AGE_MINUTES


def _bulk_payload(bulk: BulkSend, *, include_detail: bool = False) -> dict:
    age_hours = None
    report_age_minutes, report_refresh_remaining_minutes, report_refresh_available = _report_refresh_age_minutes(bulk)
    if bulk.created_at:
        age_seconds = (timezone.now() - bulk.created_at).total_seconds()
        age_hours = round(age_seconds / 3600, 2)
    result = bulk.result
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except Exception:
            result = {"raw": result}
    payload = {
        "id": bulk.pk,
        "template_id": bulk.template_id,
        "template_name": bulk.template_name or bulk.template_id,
        "subject": bulk.subject or "",
        "status": bulk.status,
        "created_at": bulk.created_at.isoformat() if bulk.created_at else None,
        "scheduled_at": bulk.scheduled_at.isoformat() if bulk.scheduled_at else None,
        "age_hours": age_hours,
        "post_reports_status": bulk.post_reports_status or "",
        "post_reports_loaded_at": bulk.post_reports_loaded_at.isoformat() if bulk.post_reports_loaded_at else None,
        "report": _report_summary_for_bulk(bulk),
        "report_refresh_age_minutes": round(report_age_minutes, 2) if report_age_minutes is not None else None,
        "report_refresh_available": report_refresh_available,
        "report_refresh_remaining_minutes": report_refresh_remaining_minutes,
        "jobs": [
            _job_payload(job)
            for job in bulk.jobs.select_related("triggered_by").order_by("-created_at")[:3]
        ],
    }
    if include_detail:
        payload.update({
            "variables": bulk.variables or {},
            "result": result,
            "log": bulk.log or "",
            "recipients_file": bulk.recipients_file.name if bulk.recipients_file else "",
            "attachments": [
                {"id": att.pk, "name": att.name, "file": att.file.name if att.file else ""}
                for att in bulk.attachments.all()
            ],
            "jobs": [
                _job_payload(job)
                for job in bulk.jobs.select_related("triggered_by").order_by("-created_at")[:10]
            ],
        })
    return payload


@require_http_methods(["GET", "POST"])
@login_required
def bulk_send_list(request: HttpRequest) -> JsonResponse:
    if not request.user.is_staff:
        return _json_error("No autorizado", status=403)
    if request.method == "POST":
        return _bulk_send_create(request)

    qs = BulkSend.objects.all().order_by("-created_at")
    period = request.GET.get("period") or "current_month"
    if period != "all":
        current_tz = timezone.get_current_timezone()
        today = timezone.localdate()
        month_start = today.replace(day=1)
        if month_start.month == 12:
            next_month = month_start.replace(year=month_start.year + 1, month=1)
        else:
            next_month = month_start.replace(month=month_start.month + 1)
        start_dt = timezone.make_aware(datetime.combine(month_start, time.min), current_tz)
        end_dt = timezone.make_aware(datetime.combine(next_month, time.min), current_tz)
        qs = qs.filter(created_at__gte=start_dt, created_at__lt=end_dt)
    status = request.GET.get("status")
    search = (request.GET.get("q") or "").strip()
    if status:
        qs = qs.filter(status=status)
    if search:
        date_q = None
        for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
            try:
                date_q = datetime.strptime(search, fmt).date()
                break
            except ValueError:
                pass
        search_filter = (
            Q(template_id__icontains=search)
            | Q(template_name__icontains=search)
            | Q(subject__icontains=search)
            | Q(status__icontains=search)
            | Q(post_reports_status__icontains=search)
        )
        if search.isdigit():
            search_filter |= Q(pk=int(search))
        if date_q:
            search_filter |= Q(created_at__date=date_q)
        qs = qs.filter(
            search_filter
        )

    paginator = Paginator(qs, int(request.GET.get("page_size") or 25))
    page = paginator.get_page(int(request.GET.get("page") or 1))
    return JsonResponse({
        "ok": True,
        "count": paginator.count,
        "page": page.number,
        "num_pages": paginator.num_pages,
        "period": period,
        "results": [_bulk_payload(bulk) for bulk in page.object_list],
    })


def _bulk_send_create(request: HttpRequest) -> JsonResponse:
    if not _can_operate(request.user):
        return _json_error("No tiene permiso para crear envíos", status=403)

    template_id = (request.POST.get("template_id") or "").strip()
    subject = (request.POST.get("subject") or "").strip()
    sender_id = (request.POST.get("sender_id") or "").strip()
    send_now = (request.POST.get("send_now") or "").lower() in {"1", "true", "yes", "on"}
    scheduled_at_raw = (request.POST.get("scheduled_at") or "").strip()
    raw_variables = (request.POST.get("variables") or "").strip()
    recipients_file = request.FILES.get("recipients_file")

    if not template_id:
        return _json_error("template_id es requerido.")
    if not recipients_file:
        return _json_error("Debe adjuntar recipients_file.")

    variables = {}
    if raw_variables:
        try:
            variables = json.loads(raw_variables)
        except json.JSONDecodeError:
            return _json_error("variables debe ser JSON válido.")
        if not isinstance(variables, dict):
            return _json_error("variables debe ser un objeto JSON.")

    if sender_id:
        sender = UserEmailConfig.objects.filter(pk=sender_id, is_active=True).first()
        if not sender:
            return _json_error("El remitente seleccionado no existe o está inactivo.")
        variables["__sender_user_config_id"] = sender.pk

    scheduled_at = None
    if scheduled_at_raw:
        scheduled_at = parse_datetime(scheduled_at_raw)
        if scheduled_at is None:
            return _json_error("scheduled_at debe ser una fecha/hora válida.")
        if timezone.is_naive(scheduled_at):
            scheduled_at = timezone.make_aware(scheduled_at, timezone.get_current_timezone())
        send_now = False

    bulk = BulkSend.objects.create(
        template_id=template_id,
        subject=subject,
        variables=variables,
        recipients_file=recipients_file,
        scheduled_at=scheduled_at,
        scheduled_by=request.user if request.user.is_authenticated else None,
    )

    if send_now:
        bulk.processing_started_at = timezone.now()
        bulk.log = ((bulk.log or "") + f"\n[API] Envío creado y encolado por {request.user.username}").strip()
        bulk.save(update_fields=["processing_started_at", "log"])
        job = BackgroundJob.objects.create(
            job_type=BackgroundJob.TYPE_BULK_SEND,
            bulk=bulk,
            triggered_by=request.user,
            message="Envío en cola",
        )

    return JsonResponse({
        "ok": True,
        "message": "Envío creado" + (" y encolado" if send_now else ""),
        "bulk": _bulk_payload(bulk, include_detail=True),
    }, status=201)


@require_GET
@login_required
def bulk_send_detail(request: HttpRequest, pk: int) -> JsonResponse:
    if not request.user.is_staff:
        return _json_error("No autorizado", status=403)
    try:
        bulk = BulkSend.objects.prefetch_related("attachments").get(pk=pk)
    except BulkSend.DoesNotExist:
        return _json_error("BulkSend no encontrado", status=404)
    return JsonResponse({"ok": True, "bulk": _bulk_payload(bulk, include_detail=True)})


@require_POST
@login_required
def bulk_send_process(request: HttpRequest, pk: int) -> JsonResponse:
    if not _can_operate(request.user):
        return _json_error("No tiene permiso para procesar envíos", status=403)
    try:
        bulk = BulkSend.objects.get(pk=pk)
    except BulkSend.DoesNotExist:
        return _json_error("BulkSend no encontrado", status=404)
    if bulk.status != "pending":
        return _json_error(f"El envío está en estado {bulk.status}; solo se procesa pending.")

    bulk.processing_started_at = timezone.now()
    bulk.log = ((bulk.log or "") + f"\n[API] Envío encolado por {request.user.username}").strip()
    bulk.save(update_fields=["processing_started_at", "log"])
    job = BackgroundJob.objects.create(
        job_type=BackgroundJob.TYPE_BULK_SEND,
        bulk=bulk,
        triggered_by=request.user,
        message="Envío en cola",
    )
    return JsonResponse({"ok": True, "message": "Envío encolado", "job": _job_payload(job), "bulk": _bulk_payload(bulk)})


@require_POST
@login_required
def bulk_send_process_report(request: HttpRequest, pk: int) -> JsonResponse:
    if not _can_operate(request.user):
        return _json_error("No tiene permiso para generar reportes", status=403)
    try:
        bulk = BulkSend.objects.get(pk=pk)
    except BulkSend.DoesNotExist:
        return _json_error("BulkSend no encontrado", status=404)
    if bulk.status != "done":
        return _json_error("El reporte se puede generar cuando el envío está en estado done.")
    age_minutes, remaining_minutes, refresh_available = _report_refresh_age_minutes(bulk)
    if not refresh_available:
        remaining = max(1, int(remaining_minutes or REPORT_MANUAL_MIN_AGE_MINUTES))
        return _json_error(f"El reporte manual está disponible en {remaining} min.")
    report_state = _report_summary_for_bulk(bulk).get("state")
    if report_state in {"pending", "processing"}:
        return _json_error(f"El reporte ya está en estado {report_state}.")
    if BackgroundJob.objects.filter(
        bulk=bulk,
        job_type=BackgroundJob.TYPE_POST_REPORT,
        state__in=[BackgroundJob.STATE_QUEUED, BackgroundJob.STATE_RUNNING],
    ).exists():
        return _json_error("Ya existe un reporte en cola o en ejecución para este envío.")

    job = BackgroundJob.objects.create(
        job_type=BackgroundJob.TYPE_POST_REPORT,
        bulk=bulk,
        triggered_by=request.user,
        message="Actualización de reporte en cola",
    )
    return JsonResponse({"ok": True, "message": "Actualización de reporte encolada", "job": _job_payload(job)})


@require_GET
@login_required
def report_download(request: HttpRequest, pk: int) -> FileResponse:
    if not request.user.is_staff:
        raise Http404("Reporte no disponible")
    try:
        report = GeneratedReport.objects.get(pk=pk)
    except GeneratedReport.DoesNotExist:
        raise Http404("Reporte no disponible")
    if report.state != GeneratedReport.STATE_READY or not report.file_path:
        raise Http404("Reporte no disponible")
    path = Path(report.file_path)
    if not path.exists() or not path.is_file():
        raise Http404("Archivo no encontrado")
    return FileResponse(open(path, "rb"), as_attachment=True, filename=path.name)


@require_GET
@login_required
def senders(request: HttpRequest) -> JsonResponse:
    if not request.user.is_staff:
        return _json_error("No autorizado", status=403)
    relay_cfg = getattr(settings, "DOPPLER_RELAY", {}) or {}
    default_from_email = str(relay_cfg.get("DEFAULT_FROM_EMAIL") or "").strip()
    default_from_name = str(relay_cfg.get("DEFAULT_FROM_NAME") or "").strip()
    rows = UserEmailConfig.objects.filter(is_active=True).select_related("user").order_by("user__username")
    return JsonResponse({
        "ok": True,
        "default_sender": {
            "from_email": default_from_email,
            "from_name": default_from_name,
            "label": f"Usar correo predeterminado: {default_from_email}" if default_from_email else "Usar correo predeterminado",
        },
        "results": [
            {
                "id": row.pk,
                "label": f"{row.user.username} - {row.from_email}",
                "from_email": row.from_email,
                "from_name": row.from_name,
            }
            for row in rows
        ],
    })


@require_GET
@login_required
def templates(request: HttpRequest) -> JsonResponse:
    if not request.user.is_staff:
        return _json_error("No autorizado", status=403)
    refresh = request.GET.get("refresh") in {"1", "true", "yes"}
    if not refresh:
        cached = cache.get(TEMPLATES_CACHE_KEY)
        if cached is not None:
            return JsonResponse({"ok": True, "cached": True, "results": cached})

    account_id = (getattr(settings, "DOPPLER_RELAY", {}) or {}).get("ACCOUNT_ID")
    if not account_id:
        return _json_error("DOPPLER_RELAY_ACCOUNT_ID no está configurado.")
    try:
        payload = DopplerRelayClient().list_templates(account_id)
        results = _normalize_template_items(payload)
    except Exception as exc:
        return _json_error(f"No se pudieron cargar plantillas: {exc}", status=502)
    cache.set(TEMPLATES_CACHE_KEY, results, TEMPLATES_CACHE_SECONDS)
    return JsonResponse({"ok": True, "cached": False, "results": results})


@require_GET
@login_required
def template_preview(request: HttpRequest, template_id: str) -> JsonResponse:
    if not request.user.is_staff:
        return _json_error("No autorizado", status=403)
    account_id = (getattr(settings, "DOPPLER_RELAY", {}) or {}).get("ACCOUNT_ID")
    if not account_id:
        return _json_error("DOPPLER_RELAY_ACCOUNT_ID no está configurado.")
    try:
        client = DopplerRelayClient()
        data = client.get_template(account_id, template_id)
        html = client.get_template_html(account_id, template_id)
    except Exception as exc:
        return _json_error(f"No se pudo cargar la plantilla: {exc}", status=502)
    preview_source = "api"
    if not html:
        try:
            from templates_admin.utils import read_cached_html
            html = read_cached_html(template_id)
            if html:
                preview_source = "cache"
        except Exception:
            html = ""
    if not html:
        html = "<p>La plantilla no tiene contenido HTML disponible para vista previa.</p>"
        preview_source = "empty"
    name = (data.get("name") or str(template_id)) if isinstance(data, dict) else str(template_id)
    subject = (data.get("subject") or "") if isinstance(data, dict) else ""
    return JsonResponse({
        "ok": True,
        "id": str(template_id),
        "name": name,
        "subject": subject,
        "html": html,
        "preview_source": preview_source,
    })


@require_GET
@login_required
def background_job_list(request: HttpRequest) -> JsonResponse:
    if not _can_view_jobs(request.user):
        return _json_error("No autorizado", status=403)

    qs = BackgroundJob.objects.select_related("bulk", "triggered_by").order_by("-created_at")
    state = request.GET.get("state")
    job_type = request.GET.get("job_type")
    bulk_id = request.GET.get("bulk_id")
    if state:
        qs = qs.filter(state=state)
    if job_type:
        qs = qs.filter(job_type=job_type)
    if bulk_id:
        qs = qs.filter(bulk_id=bulk_id)

    paginator = Paginator(qs, int(request.GET.get("page_size") or 30))
    page = paginator.get_page(int(request.GET.get("page") or 1))
    return JsonResponse({
        "ok": True,
        "count": paginator.count,
        "page": page.number,
        "num_pages": paginator.num_pages,
        "results": [_job_payload(job) | {
            "bulk_id": job.bulk_id,
            "bulk_label": str(job.bulk) if job.bulk else "",
            "bulk_template": (job.bulk.template_name or job.bulk.template_id) if job.bulk else "",
            "bulk_subject": (job.bulk.subject or "") if job.bulk else "",
        } for job in page.object_list],
    })


@require_POST
@login_required
def background_job_retry(request: HttpRequest, pk: int) -> JsonResponse:
    if not _can_operate(request.user):
        return _json_error("No tiene permiso para reintentar jobs", status=403)
    try:
        job = BackgroundJob.objects.get(pk=pk)
    except BackgroundJob.DoesNotExist:
        return _json_error("Job no encontrado", status=404)
    if job.state != BackgroundJob.STATE_ERROR:
        return _json_error(f"Solo se puede reintentar un job en error. Estado actual: {job.state}")

    retry = BackgroundJob.objects.create(
        job_type=job.job_type,
        bulk=job.bulk,
        triggered_by=request.user,
        message=f"Reintento de job {job.pk}",
        meta={"retry_of": job.pk},
    )
    return JsonResponse({
        "ok": True,
        "message": f"Job {retry.pk} encolado como reintento",
        "job": _job_payload(retry),
    }, status=201)


@require_POST
@login_required
def csv_preview(request: HttpRequest) -> JsonResponse:
    if not request.user.is_staff:
        return _json_error("No autorizado", status=403)
    upload = request.FILES.get("file")
    if not upload:
        return _json_error("Debe adjuntar un CSV en el campo file.")
    raw = upload.read()
    text = None
    encoding = ""
    for candidate in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            text = raw.decode(candidate)
            encoding = candidate
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        return _json_error("No se pudo decodificar el archivo.")

    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;")
        delimiter = dialect.delimiter
    except Exception:
        delimiter = ";"
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    headers = [str(h or "").strip() for h in (reader.fieldnames or [])]
    normalized = [h.lower() for h in headers]
    email_column = next((h for h in normalized if h in EMAIL_COLUMNS), "")
    preview = []
    valid_email_rows = 0
    total_rows = 0
    for row in reader:
        total_rows += 1
        clean = {str(k or "").strip(): ("" if v is None else str(v).strip()) for k, v in row.items()}
        if email_column:
            for key, value in clean.items():
                if key.strip().lower() == email_column and "@" in value:
                    valid_email_rows += 1
                    break
        if len(preview) < 10:
            preview.append(clean)

    return JsonResponse({
        "ok": True,
        "encoding": encoding,
        "delimiter": delimiter,
        "headers": headers,
        "email_column": email_column,
        "total_rows": total_rows,
        "valid_email_rows": valid_email_rows,
        "preview": preview,
    })
