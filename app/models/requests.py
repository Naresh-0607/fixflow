from pydantic import BaseModel, Field


class AnalyzeRequest(BaseModel):
    repository_url: str = Field(
        min_length=1,
        examples=["https://github.com/user/python-project.git"],
    )


class RepairRequest(AnalyzeRequest):
    pass
