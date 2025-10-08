from __future__ import annotations

from unittest.mock import patch

from django import forms
from django.contrib.auth.models import AnonymousUser
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core.cache import cache
from django.test import RequestFactory, TestCase, override_settings
from django.contrib.sessions.middleware import SessionMiddleware

from relay.admin import BulkSendForm
from relay.services.doppler_relay import DopplerRelayError


@override_settings(CACHES={
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'bulk-send-tests',
    }
})
class BulkSendTemplateFormTests(TestCase):
    def setUp(self) -> None:
        self.factory = RequestFactory()
        cache.clear()

    def _build_request(self):
        request = self.factory.get('/admin/relay/bulksend/add/')
        SessionMiddleware(lambda req: None).process_request(request)
        request.session.save()
        setattr(request, '_messages', FallbackStorage(request))
        request.user = AnonymousUser()
        return request

    @patch('relay.admin.BulkSendForm._schedule_refresh', lambda *args, **kwargs: None)
    @patch('relay.admin.DopplerRelayClient.list_templates')
    def test_cache_hit_uses_cached_templates(self, mock_list_templates):
        mock_list_templates.return_value = [{'id': 'tpl-1', 'name': 'Alpha'}]

        request = self._build_request()
        BulkSendForm(request=request)
        self.assertEqual(mock_list_templates.call_count, 1)

        BulkSendForm(request=request)
        self.assertEqual(
            mock_list_templates.call_count,
            1,
            msg='La segunda carga debería usar el cache',
        )

    @patch('relay.admin.BulkSendForm._schedule_refresh', lambda *args, **kwargs: None)
    @patch('relay.admin.DopplerRelayClient.list_templates')
    def test_fallback_to_manual_field_when_api_fails(self, mock_list_templates):
        mock_list_templates.side_effect = DopplerRelayError('boom')
        request = self._build_request()

        form = BulkSendForm(request=request)

        self.assertIsInstance(form.fields['template_id'], forms.CharField)
        self.assertNotIsInstance(form.fields['template_id'], forms.ChoiceField)

    @patch('relay.admin.BulkSendForm._schedule_refresh', lambda *args, **kwargs: None)
    @patch('relay.admin.DopplerRelayClient.list_templates')
    def test_clean_template_id_accepts_manual_value(self, mock_list_templates):
        mock_list_templates.return_value = [{'id': 'tpl-1', 'name': 'Alpha'}]
        form = BulkSendForm()
        form.fields['template_id'] = forms.ChoiceField(
            required=True,
            choices=[('', '— Selecciona —'), ('tpl-1', 'Alpha')],
        )
        form.cleaned_data = {'template_id': 'manual-123'}

        value = form.clean_template_id()
        self.assertEqual(value, 'manual-123')
