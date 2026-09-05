from app.auth.token_manager import PyroAuthService
from app.config import settings

fancysale_tm = PyroAuthService(
    api_key=settings.fancysale_api_key,
    login_id=settings.fancysale_login_id,
    password=settings.fancysale_password,
    secret_key=settings.fancysale_secret_key,
    base_url=settings.pyro_base_url,
    label="FANCYSALE",
)

simswap_tm = PyroAuthService(
    api_key=settings.simswap_api_key,
    login_id=settings.simswap_login_id,
    password=settings.simswap_password,
    secret_key=settings.simswap_secret_key,
    base_url=settings.pyro_base_url,
    label="SIMSWAP",
)

esim_tm = PyroAuthService(
    api_key=settings.esim_api_key,
    login_id=settings.esim_login_id,
    password=settings.esim_password,
    secret_key=settings.esim_secret_key,
    base_url=settings.pyro_base_url,
    label="ESIM",
)

ALL_DEBIT_TOKEN_MANAGERS = [fancysale_tm, simswap_tm, esim_tm]