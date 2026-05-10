from __future__ import annotations

from django.core.management.base import BaseCommand

from relay.services.operator_permissions import OPERATOR_GROUP_NAME, ensure_operator_group


class Command(BaseCommand):
    help = "Crea o actualiza el grupo de operadores para la UI nueva."

    def add_arguments(self, parser):
        parser.add_argument(
            "--name",
            default=OPERATOR_GROUP_NAME,
            help=f"Nombre del grupo. Por defecto: {OPERATOR_GROUP_NAME}",
        )

    def handle(self, *args, **options):
        group = ensure_operator_group(group_name=options["name"], stdout=self.stdout)
        self.stdout.write(self.style.SUCCESS(f"Listo: {group.name}"))
