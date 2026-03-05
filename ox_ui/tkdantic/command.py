"""Basic model for a command (GUI, RPC, etc.)
"""


from typing import Callable, List, Optional, Type
from typing_extensions import Self

from pydantic import BaseModel, Field, model_validator


class Command(BaseModel):
    """Basic command object.
    """

    title: str
    parameters: List[Type[BaseModel]] = Field(default_factory=list)
    description: str
    name: Optional[str] = None

    # If you provide a callback, that will be executed with the
    # parameters of self. If not, then we will try to do an xml-rpc
    # call with the parameters of self to the given `name` on a server.
    callback: Optional[Callable] = None

    @model_validator(mode='after')
    def validate_cmd(self) -> Self:
        "Simple validator to use title to figure out name if necessary."
        if not self.name:
            self.name = self.title.replace(' ', '_').lower()
        return self
