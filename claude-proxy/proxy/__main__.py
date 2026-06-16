"""启动入口"""
import sys
import io
import uvicorn
from .config import DEFAULT_HOST, DEFAULT_PORT, FLOWITH_BASE_URL

if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    print(f"""
=====================================
  Flowith Claude Proxy v2.0.0
=====================================

  Address:    http://{DEFAULT_HOST}:{DEFAULT_PORT}
  Upstream:   {FLOWITH_BASE_URL}

  API Docs:   http://{DEFAULT_HOST}:{DEFAULT_PORT}/docs
  Health:     http://{DEFAULT_HOST}:{DEFAULT_PORT}/health
""")

    uvicorn.run(
        "proxy.server:app",
        host=DEFAULT_HOST,
        port=DEFAULT_PORT,
    )
