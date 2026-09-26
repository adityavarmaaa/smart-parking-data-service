from fastapi import FastAPI
from sqlalchemy import text
from fastapi.middleware.cors import CORSMiddleware

from app.database.connection import engine
from app.routes.events import router as events_router
from app.routes.occupancy import router as occupancy_router
from app.routes.analytics import router as analytics_router
from app.routes.vehicles import router as vehicles_router 
from app.routes.snapshots import router as snapshots_router


app = FastAPI(
    title="Smart Parking - Data Management Service",
    description="Service 2: Data Management Service",
    version="1.0.0",
)
app.add_middleware( 
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)



# =========================================================
# ROUTES
# =========================================================

app.include_router(events_router)
app.include_router(occupancy_router)
app.include_router(analytics_router)
app.include_router(vehicles_router)
app.include_router(snapshots_router)

# =========================================================
# HEALTH CHECK
# =========================================================

@app.get("/health")
def health():
    return {
        "status": "healthy",
        "service": "data-management-service",
    }


# =========================================================
# DATABASE HEALTH CHECK
# =========================================================

@app.get("/health/database")
def database_health():
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))

        return {
            "status": "healthy",
            "database": "connected",
        }

    except Exception as error:
        return {
            "status": "unhealthy",
            "database": "disconnected",
            "error": str(error),
        }