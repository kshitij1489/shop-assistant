"""Location provider for evaluation fixtures, distinct from ordinary dev tenants."""
def evaluation_location_provider():
    from django.conf import settings

    # The fallback supports the existing focused test settings.
    return getattr(settings, "EVALUATION_LOCATION_PROVIDER", settings.LOCATION_PROVIDER)
