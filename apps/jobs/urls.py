from django.urls import path

from . import views

app_name = "jobs"

urlpatterns = [
    path("", views.job_list, name="list"),
    path("search/", views.job_search, name="search"),
    path("shop-notes/", views.shop_note_add, name="shop_note_add"),
    path("shop-notes/<int:pk>/done/", views.shop_note_tick, name="shop_note_tick"),
    path("shop-notes/<int:pk>/remove/", views.shop_note_remove, name="shop_note_remove"),
    path("<int:pk>/", views.job_detail, name="detail"),
    path("<int:pk>/edit/", views.job_edit, name="edit"),
    path("<int:pk>/action/", views.job_action, name="action"),
    path("<int:pk>/items/new/", views.item_form, name="item_add"),
    path("<int:pk>/items/<int:item_pk>/", views.item_form, name="item_edit"),
    path("<int:pk>/items/<int:item_pk>/delete/", views.item_delete, name="item_delete"),
    path("<int:pk>/tasks/new/", views.task_form, name="task_add"),
    path("<int:pk>/tasks/<int:task_pk>/", views.task_form, name="task_edit"),
    path("<int:pk>/tasks/<int:task_pk>/delete/", views.task_delete, name="task_delete"),
    path("<int:pk>/tasks/<int:task_pk>/assign/", views.task_assign, name="task_assign"),
    path("<int:pk>/notes/", views.note_add, name="note_add"),
    path("<int:pk>/files/", views.file_add, name="file_add"),
    path("<int:pk>/files/<int:file_pk>/", views.file_download, name="file_download"),
    path("<int:pk>/files/<int:file_pk>/delete/", views.file_delete, name="file_delete"),
    path("<int:pk>/mockups/", views.mockup_add, name="mockup_add"),
]
