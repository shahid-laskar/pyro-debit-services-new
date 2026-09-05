from app.config import settings
from app.debit.services.base import DebitServiceAdapter
from app.debit.services.esim import EsimAdapter
from app.debit.services.fancysale import FancySaleAdapter
from app.debit.services.simswap import SimswapAdapter
from app.debit.token_managers import esim_tm, fancysale_tm, simswap_tm

SERVICE_REGISTRY: dict[str, DebitServiceAdapter] = {
    "FANCYSALE": FancySaleAdapter(
        token_manager=fancysale_tm,
        enabled=settings.fancysale_enabled,
        batch_size=settings.fancysale_batch_size,
        interval_minutes=settings.fancysale_interval_minutes,
        stuck_minutes=settings.fancysale_stuck_minutes,
    ),
    "SIMSWAP": SimswapAdapter(
        token_manager=simswap_tm,
        enabled=settings.simswap_enabled,
        batch_size=settings.simswap_batch_size,
        interval_minutes=settings.simswap_interval_minutes,
        stuck_minutes=settings.simswap_stuck_minutes,
    ),
    "ESIM": EsimAdapter(
        token_manager=esim_tm,
        enabled=settings.esim_enabled,
        batch_size=settings.esim_batch_size,
        interval_minutes=settings.esim_interval_minutes,
        stuck_minutes=settings.esim_stuck_minutes,
    ),
}


def get_enabled_services() -> list[DebitServiceAdapter]:
    return [svc for svc in SERVICE_REGISTRY.values() if svc.enabled]


def get_service(service_type: str) -> DebitServiceAdapter:
    
    svc = SERVICE_REGISTRY.get(service_type.upper())
    if not svc:
        raise KeyError(f"Unknown service_type: {service_type!r}. "
                       f"Valid: {list(SERVICE_REGISTRY)}")
    return svc