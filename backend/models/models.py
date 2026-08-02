from pydantic import EmailStr, BaseModel


class WaitlistSignup(BaseModel):
    email: EmailStr
    name: str | None = None

class CreateChatSession(BaseModel):
    content: str

class UserChatMessage(BaseModel):
    session_id: str
    content: str