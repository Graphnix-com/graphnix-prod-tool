from django import forms

from apps.jobs.models import Status
from apps.jobs.money import no_amounts

from .models import Inquiry


class InquiryForm(forms.ModelForm):
    class Meta:
        model = Inquiry
        fields = [
            "channel",
            "received_at",
            "from_name",
            "from_email",
            "from_phone",
            "subject",
            "details",
        ]
        labels = {
            "from_name": "Name",
            "from_email": "Email",
            "from_phone": "Phone",
            "subject": "What they want, in a few words",
        }
        widgets = {
            "received_at": forms.DateTimeInput(
                attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"
            ),
            "details": forms.Textarea(attrs={"rows": 5}),
        }


class ConvertForm(forms.Form):
    START_CHOICES = [
        (Status.QUOTING, "Quote first"),
        (Status.INVOICED, "Straight to invoice"),
    ]
    start = forms.ChoiceField(choices=START_CHOICES, widget=forms.RadioSelect)
    title = forms.CharField(max_length=200, validators=[no_amounts])
    square_number = forms.CharField(
        max_length=40,
        required=False,
        label="Square job number",
        help_text="If Square has made one already. You can add it later.",
    )
