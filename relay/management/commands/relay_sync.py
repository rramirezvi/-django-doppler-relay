from django.core.management.base import BaseCommand
from django.test import RequestFactory
from relay.views import deliveries_since, events_since

class Command(BaseCommand):
    help = "Sincroniza entregas y eventos de Doppler Relay (últimas N horas)."

    def add_arguments(self, parser):
        parser.add_argument("--hours", type=int, default=24)

    def handle(self, *args, **opts):
        rf = RequestFactory()
        req = rf.get(f"/relay/deliveries/?hours={opts['hours']}")
        deliveries_since(req)
        req2 = rf.get(f"/relay/events/?hours={opts['hours']}")
        events_since(req2)
        self.stdout.write(self.style.SUCCESS("Sync OK"))
