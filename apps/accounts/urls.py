"""Sign-in and the shop-floor PC. Names are not namespaced: Django's LOGIN_URL is "login"."""

from django.urls import path

from . import views

urlpatterns = [
    path("login/", views.LoginView.as_view(), name="login"),
    path("login/microsoft/", views.microsoft_start, name="microsoft_start"),
    path("login/microsoft/callback/", views.microsoft_callback, name="microsoft_callback"),
    path("logout/", views.logout_view, name="logout"),
    path("shop-floor/", views.shop_floor, name="shop_floor"),
    path("shop-floor/setup/", views.shop_floor_setup, name="shop_floor_setup"),
    path("shop-floor/new-pin/", views.shop_floor_change_pin, name="shop_floor_change_pin"),
    path("shop-floor/<int:pk>/", views.shop_floor_pin, name="shop_floor_pin"),
]
