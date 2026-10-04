from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET
from django_ratelimit.decorators import ratelimit

from .decorators import tenant_required
from . import site_location


@login_required
@tenant_required
@require_GET
@never_cache
@ratelimit(key='user', rate='60/m', block=False)
def cities(request):
    if getattr(request, 'limited', False):
        return JsonResponse({'error': 'Too many lookups. Please wait a minute and try again.'}, status=429)
    try:
        if request.GET.get('place_id'):
            city = site_location.get_city(request.GET['place_id'])
            return JsonResponse({key: value for key, value in city.items() if key != 'components'})
        query = request.GET.get('q', '').strip()
        if len(query) > 200:
            return JsonResponse({'error': 'Search must be 200 characters or fewer.'}, status=400)
        return JsonResponse({'results': site_location.search_cities(query) if len(query) >= 2 else []})
    except site_location.InvalidCity as exc:
        return JsonResponse({'error': str(exc)}, status=400)
    except site_location.LocationUnavailable as exc:
        return JsonResponse({'error': str(exc)}, status=503)
