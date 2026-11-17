from django.urls import path

from . import views

app_name = "purchasing"

urlpatterns = [
    path("", views.po_list, name="list"),
    path("new/", views.po_build, name="build"),
    path("<int:pk>/", views.po_detail, name="detail"),
    path("<int:pk>/print/", views.po_print, name="print"),
    path("<int:pk>/order/", views.po_order, name="order"),
    path("<int:pk>/receive/", views.po_receive, name="receive"),
    path("<int:pk>/lines/", views.po_add_line, name="add_line"),
    path("<int:pk>/cancel/", views.po_cancel, name="cancel"),
    path("lines/<int:pk>/", views.line_action, name="line"),
    path("items/<int:pk>/", views.item_materials, name="item"),
]
