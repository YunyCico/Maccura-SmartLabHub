from fastapi import APIRouter

router = APIRouter(tags=["user"])


@router.get("/me")
def current_user() -> dict[str, str]:
    return {
        "user_id": "local-admin",
        "name": "本地管理员",
        "role": "department_admin",
        "data_scope": "all",
    }
