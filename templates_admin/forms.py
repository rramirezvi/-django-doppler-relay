from django import forms


class TemplateForm(forms.Form):
    name = forms.CharField(max_length=255, label="Name",
                           widget=forms.TextInput(attrs={"style": "width:100%"}))
    from_email = forms.EmailField(label="From email",
                                  widget=forms.EmailInput(attrs={"style": "width:100%"}))
    from_name = forms.CharField(max_length=255, label="From name", required=False,
                                widget=forms.TextInput(attrs={"style": "width:100%"}))
    subject = forms.CharField(max_length=255, label="Subject",
                              widget=forms.TextInput(attrs={"style": "width:100%"}))
    body_html = forms.CharField(label="HTML body",
                                widget=forms.Textarea(attrs={"rows": 28, "style": "width:100%; font-family:monospace"}))
