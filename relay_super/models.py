from relay.models import BulkSend


class BulkSendUserConfigProxy(BulkSend):
    class Meta:
        proxy = True
        app_label = 'relay_super'
        verbose_name = 'Bulk Send (por remitente)'
        verbose_name_plural = 'Bulk Sends (por remitente)'
