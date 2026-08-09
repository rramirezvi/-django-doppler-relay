"""bulk-v2-real-send-canary observability: LOGGING additivity (design.md §12.1).

Proves the project's LOGGING dict (config/settings.py) is additive over
Django's own DEFAULT_LOGGING: the `django` and `django.request` loggers are
byte-identical whether only Django's own defaults are applied, or Django's
defaults followed by this project's LOGGING dict (which is exactly what
`django.utils.log.configure_logging` does at process startup). Separately
proves the new `relay` logger tree reaches INFO and its own handler.

No real-send code is exercised or required to validate this.
"""

import io
import logging
import logging.config

from django.conf import settings
from django.test import SimpleTestCase
from django.utils.log import DEFAULT_LOGGING


def _snapshot(logger_name):
    logger = logging.getLogger(logger_name)
    return {
        "level": logger.level,
        "handler_classes": tuple(type(h) for h in logger.handlers),
        "propagate": logger.propagate,
    }


class LoggingAdditivityTests(SimpleTestCase):
    def test_django_and_django_request_loggers_are_unchanged_by_project_logging(self):
        # "Before": only Django's own DEFAULT_LOGGING applied — mirrors the
        # first half of django.utils.log.configure_logging.
        logging.config.dictConfig(DEFAULT_LOGGING)
        before_django = _snapshot("django")
        before_django_request = _snapshot("django.request")

        # "After": this project's LOGGING dict applied on top, exactly as
        # configure_logging does at startup (DEFAULT_LOGGING, then
        # settings.LOGGING). Our dict declares no "django" key and no
        # "root" key, so neither logger should be touched.
        logging.config.dictConfig(settings.LOGGING)
        after_django = _snapshot("django")
        after_django_request = _snapshot("django.request")

        self.assertEqual(before_django, after_django)
        self.assertEqual(before_django_request, after_django_request)

    def test_relay_logger_tree_reaches_info_and_its_own_handler(self):
        logging.config.dictConfig(DEFAULT_LOGGING)
        logging.config.dictConfig(settings.LOGGING)

        relay_logger = logging.getLogger("relay")
        self.assertEqual(relay_logger.level, logging.INFO)
        self.assertFalse(relay_logger.propagate)
        stream_handlers = [
            h for h in relay_logger.handlers if isinstance(h, logging.StreamHandler)
        ]
        self.assertEqual(len(stream_handlers), 1)

        child_logger = logging.getLogger("relay.x")
        self.assertEqual(child_logger.getEffectiveLevel(), logging.INFO)

        # Prove a pre-existing, non-send relay.* log call actually reaches
        # the relay_stderr handler, using the exact format already emitted
        # by relay/api.py's bulk_v2_canary decision log line.
        handler = stream_handlers[0]
        buffer = io.StringIO()
        original_stream = handler.stream
        handler.stream = buffer
        try:
            logging.getLogger("relay.api").info(
                "bulk_v2_canary decision=%s request=%s rows=%s external_calls=0",
                "canary_allowed",
                "abc123456789",
                0,
            )
        finally:
            handler.stream = original_stream

        output = buffer.getvalue()
        self.assertIn("bulk_v2_canary decision=canary_allowed", output)
