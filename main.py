from fastapi import FastAPI
from core.config import settings
from presentation.api.payments import router as payments_router

def create_app() -> FastAPI:
    app = FastAPI(title=settings.PROJECT_NAME, version="1.0.0")
    app.include_router(payments_router)

    @app.get("/health")
    async def health_check():
        return {"status": "healthy", "service": settings.PROJECT_NAME}

    return app

app = create_app()
