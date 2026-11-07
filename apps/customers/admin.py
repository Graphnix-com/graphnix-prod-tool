from django.contrib import admin

from apps.audit.admin import AuditedAdmin

from .models import Address, Contact, Customer


class ContactInline(admin.TabularInline):
    model = Contact
    extra = 0


class AddressInline(admin.StackedInline):
    model = Address
    extra = 0


@admin.register(Customer)
class CustomerAdmin(AuditedAdmin):
    list_display = ("display_name", "company_name", "email", "phone")
    search_fields = ("display_name", "company_name", "email", "phone")
    inlines = [ContactInline, AddressInline]
