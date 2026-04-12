"""python -m boba_api — запуск из конфига."""
import uvicorn
from boba_api.config import ApiConfig

cfg = ApiConfig.from_env()
uvicorn.run("boba_api.main:app", host=cfg.host, port=cfg.port, reload=cfg.reload)
