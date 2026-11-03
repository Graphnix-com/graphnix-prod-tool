from django.conf import settings


def app_info(request):
    return {"APP_ENV": settings.APP_ENV, "GIT_SHA": settings.GIT_SHA}
