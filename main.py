from astrbot.api import star

from .ottohub_adapter import OTTOhubPlatformAdapter  # noqa: F401
from .ottohub_comment_adapter import OTTOhubCommentPlatformAdapter  # noqa: F401


class Main(star.Star):
    def __init__(self, context: star.Context) -> None:
        super().__init__(context)
        self.context = context

    async def terminate(self) -> None:
        pass
