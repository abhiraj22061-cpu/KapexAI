from pydantic import EmailStr, BaseModel


class WaitlistSignup(BaseModel):
    email: EmailStr
    name: str | None = None

class CreateChatSession(BaseModel):
    email: EmailStr
    content: str

class UserChatMessage(BaseModel):
    session_id: str
    email: EmailStr
    content: str