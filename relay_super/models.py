from relay.models import BulkSend


class BulkSendUserConfigProxy(BulkSend):
    class Meta:
        proxy = True
        app_label = 'relay_super'
        verbose_name = 'Envio masivo por remitente'
        verbose_name_plural = 'Envios masivos por remitente'
