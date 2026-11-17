from django.urls import path

from . import views

app_name = "production"

urlpatterns = [
    path("", views.floor, name="floor"),
    path("stations/<int:pk>/", views.station_queue, name="queue"),
    path("mine/", views.my_tasks, name="mine"),
    path("tasks/<int:pk>/move/", views.task_move, name="task_move"),
    path("tasks/<int:pk>/plan/", views.task_plan, name="task_plan"),
    path("calendar/", views.month_view, name="calendar"),
]
