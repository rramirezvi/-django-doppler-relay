from __future__ import annotations

import logging

from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType


logger = logging.getLogger(__name__)

OPERATOR_GROUP_NAME = "Operadores UI"

OPERATOR_PERMISSIONS = (
    ("relay", "bulksend", ("view_bulksend", "add_bulksend", "change_bulksend")),
    ("relay", "backgroundjob", ("view_backgroundjob",)),
    ("relay", "useremailconfig", ("view_useremailconfig",)),
)


def ensure_operator_group(*, group_name: str = OPERATOR_GROUP_NAME, stdout=None) -> Group:
    group, created = Group.objects.get_or_create(name=group_name)
    permissions = []
    missing = []

    for app_label, model, codenames in OPERATOR_PERMISSIONS:
        try:
            content_type = ContentType.objects.get(app_label=app_label, model=model)
        except ContentType.DoesNotExist:
            missing.extend(f"{app_label}.{codename}" for codename in codenames)
            continue

        found = Permission.objects.filter(content_type=content_type, codename__in=codenames)
        permissions.extend(found)
        found_codes = {perm.codename for perm in found}
        missing.extend(f"{app_label}.{codename}" for codename in codenames if codename not in found_codes)

    if permissions:
        group.permissions.add(*permissions)

    message = (
        f"Grupo '{group_name}' {'creado' if created else 'actualizado'} "
        f"con {len(permissions)} permisos."
    )
    if stdout:
        stdout.write(message)
        if missing:
            stdout.write("Permisos no encontrados: " + ", ".join(sorted(missing)))
    elif missing:
        logger.warning("Permisos no encontrados para %s: %s", group_name, ", ".join(sorted(missing)))

    return group
