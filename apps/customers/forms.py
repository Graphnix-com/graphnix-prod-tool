from django import forms

from .models import Address, Contact, Customer


class CustomerForm(forms.ModelForm):
    class Meta:
        model = Customer
        fields = [
            "display_name",
            "company_name",
            "email",
            "phone",
            "notes",
            "notifications_opt_out",
        ]
        labels = {
            "display_name": "Name",
            "company_name": "Company (optional)",
            "notifications_opt_out": "Don't send this customer automatic emails",
        }
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}


# A company has one main contact and sometimes a second (Step 1 spec, question 4).
ContactFormSet = forms.inlineformset_factory(
    Customer,
    Contact,
    fields=["full_name", "email", "phone", "is_primary"],
    labels={"is_primary": "Main contact"},
    extra=1,
    max_num=2,
    validate_max=True,
    can_delete=True,
)

AddressFormSet = forms.inlineformset_factory(
    Customer,
    Address,
    fields=["kind", "line1", "line2", "city", "province", "postal_code"],
    extra=1,
    can_delete=True,
)
