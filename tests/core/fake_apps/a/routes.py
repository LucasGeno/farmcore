from fastapi import APIRouter

router = APIRouter()


@router.get("/ping")
def ping() -> dict[str, str]:
    return {"app": "a"}


@router.post("/ping")
def poke() -> dict[str, str]:
    return {"app": "a", "poked": "yes"}
