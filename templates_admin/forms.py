from django import forms


class TemplateForm(forms.Form):
    name = forms.CharField(
        max_length=255,
        label="Nombre de plantilla",
        widget=forms.TextInput(),
    )
    from_email = forms.EmailField(
        label="Correo remitente",
        widget=forms.EmailInput(),
    )
    from_name = forms.CharField(
        max_length=255,
        label="Nombre remitente",
        required=False,
        widget=forms.TextInput(),
    )
    subject = forms.CharField(
        max_length=255,
        label="Asunto",
        widget=forms.TextInput(),
    )
    body_html = forms.CharField(
        label="HTML de la plantilla",
        widget=forms.Textarea(attrs={"rows": 30}),
    )
